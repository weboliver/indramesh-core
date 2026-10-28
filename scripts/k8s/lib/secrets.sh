#!/bin/bash
# secrets.sh — installer-owned split-Secret lifecycle for the v16.0 k3s
# bootstrap (D-05..D-08; SEC-02; T-255-01..03). Sourceable; requires common.sh
# (step/preflight_error/K3S_KUBECONFIG) and namespace.sh (NAMESPACE_NAME).
#
# Sensitive root .env material is reconciled into split native Kubernetes
# Secrets grouped by consumer (D-05) BEFORE Helm runs (D-06): every workload
# receives only the keys it needs and no secret value ever passes through
# chart values or Helm arguments. Reconciliation is compare-before-create
# (D-07): an absent Secret is created from private 0600 file inputs via
# `kubectl create secret generic --from-file=...` (values never appear in
# argv); an existing Secret whose data matches the .env material is a no-op;
# a mismatch fails non-zero naming only the Secret/key boundary — never
# values, encoded values, or digests — and requires an explicit operator
# rotation; a normal rerun NEVER overwrites an existing Secret. Only
# sensitive keys (passwords, JWT/Fernet keys, admin/API credentials) become
# Secret data; URLs, ports, and feature flags stay non-secret chart
# configuration (D-08).
#
# Rotation (D-07): when .env credential material changes, rotate deliberately:
#   1. Prepare the new value in a private file, e.g.
#        umask 077; printf '%s' "$NEW_VALUE" > /tmp/indramesh-jwt.new
#   2. Replace the Secret explicitly — create --dry-run + apply is safe here
#      because the OPERATOR, not a rerun, is performing the rotation:
#        sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl -n indramesh \
#          create secret generic indramesh-backend-auth \
#          --from-file=JWT_SECRET_KEY=/tmp/indramesh-jwt.new \
#          --dry-run=client -o yaml \
#        | sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl -n indramesh apply -f -
#   3. Restart the consuming workloads so they mount the rotated material.
# The installer's mismatch failure is the guard that forces this explicit
# step instead of silent credential rotation on a normal rerun.
#
# Phase 311.1 D-01a: this library sources the shared secret-generation core
# (scripts/lib/secrets.sh) itself, BASH_SOURCE-relative, so it is the ONE place
# a consumer has to source. scripts/k8s/install.sh therefore receives
# ENV_SECRET_KEYS / SECRET_PLACEHOLDER / fill_env_placeholders through this
# library with no source line of its own, and so does every in-process caller of
# the reconciliation entry point — explicitly tests/bootstrap/test_namespace_secrets.sh,
# which sources only scripts/k8s/lib/common.sh and this file.
#
# Double-source guard: sourcing this library twice is a no-op.
if [ -n "${_INDRAMESH_K8S_SECRETS_SH_LOADED:-}" ]; then
  return 0
fi
_INDRAMESH_K8S_SECRETS_SH_LOADED=1
set -euo pipefail

_K8S_SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# The shared generation core, resolved relative to this file (same pattern as
# scripts/docker/lib/common.sh -> scripts/k8s/lib/common.sh).
# shellcheck source=../../lib/secrets.sh
. "$(cd "$_K8S_SECRETS_DIR/../.." && pwd)/lib/secrets.sh"

# Explicit, versioned key-to-Secret consumer table (D-05, D-08): membership is
# derived from .env.example's sensitive candidates (passwords, JWT/Fernet
# keys, admin/API credentials). Keys absent or empty in .env are skipped; a
# group with no material is left absent.
#
# Phase 308.2 (D-02): a token may carry an optional source alias,
# `<secret_key>:<env_key>`, for a Secret data key whose value comes from a
# DIFFERENT .env key. The Secret key (before ':') is the cluster-facing
# contract: it keys the private input file, the --from-file argument, the
# compare loop and every diagnostic. The source key (after ':') is only the
# .env lookup key. The value is derived, never authored twice — there is no
# second .env credential for indramesh-database's POSTGRES_PASSWORD.
#
# Phase 287.1 (PLUGDB-07/D-01; CR-02/WR-04): PLUGIN_DB_PASSWORD is grouped with
# the database secret because both Helm migration Jobs read it via an optional
# secretKeyRef from indramesh-database. The non-secret PLUGIN_DB_USER is
# deliberately NOT here — it is the chart value plugins.dbUser, empty by default
# (D-08); setting it activates the boundary. A Secret that predates this key is
# reported as MISSING the key (not as a rotation) so the operator extends it
# explicitly; see docs/CONFIGURATION.md for the plugin-role credentials.
SECRET_GROUPS=(
  "indramesh-database|POSTGRES_PASSWORD:DB_PASSWORD DB_PASSWORD PLUGIN_DB_PASSWORD"
  "indramesh-valkey|VALKEY_PASSWORD"
  "indramesh-opensearch|OPENSEARCH_PASSWORD"
  "indramesh-backend-auth|JWT_SECRET_KEY FERNET_KEY ADMIN_PASSWORD"
  "indramesh-external-api|MISTRAL_API_KEY LLM_API_KEY"
)

# Phase 311.1 (D-02, D-03, T-311.1-04): the mandatory groups. Every group above
# except the external-API one carries the Compose-guarded mandatory key set, so a
# mandatory group that resolves NO desired material must fail naming the group —
# it may never report "nothing to reconcile" as success (the group-level
# fail-open the plan closes). The optional group (indramesh-external-api, whose
# MISTRAL_API_KEY/LLM_API_KEY stay optional per D-02) keeps today's skip
# semantics exactly, so an intentional external-API-less install never turns
# into a failure (T-311.1-03).
MANDATORY_SECRET_GROUPS=(
  indramesh-database
  indramesh-valkey
  indramesh-opensearch
  indramesh-backend-auth
)

_is_mandatory_secret_group() {  # _is_mandatory_secret_group <name> — 0 when the group is mandatory
  local name="$1" candidate
  for candidate in "${MANDATORY_SECRET_GROUPS[@]}"; do
    [ "$candidate" = "$name" ] && return 0
  done
  return 1
}

require_mandatory_env_secrets() {  # require_mandatory_env_secrets <env_file> — fail closed on a missing/empty/placeholder mandatory key (D-02, D-03)
  # The fail-closed gate (D-01a) the install path was missing: for every key in
  # the shared mandatory set, an absent, present-but-empty or placeholder value
  # aborts the run, naming the KEY only — never the value. It consumes the
  # shared key list, the shared placeholder constant and the shared normalising
  # reader `env_file_value`; it re-lists no keys and re-implements no quote/CR
  # strip (the normaliser is the single definition).
  local env_file="${1:?.env file required}"
  # Prove the shared key list is USABLE without expanding it: a declaration
  # probe (`declare -p`), never "${ENV_SECRET_KEYS[@]}" on a possibly-unset name
  # (that would trip `set -u` or, worse, be read as one empty element and wave
  # every key through). A missing or empty list is a broken environment, not a
  # licence to approve keys (D-01a point 5).
  if ! declare -p ENV_SECRET_KEYS >/dev/null 2>&1; then
    preflight_error "preflight: the shared mandatory secret key list (ENV_SECRET_KEYS) is unavailable — source scripts/lib/secrets.sh through the stack secrets library (D-01a)"
    return 1
  fi
  if [ "${#ENV_SECRET_KEYS[@]}" -eq 0 ]; then
    preflight_error "preflight: the shared mandatory secret key list (ENV_SECRET_KEYS) is empty — refusing to approve every key (D-01a)"
    return 1
  fi
  [ -f "$env_file" ] || { preflight_error "environment file missing: $env_file"; return 1; }
  local key value failed=0
  for key in "${ENV_SECRET_KEYS[@]}"; do
    value="$(env_file_value "$env_file" "$key")"
    if [ -z "$value" ]; then
      preflight_error "preflight: mandatory secret '$key' is missing or empty in $(basename "$env_file") — set it in the env file and rerun (D-02)"
      failed=1
      continue
    fi
    if [ "$value" = "$SECRET_PLACEHOLDER" ]; then
      preflight_error "preflight: mandatory secret '$key' still holds the $SECRET_PLACEHOLDER placeholder in $(basename "$env_file") — set it in the env file and rerun (D-03)"
      failed=1
    fi
  done
  [ "$failed" -eq 0 ]
}

_kubectl_indramesh() {  # _kubectl_indramesh <args...> — targeted sudo + root-only kubeconfig (D-14)
  sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"
}

dotenv_value() {  # dotenv_value <env_file> <key> — literal KEY= value only (never sources .env)
  local env_file="$1" key="$2"
  grep -E "^[[:space:]]*${key}=" "$env_file" | head -n1 | cut -d= -f2- || true
}

_secret_exists() {  # _secret_exists <name> <namespace> — 0 when the Secret is present
  _kubectl_indramesh get secret "$1" -n "$2" >/dev/null 2>&1
}

_secret_data_key() {  # _secret_data_key <name> <namespace> <key> — echo the base64 data ("" when absent)
  local escaped
  escaped="$(_jsonpath_escape_key "$3")"
  _kubectl_indramesh get secret "$1" -n "$2" -o "jsonpath={.data['$escaped']}" 2>/dev/null || true
}

reconcile_secret_group() {  # reconcile_secret_group <secret> <namespace> <env_file> <tmp_dir> <key[:source]...>
  # Compare-before-create for one split Secret (D-07). Writes each desired
  # value to a private 0600 file under <tmp_dir>, creates the Secret with
  # --from-file inputs (values never appear in argv, T-255-01), and fails
  # non-zero on any mismatch between existing and desired data without
  # printing values, encoded values, or digests.
  #
  # A `<key>:<source>` token (Phase 308.2, D-02) reads the value from the
  # <source> .env key while every artefact below stays keyed on the Secret
  # key <key>; a bare token keeps source == Secret key.
  local name="$1" ns="$2" env_file="$3" tmp_dir="$4"
  shift 4
  local spec key src value dir from_args=() resolved_keys=() desired=0 existing enc
  SECRET_GROUP_SKIPPED=0
  dir="$tmp_dir/secret-$name"
  mkdir -p "$dir" && chmod 700 "$dir"
  for spec in "$@"; do
    key="${spec%%:*}"          # Secret data key — cluster-facing contract
    src="${spec#*:}"           # .env source key ...
    if [ "$src" = "$spec" ]; then
      src="$key"               # ... unless the token carried no ':' suffix
    fi
    value="$(dotenv_value "$env_file" "$src")"
    [ -n "$value" ] || continue
    # Presence is not compliance for the search admin password (review
    # 2026-10-18, M5): the OpenSearch image's demo-configuration installer
    # enforces a character rule and exits the container on a non-compliant value,
    # so a carried-over or hand-written search password would materialise a
    # Secret whose pod never starts. The shared validator
    # (scripts/lib/secrets.sh, one definition for both stacks) is applied to the
    # resolved value BEFORE the group is reconciled; the abort names the group
    # and the key — never the value.
    if [ "$key" = "OPENSEARCH_PASSWORD" ] && ! opensearch_password_compliant "$value"; then
      preflight_error "secret '$name' key '$key' does not satisfy the OpenSearch demo-configuration rule (8-100 characters, at least one uppercase, one lowercase, one digit and one special character, only the characters [A-Za-z0-9_-]) — the search pod would exit at start; set a compliant value in the env file and rerun (D-05)"
      return 1
    fi
    printf '%s' "$value" > "$dir/$key"
    chmod 600 "$dir/$key"
    from_args+=(--from-file="$key=$dir/$key")
    resolved_keys+=("$key")
    desired=$((desired + 1))
  done
  if [ "$desired" -eq 0 ]; then
    # A MANDATORY group may never degrade into "nothing to reconcile is
    # success" (Phase 311.1 D-02, T-311.1-04): its keys are the Compose-guarded
    # mandatory set, so an all-empty resolution is a broken env file, not an
    # optional install. The optional group keeps today's skip semantics.
    if _is_mandatory_secret_group "$name"; then
      preflight_error "secret '$name' is mandatory but .env provides none of its keys — a mandatory group may never reconcile to nothing (D-02)"
      return 1
    fi
    SECRET_GROUP_SKIPPED=1
    step "secrets: '$name' has no sensitive material in .env — skipped"
    return 0
  fi
  if _secret_exists "$name" "$ns"; then
    # Iterate the RESOLVED Secret keys, never the raw table tokens
    # (T-308.2-05): a '<key>:<source>' token names no file, so the [ -f ]
    # guard would skip it and a real mismatch would pass silently.
    for key in "${resolved_keys[@]}"; do
      [ -f "$dir/$key" ] || continue
      enc="$(base64 -w0 < "$dir/$key")"
      existing="$(_secret_data_key "$name" "$ns" "$key")"
      # WR-04: an ABSENT key is not rotation. A Secret that predates a newly
      # configured credential (e.g. PLUGIN_DB_PASSWORD on a Phase 287.1 upgrade)
      # must be extended explicitly, not "rotated" — the old message sent the
      # operator down the wrong path. Still fail closed: a rerun never
      # overwrites an existing Secret (D-07).
      if [ -z "$existing" ]; then
        preflight_error "secret '$name' is missing key '$key' that .env now provides — add the key explicitly (a rerun never overwrites an existing Secret, D-07); see the plugin-role credentials in docs/CONFIGURATION.md"
        return 1
      fi
      if [ "$existing" != "$enc" ]; then
        preflight_error "secret '$name' key '$key' differs from .env — rotation requires an explicit operator action; a rerun never overwrites it (D-07)"
        return 1
      fi
    done
    step "secrets: '$name' matches .env material — no change (D-07)"
  else
    step "secrets: creating '$name' from .env material (D-05, D-06)"
    _kubectl_indramesh create secret generic "$name" -n "$ns" "${from_args[@]}" >/dev/null || return 1
  fi
  return 0
}

reconcile_indramesh_secrets() {  # reconcile_indramesh_secrets <env_file> [namespace] — D-05/D-06/D-07 gate
  local env_file="${1:?.env file required}"
  local ns="${2:-${NAMESPACE_NAME:-indramesh}}"
  local spec name keys skipped_optional=0
  [ -f "$env_file" ] || { preflight_error "environment file missing: $env_file"; return 1; }
  [ -n "${TMP_DIR:-}" ] || { preflight_error "private temporary directory is required (TMP_DIR)"; return 1; }
  # The gate runs at the TOP of this entry point — after the env-file and
  # private-temp-dir checks — so EVERY path that materialises Secrets walks it,
  # the installer's and the direct-invocation harness's alike (T-311.1-01).
  require_mandatory_env_secrets "$env_file" || return 1
  step "secrets: reconciling split Secrets in '$ns' (D-05, D-06)"
  for spec in "${SECRET_GROUPS[@]}"; do
    name="${spec%%|*}"
    keys="${spec#*|}"
    # shellcheck disable=SC2086
    reconcile_secret_group "$name" "$ns" "$env_file" "$TMP_DIR" $keys || return 1
    if [ "${SECRET_GROUP_SKIPPED:-0}" -eq 1 ]; then
      skipped_optional=$((skipped_optional + 1))
    fi
  done
  # Truthful close (Phase 311.1 D-02): the mandatory groups are reconciled; a
  # skipped OPTIONAL group is reported as a skip, never as success, and never as
  # an error. A mandatory group can no longer reach this line having produced
  # nothing.
  if [ "$skipped_optional" -eq 0 ]; then
    step "secrets: mandatory split Secrets reconciled (${#MANDATORY_SECRET_GROUPS[@]} groups) — no optional group was skipped; Helm may proceed (D-06)"
  else
    step "secrets: mandatory split Secrets reconciled (${#MANDATORY_SECRET_GROUPS[@]} groups); $skipped_optional optional group(s) skipped (no material in .env) — Helm may proceed (D-06)"
  fi
  return 0
}