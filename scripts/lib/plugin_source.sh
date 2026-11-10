#!/bin/bash
# plugin_source.sh — the ONE shared plugin source-acquisition and staging helper
# both plugin-install paths use (Phase 311.1 D-10..D-14, PLGI-01). Sourceable;
# sources nothing itself.
#
# Why one helper: the Docker path (`install_plugin_docker.sh` ->
# `scripts/docker/install_plugin.sh`) and the Kubernetes path
# (`install_plugin_k8s.sh` -> `scripts/k8s/install_plugin.sh`) must accept the
# same argument form, refuse the same inputs and reach the same API-version
# verdict. Two implementations of any of those three is exactly how the two
# secret libraries drifted apart once already (Phase 311.1 D-01) — it is not
# coming back here.
#
# Contract:
#   * A source is EITHER a local directory holding `plugin.toml` OR a repository
#     URL whose scheme is on the explicit allow-list below. Anything else — an
#     empty string, an argument beginning with a dash, an unsupported scheme, a
#     local path without a manifest — is refused before anything is read.
#   * A URL source is cloned SHALLOW into a private temporary directory
#     (`--depth 1`, optional single `--branch <ref>`), on the OPERATOR'S HOST:
#     the clone runs before the copy, and `git` is therefore a documented
#     prerequisite of the quickstart. A missing git binary is a refusal, never
#     a silent fallback.
#   * Both source kinds are then staged through ONE exclusion pass that drops
#     version-control and CI metadata (D-12): the caller copies the STAGED tree,
#     never the source as found, so a local directory gets the same guarantee as
#     a clone. The pass is a `tar` pipe, deliberately not `rsync`: `tar` is in
#     the k8s bootstrap package allow-list (`curl ca-certificates tar coreutils
#     gnupg`, docs/DEPLOYMENT.md) and in every stock Ubuntu; adding a second
#     binary to the quickstart path for one copy is not acceptable.
#   * The destination name is the manifest's own `name` field (D-11), validated
#     against the same character class the loader enforces
#     (`backend/app/plugin_system/loader.py`'s `_PLUGIN_NAME_RE`). A name that
#     would fail discovery is refused here, naming the value — it is never
#     attempted. An explicit second CLI argument still overrides the name.
#   * The API-version verdict (D-13) is produced by the CORE APPLICATION'S OWN
#     RULE executed inside the Core container — never re-implemented host-side —
#     so it cannot drift from `backend/app/plugin_system/versioning.py`. Anything
#     but a clean compatible verdict is a refusal, including an invocation that
#     produced no verdict at all: a failed check is never an assumed pass.
#   * Nothing is ever evaluated through the shell: the source string, the ref
#     and the derived name are always separate argument elements, the repository
#     argument follows a `--` separator and the ref is validated against a safe
#     character class first (T-311.1-15).
#
# Prerequisites, all supplied by the sourcing environment (same convention as
# scripts/lib/secrets.sh):
#   die, step, preflight_error — from scripts/k8s/lib/common.sh, the base
#     library both stack libraries source (scripts/docker/lib/common.sh sources
#     it too), so this file is usable from either stack without depending on the
#     other one.
#   kubectl_indramesh, NAMESPACE_NAME — only for the Kubernetes entry point of
#     the API-contract verification, provided by the k8s orchestrator
#     (scripts/k8s/install_plugin.sh) and its lib/.
#
# Every refusal message goes to stderr and the function returns non-zero; the
# caller decides whether to abort. A `die` inside the library would kill
# in-process callers such as the bootstrap test harness (Phase 311.1 D-01).

# Double-source guard: sourcing this file twice is a no-op.
if [ -n "${_INDRAMESH_PLUGIN_SOURCE_SH_LOADED:-}" ]; then
  return 0
fi
_INDRAMESH_PLUGIN_SOURCE_SH_LOADED=1

# The only URL schemes accepted for a repository source. `git://` and `ssh://`
# are read-only-friendly transports; plain `https://` is what the plugin READMEs
# publish. Nothing else — `file://`, `ftp://`, an scp-style `git@host:path` — is
# accepted, so an operator typo can never be read as a local path or vice versa.
PLUGIN_SOURCE_URL_SCHEMES=(https:// http:// ssh:// git://)

# Version-control and CI metadata that must never reach a plugin directory or
# the cluster claim (D-12). Defined ONCE, as data, so the exclusion pass and its
# documentation cannot drift apart. Bare names: `tar --exclude` matches the last
# path component, so a nested `.github` is dropped as well.
PLUGIN_SOURCE_EXCLUDES=(
  .git
  .github
  .gitlab
  .gitlab-ci.yml
  .circleci
  .travis.yml
  .gitattributes
  .gitmodules
  .gitignore
  .hg
  .svn
)

# The plugin directory-name character class, mirroring loader.py's
# _PLUGIN_NAME_RE (T-282-04): no path separators, no traversal segments.
PLUGIN_SOURCE_NAME_RE='^[a-zA-Z0-9_-]+$'
PLUGIN_SOURCE_REF_RE='^[A-Za-z0-9][A-Za-z0-9._/-]*$'

# Private temporary directory, the staged tree and their cleanup. A single
# removal routine (plugin_source_cleanup) is meant for the caller's exit trap.
PLUGIN_SOURCE_TMP_DIR=""
STAGED_DIR=""

# ---------------------------------------------------------------------------
# Source identification (D-10)
# ---------------------------------------------------------------------------

plugin_source_kind() {  # plugin_source_kind <source> — echo "local" | "url"; refuse otherwise
  local source="${1:-}" scheme
  if [ -z "$source" ]; then
    preflight_error "plugin_source: no source given — pass a local plugin directory or a repository URL"
    return 1
  fi
  # Refused BEFORE it can be read as an option anywhere downstream.
  case "$source" in
    -*)
      preflight_error "plugin_source: refusing source '$source' — a source must not begin with a dash (it would be read as an option)"
      return 1
      ;;
  esac
  for scheme in "${PLUGIN_SOURCE_URL_SCHEMES[@]}"; do
    case "$source" in
      "$scheme"*) printf 'url'; return 0 ;;
    esac
  done
  # A URL-shaped string with a scheme that is not on the allow-list is refused
  # as a scheme problem, never silently treated as a local path.
  case "$source" in
    *://*)
      preflight_error "plugin_source: unsupported source scheme in '$source' — supported: ${PLUGIN_SOURCE_URL_SCHEMES[*]} (or a local directory)"
      return 1
      ;;
  esac
  if [ ! -d "$source" ]; then
    preflight_error "plugin_source: '$source' is neither a local directory nor a supported repository URL (supported schemes: ${PLUGIN_SOURCE_URL_SCHEMES[*]})"
    return 1
  fi
  if [ ! -f "$source/plugin.toml" ]; then
    preflight_error "plugin_source: $source/plugin.toml not found — '$source' is not a plugin directory"
    return 1
  fi
  printf 'local'
}

plugin_source_mktemp() {  # plugin_source_mktemp — allocate and echo the private temporary directory
  local parent candidate
  parent="$(realpath -m "${TMPDIR:-/tmp}")"
  candidate="$(mktemp -d "$parent/indramesh-plugin.XXXXXX")"
  PLUGIN_SOURCE_TMP_DIR="$(realpath -m "$candidate")"
  printf '%s' "$PLUGIN_SOURCE_TMP_DIR"
}

plugin_source_cleanup() {  # plugin_source_cleanup — remove the private temporary directory (no-op if never created)
  [ -n "${PLUGIN_SOURCE_TMP_DIR:-}" ] || return 0
  if [ -d "$PLUGIN_SOURCE_TMP_DIR" ]; then
    rm -rf -- "$PLUGIN_SOURCE_TMP_DIR"
  fi
  PLUGIN_SOURCE_TMP_DIR=""
  return 0
}

# ---------------------------------------------------------------------------
# Shallow clone (D-10)
# ---------------------------------------------------------------------------

plugin_clone() {  # plugin_clone <url> <destination> [ref]
  local url="$1" dest="$2" ref="${3:-}"
  local args=(clone --depth 1)

  if ! command -v git >/dev/null 2>&1; then
    preflight_error "plugin_source: 'git' is required to install a plugin from a repository URL, but no git binary was found on PATH — install git (a documented quickstart prerequisite), or pass a local plugin directory instead"
    return 1
  fi

  if [ -n "$ref" ]; then
    if ! printf '%s' "$ref" | grep -Eq "$PLUGIN_SOURCE_REF_RE"; then
      preflight_error "plugin_source: refusing ref '$ref' — a ref must match $PLUGIN_SOURCE_REF_RE and must not begin with a dash"
      return 1
    fi
    args+=(--branch "$ref")
  fi

  # `--` so a URL can never be read as an option; the ref is one argument
  # element of its own (never a shell string), and stdin is closed so the clone
  # can never consume a caller's input.
  args+=(-- "$url" "$dest")
  if ! git "${args[@]}" </dev/null; then
    preflight_error "plugin_source: git clone failed for '$url'${ref:+ (ref: $ref)}"
    return 1
  fi
  return 0
}

# ---------------------------------------------------------------------------
# Exclusion staging (D-12) — one code path for a clone and a local directory
# ---------------------------------------------------------------------------

plugin_stage() {  # plugin_stage <source-dir> <staged-dir> — exclusion pass; sets and echoes STAGED_DIR
  local src="$1" staged="$2" exclude patterns=()
  if [ ! -d "$src" ]; then
    preflight_error "plugin_source: staging source '$src' is not a directory"
    return 1
  fi
  mkdir -p "$staged" || {
    preflight_error "plugin_source: could not create the staging directory '$staged'"
    return 1
  }
  for exclude in "${PLUGIN_SOURCE_EXCLUDES[@]}"; do
    patterns+=("--exclude=./$exclude" "--exclude=$exclude")
  done
  if ! ( cd "$src" && tar "${patterns[@]}" -cf - . ) | tar -C "$staged" -xf -; then
    preflight_error "plugin_source: staging '$src' failed"
    return 1
  fi
  STAGED_DIR="$(realpath -m "$staged")"
  printf '%s' "$STAGED_DIR"
}

# ---------------------------------------------------------------------------
# Manifest reading: name derivation and the declared api_version (D-11, D-13)
# ---------------------------------------------------------------------------

plugin_manifest_value() {  # plugin_manifest_value <plugin-dir> <key> — echo the value, one quote layer and any CR stripped
  local dir="$1" key="$2" line value
  [ -f "$dir/plugin.toml" ] || return 0
  line="$(grep -E "^[[:space:]]*${key}[[:space:]]*=" "$dir/plugin.toml" 2>/dev/null | head -n1 || true)"
  [ -n "$line" ] || return 0
  value="${line#*=}"
  value="${value%$'\r'}"
  value="${value#"${value%%[![:space:]]*}"}"
  value="${value%"${value##*[![:space:]]}"}"
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;
    \'*\') value="${value#\'}"; value="${value%\'}" ;;
  esac
  printf '%s' "$value"
}

plugin_validate_name() {  # plugin_validate_name <name> — 0 when the loader's own character class accepts it
  local name="${1:-}"
  case "$name" in
    ""|*[!a-zA-Z0-9_-]*)
      preflight_error "plugin_source: invalid plugin directory name '$name' — must match $PLUGIN_SOURCE_NAME_RE (mirrors loader.py's own T-282-04 guard; a name that fails this check would also fail discovery)"
      return 1
      ;;
  esac
  return 0
}

plugin_derive_name() {  # plugin_derive_name <plugin-dir> — echo the manifest's own name field (D-11)
  local dir="$1" name
  name="$(plugin_manifest_value "$dir" name)"
  if [ -z "$name" ]; then
    preflight_error "plugin_source: $dir/plugin.toml declares no usable 'name' — refusing (the destination name comes from the manifest)"
    return 1
  fi
  plugin_validate_name "$name" || return 1
  printf '%s' "$name"
}

plugin_manifest_api_version() {  # plugin_manifest_api_version <plugin-dir> — echo the declared api_version
  local dir="$1" version
  version="$(plugin_manifest_value "$dir" api_version)"
  if [ -z "$version" ]; then
    preflight_error "plugin_source: $dir/plugin.toml declares no 'api_version' — refusing (the Core rule cannot be asked for a verdict)"
    return 1
  fi
  printf '%s' "$version"
}

# ---------------------------------------------------------------------------
# Version comparison for the update path (D-27). An installed plugin is only
# ever replaced by a HIGHER version: plugin migrations run forward only, so a
# downgrade is refused outright. Both sides must carry a readable three-part
# numeric version — anything else is an error, never a string comparison.
# ---------------------------------------------------------------------------

PLUGIN_SOURCE_VERSION_RE='^[0-9]+\.[0-9]+\.[0-9]+$'

plugin_manifest_version() {  # plugin_manifest_version <plugin-dir> — echo the declared three-part version
  local dir="$1" version
  version="$(plugin_manifest_value "$dir" version)"
  if [ -z "$version" ]; then
    preflight_error "plugin_source: $dir/plugin.toml declares no 'version' — refusing (an installed plugin needs a readable version to be updated)"
    return 1
  fi
  printf '%s' "$version"
}

plugin_compare_versions() {  # plugin_compare_versions <a> <b> — echo 1, 0 or -1 (a higher, equal, lower); refuse invalid input
  local a="${1:-}" b="${2:-}" value i
  local -a a_parts b_parts
  for value in "$a" "$b"; do
    if ! printf '%s' "$value" | grep -Eq "$PLUGIN_SOURCE_VERSION_RE"; then
      preflight_error "plugin_source: cannot compare '$value' — a plugin version must be three numeric parts, like 1.2.3"
      return 1
    fi
  done
  IFS=. read -r -a a_parts <<< "$a"
  IFS=. read -r -a b_parts <<< "$b"
  for i in 0 1 2; do
    if [ "$((10#${a_parts[$i]}))" -gt "$((10#${b_parts[$i]}))" ]; then
      printf '1'
      return 0
    fi
    if [ "$((10#${a_parts[$i]}))" -lt "$((10#${b_parts[$i]}))" ]; then
      printf '%s' '-1'
      return 0
    fi
  done
  printf '0'
}

plugin_update_action() {  # plugin_update_action <installed-version> <new-version> — echo replace | same | refuse
  local installed="${1:-}" new="${2:-}" verdict
  verdict="$(plugin_compare_versions "$new" "$installed")" || return 1
  case "$verdict" in
    1) printf 'replace' ;;
    0) printf 'same' ;;
    -1) printf 'refuse' ;;
    *)
      preflight_error "plugin_source: internal error comparing '$installed' with '$new'"
      return 1
      ;;
  esac
}

# ---------------------------------------------------------------------------
# The API-contract verification (D-13): the Core application's OWN rule, run
# inside the Core container. Two entry points, because the two stacks reach the
# container differently — the verdict itself is shared.
# ---------------------------------------------------------------------------

_plugin_api_python_snippet() {  # the exact rule-carrying one-liner, printed as one argument
  printf '%s' 'import sys; from app.plugin_system.versioning import CORE_API_VERSION, is_compatible; print(CORE_API_VERSION, is_compatible(CORE_API_VERSION, sys.argv[1]))'
}

_plugin_api_verdict() {  # _plugin_api_verdict <declared-version> <raw-output>
  local declared="$1" raw="$2" last core
  # The verdict is the LAST non-empty line of the invocation's output.
  last="$(printf '%s\n' "$raw" | awk 'NF { print }' | tail -n1)"
  if [ -z "$last" ]; then
    preflight_error "plugin_source: the Core produced no usable api_version verdict for '$declared' — refusing (a failed check is never an assumed pass)"
    return 1
  fi
  # A clean compatible verdict ends in the boolean `True`.
  case "$last" in
    "True") return 0 ;;
    *" True")
      core="${last% True}"
      if [ -n "$core" ]; then
        return 0
      fi
      ;;
  esac
  core="${last%% *}"
  preflight_error "plugin_source: the plugin declares api_version '$declared', incompatible with the Core's plugin API '${core}' (container verdict: '${last}') — refusing before any copy"
  return 1
}

plugin_verify_api_docker() {  # plugin_verify_api_docker <checkout-root> <declared-version>
  local root="$1" declared="$2" raw="" rc=0
  # `exec` into the RUNNING backend (never a one-shot `compose run`): the verdict
  # must come from the Core that will actually load the plugin after the restart,
  # no extra container is created, and a stack that is not running is a clean
  # refusal instead of an image build or a dependency start.
  raw="$( ( cd "$root" && docker compose exec -T backend python -c "$(_plugin_api_python_snippet)" "$declared" </dev/null ) 2>&1 )" || rc=$?
  if [ "$rc" -ne 0 ]; then
    preflight_error "plugin_source: could not evaluate the plugin's api_version ('$declared') in the running Core container — start the stack first (this install restarts backend and worker, so it requires a running stack)"
    return 1
  fi
  _plugin_api_verdict "$declared" "$raw"
}

plugin_verify_api_k8s() {  # plugin_verify_api_k8s <declared-version>
  local declared="$1" raw="" rc=0
  # The deployed backend deployment is already Running (the caller reads its
  # image today), and it is the same Core the plugin will be loaded by.
  raw="$(kubectl_indramesh exec -n "$NAMESPACE_NAME" deploy/indramesh-backend -- \
    python -c "$(_plugin_api_python_snippet)" "$declared" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    preflight_error "plugin_source: could not evaluate the plugin's api_version ('$declared') in the deployed backend — is the release healthy?"
    return 1
  fi
  _plugin_api_verdict "$declared" "$raw"
}
