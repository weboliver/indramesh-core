#!/bin/bash
# common.sh — strict shared installer safety helpers for the Phase 254 k3s
# bootstrap (D-03, D-04, D-13, D-14, D-24; T-254-02, T-254-03). Sourceable:
# sets strict mode, defines step-labelled error/status helpers, a private
# temporary-directory helper, a strict hostname validator, and safe
# non-executing dotenv parsing plus the D-13 permitted-package gate. Never
# sources .env as code, never enables tracing, and never prints values.
set -euo pipefail

# ---------------------------------------------------------------------------
# Step-labelled error and status reporting (D-04)
# ---------------------------------------------------------------------------

# A child process (sudo with use_pty, a BuildKit progress display) can leave the
# terminal in raw mode with a hidden cursor; every status line and the exit
# trap put back the settings the installer started with.
TERMINAL_STATE=""
if [ -t 0 ]; then
  TERMINAL_STATE="$(stty -g 2>/dev/null || true)"
fi

restore_terminal() {  # restore_terminal — reapply the starting tty settings, show the cursor
  [ -n "$TERMINAL_STATE" ] || return 0
  stty "$TERMINAL_STATE" 2>/dev/null || true
  if [ -t 1 ]; then
    printf '\033[?25h'
  fi
}

preflight_error() {  # preflight_error <message>
  restore_terminal
  echo "[preflight] ERROR: $*" >&2
}

die() {  # die <step> <message> — hard failure with a named step
  restore_terminal
  echo "[$1] ERROR: $2" >&2
  exit 1
}

step() {  # step <label>
  restore_terminal
  echo "== $1"
}

# ---------------------------------------------------------------------------
# jsonpath bracket-key escaping (G-255-1)
# ---------------------------------------------------------------------------

_jsonpath_escape_key() {  # _jsonpath_escape_key <key> — escape backslash, single quote, then dot (order
  # matters: escaping dot/quote first would get re-escaped by the backslash
  # step) so a key can be safely interpolated into a kubectl
  # jsonpath={.foo['<key>']} single-quoted bracket expression. Real
  # kubectl's client-go jsonpath parser treats an unescaped literal '.'
  # inside a single-quoted bracket map key as a nested-field path
  # separator, not a literal character — this closes that class
  # structurally rather than relying on every caller hand-escaping.
  local key="$1"
  printf '%s' "$key" | sed -e 's/\\/\\\\/g' -e "s/'/\\\\'/g" -e 's/\./\\./g'
}

# ---------------------------------------------------------------------------
# Private temporary-directory cleanup
# ---------------------------------------------------------------------------

PRIVATE_TMP_DIR=""
PRIVATE_TMP_PARENT=""

private_tmp() {  # private_tmp <caller-variable> — allocate in the parent shell
  local caller="${1:?caller variable required}" parent candidate
  parent="$(realpath -m "${TMPDIR:-/tmp}")"
  candidate="$(mktemp -d "$parent/indramesh-k8s.XXXXXX")"
  PRIVATE_TMP_DIR="$(realpath -m "$candidate")"
  PRIVATE_TMP_PARENT="$parent"
  printf -v "$caller" '%s' "$PRIVATE_TMP_DIR"
}

cleanup_private_tmp() {  # cleanup_private_tmp — remove the private temp dir
  local candidate="${1:-${PRIVATE_TMP_DIR:-}}"
  [ -n "${PRIVATE_TMP_DIR:-}" ] || return 0
  [ "$candidate" = "$PRIVATE_TMP_DIR" ] || return 1
  [ "$(dirname "$candidate")" = "$PRIVATE_TMP_PARENT" ] || return 1
  [[ "$(basename "$candidate")" == indramesh-k8s.* ]] || return 1
  [ -d "$candidate" ] || { PRIVATE_TMP_DIR=""; PRIVATE_TMP_PARENT=""; return 0; }
  rm -rf -- "$candidate" || return 1
  PRIVATE_TMP_DIR=""
  PRIVATE_TMP_PARENT=""
}

cleanup_private_tmp_on_exit() {
  local work_status=$? cleanup_status=0
  restore_terminal
  cleanup_private_tmp || cleanup_status=$?
  if [ "$work_status" -ne 0 ]; then
    exit "$work_status"
  fi
  exit "$cleanup_status"
}

# ---------------------------------------------------------------------------
# Strict hostname validation (D-06, T-254-03)
# ---------------------------------------------------------------------------

HOSTNAME_LABEL_REGEX='^[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?$'

validate_hostname() {  # validate_hostname <name> — 0 for a safe DNS hostname
  local name="${1:-}"
  local label
  [ -n "$name" ] || return 1
  [ "${#name}" -le 253 ] || return 1
  case "$name" in
    *[!A-Za-z0-9.-]*) return 1 ;;
    *..*|.*|*.) return 1 ;;
  esac
  local IFS='.'
  for label in $name; do
    [[ "$label" =~ $HOSTNAME_LABEL_REGEX ]] || return 1
  done
  return 0
}

validate_hostname_or_die() {  # validate_hostname_or_die <name> — T-254-03
  if ! validate_hostname "${1:-}"; then
    die "preflight" "hostname '${1:-}' is invalid — use a DNS hostname (labels 1-63 chars, letters/digits/hyphens, at most 253 chars total)"
  fi
}

# ---------------------------------------------------------------------------
# Safe non-executing dotenv parsing (D-24, T-254-02)
# ---------------------------------------------------------------------------

dotenv_keys() {  # dotenv_keys <file> — literal KEY= assignment names only (D-24)
  local file="$1"
  awk -F= '
    /^[[:space:]]*[A-Z_][A-Z0-9_]*=/ {
      line = $0
      sub(/^[[:space:]]*/, "", line)
      sub(/=.*/, "", line)
      if (!seen[line]++) print line
    }
  ' "$file"
}

validate_env_schema() {  # validate_env_schema <env_file> <schema_file> — every schema key present (D-24)
  local env_file="$1"
  local schema_file="$2"
  local key
  local missing=0
  while read -r key; do
    [ -n "$key" ] || continue
    if ! grep -qE "^[[:space:]]*${key}=" "$env_file"; then
      preflight_error "required key '$key' is missing from $(basename "$env_file") (D-24)"
      missing=1
    fi
  done < <(dotenv_keys "$schema_file")
  return "$missing"
}

require_env_value() {  # require_env_value <env_file> <key> — fail when an explicitly required value is empty (D-03)
  local env_file="$1"
  local key="$2"
  local value
  value="$(grep -E "^[[:space:]]*${key}=" "$env_file" | head -n1 | cut -d= -f2- || true)"
  if [ -z "$value" ]; then
    preflight_error "required bootstrap value '$key' is empty in $(basename "$env_file") (D-03)"
    return 1
  fi
  return 0
}

preflight_required_values() {  # preflight_required_values <env_file> <key...> — D-03 gate
  local env_file="$1"
  shift
  local key
  local ok=0
  for key in "$@"; do
    require_env_value "$env_file" "$key" || ok=1
  done
  return "$ok"
}

# ---------------------------------------------------------------------------
# Reading one literal value out of an env file — the single shared definition
# for BOTH stacks (Phase 311.1 D-01a). The env file is never sourced, so a value
# can never be executed.
#
# _normalize_env_value is the SINGLE definition of "the value this file assigns
# to a key" wherever that value decides a placeholder question (D-07). It
# removes a trailing carriage return (a CRLF .env — the Git-for-Windows
# core.autocrlf default, or a Windows-side edit, and this repository ships no
# .gitattributes), one layer of matching surrounding quotes and surrounding
# whitespace. Comparing the raw remainder instead made `CHANGE_ME` carrying a
# CR, or `"CHANGE_ME"`, look like an already-set value: the installer then
# filled nothing, reported success and left the placeholder in every guarded
# key. scripts/publish.sh's own env reader already strips the CR (see
# check_env_overlap); this is that convention, defined ONCE for the installer
# path — a second copy is how the comparison drifts back apart.
#
# It lives in this base library because both stacks already source it
# (scripts/docker/lib/common.sh sources this file): the shared secret-generation
# core (scripts/lib/secrets.sh) reaches every value through these two functions
# and must be usable from either stack without depending on the other one's
# library (Phase 311.1 D-01a).
# ---------------------------------------------------------------------------
_normalize_env_value() {  # _normalize_env_value <raw-value> — echo the comparable value
  local value="${1-}"
  value="${value%$'\r'}"                              # CRLF line ending
  value="${value#"${value%%[![:space:]]*}"}"          # trim leading whitespace
  value="${value%"${value##*[![:space:]]}"}"          # trim trailing whitespace
  case "$value" in
    \"*\") value="${value#\"}"; value="${value%\"}" ;;  # "quoted"
    \'*\') value="${value#\'}"; value="${value%\'}" ;;  # 'quoted'
  esac
  value="${value#"${value%%[![:space:]]*}"}"          # trim again: the padding
  value="${value%"${value##*[![:space:]]}"}"          # around a quoted value
  printf '%s' "$value"
}

env_file_value() {  # env_file_value <env_file> <key> — echo the literal value, normalised ("" when absent)
  # Stack-neutral reader (Phase 311.1 D-01a): first literal `KEY=` match, value
  # passed through _normalize_env_value, empty when the key is absent. The
  # Docker library's own reader survives only as a one-line wrapper around this.
  local env_file="$1" key="$2" value
  value="$(grep -E "^[[:space:]]*${key}=" "$env_file" 2>/dev/null | head -n1 | cut -d= -f2- || true)"
  _normalize_env_value "$value"
}

require_sudo() {  # require_sudo — fail before mutation when sudo is unavailable (D-14)
  if ! command -v sudo >/dev/null 2>&1; then
    preflight_error "sudo is required for host changes but was not found (D-14)"
    return 1
  fi
  if ! sudo -n true 2>/dev/null; then
    preflight_error "sudo is required for host changes but is not usable non-interactively (D-14)"
    return 1
  fi
  return 0
}

run_preflight() {  # run_preflight <env_file> <schema_file> <required-nonempty-keys...> — gate before mutation
  local env_file="$1"
  local schema_file="$2"
  shift 2
  validate_env_schema "$env_file" "$schema_file" || return 1
  if [ "$#" -gt 0 ]; then
    preflight_required_values "$env_file" "$@" || return 1
  fi
  require_sudo || return 1
  return 0
}

# ---------------------------------------------------------------------------
# k3s kubeconfig location (D-14): k3s writes a root-only (mode 600) kubeconfig
# by default. Rather than loosening it world-readable so kubectl/helm can run
# unprivileged, k3s.sh's readiness checks and helm.sh's release reconciliation
# both use this shared path with targeted `sudo env KUBECONFIG=...` calls.
# ---------------------------------------------------------------------------
K3S_KUBECONFIG="${INDRAMESH_K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"

# ---------------------------------------------------------------------------
# D-13 permitted bootstrap package gate
# ---------------------------------------------------------------------------

# Only these packages may ever be apt-installed by the bootstrap. Command-based
# entries are probed with `command -v`; coreutils has no single binary, so it
# is probed through sha256sum (a coreutils command). ca-certificates has no
# guaranteed command, so its presence is read from the apt/dpkg package
# database (dpkg-query) instead of `command -v`.
APT_ALLOWLIST=(curl tar coreutils gnupg)
APT_ALLOWLIST_CA_CERTIFICATES=ca-certificates
APT_ALLOWLIST_COREUTILS_PROBE=sha256sum

_is_allowed_package() {  # _is_allowed_package <pkg>
  local pkg="$1"
  local allowed
  for allowed in "${APT_ALLOWLIST[@]}"; do
    [ "$allowed" = "$pkg" ] && return 0
  done
  [ "$pkg" = "$APT_ALLOWLIST_CA_CERTIFICATES" ]
}

_package_probe() {  # _package_probe <pkg> — echo the command -v probe, empty for ca-certificates
  case "$1" in
    curl) echo curl ;;
    tar) echo tar ;;
    coreutils) echo "$APT_ALLOWLIST_COREUTILS_PROBE" ;;
    gnupg) echo gpg ;;
    *) echo "" ;;
  esac
}

_package_present() {  # _package_present <pkg> — 0 when the package is installed
  local pkg="$1"
  local probe
  if [ "$pkg" = "$APT_ALLOWLIST_CA_CERTIFICATES" ]; then
    dpkg-query --show --showformat='${Status}' ca-certificates 2>/dev/null | grep -q "install ok installed"
    return
  fi
  probe="$(_package_probe "$pkg")"
  [ -n "$probe" ] || return 1
  command -v "$probe" >/dev/null 2>&1
}

bootstrap_missing_packages() {  # bootstrap_missing_packages — echo allowlist entries that are absent
  local pkg
  for pkg in "${APT_ALLOWLIST[@]}" "$APT_ALLOWLIST_CA_CERTIFICATES"; do
    if ! _package_present "$pkg"; then
      echo "$pkg"
    fi
  done
}

require_bootstrap_package() {  # require_bootstrap_package <pkg> — D-13: reject unlisted packages before apt
  local pkg="$1"
  if ! _is_allowed_package "$pkg"; then
    preflight_error "package '$pkg' is not in the bootstrap allowlist (curl, ca-certificates, tar, coreutils, gnupg) — refusing apt install (D-13)"
    return 1
  fi
  return 0
}

ensure_bootstrap_packages() {  # ensure_bootstrap_packages — D-13: idempotent apt install of missing allowlist entries only
  local missing
  missing="$(bootstrap_missing_packages)"
  if [ -z "$missing" ]; then
    step "preflight: bootstrap packages present (curl, ca-certificates, tar, coreutils, gnupg)"
    return 0
  fi
  step "preflight: installing missing bootstrap packages: $(echo "$missing" | tr '\n' ' ')"
  sudo apt-get update
  # shellcheck disable=SC2086
  sudo apt-get install --yes --no-install-recommends $missing
}
