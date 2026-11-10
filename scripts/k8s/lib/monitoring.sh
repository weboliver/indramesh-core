#!/bin/bash
# monitoring.sh — installer-owned kube-prometheus-stack provisioning for the
# Phase 261 monitoring flow (MON-01..03; D-03, D-05, D-06, D-08; T-261-SC).
# Sourceable; requires common.sh (step/preflight_error/K3S_KUBECONFIG),
# artifacts.sh (load_versions/_verify_one), helm.sh (helm_binary_path), and
# secrets.sh (_kubectl_indramesh). Never self-sources — k3s.sh:1-18 convention.
#
# provision_monitoring_stack reconciles the PINNED kube-prometheus-stack on every
# installer run (MON-01): the official prometheus-community signing keyring is downloaded,
# SHA-256-verified against checksums.sha256, and passed to `helm --verify`; the
# install runs through the verified Helm binary seam (helm_binary_path) with the
# checked-in non-secret trim/caps values file (scripts/k8s/monitoring-values.yaml,
# T-254-02) — never a PATH Helm, never a moving tag, never an unverified chart
# (T-261-SC). A present release is reconciled to the pinned chart and checked-in
# values on every installer run; only an unknown release status fails closed
# rather than triggering a blind reconciliation (helm.sh:111-118 shape).
# KUBE_PROMETHEUS_STACK_CHART is an OCI reference
# (oci://ghcr.io/prometheus-community/charts/kube-prometheus-stack) that resolves
# directly as the positional `helm upgrade --install` chart argument, mirroring
# CERT_MANAGER_CHART; the OCI manifest for 88.6.2 carries a
# application/vnd.cncf.helm.chart.provenance.v1.prov layer (verified 2026-09-03),
# so `helm --verify` against the OCI ref is the already-proven cert-manager path
# (A5 closed). Ordering constraint: provision_monitoring_stack MUST run BEFORE
# the indramesh full Helm pass — the monitoring.coreos.com/v1 CRDs must exist before
# the chart renders the ServiceMonitor, else the API server rejects the manifest.
#
# Phase 323 (D-04, DEPL-01): the Grafana admin credential belongs to the
# installer. ensure_grafana_admin_secret makes the Secret
# 'indramesh-grafana-admin' exist in the monitoring namespace (adopting the
# chart's own values on a first run over an existing installation, adding only
# missing keys otherwise) BEFORE the pinned install renders Grafana;
# verify_grafana_credentials checks that same Secret against the live API.
set -euo pipefail

MONITORING_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$MONITORING_SH_DIR/../../.." && pwd)"

# Bounded readiness window (D-07): total budget = attempts * interval seconds.
MONITORING_READY_ATTEMPTS="${MONITORING_READY_ATTEMPTS:-60}"
MONITORING_READY_INTERVAL="${MONITORING_READY_INTERVAL:-2}"
# Phase 323 (D-04): the Grafana admin credential is installer-owned. The chart
# consumes it through grafana.admin.existingSecret (scripts/k8s/monitoring-values.yaml);
# GRAFANA_CHART_SECRET_NAME is the Secret the kube-prometheus-stack chart renders
# itself, read ONLY to adopt its values on a first run over an existing install.
GRAFANA_SECRET_NAME="indramesh-grafana-admin"
GRAFANA_CHART_SECRET_NAME="kube-prometheus-stack-grafana"
GRAFANA_SERVICE_NAME="kube-prometheus-stack-grafana"
GRAFANA_CHECK_ATTEMPTS="${GRAFANA_CHECK_ATTEMPTS:-30}"
GRAFANA_CHECK_INTERVAL="${GRAFANA_CHECK_INTERVAL:-2}"
GRAFANA_CHECK_CURL_TIMEOUT="${GRAFANA_CHECK_CURL_TIMEOUT:-5}"

MONITORING_NAMESPACE="monitoring"
MONITORING_OPERATOR_COMPONENT_LABEL="app.kubernetes.io/component=prometheus-operator"

_monitoring_resource_absent() {  # _monitoring_resource_absent <res> <name> — classify a kubectl get:
  # 0 = NotFound (genuinely absent), 1 = present, 2 = any other error (fails
  # closed — a transient/broken API-server read is never treated as absent,
  # helm.sh:111-118 shape).
  local out rc=0
  out="$(_kubectl_indramesh get "$@" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *NotFound*|*"not found"*) return 0 ;;
      *)
        preflight_error "kubectl get $* failed — not treating the monitoring stack as absent (D-07): ${out:-no output}"
        return 2
        ;;
    esac
  fi
  return 1
}

_monitoring_release_status() {  # _monitoring_release_status <helm_bin> <release> — echo the Helm release
  # status word (deployed|pending-install|pending-upgrade|pending-rollback|failed|
  # uninstalling|absent), or 'unknown' on any classification failure. Used by the
  # reuse gate (D-07 + 261-05 fix) to distinguish a healthy deployed release from an
  # interrupted pending/failed one that must be adopted by re-running the pinned
  # install. Fails closed: any error (bad kubeconfig, helm failure) yields 'unknown'
  # so the caller refuses to reuse blindly.
  local helm_bin="$1" release="$2" out rc=0 status=""
  out="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" status "$release" -n "$MONITORING_NAMESPACE" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *"Error: release: not found"*|*"not found"*) echo "absent" ;;
      *) preflight_error "monitoring: helm status '$release' failed — cannot classify release health (D-07): ${out:-no output}"; echo "unknown" ;;
    esac
    return
  fi
  status="$(printf '%s\n' "$out" | sed -n 's/^STATUS: //p' | tr -d ' ')"
  case "$status" in
    deployed|pending-install|pending-upgrade|pending-rollback|failed|uninstalling) echo "$status" ;;
    *) preflight_error "monitoring: unexpected Helm release status '$status' for '$release' (D-07)"; echo "unknown" ;;
  esac
}

_wait_for_monitoring_ready() {  # _wait_for_monitoring_ready — bounded Available wait for the
  # prometheus-operator Deployment in the monitoring namespace (D-07); any
  # timeout fails closed before the indramesh full pass. The label selector
  # (app.kubernetes.io/component=prometheus-operator) is robust against the
  # release-qualified deployment name (kube-prometheus-stack-operator for the
  # pinned release identity).
  local i
  for i in $(seq 1 "$MONITORING_READY_ATTEMPTS"); do
    if _kubectl_indramesh get deployment -n "$MONITORING_NAMESPACE" \
         -l "$MONITORING_OPERATOR_COMPONENT_LABEL" \
         -o jsonpath='{.items[0].status.conditions[?(@.type=="Available")].status}' 2>/dev/null | grep -q True; then
      step "monitoring: prometheus-operator Deployment Available"
      return 0
    fi
    sleep "$MONITORING_READY_INTERVAL"
  done
  preflight_error "monitoring: prometheus-operator Deployment not Available within the bounded window — refusing to proceed (D-07)"
  return 1
}

ensure_grafana_admin_secret() {  # ensure_grafana_admin_secret — make the installer-owned Grafana admin
  # credential exist without ever overwriting an existing value (Phase 323 D-04,
  # T-323-02/T-323-03/T-323-21).
  #
  # The chart consumes $GRAFANA_SECRET_NAME through grafana.admin.existingSecret
  # (scripts/k8s/monitoring-values.yaml), so the Secret must exist BEFORE
  # `helm upgrade --install` renders the Grafana pod (Pitfall 4). Presence is
  # classified with _monitoring_resource_absent, never with the error-swallowing
  # presence helper (it discards every kubectl error, so a transient API failure
  # would look like "absent" and produce a freshly generated password over a
  # live one). Existing values are read through a checked exit code for the same
  # reason, and only missing keys are added — a rotation stays an explicit
  # operator action (Phase 322 D-03/D-07). Values travel through private 0600
  # files and --from-file / --patch-file only, never argv or logs.
  local rc=0 dir="" key value_b64
  local admin_user_b64="" admin_pass_b64=""
  local -a missing_keys=()

  _monitoring_resource_absent secret "$GRAFANA_SECRET_NAME" -n "$MONITORING_NAMESPACE" || rc=$?
  case "$rc" in
    1)
      # The installer-owned Secret already exists. Add ONLY what is missing: a
      # present key is never rewritten, so a live Grafana admin keeps working.
      for key in admin-user admin-password; do
        value_b64=""
        if ! value_b64="$(_kubectl_indramesh get secret "$GRAFANA_SECRET_NAME" -n "$MONITORING_NAMESPACE" \
              -o "jsonpath={.data['$key']}" 2>/dev/null)"; then
          preflight_error "monitoring: Secret '$GRAFANA_SECRET_NAME' in '$MONITORING_NAMESPACE' exists but key '$key' could not be read — refusing to patch over a value this run cannot see (D-04)"
          return 1
        fi
        [ -n "$value_b64" ] || missing_keys+=("$key")
      done
      if [ "${#missing_keys[@]}" -eq 0 ]; then
        step "monitoring: Grafana admin Secret '$GRAFANA_SECRET_NAME' already complete — no change (D-04)"
        return 0
      fi
      dir="$(mktemp -d "${TMPDIR:-/tmp}/indramesh-grafana-secret.XXXXXX")" || {
        preflight_error "monitoring: failed to create private Grafana Secret storage — refusing to proceed (D-04)"
        return 1
      }
      chmod 700 "$dir" || {
        rm -rf "$dir"
        preflight_error "monitoring: failed to secure private Grafana Secret storage — refusing to proceed (D-04)"
        return 1
      }
      for key in "${missing_keys[@]}"; do
        case "$key" in
          admin-user) printf 'admin' > "$dir/$key" || { rm -rf "$dir"; preflight_error "monitoring: failed to write the missing Grafana admin key — refusing to proceed (D-04)"; return 1; } ;;
          admin-password) generate_secret_value admin > "$dir/$key" || { rm -rf "$dir"; preflight_error "monitoring: failed to generate the Grafana admin password — refusing to proceed (D-04)"; return 1; } ;;
        esac
        chmod 600 "$dir/$key" || { rm -rf "$dir"; preflight_error "monitoring: failed to secure the missing Grafana admin key — refusing to proceed (D-04)"; return 1; }
      done
      if ! _merge_missing_secret_keys "$GRAFANA_SECRET_NAME" "$MONITORING_NAMESPACE" "$dir" "${missing_keys[@]}"; then
        rm -rf "$dir"
        return 1
      fi
      rm -rf "$dir"
      step "monitoring: Grafana admin Secret '$GRAFANA_SECRET_NAME' gained missing key(s) ${missing_keys[*]} — existing values untouched (D-04)"
      return 0
      ;;
    0)
      # No installer-owned Secret yet. STEP 1: the namespace must exist BEFORE
      # the Secret is written — on a fresh host the only thing that ever created
      # it was helm's --create-namespace, which runs later. reconcile_indramesh_namespace
      # is NOT reused here: it is the indramesh PSA path (D-04).
      rc=0
      _monitoring_resource_absent namespace "$MONITORING_NAMESPACE" || rc=$?
      case "$rc" in
        0)
          if ! _kubectl_indramesh create namespace "$MONITORING_NAMESPACE" >/dev/null; then
            # Losing a race with a parallel creator is fine; anything else aborts.
            rc=0
            _monitoring_resource_absent namespace "$MONITORING_NAMESPACE" || rc=$?
            if [ "$rc" -ne 1 ]; then
              preflight_error "monitoring: namespace '$MONITORING_NAMESPACE' is missing and could not be created — the Grafana admin Secret cannot be written (D-04)"
              return 1
            fi
          fi
          ;;
        1) ;;
        *) return 1 ;;
      esac
      # STEP 2: adopt the chart's values when this is a first run over an
      # existing installation — never lock the operator out (Pitfall 5).
      rc=0
      _monitoring_resource_absent secret "$GRAFANA_CHART_SECRET_NAME" -n "$MONITORING_NAMESPACE" || rc=$?
      case "$rc" in
        1)
          admin_user_b64="$(_secret_data_key "$GRAFANA_CHART_SECRET_NAME" "$MONITORING_NAMESPACE" admin-user)"
          admin_pass_b64="$(_secret_data_key "$GRAFANA_CHART_SECRET_NAME" "$MONITORING_NAMESPACE" admin-password)"
          if [ -z "$admin_user_b64" ] || [ -z "$admin_pass_b64" ]; then
            preflight_error "monitoring: chart Secret '$GRAFANA_CHART_SECRET_NAME' in '$MONITORING_NAMESPACE' is missing or incomplete — refusing to derive '$GRAFANA_SECRET_NAME' from it (D-04)"
            return 1
          fi
          ;;
        0) ;;
        *) return 1 ;;
      esac
      # STEP 3: write both keys from private 0600 files. Either adopted from the
      # chart Secret (decoded, byte-identical) or freshly generated.
      dir="$(mktemp -d "${TMPDIR:-/tmp}/indramesh-grafana-secret.XXXXXX")" || {
        preflight_error "monitoring: failed to create private Grafana Secret storage — refusing to proceed (D-04)"
        return 1
      }
      chmod 700 "$dir" || {
        rm -rf "$dir"
        preflight_error "monitoring: failed to secure private Grafana Secret storage — refusing to proceed (D-04)"
        return 1
      }
      if [ -n "$admin_user_b64" ]; then
        if ! printf '%s' "$admin_user_b64" | base64 --decode > "$dir/admin-user"; then
          rm -rf "$dir"
          preflight_error "monitoring: failed to carry the chart's Grafana admin user over — refusing to proceed (D-04)"
          return 1
        fi
      else
        printf 'admin' > "$dir/admin-user" || { rm -rf "$dir"; preflight_error "monitoring: failed to write the Grafana admin user — refusing to proceed (D-04)"; return 1; }
      fi
      if [ -n "$admin_pass_b64" ]; then
        if ! printf '%s' "$admin_pass_b64" | base64 --decode > "$dir/admin-password"; then
          rm -rf "$dir"
          preflight_error "monitoring: failed to carry the chart's Grafana admin password over — refusing to proceed (D-04)"
          return 1
        fi
      else
        generate_secret_value admin > "$dir/admin-password" || { rm -rf "$dir"; preflight_error "monitoring: failed to generate the Grafana admin password — refusing to proceed (D-04)"; return 1; }
      fi
      chmod 600 "$dir/admin-user" "$dir/admin-password" || {
        rm -rf "$dir"
        preflight_error "monitoring: failed to secure the Grafana admin credential files — refusing to proceed (D-04)"
        return 1
      }
      if ! _kubectl_indramesh create secret generic "$GRAFANA_SECRET_NAME" -n "$MONITORING_NAMESPACE" \
             --from-file="admin-user=$dir/admin-user" --from-file="admin-password=$dir/admin-password" >/dev/null; then
        rm -rf "$dir"
        preflight_error "monitoring: failed to create the Grafana admin Secret '$GRAFANA_SECRET_NAME' in '$MONITORING_NAMESPACE' (D-04)"
        return 1
      fi
      rm -rf "$dir"
      if [ -n "$admin_pass_b64" ]; then
        step "monitoring: adopted the existing chart credential into '$GRAFANA_SECRET_NAME' — the value is unchanged (D-04)"
      else
        step "monitoring: created '$GRAFANA_SECRET_NAME' with a generated admin password (D-04)"
      fi
      return 0
      ;;
    *)
      return 1
      ;;
  esac
}

verify_grafana_credentials() {  # verify_grafana_credentials — prove the Grafana Secret authenticates (D-04..D-10)
  local admin_user_b64 admin_pass_b64 admin_user admin_pass netrc_file="" body_file="" pf_log_file="" pf_pid=""
  local i http_code="000" login local_port="" cleanup_status=1

  admin_user_b64="$(_secret_data_key "$GRAFANA_SECRET_NAME" "$MONITORING_NAMESPACE" admin-user)"
  admin_pass_b64="$(_secret_data_key "$GRAFANA_SECRET_NAME" "$MONITORING_NAMESPACE" admin-password)"
  if [ -z "$admin_user_b64" ] || [ -z "$admin_pass_b64" ]; then
    preflight_error "monitoring: Grafana admin Secret '$GRAFANA_SECRET_NAME' in namespace '$MONITORING_NAMESPACE' is missing or incomplete — cannot verify credentials (D-08)"
    return 1
  fi
  if ! admin_user="$(printf '%s' "$admin_user_b64" | base64 --decode)" \
    || ! admin_pass="$(printf '%s' "$admin_pass_b64" | base64 --decode)"; then
    preflight_error "monitoring: Grafana admin Secret '$GRAFANA_SECRET_NAME' in namespace '$MONITORING_NAMESPACE' is missing or incomplete — cannot verify credentials (D-08)"
    return 1
  fi
  if [ -z "$admin_user" ] || [ -z "$admin_pass" ]; then
    preflight_error "monitoring: Grafana admin Secret '$GRAFANA_SECRET_NAME' in namespace '$MONITORING_NAMESPACE' is missing or incomplete — cannot verify credentials (D-08)"
    return 1
  fi

  netrc_file="$(mktemp "${TMPDIR:-/tmp}/indramesh-grafana-netrc.XXXXXX")" || {
    preflight_error "monitoring: failed to create private Grafana credential storage — refusing to proceed (D-08)"
    return 1
  }
  trap 'if [ -n "${pf_pid:-}" ]; then kill "$pf_pid" 2>/dev/null || true; wait "$pf_pid" 2>/dev/null || true; restore_terminal; fi; rm -f "${netrc_file:-}" "${body_file:-}" "${pf_log_file:-}"; trap - RETURN; return "$cleanup_status"' RETURN
  body_file="$(mktemp "${TMPDIR:-/tmp}/indramesh-grafana-body.XXXXXX")" || {
    preflight_error "monitoring: failed to create private Grafana response storage — refusing to proceed (D-08)"
    return 1
  }
  pf_log_file="$(mktemp "${TMPDIR:-/tmp}/indramesh-grafana-port-forward.XXXXXX")" || {
    preflight_error "monitoring: failed to create private Grafana port-forward storage — refusing to proceed (D-08)"
    return 1
  }
  chmod 600 "$netrc_file" "$body_file" "$pf_log_file" || {
    preflight_error "monitoring: failed to secure private Grafana credential storage — refusing to proceed (D-08)"
    return 1
  }
  printf 'machine 127.0.0.1\nlogin %s\npassword %s\n' "$admin_user" "$admin_pass" > "$netrc_file" || {
    preflight_error "monitoring: failed to write private Grafana credential storage — refusing to proceed (D-08)"
    return 1
  }

  # sudo (use_pty) switches the terminal to raw mode whenever it believes it
  # runs in the terminal's foreground process group — which a plain `&` job of
  # a script does. Job control gives the port-forward its own process group, so
  # sudo leaves the terminal alone; the RETURN trap still restores it after the
  # kill as a second line of defence.
  if [ -t 0 ]; then
    set -m
  fi
  _kubectl_indramesh port-forward "svc/$GRAFANA_SERVICE_NAME" \
    :80 -n "$MONITORING_NAMESPACE" </dev/null >"$pf_log_file" 2>&1 &
  pf_pid=$!
  set +m

  for i in $(seq 1 "$GRAFANA_CHECK_ATTEMPTS"); do
    if ! kill -0 "$pf_pid" 2>/dev/null; then
      preflight_error "monitoring: Grafana service port-forward ended before the credential check could connect — refusing to proceed (D-08)"
      return 1
    fi
    # The remote port in kubectl's "Forwarding from" line is the POD's
    # targetPort, not the Service port we asked for: `svc/...:80` resolves to
    # the Grafana container's 3000, so kubectl prints "-> 3000". Matching a
    # literal "-> 80" never parsed a port, the loop below never issued a single
    # request, and http_code kept its "000" seed from the declaration above —
    # which the case statement then reported as "Grafana admin API unreachable"
    # while Grafana was healthy the whole time (2026-09-22). Match any remote
    # port; the local port is the only field this check needs.
    local_port="$(sed -nE 's/^Forwarding from 127\.0\.0\.1:([0-9]+) -> [0-9]+$/\1/p' "$pf_log_file" | head -n1)"
    if [ -z "$local_port" ]; then
      sleep "$GRAFANA_CHECK_INTERVAL"
      continue
    fi
    http_code="$(curl --silent --show-error --max-time "$GRAFANA_CHECK_CURL_TIMEOUT" \
      --netrc-file "$netrc_file" -o "$body_file" -w '%{http_code}' \
      "http://127.0.0.1:$local_port/api/user" 2>/dev/null || true)"
    [ "$http_code" != "000" ] && break
    sleep "$GRAFANA_CHECK_INTERVAL"
  done

  # Separate "we never got a local port to talk to" from "we talked and got
  # nothing back". Both used to surface as the 000 branch below, which sent the
  # operator looking at a healthy Grafana instead of at the port-forward.
  if [ -z "$local_port" ]; then
    preflight_error "monitoring: could not parse a local port from the port-forward to svc/$GRAFANA_SERVICE_NAME in '$MONITORING_NAMESPACE' within the bounded window ($GRAFANA_CHECK_ATTEMPTS attempts) — no request was made, so this says nothing about Grafana's health; inspect kubectl's 'Forwarding from' output format (D-08)"
    return 1
  fi

  case "$http_code" in
    000)
      preflight_error "monitoring: Grafana admin API unreachable via svc/$GRAFANA_SERVICE_NAME in '$MONITORING_NAMESPACE' within the bounded window ($GRAFANA_CHECK_ATTEMPTS attempts) — refusing to proceed (D-08)"
      return 1
      ;;
    200)
      login="$(grep -o '"login":"[^"]*"' "$body_file" | head -n1 | sed -E 's/.*"login":"([^"]*)".*/\1/')"
      if [ "$login" = "$admin_user" ]; then
        step "monitoring: Grafana admin credentials verified against the live API (D-09)"
        cleanup_status=0
        return 0
      fi
      preflight_error "monitoring: Grafana /api/user identity does not match the Secret's admin-user — credential drift between the Secret and the live Grafana account (D-08/D-09)"
      return 1
      ;;
    401|403)
      preflight_error "monitoring: Grafana rejected authentication from the admin Secret — refusing to proceed (D-06/D-08).
Manual operator action: the Kubernetes Secret '$GRAFANA_SECRET_NAME' in '$MONITORING_NAMESPACE' remains the source of truth and is never altered by this check; the installer never resets the Grafana password automatically (D-05/D-07). Repair the Grafana admin account inside the container with exactly this command, then rerun the full installer:
sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl -n monitoring exec deploy/kube-prometheus-stack-grafana -- sh -c 'grafana cli --homepath /usr/share/grafana --config /etc/grafana/grafana.ini admin reset-admin-password \"\$GF_SECURITY_ADMIN_PASSWORD\"'
The value '\$GF_SECURITY_ADMIN_PASSWORD' is Secret-backed inside the container: it is never a host-side argument and never rendered in this output. The resolved value is present in the Grafana CLI argv inside the container only for the command's lifetime; this limited in-container argv exposure is explicitly accepted by the user. No alternative recovery or reset mechanism is provided (D-06/D-07)."
      return 1
      ;;
    *)
      preflight_error "monitoring: Grafana API returned unexpected HTTP $http_code for /api/user — refusing to proceed (D-08)"
      return 1
      ;;
  esac
}

provision_monitoring_stack() {  # provision_monitoring_stack <versions_file> <checksums_file> <tmp_dir> <platform_arch> <verified_helm_archive>
  # Reconciliation gate (D-07): known release states converge to the pinned OCI
  # chart and checked-in values through the verified Helm binary with --verify
  # (T-261-SC); an unknown state fails closed rather than reconciling blindly.
  local versions_file="${1:?versions file required}"
  local checksums_file="${2:?checksums file required}"
  local tmp_dir="${3:?tmp dir required}"
  local platform_arch="${4:?platform arch required}"
  local verified_helm_archive="${5:?verified helm archive required}"
  local keyring_path helm_bin monitoring_values ns_status crd_status release_status
  load_versions "$versions_file" || {
    preflight_error "monitoring: failed to load pinned versions from $(basename "$versions_file") (D-10)"
    return 1
  }
  if [ -z "$KUBE_PROMETHEUS_STACK_VERSION" ] || [ -z "$KUBE_PROMETHEUS_STACK_CHART" ] \
     || [ -z "$PROMETHEUS_COMMUNITY_KEYRING_URL" ] || [ -z "$PROMETHEUS_COMMUNITY_KEYRING_FILE" ]; then
    preflight_error "monitoring: KUBE_PROMETHEUS_STACK_* / PROMETHEUS_COMMUNITY_KEYRING_* pins missing from versions.env (D-07)"
    return 1
  fi
  [ -f "$checksums_file" ] || { preflight_error "monitoring: checksum manifest missing: $checksums_file (D-07)"; return 1; }
  [ -d "$tmp_dir" ] || { preflight_error "monitoring: staging directory missing: $tmp_dir"; return 1; }

  monitoring_values="${MONITORING_VALUES_FILE:-$ROOT_DIR/scripts/k8s/monitoring-values.yaml}"
  [ -f "$monitoring_values" ] || {
    preflight_error "monitoring: stack values file missing: $monitoring_values (T-254-02)"
    return 1
  }

  _monitoring_resource_absent namespace "$MONITORING_NAMESPACE"; ns_status=$?
  _monitoring_resource_absent crd servicemonitors.monitoring.coreos.com; crd_status=$?
  if [ "$ns_status" -eq 1 ] && [ "$crd_status" -eq 1 ]; then
    # 261-05 live fix (Rule 1): namespace + CRDs present is NOT sufficient for a
    # healthy reuse — an interrupted helm upgrade --install leaves the release in
    # pending-install/pending-upgrade with the resources half-deployed, and a
    # subsequent run would then wrongly "reuse" forever without ever finalizing
    # the release (observed 2026-09-03: manual helm uninstall was required).
    # Probe the Helm release status via the verified-Helm seam; a pending/failed
    # release is treated as absent so the pinned install re-runs and adopts it.
    helm_bin="$(helm_binary_path "$platform_arch" "$verified_helm_archive" "$tmp_dir")" || return 1
    release_status="$(_monitoring_release_status "$helm_bin" kube-prometheus-stack)"
    case "$release_status" in
      deployed)
        step "monitoring: release deployed — reconciling to the pinned chart/values (D-01/D-02)"
        ;;
      pending-install|pending-upgrade|pending-rollback|failed|uninstalling)
        step "monitoring: release present but '$release_status' (interrupted install?) — re-running the pinned install to finalize it (261-05 fix)"
        ;;
      absent)
        step "monitoring: stack + CRDs present but release absent — re-running the pinned install (D-07)"
        ;;
      *)
        preflight_error "monitoring: cannot classify Helm release status '$release_status' — refusing to reinstall blindly (D-07)"
        return 1
        ;;
    esac
  fi
  if [ "$ns_status" -eq 2 ] || [ "$crd_status" -eq 2 ]; then
    preflight_error "monitoring: stack presence could not be classified — refusing to reinstall blindly (D-07)"
    return 1
  fi

  step "monitoring: installing pinned $KUBE_PROMETHEUS_STACK_VERSION via the verified Helm binary (D-07)"
  keyring_path="$tmp_dir/$PROMETHEUS_COMMUNITY_KEYRING_FILE"
  curl -fsSL -o "$keyring_path" "$PROMETHEUS_COMMUNITY_KEYRING_URL" || {
    preflight_error "monitoring: failed to download the official signing keyring (D-07)"
    return 1
  }
  _verify_one "$tmp_dir" "$checksums_file" "$PROMETHEUS_COMMUNITY_KEYRING_FILE" || {
    preflight_error "monitoring: keyring digest verification failed — refusing an unverified install (T-261-SC)"
    return 1
  }
  # 261-05 live fix (Rule 1): helm --verify --keyring requires a BINARY OpenPGP
  # keyring, but the upstream prometheus-community pubkey.gpg is ASCII-armored
  # (-----BEGIN PGP PUBLIC KEY BLOCK-----) — passed raw it fails with
  # "openpgp: invalid data: tag byte does not have MSB set". Dearmor
  # deterministically (same armored input -> same binary output) AFTER the
  # SHA-256 pin verified the canonical upstream artifact, so the supply-chain
  # contract (T-261-SC) is unchanged; gpg is a D-13 bootstrap package.
  if ! gpg --batch --dearmor < "$keyring_path" > "$keyring_path.bin" 2>/dev/null; then
    preflight_error "monitoring: failed to dearmor the signing keyring (T-261-SC)"
    return 1
  fi
  keyring_path="$keyring_path.bin"
  helm_bin="$(helm_binary_path "$platform_arch" "$verified_helm_archive" "$tmp_dir")" || return 1
  # Phase 323 (D-04, Pitfall 4/Pitfall 5): the installer-owned admin Secret must
  # exist BEFORE the chart renders Grafana — otherwise the pod starts against a
  # Secret that is not there. Anchored after the keyring/dearmor proof so a
  # failed provenance check never writes Secret material, and before the upgrade
  # so the chart never points at a missing Secret.
  ensure_grafana_admin_secret || return 1
  sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" upgrade --install kube-prometheus-stack --reset-values \
    "$KUBE_PROMETHEUS_STACK_CHART" \
    --version "$KUBE_PROMETHEUS_STACK_VERSION" \
    --namespace "$MONITORING_NAMESPACE" --create-namespace \
    --values "$monitoring_values" \
    --verify --keyring "$keyring_path" \
    --wait --timeout 10m || {
    preflight_error "monitoring: pinned Helm install of $KUBE_PROMETHEUS_STACK_VERSION failed (T-261-SC)"
    return 1
  }
  _wait_for_monitoring_ready || return 1
  return 0
}
