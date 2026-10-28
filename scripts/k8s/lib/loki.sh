#!/bin/bash
# loki.sh — installer-owned Loki community-chart provisioning for the Phase 301
# log stack (LOGINFRA-03, LOGINFRA-04; D-04, D-05, D-11, D-12; T-301-08..10,
# T-301-SC). Sourceable; requires common.sh (step/preflight_error/K3S_KUBECONFIG),
# artifacts.sh (load_versions/_verify_one), helm.sh (helm_binary_path), and
# secrets.sh (_kubectl_indramesh). Never self-sources — k3s.sh:1-18 convention.
#
# provision_loki_stack reconciles the PINNED Loki community chart on every
# installer run (D-11): the official grafana-community signing keyring is
# downloaded, SHA-256-verified against checksums.sha256, and dearmored to a
# binary keyring (261-05 pattern — helm --verify --keyring rejects the
# ASCII-armored upstream file), then the release installs through the verified
# Helm binary seam (helm_binary_path) with the checked-in non-secret trim
# values (scripts/k8s/loki-values.yaml, T-254-02) as
# `helm upgrade --install loki --reset-values <OCI ref> --verify --keyring`.
# LOKI_CHART is a signed OCI reference
# (oci://ghcr.io/grafana-community/helm-charts/loki) that resolves directly as
# the positional chart argument, mirroring KUBE_PROMETHEUS_STACK_CHART and
# CERT_MANAGER_CHART; --version pins 18.13.8 exactly (never a moving tag). A
# present release is reconciled to the pinned chart and checked-in values on
# every installer run; only an unknown release status fails closed rather than
# triggering a blind reconciliation (helm.sh:111-118 shape). After a successful
# install the checked-in backend-ingress NetworkPolicy is applied (Phase 305,
# LOGX-01; NetworkPolicies are additive) before the readiness wait. The
# retention period is the D-04 Helm-values override: when LOKI_RETENTION_PERIOD differs
# from the checked-in default (336h) a private values file overrides
# loki.limits_config.retention_period.
set -euo pipefail

LOKI_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$LOKI_SH_DIR/../../.." && pwd)"

LOKI_NAMESPACE="monitoring"
LOKI_RELEASE_NAME="loki"
# Bounded readiness window for the single-binary StatefulSet (D-11): total
# budget = attempts * interval seconds. Env-overridable for hermetic tests.
LOKI_READY_ATTEMPTS="${LOKI_READY_ATTEMPTS:-60}"
LOKI_READY_INTERVAL="${LOKI_READY_INTERVAL:-2}"
# Checked-in default retention (D-04, matches loki-values.yaml).
LOKI_RETENTION_DEFAULT="336h"

_loki_release_status() {  # _loki_release_status <helm_bin> — echo the Helm release status
  # word (deployed|pending-install|pending-upgrade|pending-rollback|failed|
  # uninstalling|absent), or 'unknown' on any classification failure. Used to
  # distinguish a healthy deployed release from an interrupted pending/failed
  # one that must be adopted by re-running the pinned install. Fails closed:
  # any error (bad kubeconfig, helm failure) yields 'unknown' so the caller
  # refuses to install blindly (D-11).
  local helm_bin="$1" out rc=0 status=""
  out="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" status "$LOKI_RELEASE_NAME" -n "$LOKI_NAMESPACE" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *"Error: release: not found"*|*"not found"*) echo "absent" ;;
      *) preflight_error "loki: helm status '$LOKI_RELEASE_NAME' failed — cannot classify release health (D-11): ${out:-no output}"; echo "unknown" ;;
    esac
    return
  fi
  status="$(printf '%s\n' "$out" | sed -n 's/^STATUS: //p' | tr -d ' ')"
  case "$status" in
    deployed|pending-install|pending-upgrade|pending-rollback|failed|uninstalling) echo "$status" ;;
    *) preflight_error "loki: unexpected Helm release status '$status' for '$LOKI_RELEASE_NAME' (D-11)"; echo "unknown" ;;
  esac
}

_wait_for_loki_ready() {  # _wait_for_loki_ready — bounded readiness wait for the single-binary
  # StatefulSet in the monitoring namespace (D-11); any timeout fails closed
  # before the installer claims the log stack is up. The StatefulSet is named
  # `loki` (release name == chart name -> loki.fullname == loki; verified via
  # helm template against the pinned chart).
  local i ready
  for i in $(seq 1 "$LOKI_READY_ATTEMPTS"); do
    ready="$(_kubectl_indramesh get statefulset "$LOKI_RELEASE_NAME" -n "$LOKI_NAMESPACE" \
      -o jsonpath='{.status.readyReplicas}' 2>/dev/null || true)"
    if [ "$ready" = "1" ]; then
      step "loki: single-binary StatefulSet Ready"
      return 0
    fi
    sleep "$LOKI_READY_INTERVAL"
  done
  preflight_error "loki: single-binary StatefulSet not Ready within the bounded window ($LOKI_READY_ATTEMPTS attempts) — refusing to proceed (D-11)"
  return 1
}

provision_loki_stack() {  # provision_loki_stack <versions_file> <checksums_file> <tmp_dir> <platform_arch> <verified_helm_archive>
  # Verified OCI install of the pinned Loki community chart (D-11). The keyring
  # digest gate runs BEFORE dearmor and BEFORE any helm invocation (T-301-SC):
  # a mismatched digest fails closed with no gpg and no helm call.
  local versions_file="${1:?versions file required}"
  local checksums_file="${2:?checksums file required}"
  local tmp_dir="${3:?tmp dir required}"
  local platform_arch="${4:?platform arch required}"
  local verified_helm_archive="${5:?verified helm archive required}"
  local keyring_path helm_bin loki_values release_status retention_period
  local apply_args=()

  load_versions "$versions_file" || {
    preflight_error "loki: failed to load pinned versions from $(basename "$versions_file") (D-11)"
    return 1
  }
  if [ -z "$LOKI_CHART_VERSION" ] || [ -z "$LOKI_CHART" ] \
     || [ -z "$GRAFANA_COMMUNITY_KEYRING_URL" ] || [ -z "$GRAFANA_COMMUNITY_KEYRING_FILE" ]; then
    preflight_error "loki: LOKI_* / GRAFANA_COMMUNITY_KEYRING_* pins missing from versions.env (D-11)"
    return 1
  fi
  [ -f "$checksums_file" ] || { preflight_error "loki: checksum manifest missing: $checksums_file (D-11)"; return 1; }
  [ -d "$tmp_dir" ] || { preflight_error "loki: staging directory missing: $tmp_dir"; return 1; }

  loki_values="${LOKI_VALUES_FILE:-$ROOT_DIR/scripts/k8s/loki-values.yaml}"
  [ -f "$loki_values" ] || {
    preflight_error "loki: values file missing: $loki_values (T-254-02)"
    return 1
  }

  step "loki: installing pinned $LOKI_CHART_VERSION via the verified Helm binary (D-11)"
  keyring_path="$tmp_dir/$GRAFANA_COMMUNITY_KEYRING_FILE"
  curl -fsSL -o "$keyring_path" "$GRAFANA_COMMUNITY_KEYRING_URL" || {
    preflight_error "loki: failed to download the official signing keyring (D-11)"
    return 1
  }
  _verify_one "$tmp_dir" "$checksums_file" "$GRAFANA_COMMUNITY_KEYRING_FILE" || {
    preflight_error "loki: keyring digest verification failed — refusing an unverified install (T-301-SC)"
    return 1
  }
  # helm --verify --keyring requires a BINARY OpenPGP keyring, but the upstream
  # grafana-community pubkey.gpg is ASCII-armored. Dearmor deterministically
  # (same armored input -> same binary output) AFTER the SHA-256 pin verified
  # the canonical upstream artifact, so the supply-chain contract (T-301-SC) is
  # unchanged; gpg is a D-13 bootstrap package (261-05 pattern).
  if ! gpg --batch --dearmor < "$keyring_path" > "$keyring_path.bin" 2>/dev/null; then
    preflight_error "loki: failed to dearmor the signing keyring (T-301-SC)"
    return 1
  fi
  keyring_path="$keyring_path.bin"

  # D-04 Helm-values override: only write (and pass) the private values file
  # when the operator-set retention differs from the checked-in default.
  retention_period="${LOKI_RETENTION_PERIOD:-$LOKI_RETENTION_DEFAULT}"
  apply_args=(--values "$loki_values")
  if [ "$retention_period" != "$LOKI_RETENTION_DEFAULT" ]; then
    printf 'loki:\n  limits_config:\n    retention_period: %s\n' "$retention_period" > "$tmp_dir/log-stack-loki.yaml" || {
      preflight_error "loki: failed to write the private retention override values (D-04)"
      return 1
    }
    apply_args+=(--values "$tmp_dir/log-stack-loki.yaml")
    step "loki: applying operator retention override ${retention_period} (D-04)"
  fi

  helm_bin="$(helm_binary_path "$platform_arch" "$verified_helm_archive" "$tmp_dir")" || return 1
  # Release-status classification (D-11): known release states converge to the
  # pinned OCI chart and checked-in values through the verified Helm binary with
  # --verify; an unknown state fails closed rather than reconciling blindly.
  release_status="$(_loki_release_status "$helm_bin")"
  case "$release_status" in
    deployed)
      step "loki: release deployed — reconciling to the pinned chart/values (D-11)"
      ;;
    pending-install|pending-upgrade|pending-rollback|failed|uninstalling)
      step "loki: release present but '$release_status' (interrupted install?) — re-running the pinned install to finalize it (D-11)"
      ;;
    absent)
      step "loki: release absent — running the pinned install (D-11)"
      ;;
    *)
      preflight_error "loki: cannot classify Helm release status '$release_status' — refusing to install blindly (D-11)"
      return 1
      ;;
  esac

  sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" upgrade --install "$LOKI_RELEASE_NAME" --reset-values \
    "$LOKI_CHART" \
    --version "$LOKI_CHART_VERSION" \
    --namespace "$LOKI_NAMESPACE" --create-namespace \
    "${apply_args[@]}" \
    --verify --keyring "$keyring_path" \
    --wait --timeout 10m || {
    preflight_error "loki: pinned Helm install of $LOKI_CHART_VERSION failed (T-301-SC)"
    return 1
  }
  # Phase 305 (LOGX-01, D-12): the monitoring-side ingress half of the matched
  # backend→Loki pair. The install above created the `monitoring` namespace;
  # NetworkPolicies are additive, so the chart's Alloy ingress policy stays
  # intact and this checked-in manifest adds exactly the indramesh-backend
  # peer on Loki's HTTP port 3100.
  step "loki: applying the backend ingress NetworkPolicy in '$LOKI_NAMESPACE' (LOGX-01)"
  _kubectl_indramesh apply -f "$ROOT_DIR/scripts/k8s/loki-backend-ingress-netpol.yaml" || {
    preflight_error "loki: failed to apply the backend ingress NetworkPolicy manifest (LOGX-01)"
    return 1
  }
  _wait_for_loki_ready || return 1
  return 0
}
