#!/bin/bash
# helm.sh — non-secret Helm release reconciliation for the Phase 254 k3s
# bootstrap (D-02, D-10, D-11, D-12, D-15, D-21; T-254-01..04). Sourceable;
# requires common.sh (step/preflight_error/private_tmp/K3S_KUBECONFIG/
# _jsonpath_escape_key — this is the first place helm.sh calls
# _jsonpath_escape_key(), for adopt_orphaned_release_resources()'s ownership
# reads), artifacts.sh (load_versions/helm_install_decision/
# VERIFIED_HELM_ARCHIVE), and platform.sh (PLATFORM_ARCH).
#
# Reconciles the locked canonical chart deploy/helm/indramesh as release
# `indramesh` (D-21) with `helm upgrade --install`, --wait, --wait-for-jobs, and
# a bounded timeout (D-02, D-15). Namespace creation/labelling is NOT Helm's
# job: the installer-owned namespace gate (namespace.sh, D-01) creates or
# repairs the `indramesh` restricted-PSA namespace before this release is
# invoked, so `--create-namespace` is deliberately absent. It never uses
# secret-bearing `--set` values, unredacted dry-runs, or destructive verbs
# (uninstall/reset/delete/PVC) — T-254-02, T-254-04.
#
# Helm compatibility threshold (research Assumption A3, closed here): an
# existing `helm version` whose major equals the pinned versions.env major
# (currently major 4) is compatible and retained as-is; any other existing
# major, or a missing/unparseable binary, is treated as incompatible and
# replaced with the verified pinned archive (mirrors D-11's k3s policy applied
# to Helm's major boundary).
#
# After reconciliation helm.sh reads and validates the chart's non-secret
# bootstrap.endpoint.status/url contract and exposes them as
# HELM_ENDPOINT_STATUS/HELM_ENDPOINT_URL for the installer -> reachability
# adapter handoff. Any status other than `deferred` or `ready`, or a `ready`
# contract without a valid HTTPS URL, is rejected (T-254-02, T-254-03).
set -euo pipefail

HELM_UPGRADE_TIMEOUT="${HELM_UPGRADE_TIMEOUT:-10m}"
HELM_NAMESPACE="indramesh"

# Non-secret endpoint handoff globals, set by read_endpoint_contract.
HELM_ENDPOINT_STATUS=""
HELM_ENDPOINT_URL=""

# Non-secret apps.enabled toggle value files (D-16), written by
# write_app_toggle_values and consumed by reconcile_helm_release. They carry no
# secret material (T-257-02) and live inside the installer's private TMP_DIR.
APPS_DISABLED_VALUES_FILE=""
APPS_ENABLED_VALUES_FILE=""

helm_installed_version() {  # helm_installed_version [bin] — echo "" or a parseable version
  local bin="${1:-helm}"
  local out ver
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo ""
    return 0
  fi
  out="$("$bin" --version 2>/dev/null || true)"
  ver="$(printf '%s\n' "$out" | sed -nE 's/.*\b(v?[0-9]+\.[0-9]+\.[0-9]+).*/\1/p' | head -n1)"
  echo "$ver"
}

_extract_verified_helm() {  # _extract_verified_helm <archive> <tmpdir> <arch> — echo extracted helm binary
  local archive="$1" tmpdir="$2" arch="$3"
  local out
  [ -f "$archive" ] || { preflight_error "verified Helm archive missing: $archive (T-254-01)"; return 1; }
  out="$tmpdir/helm-extract"
  mkdir -p "$out"
  tar -xzf "$archive" -C "$out" || {
    preflight_error "failed to extract the verified Helm archive (T-254-01)"
    return 1
  }
  echo "$out/linux-${arch}/helm"
}

helm_binary_path() {  # helm_binary_path <arch> <verified_archive> <tmpdir> — echo the Helm binary to use
  local arch="$1" archive="$2" tmpdir="$3"
  local installed decision extracted
  load_versions
  installed="$(helm_installed_version)"
  decision="$(helm_install_decision "$installed")"
  if [ "$decision" = "keep" ]; then
    step "helm: existing compatible ${installed:-helm} retained (D-11)" >&2
    echo "helm"
    return 0
  fi
  step "helm: replacing missing/incompatible Helm with the verified pinned archive (D-11)" >&2
  extracted="$(_extract_verified_helm "$archive" "$tmpdir" "$arch")" || return 1
  [ -x "$extracted" ] || { preflight_error "extracted Helm binary is not executable: $extracted"; return 1; }
  echo "$extracted"
}

# The one chart RBAC resource that can predate a real Helm release (Phase 255
# created serviceaccount indramesh-backend outside Helm; the system-logs
# Role/RoleBinding were deleted by Phase 305/LOGX-05, so they are no longer
# adoptable) — and therefore the only resource this codebase ever adopts.
_HELM_ADOPTABLE_RESOURCES=("serviceaccount indramesh-backend")

adopt_orphaned_release_resources() {  # adopt_orphaned_release_resources — repair ownership metadata on any
  # pre-existing chart RBAC resource that lacks it, BEFORE helm upgrade
  # --install runs (G-255-2; T-255-06-02). A resource that does not exist yet
  # is skipped (Helm creates it fresh); a resource that already carries the
  # exact correct app.kubernetes.io/managed-by label AND both meta.helm.sh
  # annotations is skipped (idempotent no-op — no annotate/label calls for
  # it). Fails closed (D-04): a kubectl annotate/label error returns 1
  # instead of silently continuing to helm upgrade --install. The initial
  # existence check also fails closed: only a recognizable NotFound error
  # is treated as "absent" (skip); any other kubectl get error (transient
  # API-server hiccup, expired kubeconfig, RBAC glitch) returns 1 instead
  # of being silently treated as "nothing to adopt" (G-255-2).
  local rel_name_key rel_ns_key entry kind name managed rel_name rel_ns get_err
  rel_name_key="$(_jsonpath_escape_key 'meta.helm.sh/release-name')"
  rel_ns_key="$(_jsonpath_escape_key 'meta.helm.sh/release-namespace')"
  for entry in "${_HELM_ADOPTABLE_RESOURCES[@]}"; do
    kind="${entry%% *}"
    name="${entry##* }"
    if ! get_err="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get "$kind" "$name" -n "$HELM_NAMESPACE" 2>&1 >/dev/null)"; then
      case "$get_err" in
        *NotFound*|*"not found"*) continue ;;  # resource genuinely absent — Helm creates it fresh
        *)
          preflight_error "failed to check existing ownership metadata for $kind/$name (G-255-2): ${get_err:-kubectl get failed with no output}"
          return 1
          ;;
      esac
    fi
    managed="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get "$kind" "$name" -n "$HELM_NAMESPACE" -o jsonpath='{.metadata.labels.app\.kubernetes\.io/managed-by}' 2>/dev/null || true)"
    rel_name="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get "$kind" "$name" -n "$HELM_NAMESPACE" -o "jsonpath={.metadata.annotations['$rel_name_key']}" 2>/dev/null || true)"
    rel_ns="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get "$kind" "$name" -n "$HELM_NAMESPACE" -o "jsonpath={.metadata.annotations['$rel_ns_key']}" 2>/dev/null || true)"
    if [ "$managed" = "Helm" ] && [ "$rel_name" = "$HELM_RELEASE_NAME" ] && [ "$rel_ns" = "$HELM_NAMESPACE" ]; then
      step "helm: $kind/$name already carries correct ownership metadata — nothing to adopt"
      continue
    fi
    step "helm: adopting orphaned $kind/$name — repairing ownership metadata (G-255-2)"
    sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl annotate "$kind" "$name" -n "$HELM_NAMESPACE" --overwrite \
      "meta.helm.sh/release-name=$HELM_RELEASE_NAME" "meta.helm.sh/release-namespace=$HELM_NAMESPACE" >/dev/null \
      || { preflight_error "failed to annotate $kind/$name with ownership metadata (G-255-2)"; return 1; }
    sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl label "$kind" "$name" -n "$HELM_NAMESPACE" --overwrite \
      "app.kubernetes.io/managed-by=Helm" >/dev/null \
      || { preflight_error "failed to label $kind/$name with ownership metadata (G-255-2)"; return 1; }
  done
  return 0
}

_component_switch_enabled() {  # _component_switch_enabled <raw switch value> — echo `true` only for the exact string `true`
  # Phase 311.1 D-06: a component switch enables its component on the exact
  # string `true` alone — the same strict-literal convention OTLP_TLS_INSECURE
  # uses. Any other value, including empty, is off. Both values writers below go
  # through this, so the values file the installer hands Helm always agrees with
  # the gate that decided whether the component was provisioned.
  local raw="${1:-}"
  if [ "$raw" = "true" ]; then echo "true"; else echo "false"; fi
}

write_app_toggle_values() {  # write_app_toggle_values <tmpdir> — write the two private non-secret
  # apps.enabled values files (D-16). apps-disabled.yaml drives the stateful-only
  # bootstrap pass (no app resource or migration hook renders); apps-enabled.yaml
  # drives the full release pass. Both are non-secret (T-257-02) and live in the
  # installer's private workspace. Sets APPS_DISABLED_VALUES_FILE and
  # APPS_ENABLED_VALUES_FILE for reconcile_helm_release.
  #
  # Phase 311.1 D-06: apps-disabled.yaml also carries this run's monitoring and
  # Headlamp switches, read from the installer's exported MONITORING_ENABLED/
  # HEADLAMP_ENABLED (default `false` — the lean quickstart). Without them the
  # stateful-only pass would inherit the chart's own `headlamp.enabled: true`
  # default and create a Headlamp Deployment whose imagePullPolicy: Never image
  # was never imported (the switch is off), leaving an ErrImageNeverPull pod
  # until the full pass prunes it (research Pitfall 4).
  local tmpdir="$1"
  local mon hl lf
  [ -n "$tmpdir" ] || { preflight_error "write_app_toggle_values: private temporary directory is required (TMP_DIR)"; return 1; }
  [ -d "$tmpdir" ] || { preflight_error "write_app_toggle_values: private temporary directory missing: $tmpdir"; return 1; }
  mon="$(_component_switch_enabled "${MONITORING_ENABLED:-false}")"
  hl="$(_component_switch_enabled "${HEADLAMP_ENABLED:-false}")"
  lf="$(_component_switch_enabled "${LOGINFRA_ENABLED:-false}")"
  APPS_DISABLED_VALUES_FILE="$tmpdir/apps-disabled.yaml"
  APPS_ENABLED_VALUES_FILE="$tmpdir/apps-enabled.yaml"
  {
    printf 'apps:\n  enabled: false\n'
    printf 'monitoring:\n  enabled: %s\n' "$mon"
    printf 'headlamp:\n  enabled: %s\n' "$hl"
  } > "$APPS_DISABLED_VALUES_FILE"
  printf 'apps:\n  enabled: true\n' > "$APPS_ENABLED_VALUES_FILE"
  # Phase 318 (host-B clean-host finding): the backend's LOKI_QUERY_URL has no
  # hard-coded chart default any more. A lean install (LOGINFRA_ENABLED=false)
  # carries no Loki, so the log-explorer plugin must answer `not_configured`
  # instead of probing loki.monitoring.svc.cluster.local and reporting
  # `unreachable` on a fresh quickstart. Only a run that actually provisions the
  # log stack writes the bundled query base; an external Loki target is the
  # operator's own values-file entry.
  if [ "$lf" = "true" ]; then
    printf '  backend:\n    lokiQueryUrl: http://loki.monitoring.svc.cluster.local:3100\n' >> "$APPS_ENABLED_VALUES_FILE"
  fi
  return 0
}

_chart_networkpolicies_enabled() {  # _chart_networkpolicies_enabled <values_file> —
  # echo the networkPolicies.enabled value; absent section or absent key
  # defaults to true (D-10). Drives install.sh's fail-closed apiServerCidr
  # discovery gate (Plan 03 decision 2).
  local file="$1"
  awk '
    BEGIN { netpol = -1; found = "" }
    {
      content = $0
      sub(/^[[:space:]]*/, "", content)
      if (content == "") next
      indent = length($0) - length(content)
      if (netpol >= 0 && indent <= netpol) { netpol = -2 }
      if (netpol == -1 && content ~ /^networkPolicies:[[:space:]]*$/) { netpol = indent; next }
      if (netpol >= 0 && content ~ /^enabled:[[:space:]]*/) {
        val = content
        sub(/^enabled:[[:space:]]*/, "", val)
        gsub(/"/, "", val)
        found = val
        exit
      }
    }
    END { if (found == "") print "true"; else print found }
  ' "$file"
}

_chart_headlamp_enabled() {  # _chart_headlamp_enabled <values_file> —
  # echo the headlamp.enabled value; absent section or absent key defaults to
  # true (D-03: default ON, consistent with monitoring/backup). Drives
  # install.sh's fail-closed headlamp image acquisition gate (Plan 262-02).
  local file="$1"
  awk '
    BEGIN { hl = -1; found = "" }
    {
      content = $0
      sub(/^[[:space:]]*/, "", content)
      if (content == "") next
      indent = length($0) - length(content)
      if (hl >= 0 && indent <= hl) { hl = -2 }
      if (hl == -1 && content ~ /^headlamp:[[:space:]]*$/) { hl = indent; next }
      if (hl >= 0 && content ~ /^enabled:[[:space:]]*/) {
        val = content
        sub(/^enabled:[[:space:]]*/, "", val)
        gsub(/"/, "", val)
        found = val
        exit
      }
    }
    END { if (found == "") print "true"; else print found }
  ' "$file"
}

_chart_image_field() {  # _chart_image_field <values_file> <section> <field> —
  # echo `<section>.image.<field>` from the chart values (e.g.
  # `_chart_image_field "$CHART_DIR/values.yaml" postgres tag` -> 18.4-age1.8.0-pgv0.8.7).
  # Absent section, absent image block or absent field echoes nothing, and every
  # caller fails closed on an empty result: the installer must acquire exactly
  # the ref the stateful pods will reference (pullPolicy IfNotPresent), and
  # reading that ref out of the chart is what removes the installer/chart
  # disagreement class — the Phase 311.1 clean-host finding was a chart pinned to
  # a wrapper image that no install-path step ever built or imported.
  local file="$1" section="$2" field="$3"
  awk -v section="$section" -v field="$field" '
    BEGIN { sec = -1; img = -1; found = "" }
    {
      content = $0
      sub(/^[[:space:]]*/, "", content)
      if (content == "") next
      indent = length($0) - length(content)
      if (sec >= 0 && indent <= sec) { sec = -2; img = -2 }
      if (img >= 0 && indent <= img) { img = -2 }
      if (sec == -1 && content == (section ":")) { sec = indent; next }
      if (sec >= 0 && img == -1 && content == "image:") { img = indent; next }
      if (img >= 0 && content ~ ("^" field ":[[:space:]]*")) {
        val = content
        sub(("^" field ":[[:space:]]*"), "", val)
        gsub(/"/, "", val)
        found = val
        exit
      }
    }
    END { if (found != "") print found }
  ' "$file"
}

FULL_PASS_BOOT_VALUES_FILE=""

write_full_pass_boot_values() {  # write_full_pass_boot_values <tmpdir> <api_server_cidr> <repo=tag...> —
  # write the private full-pass boot values file (259-04 decision 2): the
  # freshly acquired image tags per chart key (indramesh-backend → apps.backend,
  # indramesh-frontend-static → apps.frontend, indramesh-nginx → apps.nginx),
  # apps.nginx.enabled: true, and the installer-discovered
  # networkPolicies.apiServerCidr. Phase 311.1 D-06 adds the run's
  # monitoring.enabled and headlamp.enabled — read from the installer's exported
  # MONITORING_ENABLED/HEADLAMP_ENABLED (default `false`, the lean quickstart) —
  # so the right-most values file carries the same switch the component gate
  # honoured: the chart then renders no monitoring.coreos.com object and no
  # Headlamp Deployment/RBAC when the component is off, while a switched-on run
  # still gets both. Non-secret, lives in the installer's private workspace;
  # consumed as the right-most trailing --values file of
  # reconcile_helm_release_full_pass. Sets FULL_PASS_BOOT_VALUES_FILE.
  local tmpdir="$1" api_cidr="$2"
  shift 2
  local out="$tmpdir/full-pass-boot.yaml"
  local spec repo tag key mon hl
  [ -n "$tmpdir" ] || { preflight_error "write_full_pass_boot_values: private temporary directory is required (TMP_DIR)"; return 1; }
  [ -d "$tmpdir" ] || { preflight_error "write_full_pass_boot_values: private temporary directory missing: $tmpdir"; return 1; }
  mon="$(_component_switch_enabled "${MONITORING_ENABLED:-false}")"
  hl="$(_component_switch_enabled "${HEADLAMP_ENABLED:-false}")"
  printf 'apps:\n' > "$out"
  # Phase 272/K8SFIX-04: the installer sets the in-cluster backend/worker
  # ENVIRONMENT from the same root .env source the Compose stack uses
  # (APP_ENVIRONMENT, default production) — one variable, both stacks.
  if [ -n "${APP_ENVIRONMENT:-}" ]; then
    printf '  environment: %s\n' "$APP_ENVIRONMENT" >> "$out"
  fi
  for spec in "$@"; do
    repo="${spec%%=*}"
    tag="${spec#*=}"
    case "$repo" in
      indramesh-backend) key="backend" ;;
      indramesh-frontend-static) key="frontend" ;;
      indramesh-nginx) key="nginx" ;;
      *) key="$repo" ;;
    esac
    printf '  %s:\n' "$key" >> "$out"
    [ "$key" = "nginx" ] && printf '    enabled: true\n' >> "$out"
    if [ "$key" = "nginx" ] && [ -n "${HOST_IP:-}" ]; then
      printf '    hostIp: %s\n' "$HOST_IP" >> "$out"
    fi
    printf '    image:\n      tag: %s\n' "$tag" >> "$out"
  done
  if [ -n "$api_cidr" ]; then
    printf 'networkPolicies:\n  apiServerCidr: %s\n' "$api_cidr" >> "$out"
  fi
  # Phase 311.1 D-06: the two component switches, as plain true/false scalars at
  # top level, beside the apps/networkPolicies blocks. No secret material here
  # (T-257-02) — the values are the operator's non-secret .env switches.
  printf 'monitoring:\n  enabled: %s\n' "$mon" >> "$out"
  printf 'headlamp:\n  enabled: %s\n' "$hl" >> "$out"
  # Phase 311.1 clean-host finding: the plugin DB boundary. `plugins.dbUser`
  # gates the PLUGIN_DB_USER/PLUGIN_DB_PASSWORD render in the migration Jobs and
  # in the backend Deployment, and the .env already carries both — Compose's
  # backend/start.sh reads them straight out of the same file, so carrying them
  # over here is what makes the two stacks agree. Emitted ONLY when the password
  # is present: a role name without a password is the documented fatal
  # misconfiguration (docs/CONFIGURATION.md credential matrix), so an install
  # without the credential renders neither variable and keeps the weakened
  # fallback selectable exactly as before. Non-secret: only the role NAME lands
  # in a values file, never the password (T-257-02).
  if [ "${PLUGIN_DB_PASSWORD_PRESENT:-0}" = "1" ] && [ -n "${PLUGIN_DB_USER_VALUE:-}" ]; then
    printf 'plugins:\n  dbUser: %s\n' "$PLUGIN_DB_USER_VALUE" >> "$out"
  fi
  FULL_PASS_BOOT_VALUES_FILE="$out"
  return 0
}

RUNTIME_VALUES_FILE=""

_ipv4_is_private() {  # _ipv4_is_private <ipv4> — 0 for every range publicEgress.except closes
  # Mirrors the chart's networkPolicies.publicEgress.except list: an address in
  # one of these ranges is NOT admitted by the public rule and needs its own
  # externalEgress entry.
  local a b
  IFS=. read -r a b _ _ <<< "$1"
  case "$a" in
    0|10|127) return 0 ;;
    100) [ "$b" -ge 64 ] && [ "$b" -le 127 ] && return 0 ;;
    169) [ "$b" -eq 254 ] && return 0 ;;
    172) [ "$b" -ge 16 ] && [ "$b" -le 31 ] && return 0 ;;
    192) [ "$b" -eq 168 ] && return 0 ;;
  esac
  return 1
}

_is_ipv4() {  # _is_ipv4 <value> — 0 for a dotted-quad with every octet 0-255
  local o
  [[ "$1" =~ ^([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})\.([0-9]{1,3})$ ]] || return 1
  for o in "${BASH_REMATCH[@]:1}"; do
    [ "$o" -le 255 ] || return 1
  done
  return 0
}

_url_host_port() {  # _url_host_port <url> — echo "<host> <port>"; port from the scheme when absent
  local url="$1" scheme="" authority port=""
  case "$url" in
    *://*) scheme="${url%%://*}"; authority="${url#*://}" ;;
    *) return 1 ;;
  esac
  authority="${authority%%/*}"
  authority="${authority##*@}"
  case "$authority" in
    \[*) return 1 ;;  # IPv6 literal — out of the IPv4-only derivation
    *:*) port="${authority##*:}"; authority="${authority%:*}" ;;
  esac
  if [ -z "$port" ]; then
    case "$scheme" in
      https) port=443 ;;
      http) port=80 ;;
      *) return 1 ;;
    esac
  fi
  [[ "$port" =~ ^[0-9]+$ ]] || return 1
  [ -n "$authority" ] || return 1
  printf '%s %s' "$authority" "$port"
}

derive_external_egress() {  # derive_external_egress <allow_list> <llm_base_url> — echo "<cidr> <port>" lines
  # Phase 318 (K8S-LLM-EGRESS-01): the networkPolicies.externalEgress entries
  # for this run. <allow_list> is the operator's K8S_EGRESS_ALLOW (comma or
  # space separated `IP[/len]:port` entries — an on-prem Jira, a LAN model
  # server); a malformed entry fails the run naming the entry. <llm_base_url>
  # is the resolved LLM endpoint: a host on a private address, or on a public
  # address outside port 443, gets its own /32 entry because the chart's
  # public 443 rule does not admit it. A host this machine cannot resolve (an
  # in-cluster Service name, say) is reported, never guessed.
  local allow="$1" llm_url="$2" entry cidr port hp host addrs ip
  for entry in ${allow//,/ }; do
    cidr="${entry%:*}"
    port="${entry##*:}"
    [ "$cidr" != "$entry" ] || { preflight_error "K8S_EGRESS_ALLOW entry '$entry' has no port — use IP[/len]:port (e.g. 10.0.5.20/32:8080)"; return 1; }
    case "$cidr" in */*) ;; *) cidr="$cidr/32" ;; esac
    if ! _is_ipv4 "${cidr%/*}" || ! [[ "${cidr#*/}" =~ ^[0-9]{1,2}$ ]] || [ "${cidr#*/}" -gt 32 ]; then
      preflight_error "K8S_EGRESS_ALLOW entry '$entry' is not an IPv4 address or range — a NetworkPolicy cannot name a hostname"
      return 1
    fi
    if ! [[ "$port" =~ ^[0-9]+$ ]] || [ "$port" -lt 1 ] || [ "$port" -gt 65535 ]; then
      preflight_error "K8S_EGRESS_ALLOW entry '$entry' has an invalid port"
      return 1
    fi
    printf '%s %s\n' "$cidr" "$port"
  done
  [ -n "$llm_url" ] || return 0
  hp="$(_url_host_port "$llm_url")" || {
    step "egress: cannot derive host and port from LLM_BASE_URL '$llm_url' — add the endpoint to K8S_EGRESS_ALLOW if it is not public on 443" >&2
    return 0
  }
  host="${hp% *}"
  port="${hp#* }"
  if _is_ipv4 "$host"; then
    addrs="$host"
  else
    addrs="$(getent ahostsv4 "$host" 2>/dev/null | awk '{print $1}' | sort -u || true)"
  fi
  if [ -z "$addrs" ]; then
    step "egress: LLM host '$host' does not resolve on this machine — if it is not public on 443, add it to K8S_EGRESS_ALLOW" >&2
    return 0
  fi
  for ip in $addrs; do
    if _ipv4_is_private "$ip"; then
      printf '%s/32 %s\n' "$ip" "$port"
    elif [ "$port" != "443" ]; then
      step "egress: LLM host '$host' is public on port $port — admitting its current address $ip; a provider that changes addresses needs K8S_EGRESS_ALLOW" >&2
      printf '%s/32 %s\n' "$ip" "$port"
    fi
  done
  return 0
}

write_runtime_values() {  # write_runtime_values <tmpdir> <external_api_revision> — the .env-derived runtime contract
  # Phase 318 (K8S-LLM-EGRESS-01, K8S-CONFIG-ROLLOUT-01): the private,
  # non-secret values file carrying what the .env decides at runtime on
  # Compose and the chart otherwise pins:
  #   - apps.externalApiRevision: the indramesh-external-api Secret's
  #     uid/resourceVersion, so a created or rotated LLM key rolls
  #     backend/worker on this pass;
  #   - apps.backend.llm / apps.worker.llm from LLM_PROVIDER/LLM_BASE_URL/
  #     LLM_MODEL/LLM_MODE, written only when the .env sets any of them (an
  #     empty baseUrl/mode keeps the provider preset, as on Compose; an empty
  #     model keeps the chart default);
  #   - networkPolicies.externalEgress from derive_external_egress.
  # Reads LLM_*_VALUE and K8S_EGRESS_ALLOW_VALUE exported by install.sh.
  # Sets RUNTIME_VALUES_FILE.
  local tmpdir="$1" revision="$2"
  local out="$tmpdir/runtime.yaml" provider base_url entries role cidr port
  [ -n "$tmpdir" ] && [ -d "$tmpdir" ] || { preflight_error "write_runtime_values: private temporary directory missing: ${tmpdir:-<unset>}"; return 1; }
  provider="${LLM_PROVIDER_VALUE:-}"
  base_url="${LLM_BASE_URL_VALUE:-}"
  # Only an explicit LLM_BASE_URL is derived: the provider presets are public
  # endpoints on 443, which publicEgress already admits.
  entries="$(derive_external_egress "${K8S_EGRESS_ALLOW_VALUE:-}" "$base_url")" || return 1
  {
    printf 'apps:\n'
    printf '  externalApiRevision: "%s"\n' "$revision"
    if [ -n "$provider$base_url${LLM_MODEL_VALUE:-}${LLM_MODE_VALUE:-}" ]; then
      for role in backend worker; do
        printf '  %s:\n    llm:\n' "$role"
        printf '      provider: "%s"\n' "${provider:-mistral}"
        printf '      baseUrl: "%s"\n' "$base_url"
        [ -n "${LLM_MODEL_VALUE:-}" ] && printf '      model: "%s"\n' "$LLM_MODEL_VALUE"
        printf '      mode: "%s"\n' "${LLM_MODE_VALUE:-}"
      done
    fi
    if [ -n "$entries" ]; then
      printf 'networkPolicies:\n  externalEgress:\n'
      while read -r cidr port; do
        printf '    - cidr: %s\n      port: %s\n' "$cidr" "$port"
      done <<< "$entries"
    fi
  } > "$out"
  RUNTIME_VALUES_FILE="$out"
  return 0
}

external_api_secret_revision() {  # external_api_secret_revision [namespace] — echo "<uid>/<resourceVersion>" or ""
  # Metadata only (never data, never a digest of it): changes exactly when the
  # Secret is created, replaced or edited, and stays put across reruns that
  # leave it alone — so an unchanged key never restarts a pod.
  local ns="${1:-$HELM_NAMESPACE}" out
  out="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get secret indramesh-external-api -n "$ns" \
    -o 'jsonpath={.metadata.uid}/{.metadata.resourceVersion}' 2>/dev/null || true)"
  [ "$out" = "/" ] && out=""
  printf '%s' "$out"
}

_helm_release_is_absent() {  # _helm_release_is_absent <helm_bin> — classify the named release via
  # `helm status`: 0 = absent (fresh), 1 = present, 2 = operational failure
  # (fails closed, T-257-03). Only a recognizable named-release-not-found status
  # is treated as fresh; any other status error aborts before Helm rather than
  # being mistaken for an absent release (R-02, T-257-03).
  local helm_bin="$1"
  local out rc=0
  out="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" status "$HELM_RELEASE_NAME" -n "$HELM_NAMESPACE" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *"not found"*|*"NotFound"*)
        return 0
        ;;
      *)
        preflight_error "helm status inspection of release '$HELM_RELEASE_NAME' failed — not treating as absent (T-257-03): ${out:-no output}"
        return 2
        ;;
    esac
  fi
  return 1
}

_reconcile_helm_setup() {  # _reconcile_helm_setup <chart> — shared preflight for both passes:
  # chart validation, orphan-resource adoption, and apps.enabled toggle-values
  # materialization (D-16). Populates the module-global profile_args array
  # via the RECONCILE_PROFILE_ARGS nameref-free convention (bash 3.2-safe:
  # callers read the RECONCILE_PROFILE_ARGS array directly after calling).
  local chart="${1:?chart path required}"
  load_versions
  [ -f "$chart/Chart.yaml" ] || { preflight_error "chart metadata missing at $chart (D-21)"; return 1; }
  adopt_orphaned_release_resources || return 1
  [ -n "${TMP_DIR:-}" ] || { preflight_error "private temporary directory is required (TMP_DIR) for the apps toggle (D-16)"; return 1; }
  if [ -z "${APPS_DISABLED_VALUES_FILE:-}" ] || [ -z "${APPS_ENABLED_VALUES_FILE:-}" ]; then
    write_app_toggle_values "$TMP_DIR" || return 1
  fi
  [ -f "$APPS_DISABLED_VALUES_FILE" ] && [ -f "$APPS_ENABLED_VALUES_FILE" ] \
    || { preflight_error "apps.enabled toggle values not materialized (D-16)"; return 1; }
  RECONCILE_PROFILE_ARGS=()
  if [ -n "${STATEFUL_PROFILE_VALUES_FILE:-}" ]; then
    [ -f "$STATEFUL_PROFILE_VALUES_FILE" ] || { preflight_error "stateful profile values file is missing"; return 1; }
    RECONCILE_PROFILE_ARGS+=(--values "$STATEFUL_PROFILE_VALUES_FILE")
  fi
  return 0
}

reconcile_helm_release_stateful_pass() {  # reconcile_helm_release_stateful_pass <chart> [helm_bin] [timeout]
  # D-16 pass 1: on a FRESH release (named-release-absent only) runs a
  # stateful-only `upgrade --install` with apps disabled (no migration hook,
  # no app resource) and returns 0 — the caller MUST then run
  # `fresh_install_stateful_ownership_fixup` (stateful_storage.sh) before the
  # full pass, since this call deliberately omits --wait/--wait-for-jobs: on
  # a truly fresh install Postgres cannot become Ready until its local-path
  # PVC host dir is host-chowned, which cannot happen until this pass has
  # created the PVC (T-256-12 mechanical reason). Waiting here would just
  # block until HELM_UPGRADE_TIMEOUT trips with no chance to fix it first.
  #
  # On an EXISTING release, does nothing and returns 1 — the full pass alone
  # is the correct and sufficient reconciliation. On a hard `helm status`
  # inspection failure, returns 2 (fail-closed, T-257-03) without touching
  # Helm at all.
  local chart="${1:?chart path required}"
  local helm_bin="${2:-helm}"
  local timeout="${3:-$HELM_UPGRADE_TIMEOUT}"
  local release_state
  _reconcile_helm_setup "$chart" || return 2
  _helm_release_is_absent "$helm_bin"
  release_state=$?
  case "$release_state" in
    0)
      step "helm: reconciling release '$HELM_RELEASE_NAME' at $chart — stateful-only bootstrap with apps disabled (D-16)"
      # Pass 1: bounded-timeout stateful-only install, no --wait/--wait-for-jobs
      # (see function comment above) — the upgrade --install contract creates
      # the release and its PVCs and returns as soon as the API accepts them.
      sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" upgrade --install "$HELM_RELEASE_NAME" "$chart" \
        --namespace "$HELM_NAMESPACE" \
        --timeout "$timeout" \
        --values "$APPS_DISABLED_VALUES_FILE" "${RECONCILE_PROFILE_ARGS[@]}" || return 2
      return 0
      ;;
    1) return 1 ;;
    *) return 2 ;;
  esac
}

reconcile_helm_release_full_pass() {  # reconcile_helm_release_full_pass <chart> [helm_bin] [timeout] [trailing_values_file...]
  # D-16 pass 2 (fresh) / sole pass (existing): --reset-values forces the
  # chart back to apps.enabled=true even if a stored value disabled it
  # (T-257-02); no --install on an existing release. Bounded --wait
  # --wait-for-jobs is the sole readiness gate for the whole release
  # (stateful tier, pre-upgrade migration hook, and app tier).
  #
  # The optional 4th and later arguments (Plan 258-03, D-25; WR-01) are
  # private, non-secret values files appended in order as --values arguments,
  # the LAST one being the RIGHT-MOST — Helm gives priority to the last
  # --values file, so this is how the rebuild wrapper's fresh image tags win
  # over both the chart defaults --reset-values just restored and every
  # earlier --values file in this same command. Passing the deployed-values
  # contract BEFORE the image-tags file lets Helm deep-merge them: the
  # deployed contract re-asserts every non-image operator override (stateful
  # resource profile, nginxTrustedIp, corsAllowedOrigins, apps.*.resources,
  # apps.*.wait) while the right-most image-tags file still moves the tags
  # (WR-01). With no 4th argument the emitted argv is byte-identical to
  # before this change (install.sh passes 2 args; the back-compat
  # reconcile_helm_release wrapper passes 3; neither is affected).
  local chart="${1:?chart path required}"
  local helm_bin="${2:-helm}"
  local timeout="${3:-$HELM_UPGRADE_TIMEOUT}"
  local trailing_args=()
  local f
  local i
  # Positional args 4+ are all trailing values files, appended in order as
  # --values arguments (the LAST one stays right-most). Indexed loop (not
  # `shift 3`): callers legitimately pass fewer than three args (install.sh
  # passes 2, the back-compat wrapper 3), and `shift 3` would trip set -e
  # with "shift count out of range" on those short argv lists.
  for (( i = 4; i <= $#; i++ )); do
    f="${!i}"
    [ -n "$f" ] || continue
    [ -r "$f" ] || { preflight_error "trailing values file is not readable: $f (D-25)"; return 1; }
    trailing_args+=(--values "$f")
  done
  _reconcile_helm_setup "$chart" || return 1
  step "helm: reconciling release '$HELM_RELEASE_NAME' at $chart — full upgrade with apps enabled (D-16)"
  sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" upgrade --reset-values "$HELM_RELEASE_NAME" "$chart" \
    --namespace "$HELM_NAMESPACE" \
    --wait --wait-for-jobs \
    --timeout "$timeout" \
    --values "$APPS_ENABLED_VALUES_FILE" "${RECONCILE_PROFILE_ARGS[@]}" "${trailing_args[@]}"
}

reconcile_helm_release() {  # reconcile_helm_release <chart_path> [helm_bin] [timeout]
  # Back-compat single entry point: runs the stateful pass followed
  # unconditionally by the full pass, EXCEPT that a fresh-install stateful
  # pass (return 0) is followed by the D-16 ownership fixup before the full
  # pass — kept for callers that don't need to interleave anything between
  # the two passes (e.g. tests/bootstrap/test_namespace_secrets.sh). Callers
  # that DO need the fixup interleaved (scripts/k8s/install.sh) call the two
  # pass functions directly instead of this wrapper.
  local chart="$1" helm_bin="${2:-helm}" timeout="${3:-$HELM_UPGRADE_TIMEOUT}"
  local rc
  # `if ...; then rc=0; else rc=$?; fi` (not a bare statement + `rc=$?`) is
  # deliberate: this function's own `set -euo pipefail` (module-level) would
  # otherwise abort the caller the instant reconcile_helm_release_stateful_pass
  # returns 1 (existing release) or 2 (hard failure) as a bare statement,
  # before `rc=$?` ever runs — bash only suspends errexit for a function call
  # whose exit status is itself being tested (if/&&/||), not for a plain
  # "call it, then read $?" pair (confirmed live, 2026-09-01).
  if reconcile_helm_release_stateful_pass "$chart" "$helm_bin" "$timeout"; then
    rc=0
  else
    rc=$?
  fi
  case "$rc" in
    0) fresh_install_stateful_ownership_fixup || return 1 ;;
    1) ;;
    *) return 1 ;;
  esac
  reconcile_helm_release_full_pass "$chart" "$helm_bin" "$timeout"
}

_endpoint_contract_field() {  # _endpoint_contract_field <values_file> <field> — echo bootstrap.endpoint.<field> (status|url)
  # Indentation-aware, scoped strictly to the bootstrap: -> endpoint: block
  # (mirrors wsl_conf_has_systemd's section-tracking in platform.sh), so a
  # future top-level or nested status:/url: key elsewhere in values.yaml
  # (Phase 257/259 workloads, Secrets, Ingress) can never be picked up
  # instead of the real endpoint contract.
  local file="$1" field="$2"
  awk -v field="$field" '
    {
      content = $0
      sub(/^[[:space:]]*/, "", content)
      if (content == "") next
      indent = length($0) - length(content)

      if (endpoint_indent >= 0 && indent <= endpoint_indent) { endpoint_indent = -1 }
      if (bootstrap_indent >= 0 && indent <= bootstrap_indent) { bootstrap_indent = -1; endpoint_indent = -1 }

      if (bootstrap_indent < 0 && content ~ /^bootstrap:[[:space:]]*$/) { bootstrap_indent = indent; next }
      if (bootstrap_indent >= 0 && endpoint_indent < 0 && content ~ /^endpoint:[[:space:]]*$/) { endpoint_indent = indent; next }

      if (endpoint_indent >= 0 && content ~ ("^" field ":[[:space:]]*")) {
        val = content
        sub("^" field ":[[:space:]]*", "", val)
        gsub(/"/, "", val)
        print val
        exit
      }
    }
  ' "$file"
}

read_endpoint_contract() {  # read_endpoint_contract <values_file> — validate + set HELM_ENDPOINT_STATUS/URL
  local values="${1:?values.yaml path required}"
  local status url
  [ -f "$values" ] || { preflight_error "chart values file missing: $values"; return 1; }
  status="$(_endpoint_contract_field "$values" status)"
  url="$(_endpoint_contract_field "$values" url)"
  HELM_ENDPOINT_STATUS=""
  HELM_ENDPOINT_URL=""
  case "$status" in
    deferred)
      [ -z "$url" ] || { preflight_error "bootstrap.endpoint.status=deferred must declare an empty bootstrap.endpoint.url (T-254-02)"; return 1; }
      ;;
    ready)
      [ -n "$url" ] || { preflight_error "bootstrap.endpoint.status=ready requires a valid HTTPS bootstrap.endpoint.url (D-08)"; return 1; }
      case "$url" in
        https://*) ;;
        *) preflight_error "bootstrap.endpoint.url '$url' is not an https URL (T-254-03)"; return 1 ;;
      esac
      ;;
    *)
      preflight_error "unknown bootstrap.endpoint.status '$status' — expected 'deferred' or 'ready'"
      return 1
      ;;
  esac
  HELM_ENDPOINT_STATUS="$status"
  HELM_ENDPOINT_URL="$url"
  return 0
}
