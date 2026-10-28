#!/bin/bash
# nginx_tls.sh — installer-owned TLS Secret lifecycle for the nginx k3s
# workload (IMG-01, D-01, D-05). Sourceable; requires common.sh (step/
# preflight_error/K3S_KUBECONFIG) and secrets.sh (_secret_exists/
# _secret_data_key/_kubectl_indramesh).
#
# indramesh-nginx-tls is reconciled compare-before-create (D-05), mirroring
# secrets.sh's reconcile_secret_group shape: an absent Secret is created from
# the tracked dev cert/key files via `kubectl create secret tls --cert=
# --key=` (values never appear in argv, T-254-02/T-255-01); an existing
# Secret whose tls.crt/tls.key match the desired files is a no-op; any
# mismatch fails non-zero naming only the Secret/key boundary — never
# values, encoded values, or digests — and requires an explicit operator
# rotation (a rerun never overwrites differing material).
#
# nginx_tls_content_hash() computes a deterministic digest-of-digests over
# the desired cert+key material; reconcile_nginx_tls_secret() sets it into
# the global TLS_CONTENT_HASH so the caller can feed it to the chart's
# apps.nginx.tlsContentHash value (checksum/secret Pod-template annotation,
# D-04/D-08) — the Secret itself is installer-owned, so the chart cannot
# compute this hash.
set -euo pipefail

# Own script directory, private-prefixed so sourcing this file never clobbers
# a caller's own SCRIPT_DIR (images.sh documents this same hazard; discovered
# live in Plan 258-03 when rebuild.sh's own SCRIPT_DIR — computed BEFORE
# sourcing this file — was silently overwritten by this then-generic name,
# breaking a default that resolves relative to it).
_NGINX_TLS_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$_NGINX_TLS_SH_DIR/../../.." && pwd)"

# Env seams for the tracked dev cert/key material (git-tracked, non-secret —
# real secret rotation is an explicit operator action, see the header above).
INDRAMESH_NGINX_CERT_FILE="${INDRAMESH_NGINX_CERT_FILE:-$ROOT_DIR/nginx/certs/server.crt}"
INDRAMESH_NGINX_KEY_FILE="${INDRAMESH_NGINX_KEY_FILE:-$ROOT_DIR/nginx/certs/server.key}"

nginx_tls_content_hash() {  # nginx_tls_content_hash <cert_file> <key_file> — deterministic digest-of-digests
  local cert_file="$1" key_file="$2"
  { sha256sum "$cert_file"; sha256sum "$key_file"; } | sha256sum | awk '{print $1}'
}

reconcile_nginx_tls_secret() {  # reconcile_nginx_tls_secret [cert_file] [key_file] [namespace]
  local cert_file="${1:-$INDRAMESH_NGINX_CERT_FILE}"
  local key_file="${2:-$INDRAMESH_NGINX_KEY_FILE}"
  local ns="${3:-${NAMESPACE_NAME:-indramesh}}"
  local name="indramesh-nginx-tls"
  local enc_crt enc_key existing_crt existing_key

  [ -f "$cert_file" ] || { preflight_error "nginx TLS cert file missing: $cert_file"; return 1; }
  [ -f "$key_file" ] || { preflight_error "nginx TLS key file missing: $key_file"; return 1; }

  TLS_CONTENT_HASH="$(nginx_tls_content_hash "$cert_file" "$key_file")"

  if _secret_exists "$name" "$ns"; then
    enc_crt="$(base64 -w0 < "$cert_file")"
    enc_key="$(base64 -w0 < "$key_file")"
    existing_crt="$(_secret_data_key "$name" "$ns" "tls.crt")"
    existing_key="$(_secret_data_key "$name" "$ns" "tls.key")"
    if [ "$existing_crt" != "$enc_crt" ] || [ "$existing_key" != "$enc_key" ]; then
      preflight_error "secret '$name' TLS material differs from the source files — rotation requires an explicit operator action; a rerun never overwrites it (D-05)"
      return 1
    fi
    step "nginx TLS: '$name' matches the source cert/key — no change (D-05)"
  else
    step "nginx TLS: creating '$name' from the source cert/key (D-01, D-05)"
    _kubectl_indramesh create secret tls "$name" -n "$ns" --cert="$cert_file" --key="$key_file" >/dev/null || return 1
  fi
  return 0
}
