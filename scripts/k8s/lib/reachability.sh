#!/bin/bash
# reachability.sh — platform-correct consumer-side HTTPS reachability adapter
# for the Phase 254 k3s bootstrap (D-05, D-06, D-08, D-09, D-23; T-254-02..04).
# Sourceable; requires common.sh for validate_hostname/preflight_error/step/
# require_sudo.
#
# Consumed Helm handoff contract (non-secret, helm.sh -> here):
#   bootstrap.endpoint.status = deferred | ready
#   bootstrap.endpoint.url    = validated HTTPS URL (required when ready)
# Phase 254 produces `deferred`; Phase 259 flips the contract to `ready`
# when it supplies Ingress/TLS (D-23). The adapter never certifies an endpoint
# from a deferred contract and never treats a Linux/in-cluster probe as
# Windows-browser proof (D-08, D-09, T-254-02).
#
# The endpoint is https://localhost: the local-forward bridge (local_forward.sh)
# puts a real listener on 127.0.0.1:{80,443} of the k3s host, and WSL2's
# localhost forwarding hands Windows' localhost to it, so no hosts file is
# written and no Windows step needs elevation. Ready contracts probe from the
# correct consumer origin: local curl on a native host, a Windows-side
# PowerShell/curl.exe probe on WSL2. Probe diagnostics report only
# endpoint/status information and never .env content (T-254-02).
set -euo pipefail

DEFAULT_INDRAMESH_HOSTNAME="localhost"

ENDPOINT_STATUS_DEFERRED="deferred"
ENDPOINT_STATUS_READY="ready"

# Outcome state, machine-consumable via endpoint_outcome/endpoint_diagnostic:
#   deferred  bootstrap completed; endpoint certification pending (Phase 259)
#   verified  probe from the correct consumer origin succeeded
#   failed    probe or contract failure propagated non-zero
ENDPOINT_OUTCOME=""
ENDPOINT_DIAGNOSTIC=""

reachability_hostname() {  # reachability_hostname [override] — echo validated hostname (D-06, T-254-03)
  local hostname="${1:-$DEFAULT_INDRAMESH_HOSTNAME}"
  if ! validate_hostname "$hostname"; then
    preflight_error "endpoint hostname '$hostname' is invalid — refusing to build probe/PowerShell arguments (T-254-03)"
    return 1
  fi
  echo "$hostname"
}

_validate_endpoint_url() {  # _validate_endpoint_url <url> — 0 for an https URL with a valid host and a safe path/query (D-08, T-254-03)
  local url="$1"
  local host
  case "$url" in
    https://*) ;;
    *)
      preflight_error "endpoint URL '$url' is not an https URL — refusing probe (D-08)"
      return 1
      ;;
  esac
  host="${url#https://}"
  host="${host%%/*}"
  host="${host%%:*}"
  if [ -z "$host" ] || ! validate_hostname "$host"; then
    preflight_error "endpoint URL '$url' has an invalid host (T-254-03)"
    return 1
  fi
  # The full URL (including path/query/fragment) is embedded in a PowerShell
  # command string by probe_windows_https via an environment variable, but it
  # is still allowlist-validated here so no caller of this URL can ever be
  # handed a value containing quoting/metacharacters (T-254-03). This permits
  # the RFC 3986 unreserved + pchar set used by ordinary URLs, minus the
  # single quote (which is what terminates a PowerShell single-quoted string).
  case "$url" in
    *[!A-Za-z0-9:/?#@!\$\&\(\)\*+,\;=._~%-]*)
      preflight_error "endpoint URL '$url' contains a disallowed character — refusing probe (T-254-03)"
      return 1
      ;;
  esac
  return 0
}

verify_endpoint_contract() {  # verify_endpoint_contract <status> [url] — 0 for deferred|ready, non-zero otherwise
  local status="${1:-}"
  local url="${2:-}"
  ENDPOINT_OUTCOME=""
  ENDPOINT_DIAGNOSTIC=""
  case "$status" in
    "$ENDPOINT_STATUS_DEFERRED")
      ENDPOINT_OUTCOME="$ENDPOINT_STATUS_DEFERRED"
      ENDPOINT_DIAGNOSTIC="endpoint verification deferred to Phase 259 (Ingress/TLS handoff): bootstrap completed with certification pending — not certified"
      return 0
      ;;
    "$ENDPOINT_STATUS_READY")
      [ -n "$url" ] || {
        preflight_error "bootstrap.endpoint.status=ready requires bootstrap.endpoint.url (D-08)"
        return 1
      }
      _validate_endpoint_url "$url" || return 1
      ENDPOINT_OUTCOME="$ENDPOINT_STATUS_READY"
      ENDPOINT_DIAGNOSTIC="endpoint contract ready — probe eligible"
      return 0
      ;;
    *)
      preflight_error "unknown bootstrap.endpoint.status '$status' — expected 'deferred' or 'ready'"
      return 1
      ;;
  esac
}

endpoint_outcome() {  # endpoint_outcome — machine-consumable outcome: deferred | verified | failed
  echo "$ENDPOINT_OUTCOME"
}

endpoint_diagnostic() {  # endpoint_diagnostic — non-secret human diagnostic for the last outcome (T-254-02)
  echo "$ENDPOINT_DIAGNOSTIC"
}

endpoint_result_message() {  # endpoint_result_message — installer-facing completion vs certification wording
  case "$ENDPOINT_OUTCOME" in
    "$ENDPOINT_STATUS_DEFERRED")
      echo "bootstrap completed; endpoint verification pending (Phase 259 Ingress/TLS handoff)"
      ;;
    verified)
      echo "endpoint verified from the consumer host"
      ;;
    failed)
      echo "endpoint verification failed — see reachability diagnostics"
      ;;
    *)
      echo "endpoint contract not evaluated"
      ;;
  esac
}

probe_native_https() {  # probe_native_https <url> — local curl; trust-store-verified after Phase 259, no bypass flag (D-08/D-09/BOOT-03)
  local url="$1"
  if ! command -v curl >/dev/null 2>&1; then
    preflight_error "reachability: curl is required for the native HTTPS probe (D-09)"
    return 1
  fi
  step "reachability: probing $url from the native host"
  if ! curl --fail --silent --show-error --max-time 15 "$url" >/dev/null 2>&1; then
    preflight_error "reachability: HTTPS probe of $url failed (DNS/TCP/HTTP/connectivity/trust error)"
    return 1
  fi
  return 0
}

probe_windows_https() {  # probe_windows_https <url> — Windows-origin probe via powershell.exe (WSL2, D-08/D-09)
  local url="$1"
  local ps_cmd
  if ! command -v powershell.exe >/dev/null 2>&1; then
    preflight_error "reachability: Windows interop (powershell.exe) is unavailable — cannot certify Windows-host access (D-09)"
    return 1
  fi
  step "reachability: probing $url from Windows (powershell.exe)"
  # Trust-store-verified after Phase 259: the retained CA root is imported
  # into the Windows LocalMachine\Root store by trust_store.sh BEFORE this
  # probe, so curl.exe runs with NO bypass flag (-k) — a successful probe
  # proves trust as well as connectivity (BOOT-03, research 429). DNS, TCP,
  # HTTP, and status failures remain errors (D-08). The full URL (host and
  # path/query) is allowlist-validated by _validate_endpoint_url before this
  # function is ever reached (T-254-03), and is additionally passed via the
  # INDRAMESH_PROBE_URL environment variable rather than string interpolation,
  # so it can never share a quoting context with the PowerShell command
  # string itself.
  #
  # gap-closure (BOOT-03 follow-up, confirmed live): --ssl-no-revoke is
  # required, not optional. curl.exe on Windows uses Schannel, which
  # performs an online CRL/OCSP revocation check by default and HARD-fails
  # the handshake (CRYPT_E_NO_REVOCATION_CHECK) when the certificate offers
  # neither — exactly the case for our internal cert-manager-issued chain.
  # A real browser soft-fails this same missing-revocation-info case and
  # loads the page normally, so this flag only closes that curl.exe/browser
  # trust-check gap — it does NOT skip certificate, hostname, or chain
  # validation the way -k/--insecure would, and BOOT-03's no-bypass
  # contract is unaffected.
  ps_cmd='$code = & curl.exe -s -o NUL -w "%{http_code}" --ssl-no-revoke $env:INDRAMESH_PROBE_URL; if ($code -ge 200 -and $code -lt 400) { exit 0 } else { exit 1 }'
  # WSLENV registration required — see trust_store.sh's _trust_import_windows
  # for why an unregistered variable arrives as $null in the Windows process
  # (no /p flag here: INDRAMESH_PROBE_URL is a URL, not a filesystem path).
  local _wslenv="INDRAMESH_PROBE_URL"
  [ -n "${WSLENV:-}" ] && _wslenv="${WSLENV}:${_wslenv}"
  if ! INDRAMESH_PROBE_URL="$url" WSLENV="$_wslenv" powershell.exe -NoProfile -NonInteractive -Command "$ps_cmd" >/dev/null 2>&1; then
    preflight_error "reachability: Windows-side HTTPS probe of $url failed (DNS/TCP/HTTP/status error)"
    return 1
  fi
  return 0
}

_reachability_discover_lb_ip() {  # _reachability_discover_lb_ip — the Traefik
  # LoadBalancer IP the local-forward bridge relays to (gap-closure, D-05). k3s's
  # default ServiceLB (klipper-lb) redirects port 80/443 traffic to Traefik
  # via an iptables PREROUTING DNAT rule targeting its own assigned
  # LoadBalancer IP; PREROUTING never sees host-originated loopback traffic,
  # so 127.0.0.1 is unreachable even from the k3s host itself — confirmed
  # live ("No route to host" on 127.0.0.1:443; a real TLS handshake on the
  # LB IP). Discovered fresh every call, never hardcoded or cached to a
  # file — fail-closed when the LoadBalancer has no IP yet.
  local ip
  ip="$(_kubectl_indramesh get svc traefik -n kube-system -o jsonpath='{.status.loadBalancer.ingress[0].ip}' 2>/dev/null)" || {
    preflight_error "reachability: failed to discover the Traefik LoadBalancer IP for the local-forward bridge (D-05)"
    return 1
  }
  if [ -z "$ip" ]; then
    preflight_error "reachability: Traefik LoadBalancer has no assigned IP yet — refusing to configure the local-forward bridge without one (D-05)"
    return 1
  fi
  printf '%s' "$ip"
}

ensure_local_access() {  # ensure_local_access — reconcile the 127.0.0.1:{80,443} bridge to Traefik
  local lb_ip
  lb_ip="$(_reachability_discover_lb_ip)" || return 1
  reconcile_local_forward "$lb_ip" || return 1
}

verify_endpoint() {  # verify_endpoint <status> <url> [platform] [hostname_override] — full adapter outcome
  local status="${1:-}"
  local url="${2:-}"
  local platform="${3:-}"
  local hostname_override="${4:-}"

  verify_endpoint_contract "$status" "$url" || return 1
  case "$ENDPOINT_OUTCOME" in
    "$ENDPOINT_STATUS_DEFERRED")
      step "reachability: endpoint contract deferred — no probe run, endpoint NOT certified (D-23)"
      return 0
      ;;
    "$ENDPOINT_STATUS_READY")
      reachability_hostname "$hostname_override" >/dev/null || return 1
      if [ -z "$platform" ]; then
        if [ "${PLATFORM_WSL:-0}" -eq 1 ]; then
          platform="wsl"
        else
          platform="native"
        fi
      fi
      case "$platform" in
        native)
          if ensure_local_access && probe_native_https "$url"; then
            ENDPOINT_OUTCOME="verified"
            ENDPOINT_DIAGNOSTIC="endpoint $url reachable from the native consumer host"
            return 0
          fi
          ENDPOINT_OUTCOME="failed"
          return 1
          ;;
        wsl)
          if ensure_local_access && probe_windows_https "$url"; then
            ENDPOINT_OUTCOME="verified"
            ENDPOINT_DIAGNOSTIC="endpoint $url reachable from the Windows consumer host"
            return 0
          fi
          ENDPOINT_OUTCOME="failed"
          return 1
          ;;
        *)
          preflight_error "reachability: unknown platform '$platform' — expected native or wsl"
          ENDPOINT_OUTCOME="failed"
          return 1
          ;;
      esac
      ;;
    *)
      preflight_error "reachability: contract outcome '${ENDPOINT_OUTCOME:-}' cannot be dispatched"
      return 1
      ;;
  esac
}