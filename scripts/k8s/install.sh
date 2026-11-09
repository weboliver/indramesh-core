#!/bin/bash
# install.sh — root installer orchestration for the Phase 254 k3s bootstrap
# (D-01..D-04, D-07, D-08, D-10..D-15, D-21, D-23, D-24; T-254-01..04).
# Orchestration only: sources the lib/ modules and drives one explicit
# fail-fast sequence. It runs non-interactively from the repository root,
# uses targeted sudo only (D-14), and never prints secret values (T-254-02).
#
# Sequence (254-VALIDATION.md 254-05-01; 255-01 task 1; 259-04 task 2):
#   1. D-24 gate: every key defined in root .env.example must exist in .env
#   2. platform adapter (native sysctl; WSL prerequisites; restart/rerun stop)
#   3. verified artifact acquisition (D-12)
#   4. k3s install-or-reuse + bounded systemd/API/node readiness (D-11, D-15)
#   5. namespace gate: installer-owned `indramesh` PSA restricted label,
#      repaired and read back before Helm (D-01, D-02, SEC-01)
#   6. split-Secret materialization from .env before Helm (D-05, D-06, SEC-02)
#   7. cert-manager provision + two-tier PKI reconcile, leaf Ready=True before
#      any Nginx rollout (D-03, D-07; cert_manager.sh). The monitoring stack
#      (kube-prometheus-stack; MON-01, monitoring.sh) and its Grafana credential
#      check (MON-04) are ONE opt-in unit gated on MONITORING_ENABLED exactly
#      `true` (311.1 D-06); when it is on it MUST run before the full Helm pass
#      so the monitoring.coreos.com/v1 CRDs exist when the chart renders the
#      ServiceMonitor (anti-pattern research line 296-298). The log stack
#      provisions the signed, pinned Loki community chart and the SHA-256-pinned
#      Alloy release (LOGINFRA-03/04, D-02/D-04/D-10/D-11/D-12; loki.sh,
#      alloy.sh) only when LOGINFRA_ENABLED is exactly `true` — the lean
#      default (311.1 D-06/D-09). A switched-off component is never removed:
#      an existing release keeps running, unmanaged.
#   8. Helm upgrade-or-install of the locked chart: stateful-only bootstrap
#      pass, fresh-install ownership fixup, local image acquisition when
#      absent (A2) + full-pass boot values (nginx-enabled + acquired tags +
#      networkPolicies.apiServerCidr), then the full apps-enabled pass
#      (D-02, D-16, D-21; Plan 03 decision 2)
#   9. retained CA root export + consumer trust-store import (D-08;
#      trust_store.sh) BEFORE the no-bypass endpoint probe
#  10. endpoint contract handoff + platform reachability adapter (D-08, D-23)
#  11. helm test smoke hooks (OPS-01, D-09..D-11): the explicit `helm test`
#      after the full pass — test hooks run ONLY on helm test, never during
#      install/upgrade (Pitfall 5); a red smoke hook exits the install non-zero
#
# The full pass uses --wait --wait-for-jobs (helm.sh:340-344), so the smoke
# targets (backend:8000/health, postgres:5432) are Ready before helm test runs.
#
# A failure at any stage stops every later stage (fail-fast, D-04) and the
# partial state stays available for diagnosis; a later rerun resumes without
# destroying cluster/PVC state (D-02). Only the declared `deferred` adapter
# result completes the bootstrap foundation with endpoint verification
# pending; every ready-probe failure propagates non-zero and can never become
# a success claim (D-08, D-09, D-23).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
K8S_DIR="$(dirname "$SCRIPT_DIR")"
ROOT_DIR="$(cd "$K8S_DIR/.." && pwd)"

. "$LIB_DIR/common.sh"
. "$LIB_DIR/platform.sh"
. "$LIB_DIR/resource_profiles.sh"
. "$LIB_DIR/artifacts.sh"
. "$LIB_DIR/k3s.sh"
. "$LIB_DIR/helm.sh"
. "$LIB_DIR/reachability.sh"
. "$LIB_DIR/local_forward.sh"
. "$LIB_DIR/namespace.sh"
. "$LIB_DIR/secrets.sh"
. "$LIB_DIR/cert_manager.sh"
. "$LIB_DIR/monitoring.sh"
. "$LIB_DIR/loki.sh"
. "$LIB_DIR/alloy.sh"
. "$LIB_DIR/images.sh"
. "$LIB_DIR/trust_store.sh"
. "$LIB_DIR/stateful_storage.sh"
# (The installer-owned local TLS flow is removed from the Kubernetes path per
# D-03 — the cert-manager leaf Secret owns the TLS material; the manual
# tlsContentHash annotation path survives in rebuild.sh via the deployed
# checksum/secret annotation read, D-02.)

# Hermetic test seams (default to the real host files).
ENV_FILE="${INDRAMESH_ENV_FILE:-$ROOT_DIR/.env}"
ENV_SCHEMA_FILE="${INDRAMESH_ENV_SCHEMA_FILE:-$ROOT_DIR/.env.example}"
OS_RELEASE_FILE="${INDRAMESH_OS_RELEASE_FILE:-/etc/os-release}"
WSL_CONF_FILE="${INDRAMESH_WSL_CONF_FILE:-/etc/wsl.conf}"
WSL_CONFIG_FILE="${INDRAMESH_WSL_CONFIG_FILE:-}"
SYSCTL_DIR="${INDRAMESH_SYSCTL_DIR:-/etc/sysctl.d}"
MOUNTS_FILE="${INDRAMESH_MOUNTS_FILE:-/proc/mounts}"
CHECKSUMS_FILE="${INDRAMESH_CHECKSUMS_FILE:-$K8S_DIR/checksums.sha256}"
VERSIONS_FILE="${INDRAMESH_VERSIONS_FILE:-$K8S_DIR/versions.env}"

load_versions "$VERSIONS_FILE" || die "install" "failed to load pinned versions (D-10)"
if [ -n "${INDRAMESH_CHART_PATH:-}" ]; then
  CHART_DIR="$INDRAMESH_CHART_PATH"
else
  CHART_DIR="$ROOT_DIR/$HELM_CHART_PATH"
fi

# --- Phase 311.1 D-01: the .env bootstrap (k8s quickstart = one command) -----
# Create .env from .env.example when the configured env-file path is absent —
# BEFORE the APP_ENVIRONMENT read below (a fresh clone must read the created
# file, not silently default) and before the D-24 schema gate (a clone with no
# .env must never die there). Mode 600; an existing file is never overwritten.
# The fill follows the schema gate, and both run before any host mutation.
step "install: environment file"
if [ -f "$ENV_FILE" ]; then
  echo "  .env already exists — nothing is overwritten; only empty or placeholder values are filled"
else
  [ -f "$ENV_SCHEMA_FILE" ] || die "install" ".env.example not found at $ENV_SCHEMA_FILE — run this from a repository clone"
  umask 077
  cp "$ENV_SCHEMA_FILE" "$ENV_FILE" || die "install" "could not create $ENV_FILE from $ENV_SCHEMA_FILE"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "  created .env from .env.example"
fi

# Phase 322 D-02: an existing 1.0.0 .env gains ONLY the two missing DB_APP_*
# lines before the schema gate below runs, so validate_env_schema sees every
# template key and fill_env_placeholders can generate DB_APP_PASSWORD. An
# existing value is never rewritten, reordered or dropped.
append_missing_app_db_keys "$ENV_FILE" "$ENV_SCHEMA_FILE" || exit 1

# Phase 272/K8SFIX-03/04: single ENVIRONMENT source for BOTH stacks — the same
# root .env variable the Compose stack uses drives the k8s frontend build
# (VITE_ENVIRONMENT baked at build time) and the in-cluster backend/worker
# ENVIRONMENT (via write_full_pass_boot_values). Default production.
APP_ENVIRONMENT="$(dotenv_value "$ENV_FILE" "ENVIRONMENT" 2>/dev/null || true)"
[ -n "$APP_ENVIRONMENT" ] || APP_ENVIRONMENT="production"

private_tmp TMP_DIR
trap cleanup_private_tmp_on_exit EXIT

step "install: IndraMesh k3s bootstrap"
step "install: validating root .env schema (D-24)"
validate_env_schema "$ENV_FILE" "$ENV_SCHEMA_FILE" || {
  echo "install: fix the missing key(s) above and rerun ./install_indramesh_k8s.sh (D-24)" >&2
  exit 1
}

# --- Phase 311.1 D-01: fill the placeholders with the shared generator -------
# Docker is a hard prerequisite of the k8s quickstart: the fill's
# container-backed generation (FERNET_KEY/JWT_SECRET_KEY) and the later local
# image build both need Docker Engine + the Compose plugin (D-01). Preflight
# them before the fill, so a missing Docker aborts with a named prerequisite
# instead of the fill's opaque "secret generation produced no value" message.
step "install: preflight — Docker Engine + Compose plugin (D-01)"
docker info </dev/null >/dev/null 2>&1 \
  || die "install" "the Docker daemon is not reachable — Docker Engine and the Docker Compose plugin are prerequisites of the k8s quickstart (the container-backed key generation and the local image build both use them)"
docker compose version </dev/null >/dev/null 2>&1 \
  || die "install" "the Docker Compose plugin is not available — Docker Engine and the Docker Compose plugin are prerequisites of the k8s quickstart"

step "install: generating secrets with the shared generator (D-01)"
fill_env_placeholders "$ENV_FILE" || exit 1
echo "  filled ${GENERATED_SECRET_COUNT} secret value(s)"

# The fill's own closing loop fails closed on a value it did not reach; this
# explicit gate re-checks the mandatory set through the shared normalising
# reader right after it. It sits BEFORE require_sudo and
# ensure_bootstrap_packages — the first host mutation (ensure_bootstrap_packages
# apt-installs) — so a run that would have aborted on an empty secret never
# installs a package or k3s first (D-02/D-03). The reconciliation entry point
# carries the same gate for every other caller; these two call sites are
# deliberate (closes the 308.2 WR-01 gate that existed but was never walked on
# the install path).
step "install: verifying the mandatory secrets are set (D-02, D-03)"
require_mandatory_env_secrets "$ENV_FILE" || exit 1

# --- Phase 311.1 D-06: the lean quickstart component switches ----------------
# The quickstart installs Core, the stateful services and TLS; the monitoring
# stack, Headlamp and the log stack are opt-in (D-06). All three switches are
# read here, once, next to the log-stack dotenv block's home — earlier than that
# block so the monitoring gate further down sees its switch, and before any host
# mutation, so an operator learns what will be skipped before anything changes.
# Each defaults to the string `false` when absent, and ONLY the exact string
# `true` enables a component — the same strict-literal convention
# OTLP_TLS_INSECURE already uses; any other value (including empty) means off.
# Switching a component off only stops managing it: no branch below removes or
# deletes an existing release (D-09).
MONITORING_ENABLED="$(dotenv_value "$ENV_FILE" "MONITORING_ENABLED" 2>/dev/null || true)"
[ -n "$MONITORING_ENABLED" ] || MONITORING_ENABLED="false"
HEADLAMP_ENABLED="$(dotenv_value "$ENV_FILE" "HEADLAMP_ENABLED" 2>/dev/null || true)"
[ -n "$HEADLAMP_ENABLED" ] || HEADLAMP_ENABLED="false"
LOGINFRA_ENABLED="$(dotenv_value "$ENV_FILE" "LOGINFRA_ENABLED" 2>/dev/null || true)"
[ -n "$LOGINFRA_ENABLED" ] || LOGINFRA_ENABLED="false"
export MONITORING_ENABLED HEADLAMP_ENABLED LOGINFRA_ENABLED

# Plugin DB boundary (Phase 311.1 clean-host finding). The .env carries the
# non-secret role name and the password, and Compose hands both to the backend
# directly (backend/start.sh reads the same file); on k8s the chart needs the
# role name as `plugins.dbUser` — the migration Jobs AND the backend Deployment
# gate the PLUGIN_DB_* render on it, and the plugin-install migrate exec runs in
# that Deployment. Read here, after fill_env_placeholders, so the password is a
# real generated value. The password itself is never written anywhere by the
# installer (reconcile_indramesh_secrets puts it in the Secret); only its
# PRESENCE is carried into the values writer, which is what keeps the documented
# "role name without a password is fatal" case unreachable.
PLUGIN_DB_USER_VALUE="$(dotenv_value "$ENV_FILE" "PLUGIN_DB_USER" 2>/dev/null || true)"
PLUGIN_DB_PASSWORD_PRESENT=0
[ -n "$(dotenv_value "$ENV_FILE" "PLUGIN_DB_PASSWORD" 2>/dev/null || true)" ] && PLUGIN_DB_PASSWORD_PRESENT=1
export PLUGIN_DB_USER_VALUE PLUGIN_DB_PASSWORD_PRESENT

# Phase 322 D-04: the non-secret application role name, read out of THIS
# installation's .env after append_missing_app_db_keys and
# fill_env_placeholders, so an upgrade that just appended the key is picked up
# in the same run. write_full_pass_boot_values consumes only shell variables and
# never reads .env, so without this read a real install renders
# `postgres.appUser: ""` and the backend/worker silently fall back to DB_USER.
# Only the role name travels; the password stays in the Secret (T-322-05).
read_app_db_user_value "$ENV_FILE"

# Phase 318 (K8S-LLM-EGRESS-01): the .env LLM endpoint and the operator's extra
# egress targets. On Compose the backend reads LLM_* straight from the .env; on
# k8s the chart pinned them, so a non-Mistral provider in the .env was silently
# ignored. write_runtime_values carries them over and derives the
# networkPolicies.externalEgress entries a private endpoint needs.
LLM_PROVIDER_VALUE="$(env_file_value "$ENV_FILE" "LLM_PROVIDER")"
LLM_BASE_URL_VALUE="$(env_file_value "$ENV_FILE" "LLM_BASE_URL")"
LLM_MODEL_VALUE="$(env_file_value "$ENV_FILE" "LLM_MODEL")"
LLM_MODE_VALUE="$(env_file_value "$ENV_FILE" "LLM_MODE")"
K8S_EGRESS_ALLOW_VALUE="$(env_file_value "$ENV_FILE" "K8S_EGRESS_ALLOW")"
export LLM_PROVIDER_VALUE LLM_BASE_URL_VALUE LLM_MODEL_VALUE LLM_MODE_VALUE K8S_EGRESS_ALLOW_VALUE

detect_platform "$OS_RELEASE_FILE"
require_sudo || exit 1

step "install: ensuring bootstrap packages (D-13)"
ensure_bootstrap_packages || exit 1

step "install: preparing the host platform"
platform_prepare "$OS_RELEASE_FILE" "$WSL_CONF_FILE" "$WSL_CONFIG_FILE" "$SYSCTL_DIR" "$MOUNTS_FILE" || {
  rc=$?
  if [ "$rc" -eq "$WSL_RESTART_EXIT" ]; then
    echo "install: WSL configuration changed — restart WSL and rerun ./install_indramesh_k8s.sh (D-07)" >&2
  fi
  exit "$rc"
}

detect_host_ip || exit 1
export HOST_IP
if [ -n "$HOST_IP" ]; then
  step "install: host IP $HOST_IP is added to the certificate and the routes (INDRAMESH_HOST_IP= switches it off)"
fi

step "install: acquiring verified k3s/Helm artifacts (D-12)"
acquire_artifacts "$PLATFORM_ARCH" "$TMP_DIR" "$CHECKSUMS_FILE" "$VERSIONS_FILE" || exit 1

step "install: installing or reusing k3s (D-11)"
install_or_reuse_k3s "$VERIFIED_INSTALLER" "$VERIFIED_K3S_BINARY" "$PLATFORM_ARCH" || exit 1
ensure_k3s_running || exit 1
step "install: waiting for k3s readiness (D-15)"
wait_for_k3s_readiness || exit 1

step "install: reconciling the '$HELM_NAMESPACE' namespace PSA label (D-01)"
reconcile_indramesh_namespace || exit 1

step "install: materializing split Secrets from .env (D-05, D-06)"
reconcile_indramesh_secrets "$ENV_FILE" || exit 1

step "install: provisioning cert-manager (D-07)"
provision_cert_manager "$VERSIONS_FILE" "$CHECKSUMS_FILE" "$TMP_DIR" "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" || exit 1

# --- Phase 311.1 D-06: the monitoring stack is opt-in -------------------------
# provision_monitoring_stack and verify_grafana_credentials are ONE unit: the
# credential check authenticates against the Grafana that the provisioning step
# created, so a lean run (MONITORING_ENABLED not exactly `true`) must skip both —
# splitting them would probe a Grafana that does not exist. A switched-off run
# leaves an existing kube-prometheus-stack release untouched: it simply stops
# managing it (D-09). Nothing here removes or deletes a release.
if [ "$MONITORING_ENABLED" = "true" ]; then
  step "install: provisioning kube-prometheus-stack (MON-01)"
  provision_monitoring_stack "$VERSIONS_FILE" "$CHECKSUMS_FILE" "$TMP_DIR" "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" || exit 1

  step "install: verifying Grafana admin credentials against the live API (MON-04, D-04)"
  verify_grafana_credentials || exit 1
else
  step "install: monitoring stack provisioning skipped (MONITORING_ENABLED=$MONITORING_ENABLED; D-06) — an existing kube-prometheus-stack release is left untouched"
fi

# Log stack provisioning (LOGINFRA-03/04, D-02/D-04/D-10/D-11/D-12): the pinned
# signed Loki community chart plus the SHA-256-pinned Alloy release beside
# kube-prometheus-stack. Both releases share ONE env model read from the root
# .env (mirroring Compose, LOGINFRA-04) with fail-safe defaults:
#   LOGINFRA_ENABLED=true   installs the whole log stack; the lean default in
#                           .env.example is `false` and ANY other value is off —
#                           only the exact string `true` enables it (311.1 D-06).
#                           A switched-off run leaves existing Loki/Alloy
#                           releases untouched; the Grafana Loki datasource stays
#                           in place and points into empty space (documented in
#                           docs/LOGGING-INFRASTRUCTURE.md, Review-Korrektur 5)
#   LOKI_RETENTION_PERIOD   Helm-values retention override (default 336h / D-04)
#   ALLOY_SINK              checked-in sink fragment selector (default loki)
#   OTLP_ENDPOINT / OTLP_AUTHORIZATION  external OTLP target (D-02)
#   OTLP_TLS_INSECURE       shared `false` = TLS default in BOTH stacks; only
#                           the explicit string `true` enables plaintext gRPC
#                           (D-02 mirror, Plan 01/02)
#   LOKI_URL                bundled push endpoint override (default in alloy.sh)
LOKI_RETENTION_PERIOD="$(dotenv_value "$ENV_FILE" "LOKI_RETENTION_PERIOD" 2>/dev/null || true)"
[ -n "$LOKI_RETENTION_PERIOD" ] || LOKI_RETENTION_PERIOD="336h"
ALLOY_SINK="$(dotenv_value "$ENV_FILE" "ALLOY_SINK" 2>/dev/null || true)"
[ -n "$ALLOY_SINK" ] || ALLOY_SINK="loki"
OTLP_ENDPOINT="$(dotenv_value "$ENV_FILE" "OTLP_ENDPOINT" 2>/dev/null || true)"
OTLP_AUTHORIZATION="$(dotenv_value "$ENV_FILE" "OTLP_AUTHORIZATION" 2>/dev/null || true)"
OTLP_TLS_INSECURE="$(dotenv_value "$ENV_FILE" "OTLP_TLS_INSECURE" 2>/dev/null || true)"
[ -n "$OTLP_TLS_INSECURE" ] || OTLP_TLS_INSECURE="false"
export LOKI_RETENTION_PERIOD ALLOY_SINK \
  OTLP_ENDPOINT OTLP_AUTHORIZATION OTLP_TLS_INSECURE
if [ "$LOGINFRA_ENABLED" = "true" ]; then
  step "install: provisioning the log stack — Loki (LOGINFRA-03, D-11)"
  provision_loki_stack "$VERSIONS_FILE" "$CHECKSUMS_FILE" "$TMP_DIR" "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" || exit 1
  step "install: provisioning the log stack — Alloy (LOGINFRA-03/04, D-10/D-11)"
  provision_alloy_stack "$VERSIONS_FILE" "$CHECKSUMS_FILE" "$TMP_DIR" "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" || exit 1
else
  step "install: log stack provisioning skipped (LOGINFRA_ENABLED=$LOGINFRA_ENABLED; D-06/D-11) — existing Loki/Alloy releases are left untouched"
fi

step "install: reconciling the two-tier PKI (D-03)"
reconcile_pki || exit 1

step "install: selecting stateful resource profile (D-14 through D-17)"
select_resource_profile || exit 1
write_resource_profile_values "$TMP_DIR/stateful-resource-profile.yaml" || exit 1

step "install: materializing private apps.enabled toggle values (D-16; 311.1 D-06 component flags)"
# The writer also emits this run's monitoring.enabled and headlamp.enabled from
# the exported switches below, so the stateful-only pass never renders a
# Headlamp Deployment whose never-pulled image was skipped (311.1 D-06).
write_app_toggle_values "$TMP_DIR" || exit 1

step "install: preparing stateful local-path ownership before Helm (D-06 companion)"
prepare_stateful_local_path_ownership || exit 1

# Stateful wrapper images (Phase 311.1 clean-host finding; Phase 312.1: three
# wrappers). The stateful-only pass below renders postgres-0/valkey-0/opensearch-0
# from the chart's LOCAL wrapper refs (values.yaml postgres.image / valkey.image /
# opensearch.image) with imagePullPolicy IfNotPresent. No public registry carries
# those names, so a bare host — a fresh containerd, as in the cold clean-host
# quickstart proof — lands in ImagePullBackOff and the platform never comes up.
# They are built from deploy/k8s-images/stateful/{postgres,valkey,opensearch} and
# imported BEFORE the pass, exactly like the app images below are for the full
# pass. The refs are read from the chart itself, so the tag this loop builds can
# never disagree with the tag the pods reference; an unreadable ref exits the
# install rather than guessing (fail closed).
step "install: acquiring the stateful wrapper images when absent (Phase 311.1)"
for _stateful_section in postgres valkey opensearch; do
  _stateful_repo="$(_chart_image_field "$CHART_DIR/values.yaml" "$_stateful_section" repository)"
  _stateful_tag="$(_chart_image_field "$CHART_DIR/values.yaml" "$_stateful_section" tag)"
  [ -n "$_stateful_repo" ] && [ -n "$_stateful_tag" ] || {
    preflight_error "install: could not read $_stateful_section.image repository/tag from the chart values — refusing to guess the stateful wrapper ref (Phase 311.1)"
    exit 1
  }
  _stateful_ref="$(normalize_image_ref "$_stateful_repo:$_stateful_tag")"
  if containerd_exact_ref "$_stateful_ref" >/dev/null 2>&1; then
    step "install: image $_stateful_ref already present in containerd (Phase 311.1)"
  else
    step "install: acquiring local stateful image $_stateful_repo:$_stateful_tag (Phase 311.1)"
    build_image "$_stateful_repo" "$_stateful_tag" || exit 1
    import_image_stream "$_stateful_repo:$_stateful_tag" || exit 1
    containerd_exact_ref "$_stateful_ref" >/dev/null 2>&1 \
      || { preflight_error "install: imported stateful image not found in containerd: $_stateful_ref (Phase 311.1)"; exit 1; }
  fi
done

step "install: reconciling the Helm release — stateful-only bootstrap pass (D-02, D-16)"
HELM_BIN="$(helm_binary_path "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" "$TMP_DIR")" || exit 1
if reconcile_helm_release_stateful_pass "$CHART_DIR" "$HELM_BIN"; then
  RECONCILE_STATEFUL_RC=0
else
  RECONCILE_STATEFUL_RC=$?
fi
case "$RECONCILE_STATEFUL_RC" in
  0)
    # Fresh install: pass 1 just created the stateful PVCs without waiting for
    # readiness (D-16 fresh-install fixup) — fix host-dir ownership now, in
    # this same invocation, before the full pass blocks on Postgres readiness.
    step "install: fixing fresh-install stateful ownership before the full pass (D-16 fresh-install fixup)"
    fresh_install_stateful_ownership_fixup || exit 1
    ;;
  1) ;; # existing release — nothing to fix, go straight to the full pass
  *) exit 1 ;; # hard failure, already diagnosed by reconcile_helm_release_stateful_pass
esac

# Fresh-install local image acquisition (decision 1 / A2, research Pitfall 6):
# the full pass renders the three app images with imagePullPolicy Never, so a
# genuinely bare host needs them imported into containerd BEFORE the full pass
# — the final BOOT-03 probe cannot pass without a running nginx. Each repo in
# the images.sh central mapping is checked by exact containerd ref; when
# absent, build + import + re-verify (D-16/D-17). A failure exits the install.
step "install: acquiring the local app images when absent (A2)"
FRESH_INSTALL_TAG="$(tool_image_tag)" || exit 1
INSTALL_ACQUIRED_TAGS=()
for repo in indramesh-backend indramesh-frontend-static indramesh-nginx; do
  install_ref="$(normalize_image_ref "$repo:$FRESH_INSTALL_TAG")"
  if containerd_exact_ref "$install_ref" >/dev/null 2>&1; then
    step "install: image $install_ref already present in containerd (A2)"
  else
    step "install: acquiring local image $repo:$FRESH_INSTALL_TAG (A2)"
    if [ "$repo" = "indramesh-frontend-static" ]; then
      build_image "$repo" "$FRESH_INSTALL_TAG" --build-arg "VITE_ENVIRONMENT=$APP_ENVIRONMENT" || exit 1
    else
      build_image "$repo" "$FRESH_INSTALL_TAG" || exit 1
    fi
    import_image_stream "$repo:$FRESH_INSTALL_TAG" || exit 1
    containerd_exact_ref "$install_ref" >/dev/null 2>&1 \
      || { preflight_error "install: imported image not found in containerd: $install_ref (D-17)"; exit 1; }
  fi
  INSTALL_ACQUIRED_TAGS+=("$repo=$FRESH_INSTALL_TAG")
done

# Headlamp image acquisition (Plan 262-02, D-02/D-03, research Pitfall 5; 311.1
# D-06 re-gate): the chart renders the headlamp Deployment with
# imagePullPolicy: Never (Phase 258 convention) whenever headlamp.enabled — a
# fresh install must import the pinned upstream image into containerd BEFORE the
# full Helm pass below, or the pod sits in ImagePullBackOff and `--wait` times
# out. This MUST run before reconcile_helm_release_full_pass; position is
# load-bearing, exactly like the app-image loop above.
#
# The gate is the operator's HEADLAMP_ENABLED switch, NOT the chart's own
# values.yaml: `_chart_headlamp_enabled` reads the chart FILE, which is also
# what the chart renders, so a chart-default flip would silently disable image
# acquisition for an operator who switched Headlamp ON. The switch is the single
# input (research Pitfall 4) — both this gate and the enabled flag the full-pass
# values writer emits read the same HEADLAMP_ENABLED.
# Upstream image: pull, never build (no build_image call — HEADLAMP_IMAGE/
# HEADLAMP_VERSION come exclusively from the pins loaded via load_versions, never
# hardcoded here; D-04: no secret/token material in the installer).
if [ "$HEADLAMP_ENABLED" = "true" ]; then
  headlamp_install_ref="$(normalize_image_ref "$HEADLAMP_IMAGE:$HEADLAMP_VERSION")"
  if containerd_exact_ref "$headlamp_install_ref" >/dev/null 2>&1; then
    step "install: image $headlamp_install_ref already present in containerd (D-02)"
  else
    step "install: acquiring the pinned upstream headlamp image $HEADLAMP_IMAGE:$HEADLAMP_VERSION (D-02)"
    docker pull "$HEADLAMP_IMAGE:$HEADLAMP_VERSION" \
      || { preflight_error "install: failed to pull the pinned headlamp image $HEADLAMP_IMAGE:$HEADLAMP_VERSION (D-02)"; exit 1; }
    import_image_stream "$HEADLAMP_IMAGE:$HEADLAMP_VERSION" || exit 1
    containerd_exact_ref "$headlamp_install_ref" >/dev/null 2>&1 \
      || { preflight_error "install: imported headlamp image not found in containerd: $headlamp_install_ref (D-17)"; exit 1; }
  fi
else
  step "install: headlamp image acquisition skipped (HEADLAMP_ENABLED=$HEADLAMP_ENABLED; D-06) — the chart-owned Headlamp Deployment is pruned by the next full pass"
fi

# Full-pass boot values (decision 2): apps.nginx.enabled + the freshly
# acquired image tags + the installer-discovered networkPolicies.apiServerCidr
# (default/kubernetes ClusterIP as /32, fail-closed on discovery failure while
# the policy layer is enabled — Plan 03 decision 2, research Pitfall 5). Phase
# 311.1 D-06: the writer also emits this run's monitoring.enabled and
# headlamp.enabled from the exported switches, so the chart's monitoring-gated
# objects and its Headlamp Deployment/RBAC agree with the gate that decided
# whether the components were provisioned.
APISERVER_CIDR=""
if [ "$(_chart_networkpolicies_enabled "$CHART_DIR/values.yaml")" = "true" ]; then
  step "install: discovering the kube-apiserver ClusterIP for networkPolicies.apiServerCidr (Plan 03 decision 2)"
  APISERVER_IP="$(_kubectl_indramesh get svc kubernetes -n default -o jsonpath='{.spec.clusterIP}' 2>/dev/null)" || {
    preflight_error "install: failed to discover the kube-apiserver ClusterIP — networkPolicies.apiServerCidr cannot be derived (Plan 03 decision 2)"
    exit 1
  }
  [ -n "$APISERVER_IP" ] || {
    preflight_error "install: kube-apiserver ClusterIP discovery returned empty — refusing to proceed without networkPolicies.apiServerCidr (Plan 03 decision 2)"
    exit 1
  }
  APISERVER_CIDR="$APISERVER_IP/32"
fi
step "install: materializing the full-pass boot values (decision 2)"
write_full_pass_boot_values "$TMP_DIR" "$APISERVER_CIDR" "${INSTALL_ACQUIRED_TAGS[@]}" || exit 1
# Phase 318 (K8S-CONFIG-ROLLOUT-01, K8S-LLM-EGRESS-01): the Secret revision is
# read here, after reconcile_indramesh_secrets, so a key this run created rolls
# backend/worker in the full pass below.
write_runtime_values "$TMP_DIR" "$(external_api_secret_revision)" || exit 1

step "install: reconciling the Helm release — full pass (D-02)"
if ! reconcile_helm_release_full_pass "$CHART_DIR" "$HELM_BIN" "$HELM_UPGRADE_TIMEOUT" "$RUNTIME_VALUES_FILE" "$FULL_PASS_BOOT_VALUES_FILE"; then
  preflight_error "install: full pass failed — if the error above mentions postgres-0/pgdata readiness or a backup-pvc-init/migration hook Job not ready, this can be a known kubelet subPath-ownership race on a fresh install (D-16 fresh-install fixup; the chown can land before kubelet creates the pgdata subPath on the pod's first real mount). Simply re-running ./install_indramesh_k8s.sh is the documented, idempotent, non-destructive recovery — it re-chowns the now-existing subPath before this pass runs again."
  exit 1
fi
read_endpoint_contract "$CHART_DIR/values.yaml" || exit 1

step "install: running helm test smoke hooks (OPS-01)"
# NOTE: no --logs flag — Helm v4.2.4's --logs looks up a pod named exactly
# indramesh-smoke-test, but Jobs generate suffixed pod names (indramesh-smoke-test-xxxxx),
# so --logs always fails with 'pods ... not found' and would abort the install
# even though the suite Succeeded (261-05 Rule 1 fix, live-verified). The smoke
# diagnostics stay retrievable via kubectl logs on the retained Job pod
# (hook-delete-policy before-hook-creation only).
sudo env KUBECONFIG="$K3S_KUBECONFIG" "$HELM_BIN" test "$HELM_RELEASE_NAME" -n "$HELM_NAMESPACE" || exit 1

step "install: installing the retained CA root into the trust store (D-08)"
export_ca_root "$TMP_DIR/indramesh-ca.crt" || exit 1
install_ca_trust "$TMP_DIR/indramesh-ca.crt" || exit 1

print_install_banner() {  # print_install_banner — closing URL/trust/login/conditional-password summary (311.1 D-05)
  # The UI URL comes from the endpoint contract the installer just resolved and
  # verified above — never a hard-coded literal and never the raw port.
  local url="${HELM_ENDPOINT_URL:-}"
  echo "install: $(endpoint_result_message)"
  step "install: bootstrap complete — next: ./start and ./stop manage the k3s service"
  if [ -n "$url" ]; then
    echo "  URL:    $url"
  else
    echo "  URL:    not published yet — the bootstrap endpoint contract is not ready (see bootstrap.endpoint in the chart values)"
  fi
  if [ -n "${HOST_IP:-}" ]; then
    echo "          Also reachable at https://${HOST_IP} from other machines on the network."
  fi
  echo "          The bundled certificate is self-signed — accept the browser warning."
  echo "          The installer imported the retained CA root into this host's trust store (D-08)."
  if [ "${PLATFORM_WSL:-0}" -eq 1 ]; then
    echo "          The Windows browser reaches the same URL through WSL's localhost forwarding."
  fi
  echo "  Login:  admin"
  # The password line is conditional on the fill HAVING GENERATED it this run
  # (D-05, mirroring 311 D-10): a later run prints no value at all and points at
  # the env file, so no pre-existing credential ever reaches stdout.
  if [ -n "${GENERATED_ADMIN_PASSWORD:-}" ]; then
    echo "  Password: ${GENERATED_ADMIN_PASSWORD}"
    echo "          Generated by this run. It is used only when the admin user is first"
    echo "          created; changing ADMIN_PASSWORD in .env later has no effect — change"
    echo "          the password in the UI instead."
  else
    echo "  Password: already set before this run — see ADMIN_PASSWORD in .env"
  fi
}

step "install: endpoint contract handoff (D-23)"
verify_endpoint "$HELM_ENDPOINT_STATUS" "$HELM_ENDPOINT_URL" || {
  echo "install: endpoint verification failed — the installer cannot claim endpoint success (BOOT-03)" >&2
  exit 1
}

print_install_banner
