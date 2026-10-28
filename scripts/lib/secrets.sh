#!/bin/bash
# secrets.sh — the ONE shared secret-generation core both quickstarts use
# (Phase 311.1 D-01/D-01a, extracted from the Docker library of Phase 311).
# Sourceable; sources nothing itself.
#
# Contract (unchanged from the Docker original — every placeholder comparison,
# the compare-before-write semantics and the fail-closed close):
#   * Placeholder detection, generation and fill walk the SECRET key set — wider
#     than the Compose-guarded set, because PLUGIN_DB_PASSWORD also becomes
#     CHANGE_ME and carries no Compose guard, yet a placeholder left there would
#     be a publicly known plugin-role password.
#   * Compare-before-write (D-06): only a value that is empty or the literal
#     placeholder is filled. An already-set value is never rewritten, so a
#     Postgres volume initialised with the old password stays openable.
#   * Every placeholder comparison runs the value through _normalize_env_value
#     (defined in scripts/k8s/lib/common.sh — the single definition): a trailing
#     CR or one layer of surrounding quotes is removed first, so `CHANGE_ME\r`
#     and `"CHANGE_ME"` are recognised as placeholders instead of passing as
#     already-set values (D-07). A filled value is always written back unquoted
#     and without a CR.
#   * The file is rebuilt and moved into place, so it always holds either the
#     previous or the new complete content — an interrupted or concurrent run
#     cannot leave a half-written .env. It is written under a restrictive umask.
#   * No value ever reaches stdout except the generated ADMIN_PASSWORD, reported
#     once by the caller on the run that generated it (D-10).
#
# Prerequisites, all supplied by the sourcing environment (D-01a):
#   die, step, env_file_value, _normalize_env_value
#     — from scripts/k8s/lib/common.sh, the base library both stack libraries
#       source. This core reaches every value through the stack-neutral reader
#       `env_file_value` ONLY, never through a stack-namespaced helper, so it is
#       usable from either stack; it calls no `docker_*` and no k8s-specific
#       helper.
#   ROOT_DIR
#     — only for the container-backed value helper (`ensure_backend_image`,
#       `container_python_value`).
#
# Both stack libraries (scripts/docker/lib/secrets.sh and
# scripts/k8s/lib/secrets.sh) source this file themselves, BASH_SOURCE-relative,
# so no consumer ever needs a source line of its own and no environment depends
# on the other stack's library. A second definition of the key set, the
# placeholder constant or the fill routine is exactly the drift that produced
# the empty-DB_PASSWORD fail-open — it does not belong in either stack library.

# Double-source guard: sourcing this file twice is a no-op.
if [ -n "${_INDRAMESH_SHARED_SECRETS_SH_LOADED:-}" ]; then
  return 0
fi
_INDRAMESH_SHARED_SECRETS_SH_LOADED=1

# Every key the installer fills. Wider than the Compose-guarded set: the guarded
# set is DB_PASSWORD, VALKEY_PASSWORD, OPENSEARCH_PASSWORD, JWT_SECRET_KEY,
# FERNET_KEY, ADMIN_PASSWORD — PLUGIN_DB_PASSWORD is filled here as well.
# OPENSEARCH_PASSWORD is the one key whose VALUE is additionally checked, not just
# its presence: the OpenSearch image's demo-configuration installer enforces a
# password rule and exits the container on a non-compliant value, so the fill
# ends with `opensearch_password_compliant` (see below).
ENV_SECRET_KEYS=(
  DB_PASSWORD
  PLUGIN_DB_PASSWORD
  VALKEY_PASSWORD
  OPENSEARCH_PASSWORD
  JWT_SECRET_KEY
  FERNET_KEY
  ADMIN_PASSWORD
)
SECRET_PLACEHOLDER="CHANGE_ME"

generate_secret_value() {  # generate_secret_value [hex|admin|opensearch] — echo a fresh secret
  case "${1:-hex}" in
    # Hex for every password: it is URL-safe, so the redis:// URL and the
    # startup-built database URL stay parseable.
    hex) openssl rand -hex 24 ;;
    # Alphanumeric, 24 characters (Phase 318, host-B login finding): the value
    # is read from the installer's report or .env and typed by hand into the
    # login form, so it is restricted to plain letters and digits — base64's
    # `+`, `/` and `=` are dropped as easy to mistype or mis-select. The login
    # path itself handles them correctly (URLSearchParams encodes `+` as `%2B`).
    # The OpenSearch branch filters to the same alphabet (M4).
    admin|base64) printf '%s' "$(openssl rand -base64 32 | tr -dc 'A-Za-z0-9' | cut -c1-24)" ;;
    # The OpenSearch admin password must satisfy the rule the image's
    # demo-configuration installer enforces (8-100 characters, at least one
    # uppercase, one lowercase, one digit and one special character, not similar
    # to the user name) — a weak value makes the container exit at start, so the
    # stack's usual 48-hex value would never come up. The fixed leading quartet
    # carries the four classes, the rest is random alphanumeric material.
    # The character set is deliberately narrow (review 2026-10-18, M4): the value
    # is interpolated by Compose (`$`), expanded inside the probe/healthcheck
    # shell strings and embedded in `-u user:pass`, so `$`, backtick, quotes,
    # backslash, `:`, `@`, `#`, `!`, `&`, `;`, `*`, `?`, whitespace and the
    # base64 characters `+`/`/`/`=` are all excluded; `_` alone satisfies the
    # plugin's non-alphanumeric class.
    opensearch) printf 'Aa1_%s' "$(openssl rand -base64 48 | tr -dc 'A-Za-z0-9' | cut -c1-48)" ;;
    *) die "secrets" "unknown secret kind '${1}'" ;;
  esac
}

# opensearch_password_compliant <value> — 0 when the value satisfies the rule the
# OpenSearch image's demo-configuration installer enforces on every first start
# (docs.opensearch.org, "Setting up a demo configuration"): 8-100 characters, at
# least one uppercase, one lowercase, one digit and one special character, drawn
# only from the characters that survive Compose interpolation, the shell
# expansions above and `-u user:pass` — `[A-Za-z0-9_-]`. A carried-over value
# (for example the developer machine's old 48-hex search password) passes every
# presence check but makes the search container exit at start; the fail-closed
# gate in fill_env_placeholders calls this for OPENSEARCH_PASSWORD, and
# scripts/k8s/lib/secrets.sh reuses it for the Kubernetes Secret group, so both
# stacks abort by key name instead.
opensearch_password_compliant() {
  local value="${1:-}"
  local length="${#value}"
  [ "$length" -ge 8 ] && [ "$length" -le 100 ] || return 1
  printf '%s' "$value" | grep -q '[A-Z]' || return 1
  printf '%s' "$value" | grep -q '[a-z]' || return 1
  printf '%s' "$value" | grep -q '[0-9]' || return 1
  printf '%s' "$value" | grep -q '[^A-Za-z0-9]' || return 1
  if printf '%s' "$value" | grep -q '[^A-Za-z0-9_-]'; then
    return 1
  fi
  return 0
}

CONTAINER_IMAGE_BUILT=0

ensure_backend_image() {  # build the shared backend image once (a build, never a start)
  [ "${CONTAINER_IMAGE_BUILT:-0}" = "1" ] && return 0
  [ -n "${ROOT_DIR:-}" ] || die "secrets" "ROOT_DIR is not set — source this library through a stack library (scripts/docker/lib/secrets.sh or scripts/k8s/lib/secrets.sh)"
  step "secrets: building the backend image for key generation (build only — no container is started)"
  # `</dev/null` on every Docker invocation in this file: a build or run that
  # inherits the caller's stdin silently consumes it. Inside fill_env_placeholders
  # the caller is a `while read` loop over the env file, so an unredirected
  # invocation truncates .env at the first container-generated key — see the
  # comment on container_python_value.
  ( cd "$ROOT_DIR" && docker compose build backend </dev/null ) || die "secrets" "docker compose build backend failed"
  CONTAINER_IMAGE_BUILT=1
}

container_python_value() {  # container_python_value <python-code> — echo the value
  # Generates the two keys whose format the application owns, using the
  # project's own image (the route README already documents) so no host
  # `cryptography` is needed.
  local code="$1"
  ensure_backend_image
  # `--no-deps` is MANDATORY: without it Compose starts postgres, whose fresh
  # volume would be initialised with the CHANGE_ME placeholder and could then
  # never be opened with the password generated a moment later (D-06 forbids
  # rotating it afterwards).
  #
  # `</dev/null` is MANDATORY too: the caller is the `while read` loop in
  # fill_env_placeholders, whose stdin is the env file being rewritten. `docker
  # compose run` inherits and consumes that stdin, so an unredirected call
  # swallows everything after the first container-generated key (FERNET_KEY):
  # the loop then sees EOF, the rebuilt .env is truncated at that line, and the
  # later keys (JWT_SECRET_KEY, ADMIN_PASSWORD) are absent — not merely
  # unfilled. Measured on a clean host; see .planning/WINDOWS.md.
  ( cd "$ROOT_DIR" && docker compose run --rm --no-deps --entrypoint python backend -c "$code" </dev/null ) \
    | awk 'NF { print }' | tail -n1
}

_gen_for_key() {  # _gen_for_key <key> — echo a generated value for one secret key
  case "$1" in
    JWT_SECRET_KEY) container_python_value "import secrets; print(secrets.token_urlsafe(32))" ;;
    FERNET_KEY)     container_python_value "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())" ;;
    ADMIN_PASSWORD) generate_secret_value admin ;;
    OPENSEARCH_PASSWORD) generate_secret_value opensearch ;;
    *)              generate_secret_value hex ;;
  esac
}

_is_env_secret_key() {  # _is_env_secret_key <key> — 0 when the installer owns the key
  local key="$1" candidate
  for candidate in "${ENV_SECRET_KEYS[@]}"; do
    [ "$candidate" = "$key" ] && return 0
  done
  return 1
}

_env_placeholder_keys() {  # _env_placeholder_keys <env_file> — echo the secret keys still holding a placeholder or nothing
  local env_file="$1" line key value
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*) continue ;;
    esac
    key=""
    if printf '%s' "$line" | grep -qE '^[[:space:]]*[A-Z_][A-Z0-9_]*='; then
      key="$(printf '%s' "$line" | sed -E 's/^[[:space:]]*([A-Z_][A-Z0-9_]*)=.*/\1/')"
    fi
    [ -n "$key" ] || continue
    _is_env_secret_key "$key" || continue
    # Normalised before the comparison (the single definition in common.sh):
    # the raw remainder would read a CRLF or quoted placeholder as a set value.
    value="$(_normalize_env_value "${line#*=}")"
    if [ -z "$value" ] || [ "$value" = "$SECRET_PLACEHOLDER" ]; then
      printf '%s\n' "$key"
    fi
  done < "$env_file"
}

fill_env_placeholders() {  # fill_env_placeholders <env_file>
  # Sets GENERATED_SECRET_COUNT and GENERATED_ADMIN_PASSWORD (the latter only
  # when THIS run generated the admin password — D-10).
  local env_file="${1:?env file required}"
  local tmp line key value new_value
  GENERATED_SECRET_COUNT=0
  GENERATED_ADMIN_PASSWORD=""
  [ -f "$env_file" ] || die "secrets" "environment file missing: $env_file"

  umask 077
  tmp="${env_file}.indramesh.tmp.$$"
  : > "$tmp" || die "secrets" "cannot write a temporary file next to $env_file"

  # Pre-scan for the container-generated keys, so the shared backend image is
  # built exactly once. It must be built from THIS shell (not from the command
  # substitution below, which runs in a subshell) or the flag would not survive
  # and the build would be repeated per key.
  local pending=""
  pending="$(_env_placeholder_keys "$env_file")"
  if printf '%s\n' "$pending" | grep -qx 'JWT_SECRET_KEY' \
    || printf '%s\n' "$pending" | grep -qx 'FERNET_KEY'; then
    ensure_backend_image
  fi

  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      ''|'#'*)
        printf '%s\n' "$line" >> "$tmp"
        continue
        ;;
    esac
    key=""
    if printf '%s' "$line" | grep -qE '^[[:space:]]*[A-Z_][A-Z0-9_]*='; then
      key="$(printf '%s' "$line" | sed -E 's/^[[:space:]]*([A-Z_][A-Z0-9_]*)=.*/\1/')"
    fi
    if [ -n "$key" ] && _is_env_secret_key "$key"; then
      # Same single definition as the pre-scan and the fail-closed check below:
      # a CRLF (`CHANGE_ME` + CR) or quoted placeholder must be filled, not
      # mistaken for an already-set value (D-07). A genuine value that merely
      # carries a CR or quotes is NOT touched here — the raw line is copied
      # through below; only a generated value replaces a line, and it is
      # written back unquoted and without a CR.
      value="$(_normalize_env_value "${line#*=}")"
      if [ -z "$value" ] || [ "$value" = "$SECRET_PLACEHOLDER" ]; then
        new_value="$(_gen_for_key "$key")"
        [ -n "$new_value" ] || die "secrets" "secret generation produced no value for $key"
        printf '%s=%s\n' "$key" "$new_value" >> "$tmp"
        GENERATED_SECRET_COUNT=$((GENERATED_SECRET_COUNT + 1))
        if [ "$key" = "ADMIN_PASSWORD" ]; then
          GENERATED_ADMIN_PASSWORD="$new_value"
        fi
        continue
      fi
    fi
    printf '%s\n' "$line" >> "$tmp"
  done < "$env_file"

  chmod 600 "$tmp" 2>/dev/null || true
  mv -f "$tmp" "$env_file" || { rm -f "$tmp"; die "secrets" "could not replace $env_file"; }

  # Fail closed: no placeholder may survive a run (D-07 — a placeholder is a
  # publicly known credential). An absent/empty key and a surviving placeholder
  # are different failures and are reported as such: an absent key means the
  # fill never reached it (a truncated or incomplete env file), not that the
  # placeholder is still there. `env_file_value` applies the same
  # _normalize_env_value as the fill, so "not filled" can never mean "still
  # holds the placeholder, spelled with a CR or in quotes".
  for key in "${ENV_SECRET_KEYS[@]}"; do
    value="$(env_file_value "$env_file" "$key")"
    if [ -z "$value" ]; then
      die "secrets" "$key is missing or empty in $env_file after the fill — the fill did not reach every key (truncated or incomplete file); set it manually and rerun"
    fi
    if [ "$value" = "$SECRET_PLACEHOLDER" ]; then
      die "secrets" "$key still holds the $SECRET_PLACEHOLDER placeholder after the fill — set it in $env_file manually and rerun"
    fi
    # Presence is not compliance for the OpenSearch admin password: an
    # operator-supplied or carried-over value that misses the demo-configuration
    # rule would make the search container exit at start, so it is refused here
    # by key name instead (review 2026-10-18, M5).
    if [ "$key" = "OPENSEARCH_PASSWORD" ] && ! opensearch_password_compliant "$value"; then
      die "secrets" "$key does not satisfy the OpenSearch demo-configuration rule (8-100 characters, at least one uppercase, one lowercase, one digit and one special character, only the characters [A-Za-z0-9_-]) — the search container would exit at start; set a compliant value in $env_file and rerun"
    fi
  done
  return 0
}
