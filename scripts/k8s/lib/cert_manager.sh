#!/bin/bash
# cert_manager.sh — installer-owned cert-manager operator + two-tier PKI
# lifecycle for the Phase 259 TLS flow (NET-03; D-02, D-03, D-07, D-08;
# T-259-05..09). Sourceable; requires common.sh (step/preflight_error/
# private_tmp/K3S_KUBECONFIG), artifacts.sh (load_versions/_verify_one),
# helm.sh (helm_binary_path/HELM_NAMESPACE), and secrets.sh (_kubectl_indramesh).
# Never self-sources — k3s.sh:1-18 convention.
#
# provision_cert_manager installs-or-reuses the PINNED cert-manager operator
# (D-07): the official signing keyring is downloaded, SHA-256-verified against
# checksums.sha256, and passed to `helm --verify`; the install runs through the
# verified Helm binary seam (helm_binary_path) — never a PATH Helm, never a
# moving tag, never an unverified chart (T-259-05). A present operator is
# reused, never downgraded or reinstalled; only a recognizable NotFound is
# treated as absent (fail-closed classification, helm.sh:111-118 shape).
#
# reconcile_pki applies the two-tier SelfSigned→CA PKI in the fixed order
# (D-03): SelfSigned ClusterIssuer indramesh-selfsigned → retained-root CA
# Certificate indramesh-ca-root (isCA, duration 87600h, renewal.policy Disabled,
# ECDSA P-256, namespace cert-manager) → CA ClusterIssuer indramesh-ca → renewable
# leaf Certificate indramesh-local-tls (dnsNames [localhost], ipAddresses [127.0.0.1, host IP], duration 8760h,
# renewBeforePercentage 33, rotationPolicy Always, namespace indramesh). Every
# tier is bounded-waited Ready BEFORE the next applies; the leaf must be
# Ready=True before any Nginx rollout consumes its Secret (D-03). NEVER passes
# --enable-certificate-owner-ref (default Secret retention keeps the retained
# root across reinstalls, research 200) and never prints Secret data (T-259-08).
set -euo pipefail

# Bounded readiness windows (D-03): total budget = attempts * interval seconds.
CERT_READY_ATTEMPTS="${CERT_READY_ATTEMPTS:-90}"
CERT_READY_INTERVAL="${CERT_READY_INTERVAL:-2}"
CERT_OPERATOR_READY_ATTEMPTS="${CERT_OPERATOR_READY_ATTEMPTS:-60}"
CERT_OPERATOR_READY_INTERVAL="${CERT_OPERATOR_READY_INTERVAL:-2}"

CERT_MANAGER_NAMESPACE="cert-manager"
CERT_MANAGER_CHART="oci://quay.io/jetstack/charts/cert-manager"

_cert_resource_absent() {  # _cert_resource_absent <res> <name> — classify a kubectl get:
  # 0 = NotFound (genuinely absent), 1 = present, 2 = any other error (fails
  # closed — a transient/broken API-server read is never treated as absent,
  # helm.sh:111-118 shape).
  local out rc=0
  out="$(_kubectl_indramesh get "$@" 2>&1)" || rc=$?
  if [ "$rc" -ne 0 ]; then
    case "$out" in
      *NotFound*|*"not found"*) return 0 ;;
      *)
        preflight_error "kubectl get $* failed — not treating cert-manager as absent (D-07): ${out:-no output}"
        return 2
        ;;
    esac
  fi
  return 1
}

_wait_for_operator_ready() {  # _wait_for_operator_ready — bounded Available wait for
  # the cert-manager and cert-manager-webhook Deployments (D-07); any timeout
  # fails closed before the PKI tier.
  local i
  for i in $(seq 1 "$CERT_OPERATOR_READY_ATTEMPTS"); do
    if _kubectl_indramesh get deployment cert-manager -n "$CERT_MANAGER_NAMESPACE" \
         -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null | grep -q True \
       && _kubectl_indramesh get deployment cert-manager-webhook -n "$CERT_MANAGER_NAMESPACE" \
         -o jsonpath='{.status.conditions[?(@.type=="Available")].status}' 2>/dev/null | grep -q True; then
      step "cert-manager: operator deployments Available"
      return 0
    fi
    sleep "$CERT_OPERATOR_READY_INTERVAL"
  done
  preflight_error "cert-manager: operator deployments not Available within the bounded window — refusing to proceed (D-07)"
  return 1
}

provision_cert_manager() {  # provision_cert_manager <versions_file> <checksums_file> <tmp_dir> <platform_arch> <verified_helm_archive>
  # Install-or-reuse gate (D-07): namespace + CRDs both present → reuse, never
  # downgrade, never reinstall a present operator. Otherwise download the
  # official keyring, verify its SHA-256 against checksums.sha256, and install
  # the pinned OCI chart through the verified Helm binary with --verify.
  local versions_file="${1:?versions file required}"
  local checksums_file="${2:?checksums file required}"
  local tmp_dir="${3:?tmp dir required}"
  local platform_arch="${4:?platform arch required}"
  local verified_helm_archive="${5:?verified helm archive required}"
  local keyring_path helm_bin ns_status crd_status
  load_versions "$versions_file" || {
    preflight_error "cert-manager: failed to load pinned versions from $(basename "$versions_file") (D-10)"
    return 1
  }
  if [ -z "$CERT_MANAGER_VERSION" ] || [ -z "$CERT_MANAGER_KEYRING_URL" ] || [ -z "$CERT_MANAGER_KEYRING_FILE" ]; then
    preflight_error "cert-manager: CERT_MANAGER_* pins missing from versions.env (D-07)"
    return 1
  fi
  [ -f "$checksums_file" ] || { preflight_error "cert-manager: checksum manifest missing: $checksums_file (D-07)"; return 1; }
  [ -d "$tmp_dir" ] || { preflight_error "cert-manager: staging directory missing: $tmp_dir"; return 1; }

  _cert_resource_absent namespace "$CERT_MANAGER_NAMESPACE"; ns_status=$?
  _cert_resource_absent crd clusterissuers.cert-manager.io; crd_status=$?
  if [ "$ns_status" -eq 1 ] && [ "$crd_status" -eq 1 ]; then
    step "cert-manager: operator + CRDs present — reusing (D-07)"
    return 0
  fi
  if [ "$ns_status" -eq 2 ] || [ "$crd_status" -eq 2 ]; then
    preflight_error "cert-manager: operator presence could not be classified — refusing to reinstall blindly (D-07)"
    return 1
  fi

  step "cert-manager: installing pinned $CERT_MANAGER_VERSION via the verified Helm binary (D-07)"
  keyring_path="$tmp_dir/$CERT_MANAGER_KEYRING_FILE"
  curl -fsSL -o "$keyring_path" "$CERT_MANAGER_KEYRING_URL" || {
    preflight_error "cert-manager: failed to download the official signing keyring (D-07)"
    return 1
  }
  _verify_one "$tmp_dir" "$checksums_file" "$CERT_MANAGER_KEYRING_FILE" || {
    preflight_error "cert-manager: keyring digest verification failed — refusing an unverified install (T-259-05)"
    return 1
  }
  helm_bin="$(helm_binary_path "$platform_arch" "$verified_helm_archive" "$tmp_dir")" || return 1
  sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" upgrade --install cert-manager \
    "$CERT_MANAGER_CHART" \
    --version "$CERT_MANAGER_VERSION" \
    --namespace "$CERT_MANAGER_NAMESPACE" --create-namespace \
    --set crds.enabled=true \
    --verify --keyring "$keyring_path" \
    --wait --timeout 10m || {
    preflight_error "cert-manager: pinned Helm install of $CERT_MANAGER_VERSION failed (T-259-05)"
    return 1
  }
  _wait_for_operator_ready || return 1
  return 0
}

wait_for_clusterissuer_ready() {  # wait_for_clusterissuer_ready <name> — bounded Ready wait
  # for a ClusterIssuer (D-03); fails closed naming the issuer.
  local name="$1" i
  for i in $(seq 1 "$CERT_READY_ATTEMPTS"); do
    if _kubectl_indramesh get clusterissuer "$name" \
         -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; then
      step "cert-manager: ClusterIssuer '$name' Ready"
      return 0
    fi
    sleep "$CERT_READY_INTERVAL"
  done
  preflight_error "clusterissuer '$name' not Ready within the bounded window — refusing the next PKI tier (D-03)"
  return 1
}

wait_for_certificate_ready() {  # wait_for_certificate_ready <name> <namespace> — bounded
  # Certificate Ready=True wait (D-03); fails closed naming the certificate —
  # Nginx is never submitted without TLS material.
  local name="$1" namespace="$2" i
  for i in $(seq 1 "$CERT_READY_ATTEMPTS"); do
    if _kubectl_indramesh get certificate "$name" -n "$namespace" \
         -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}' 2>/dev/null | grep -q True; then
      step "cert-manager: certificate '$name' Ready"
      return 0
    fi
    sleep "$CERT_READY_INTERVAL"
  done
  preflight_error "certificate '$name' not Ready within the bounded window — refusing to deploy Nginx without TLS material (D-03)"
  return 1
}

reconcile_pki() {  # reconcile_pki — apply the two-tier SelfSigned→CA PKI in the fixed
  # order (D-03), bounded-waiting every tier Ready before the next. kubectl
  # apply is idempotent for re-runs; a retained root (indramesh-ca-root,
  # renewal.policy Disabled) survives indramesh release recreation because no
  # --enable-certificate-owner-ref is ever passed (research 200).
  local ns="${1:-${NAMESPACE_NAME:-indramesh}}"
  step "cert-manager: reconciling the two-tier PKI (D-03)"

  _kubectl_indramesh apply -f - <<'YAML' || { preflight_error "cert-manager: failed to apply ClusterIssuer indramesh-selfsigned (D-03)"; return 1; }
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: indramesh-selfsigned
spec:
  selfSigned: {}
YAML
  wait_for_clusterissuer_ready indramesh-selfsigned || return 1

  _kubectl_indramesh apply -f - <<'YAML' || { preflight_error "cert-manager: failed to apply Certificate indramesh-ca-root (D-03)"; return 1; }
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: indramesh-ca-root
  namespace: cert-manager
spec:
  isCA: true
  commonName: indramesh-local-ca
  duration: 87600h
  renewal:
    policy: Disabled
  privateKey:
    algorithm: ECDSA
    size: 256
  secretName: indramesh-ca-root
  issuerRef:
    name: indramesh-selfsigned
    kind: ClusterIssuer
    group: cert-manager.io
YAML
  wait_for_certificate_ready indramesh-ca-root cert-manager || return 1

  _kubectl_indramesh apply -f - <<'YAML' || { preflight_error "cert-manager: failed to apply ClusterIssuer indramesh-ca (D-03)"; return 1; }
apiVersion: cert-manager.io/v1
kind: ClusterIssuer
metadata:
  name: indramesh-ca
spec:
  ca:
    secretName: indramesh-ca-root
YAML
  wait_for_clusterissuer_ready indramesh-ca || return 1

  # Unquoted heredoc delimiter: the leaf namespace ($ns) and the optional host
  # IP SAN are parameterized; the YAML contains no other $-expansions or
  # backticks, so expansion is safe. HOST_IP passed detect_host_ip's IPv4 check.
  local host_ip_san=""
  if [ -n "${HOST_IP:-}" ]; then
    host_ip_san="    - ${HOST_IP}"
  fi
  _kubectl_indramesh apply -f - <<YAML || { preflight_error "cert-manager: failed to apply Certificate indramesh-local-tls (D-03)"; return 1; }
apiVersion: cert-manager.io/v1
kind: Certificate
metadata:
  name: indramesh-local-tls
  namespace: $ns
spec:
  dnsNames:
    - localhost
  ipAddresses:
    - 127.0.0.1
${host_ip_san}
  duration: 8760h
  renewBeforePercentage: 33
  privateKey:
    algorithm: ECDSA
    size: 256
    rotationPolicy: Always
  secretName: indramesh-local-tls
  issuerRef:
    name: indramesh-ca
    kind: ClusterIssuer
    group: cert-manager.io
YAML
  wait_for_certificate_ready indramesh-local-tls "$ns" || return 1

  step "cert-manager: two-tier PKI reconciled — leaf Ready (D-03)"
  return 0
}