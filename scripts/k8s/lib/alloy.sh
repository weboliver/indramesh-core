#!/bin/bash
# alloy.sh — installer-owned Grafana Alloy provisioning for the Phase 301 log
# stack (LOGINFRA-03, LOGINFRA-04; D-10, D-11, D-12; T-301-11..13, T-301-SC).
# Sourceable; requires common.sh (step/preflight_error/K3S_KUBECONFIG),
# artifacts.sh (load_versions/_verify_one), helm.sh (helm_binary_path), and
# secrets.sh (_kubectl_indramesh/dotenv_value). Never self-sources — k3s.sh:1-18
# convention.
#
# provision_alloy_stack reconciles the PINNED Alloy chart on every installer run
# (D-11). The chart is UNSIGNED (no .prov / OCI provenance), so the verified
# path is a SHA-256-pinned local tgz: download → `_verify_one` digest gate →
# `helm upgrade --install <local-tgz>` through the verified Helm binary seam
# (helm_binary_path). There is deliberately no `--verify`, no `--version` and
# no moving tag — the pinned tgz IS the version (D-10/D-11). Before Helm, the
# exact D-10 namespaced RBAC (scripts/k8s/alloy-rbac.yaml: Role + RoleBinding
# for SA alloy/monitoring in the indramesh namespace) is applied, so the Alloy
# pod is authorized the moment it starts. The release is a 1-replica non-root
# Deployment tailing pod logs through the Kubernetes API (never hostPath).
#
# The runnable config is assembled from the SAME checked-in sink fragments as
# Compose (D-02 mirror): a private values file sets alloy.configMap.content =
# cat(base-kubernetes.alloy, sinks/${ALLOY_SINK:-loki}.alloy) plus alloy.extraEnv
# (LOG_ENV/LOKI_URL/OTLP_*) and the D-12 paired NetPolicy egress rules. Both the
# apiserver ClusterIP (/32, OQ3) and — Phase 318 — an additional PORT-ONLY rule
# (TCP 443 and 6443, no `to`): kube-router evaluates egress after DNAT, so the
# ClusterIP rule alone leaves Alloy's discovery blind and the log stack collects
# nothing while still reporting healthy. Port-only keeps it correct across WSL2
# node-IP changes and multi-control-plane endpoints. An external OTLP sink adds
# exactly one egress rule with the port derived from OTLP_ENDPOINT (OQ4).
set -euo pipefail

ALLOY_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$ALLOY_SH_DIR/../../.." && pwd)"

ALLOY_NAMESPACE="monitoring"
ALLOY_RELEASE_NAME="alloy"
# Bounded readiness window for the single Deployment (D-10): total budget =
# attempts * interval seconds. Env-overridable for hermetic tests.
ALLOY_READY_ATTEMPTS="${ALLOY_READY_ATTEMPTS:-60}"
ALLOY_READY_INTERVAL="${ALLOY_READY_INTERVAL:-2}"
# The in-cluster bundled-Loki push endpoint (same contract as Compose's LOKI_URL
# default). Overridable via LOKI_URL from the root .env.
ALLOY_DEFAULT_LOKI_URL="http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"

_alloy_release_status() {  # _alloy_release_status <helm_bin> — echo the Helm release status
  # word (deployed|pending-install|pending-upgrade|pending-rollback|failed|
  # uninstalling|absent), or 'unknown' on any classification failure. Used to
  # distinguish a healthy deployed release from an interrupted pending/failed
  # one that must be adopted by re-running the pinned install. Fails closed:
  # any error (bad kubeconfig, helm failure) yields 'unknown' so the caller
  # refuses to install blindly (D-11).
  local helm_bin="$1" out rc=0 status=""
  out="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" status "$ALLOY_RELEASE_NAME" -n "$ALLOY_NAMESPACE" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *"Error: release: not found"*|*"not found"*) echo "absent" ;;
      *) preflight_error "alloy: helm status '$ALLOY_RELEASE_NAME' failed — cannot classify release health (D-11): ${out:-no output}"; echo "unknown" ;;
    esac
    return
  fi
  status="$(printf '%s\n' "$out" | sed -n 's/^STATUS: //p' | tr -d ' ')"
  case "$status" in
    deployed|pending-install|pending-upgrade|pending-rollback|failed|uninstalling) echo "$status" ;;
    *) preflight_error "alloy: unexpected Helm release status '$status' for '$ALLOY_RELEASE_NAME' (D-11)"; echo "unknown" ;;
  esac
}

_wait_for_alloy_ready() {  # _wait_for_alloy_ready — bounded readiness wait for the alloy
  # Deployment in the monitoring namespace (D-10). The Deployment is named
  # `alloy` (release name == chart name -> alloy.fullname == alloy; verified
  # via helm template against the pinned chart). Any timeout fails closed
  # before the installer claims the log stack is up.
  local i ready
  for i in $(seq 1 "$ALLOY_READY_ATTEMPTS"); do
    ready="$(_kubectl_indramesh get deployment "$ALLOY_RELEASE_NAME" -n "$ALLOY_NAMESPACE" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    if [ "$ready" = "1" ]; then
      step "alloy: Deployment Ready"
      return 0
    fi
    sleep "$ALLOY_READY_INTERVAL"
  done
  preflight_error "alloy: Deployment not Ready within the bounded window ($ALLOY_READY_ATTEMPTS attempts) — refusing to proceed (D-10)"
  return 1
}

_alloy_sink_file() {  # _alloy_sink_file <sink> — echo the checked-in fragment path (D-02)
  local sink="${1:-loki}" f
  case "$sink" in
    loki|otlp|otlphttp|none) ;;
    *)
      preflight_error "alloy: unknown ALLOY_SINK '$sink' — expected loki|otlp|otlphttp|none (D-02)"
      return 1
      ;;
  esac
  f="$ROOT_DIR/observability/alloy/sinks/$sink.alloy"
  [ -f "$f" ] || { preflight_error "alloy: sink fragment missing: $f (D-02)"; return 1; }
  printf '%s' "$f"
}

_alloy_assemble_content() {  # _alloy_assemble_content — echo base + selected sink, verbatim cat
  # The fragment files are concatenated unchanged (no templating, no sed on
  # their CONTENT); only the block-scalar indentation is applied by the caller.
  # The chart runs alloy.configMap.content through `tpl`, so no fragment may
  # contain `{{` (guarded by the test suite).
  local sink_file
  sink_file="$(_alloy_sink_file "${ALLOY_SINK:-loki}")" || return 1
  cat "$ROOT_DIR/observability/alloy/base-kubernetes.alloy" "$sink_file" || return 1
}

_alloy_yaml_dq() {  # _alloy_yaml_dq <value> — a YAML double-quoted scalar (escape \ and ")
  local s="$1"
  s="${s//\\/\\\\}"
  s="${s//\"/\\\"}"
  printf '"%s"' "$s"
}

_alloy_env_entry() {  # _alloy_env_entry <name> <value> — one alloy.extraEnv list item
  printf '    - name: %s\n' "$1"
  printf '      value: %s\n' "$(_alloy_yaml_dq "${2:-}")"
}

_alloy_apiserver_cidr() {  # _alloy_apiserver_cidr — discover default/kubernetes ClusterIP as /32 (OQ3)
  # Mirrors the indramesh chart's installer-discovered networkPolicies.
  # apiServerCidr contract; fail-closed on discovery error or empty output.
  local ip
  ip="$(_kubectl_indramesh get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null)" || {
    preflight_error "alloy: failed to discover the kube-apiserver ClusterIP for the networkPolicies.apiServerCidr-equivalent NetPol egress rule — it cannot be derived (OQ3)"
    return 1
  }
  [ -n "$ip" ] || {
    preflight_error "alloy: kube-apiserver ClusterIP discovery returned empty — refusing to proceed without the egress CIDR (OQ3)"
    return 1
  }
  printf '%s/32' "$ip"
}

_alloy_otlp_port() {  # _alloy_otlp_port <endpoint> — the single TCP port for the OTLP egress rule (OQ4)
  # Explicit port wins (scheme://host:port or host:port). Without a port the
  # http/https scheme decides (443/80). Anything else cannot be derived
  # fail-closed before Helm — fixed 4317/4318 would silently block an OTLP
  # intake behind HTTPS/443 (the normal case for SaaS/proxied otlphttp).
  local endpoint="${1:-}" scheme="" authority="" port=""
  [ -n "$endpoint" ] || {
    preflight_error "alloy: OTLP sink selected but OTLP_ENDPOINT is empty — cannot derive the egress port (LOGINFRA-04)"
    return 1
  }
  case "$endpoint" in
    *"://"*) scheme="${endpoint%%://*}"; authority="${endpoint#*://}" ;;
    *) authority="$endpoint" ;;
  esac
  authority="${authority%%/*}"     # drop any /path or ?query
  authority="${authority%%\?*}"
  case "$authority" in
    *:*) port="${authority##*:}" ;;
  esac
  case "$port" in
    ''|*[!0-9]*)
      case "$scheme" in
        https) printf '443'; return 0 ;;
        http) printf '80'; return 0 ;;
        *)
          preflight_error "alloy: cannot derive an OTLP egress port from OTLP_ENDPOINT '$endpoint' (no explicit port and no http/https scheme) — set host:port or a scheme (LOGINFRA-04)"
          return 1
          ;;
      esac
      ;;
    *) printf '%s' "$port"; return 0 ;;
  esac
}

write_alloy_private_values() {  # write_alloy_private_values <out_file> <apiserver_cidr>
  # Assemble the installer-generated private values (non-secret; lives in the
  # private workspace): alloy.configMap.content (base-kubernetes + selected sink,
  # 6-space block scalar), alloy.extraEnv (LOG_ENV/LOKI_URL/OTLP_*), and the
  # D-12 paired networkPolicy.egress rules. Reads ALLOY_SINK, OTLP_ENDPOINT,
  # OTLP_AUTHORIZATION, OTLP_TLS_INSECURE, LOKI_URL and APP_ENVIRONMENT from the
  # installer environment (mirroring the Compose env model, D-02).
  local out="${1:?out file required}"
  local cidr="${2:?apiserver cidr required}"
  local sink="${ALLOY_SINK:-loki}"
  local log_env="${APP_ENVIRONMENT:-production}"
  local loki_url="${LOKI_URL:-$ALLOY_DEFAULT_LOKI_URL}"
  local otlp_endpoint="${OTLP_ENDPOINT:-}"
  local otlp_auth="${OTLP_AUTHORIZATION:-}"
  # Shared default with Compose (Plan 01 `:-false`) and the fragment docs
  # (Plan 02 `== "true"` opt-in): unset/false = TLS in BOTH stacks. Plaintext
  # gRPC is reachable only with the explicit string "true" (D-02 mirror).
  local otlp_insecure="${OTLP_TLS_INSECURE:-false}"
  local content port

  content="$(_alloy_assemble_content)" || return 1
  [ -n "$content" ] || { preflight_error "alloy: assembled config content is empty (D-02)"; return 1; }
  # Resolve the OTLP port BEFORE opening the output file so a non-derivable
  # endpoint fails closed with nothing partially written and no helm call.
  case "$sink" in
    otlp|otlphttp) port="$(_alloy_otlp_port "$otlp_endpoint")" || return 1 ;;
  esac

  {
    printf 'alloy:\n'
    printf '  configMap:\n'
    printf '    content: |-\n'
    printf '%s\n' "$content" | sed 's/^/      /'
    printf '  extraEnv:\n'
    _alloy_env_entry LOG_ENV "$log_env"
    _alloy_env_entry LOKI_URL "$loki_url"
    _alloy_env_entry OTLP_ENDPOINT "$otlp_endpoint"
    _alloy_env_entry OTLP_AUTHORIZATION "$otlp_auth"
    _alloy_env_entry OTLP_TLS_INSECURE "$otlp_insecure"
    # D-12 paired NetworkPolicy egress. kube-dns (both protocols), the discovered
    # kube-apiserver /32, and the bundled Loki. The external OTLP target is a
    # separate conditional clause (OQ4) — never a fixed 4317/4318.
    printf 'networkPolicy:\n'
    printf '  egress:\n'
    printf '    - to:\n'
    printf '        - namespaceSelector:\n'
    printf '            matchLabels:\n'
    printf '              kubernetes.io/metadata.name: kube-system\n'
    printf '          podSelector:\n'
    printf '            matchLabels:\n'
    printf '              k8s-app: kube-dns\n'
    printf '      ports:\n'
    printf '        - protocol: UDP\n'
    printf '          port: 53\n'
    printf '        - protocol: TCP\n'
    printf '          port: 53\n'
    printf '    - to:\n'
    printf '        - ipBlock:\n'
    printf '            cidr: %s\n' "$cidr"
    printf '      ports:\n'
    printf '        - protocol: TCP\n'
    printf '          port: 443\n'
    # Phase 318 (host-B clean-host finding): the rule that actually admits the
    # discovery. kube-router evaluates egress AFTER the kube-proxy DNAT, so the
    # ClusterIP rule above never matches on k3s — Alloy gets zero targets, stays
    # "healthy" without a single error line, and the log stack collects nothing.
    # Deliberately port-only, with no `to`: the API endpoint of a WSL2/k3s node
    # is its eth0 address and changes on every restart (a baked /32 would break
    # silently), and a multi-control-plane cluster has several endpoints. 443
    # covers managed/Service-port setups, 6443 the k3s apiserver itself.
    printf '    - ports:\n'
    printf '        - protocol: TCP\n'
    printf '          port: 443\n'
    printf '        - protocol: TCP\n'
    printf '          port: 6443\n'
    printf '    - to:\n'
    printf '        - namespaceSelector:\n'
    printf '            matchLabels:\n'
    printf '              kubernetes.io/metadata.name: monitoring\n'
    printf '          podSelector:\n'
    printf '            matchLabels:\n'
    printf '              app.kubernetes.io/name: loki\n'
    printf '      ports:\n'
    printf '        - protocol: TCP\n'
    printf '          port: 3100\n'
    case "$sink" in
      otlp|otlphttp)
        printf '    - to:\n'
        printf '        - ipBlock:\n'
        printf '            cidr: 0.0.0.0/0\n'
        printf '      ports:\n'
        printf '        - protocol: TCP\n'
        printf '          port: %s\n' "$port"
        ;;
    esac
  } > "$out" || return 1
  return 0
}

provision_alloy_stack() {  # provision_alloy_stack <versions_file> <checksums_file> <tmp_dir> <platform_arch> <verified_helm_archive>
  # Verified local-tgz install of the pinned Alloy chart (D-11). The RBAC
  # manifest is applied BEFORE any Helm call (D-10), the tgz digest gate runs
  # BEFORE Helm (T-301-12), and the apiserver CIDR + OTLP port derivations fail
  # closed before Helm (OQ3/OQ4).
  local versions_file="${1:?versions file required}"
  local checksums_file="${2:?checksums file required}"
  local tmp_dir="${3:?tmp dir required}"
  local platform_arch="${4:?platform arch required}"
  local verified_helm_archive="${5:?verified helm archive required}"
  local helm_bin values_file private_values cidr release_status

  load_versions "$versions_file" || {
    preflight_error "alloy: failed to load pinned versions from $(basename "$versions_file") (D-11)"
    return 1
  }
  if [ -z "$ALLOY_CHART_VERSION" ] || [ -z "$ALLOY_CHART_URL" ] || [ -z "$ALLOY_CHART_FILE" ]; then
    preflight_error "alloy: ALLOY_CHART_* pins missing from versions.env (D-11)"
    return 1
  fi
  [ -f "$checksums_file" ] || { preflight_error "alloy: checksum manifest missing: $checksums_file (D-11)"; return 1; }
  [ -d "$tmp_dir" ] || { preflight_error "alloy: staging directory missing: $tmp_dir"; return 1; }

  values_file="${ALLOY_VALUES_FILE:-$ROOT_DIR/scripts/k8s/alloy-values.yaml}"
  [ -f "$values_file" ] || {
    preflight_error "alloy: values file missing: $values_file (T-254-02)"
    return 1
  }

  # 1. RBAC FIRST (D-10): the namespaced Role/RoleBinding must exist before the
  #    Alloy pod starts, so it can read pods/pods-log immediately.
  step "alloy: applying the namespaced log-reader RBAC in '$ALLOY_NAMESPACE' (D-10)"
  _kubectl_indramesh apply -f "$ROOT_DIR/scripts/k8s/alloy-rbac.yaml" || {
    preflight_error "alloy: failed to apply the namespaced RBAC manifest (T-301-11)"
    return 1
  }

  # 2. tgz acquisition + digest gate (T-301-12). No --verify, no --version: the
  #    SHA-256-pinned local tgz is the verified version (D-11).
  step "alloy: installing pinned $ALLOY_CHART_VERSION via the SHA-256-gated local tgz (D-11)"
  curl -fsSL -o "$tmp_dir/$ALLOY_CHART_FILE" "$ALLOY_CHART_URL" || {
    preflight_error "alloy: failed to download the pinned chart tgz (D-11)"
    return 1
  }
  _verify_one "$tmp_dir" "$checksums_file" "$ALLOY_CHART_FILE" || {
    preflight_error "alloy: chart tgz digest verification failed — refusing an unverified install (T-301-12)"
    return 1
  }

  # 3. apiserver CIDR for the NetPol egress rule (OQ3), fail-closed — plus the
  #    ENDPOINT the Service DNATs to (Phase 318: kube-router matches egress after
  #    DNAT, so the ClusterIP rule alone leaves the discovery blind).
  cidr="$(_alloy_apiserver_cidr)" || return 1

  # 4. Private values (config assembly + env + paired NetPol egress).
  private_values="$tmp_dir/log-stack-alloy.yaml"
  write_alloy_private_values "$private_values" "$cidr" || return 1

  # 5. Release-status classification (D-11): known states converge to the pinned
  #    tgz; an unknown state fails closed.
  helm_bin="$(helm_binary_path "$platform_arch" "$verified_helm_archive" "$tmp_dir")" || return 1
  release_status="$(_alloy_release_status "$helm_bin")"
  case "$release_status" in
    deployed)
      step "alloy: release deployed — reconciling to the pinned chart/values (D-11)"
      ;;
    pending-install|pending-upgrade|pending-rollback|failed|uninstalling)
      step "alloy: release present but '$release_status' (interrupted install?) — re-running the pinned install to finalize it (D-11)"
      ;;
    absent)
      step "alloy: release absent — running the pinned install (D-11)"
      ;;
    *)
      preflight_error "alloy: cannot classify Helm release status '$release_status' — refusing to install blindly (D-11)"
      return 1
      ;;
  esac

  # 6. Local-tgz install (no --version, no --verify).
  sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" upgrade --install "$ALLOY_RELEASE_NAME" --reset-values \
    "$tmp_dir/$ALLOY_CHART_FILE" \
    --namespace "$ALLOY_NAMESPACE" --create-namespace \
    --values "$values_file" \
    --values "$private_values" \
    --wait --timeout 10m || {
    preflight_error "alloy: pinned Helm install of $ALLOY_CHART_VERSION failed (T-301-12)"
    return 1
  }
  _wait_for_alloy_ready || return 1
  return 0
}
