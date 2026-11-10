#!/bin/bash
# rebuild.sh — registry-free rebuild->reimport->redeploy orchestration for
# the Phase 258 k3s bootstrap (IMG-03, D-07, D-09..D-25). Sourceable-library
# consumer, not sourceable itself: mirrors install.sh's structure (lib
# sourcing, private_tmp lifecycle, step-labelled fail-fast, set -euo
# pipefail) but reconciles ONLY the full Helm pass against an EXISTING
# release (D-23) — it never runs the stateful bootstrap pass and never
# implicitly reinstalls.
#
# Sequence (D-24, fail-fast at every step, D-19):
#   1. preflight: cluster reachable, platform detected, release present (D-23)
#   2. build selected images (D-13/D-14/D-15), one shared tag per run (D-09)
#   3. import + exact-match verify every selected image (D-16/D-17)
#   4. [nginx selector] in-cluster `nginx -t` validation Pod against the
#      cert-manager leaf Secret material, existing pods untouched on failure
#      (D-03/D-07)
#   5. write the private image-tags values file carrying the FULL RUNNING
#      CONTRACT: fresh tags for selected repos, the currently-deployed tags
#      for every non-selected repo (fail-closed read), the deployed release
#      values (helm get values, fail-closed) re-asserting every non-image
#      operator override (stateful profile, nginxTrustedIp, corsAllowedOrigins,
#      resources, wait), and nginx.enabled + the deployed tlsContentHash
#      whenever the nginx Deployment exists or nginx is selected (D-22/D-25,
#      WR-01) — otherwise `--reset-values` would roll non-selected workloads
#      and every non-image override back to the chart defaults (Pitfall 9)
#   6. one Helm reconciliation: the full pass with this values file appended
#      right-most (D-24/D-25) — never the stateful pass, never `--set`
#   7. verify rollout + the Phase 256 pod-image ref-chain for every affected
#      Deployment (D-10/D-24)
#   8. cleanup obsolete tool-owned images only after step 7 is fully green
#      (D-18) — never a currently-deployed tag, never a phase-NNN baseline
#
# A build/import/validation/Helm/rollout failure stops every later step
# (fail-fast, D-19); cleanup never runs on a non-green loop. Uses only the
# private image-tags values file to move tags — no `kubectl set image`, no
# deployment patch, no `helm uninstall` anywhere in this file (D-25, repo
# destructive-verb ban).
set -euo pipefail

# rebuild.sh lives directly in scripts/k8s/ — one level above lib/*.sh and
# two levels below the repository root. K8S_DIR is SCRIPT_DIR itself (the
# checksums/versions/nginx-validate-pod.yaml seams all sit beside this
# script); ROOT_DIR is two levels up. This matches install.sh's depth math:
# install.sh sets K8S_DIR="$(dirname "$SCRIPT_DIR")" (one level up) and
# ROOT_DIR=K8S_DIR/.. — for a script at this same depth, SCRIPT_DIR/../..
# is the identical repo-root resolution.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
K8S_DIR="$SCRIPT_DIR"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

. "$LIB_DIR/common.sh"
. "$LIB_DIR/platform.sh"
. "$LIB_DIR/artifacts.sh"
. "$LIB_DIR/helm.sh"
. "$LIB_DIR/secrets.sh"
. "$LIB_DIR/images.sh"
# (The installer-owned local TLS flow is removed from the Kubernetes path per
# D-03 — the cert-manager leaf Secret owns the TLS material; the tlsContentHash
# annotation value below comes from the deployed checksum/secret annotation
# read only, D-02)

# Defense-in-depth (WR-02): images.sh overwrites the global ROOT_DIR with its
# own lib-relative depth math — assert the result is still the repository root
# so a future source reorder can never silently break the ROOT_DIR-relative
# seams below (CHART_DIR).
[ -f "$ROOT_DIR/rebuild_indramesh_k8s.sh" ] \
  || die "rebuild" "ROOT_DIR resolved to '$ROOT_DIR' — expected the repository root (WR-02)"

# Hermetic test seams (default to the real host files) — mirrors install.sh's
# convention exactly.
CHECKSUMS_FILE="${INDRAMESH_CHECKSUMS_FILE:-$K8S_DIR/checksums.sha256}"
VERSIONS_FILE="${INDRAMESH_VERSIONS_FILE:-$K8S_DIR/versions.env}"
HELM_UPGRADE_TIMEOUT="${HELM_UPGRADE_TIMEOUT:-10m}"
NGINX_VALIDATE_TIMEOUT="${INDRAMESH_NGINX_VALIDATE_TIMEOUT:-120s}"
ROLLOUT_TIMEOUT="${INDRAMESH_REBUILD_ROLLOUT_TIMEOUT:-180s}"
NGINX_VALIDATE_MANIFEST="${INDRAMESH_NGINX_VALIDATE_MANIFEST:-$SCRIPT_DIR/nginx-validate-pod.yaml}"

load_versions "$VERSIONS_FILE" || die "rebuild" "failed to load pinned versions (D-10)"
if [ -n "${INDRAMESH_CHART_PATH:-}" ]; then
  CHART_DIR="$INDRAMESH_CHART_PATH"
else
  CHART_DIR="$ROOT_DIR/$HELM_CHART_PATH"
fi

# Phase 272/K8SFIX-03/04: single ENVIRONMENT source for BOTH stacks. The same
# root .env variable the Compose stack derives VITE_ENVIRONMENT/environment
# from also drives the k8s build + Helm reconciliation — production default,
# `development` bakes the GraphQL entry/endpoint back in on the k8s stack too.
ENV_FILE="${INDRAMESH_ENV_FILE:-$ROOT_DIR/.env}"
APP_ENVIRONMENT="$(dotenv_value "$ENV_FILE" "ENVIRONMENT" 2>/dev/null || true)"
[ -n "$APP_ENVIRONMENT" ] || APP_ENVIRONMENT="production"

kubectl_indramesh() { sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"; }

# ---------------------------------------------------------------------------
# Selector parsing (D-21/D-22) — zero or more of backend|frontend|nginx in
# any order, plus an optional --no-cache (Phase 272): `rebuild.sh backend
# frontend` builds both, mirroring `docker compose build backend frontend`.
# No `worker` selector exists; `backend` selects both Backend and Worker
# together (they share one image, Pitfall 6). No selector = all three.
# --cutover (Phase 296, D-10) enables the one-way cut-over pre-upgrade hooks
# (apps.maintenance.cutover) for THIS run only; every other run re-asserts it
# as false so a value captured by `helm get values` never stays sticky.
# ---------------------------------------------------------------------------
NO_CACHE_FLAG=""
MAINTENANCE_CUTOVER=false
BUILD_BACKEND=0
BUILD_FRONTEND=0
BUILD_NGINX=0
SELECTORS_SPECIFIED=0
for arg in "$@"; do
  [ -n "$arg" ] || continue
  case "$arg" in
    --no-cache) NO_CACHE_FLAG="--no-cache" ;;
    --cutover) MAINTENANCE_CUTOVER=true ;;
    backend) BUILD_BACKEND=1; SELECTORS_SPECIFIED=1 ;;
    frontend) BUILD_FRONTEND=1; SELECTORS_SPECIFIED=1 ;;
    nginx) BUILD_NGINX=1; SELECTORS_SPECIFIED=1 ;;
    *)
      echo "usage: rebuild.sh [backend|frontend|nginx ...] [--no-cache] [--cutover]" >&2
      exit 2
      ;;
  esac
done
if [ "$SELECTORS_SPECIFIED" -eq 0 ]; then
  BUILD_BACKEND=1
  BUILD_FRONTEND=1
  BUILD_NGINX=1
fi

SELECTED_REPOS=()
[ "$BUILD_BACKEND" -eq 1 ] && SELECTED_REPOS+=(indramesh-backend)
[ "$BUILD_FRONTEND" -eq 1 ] && SELECTED_REPOS+=(indramesh-frontend-static)
[ "$BUILD_NGINX" -eq 1 ] && SELECTED_REPOS+=(indramesh-nginx)

# ---------------------------------------------------------------------------
# Private temp dir + disposable nginx-validation cleanup — ONE combined EXIT
# trap (bash only honors the most-recently-registered EXIT trap), covering
# both the TMP_DIR lifecycle (common.sh convention) and the unconditional
# delete of the validation Pod + ConfigMap on every exit (D-07: success,
# failure, or interruption alike).
# ---------------------------------------------------------------------------
private_tmp TMP_DIR
NGINX_VALIDATE_CREATED=0
NGINX_VALIDATE_POD_NAME="indramesh-nginx-validate"
NGINX_VALIDATE_CONFIGMAP_NAME="indramesh-nginx-validate"

rebuild_cleanup_on_exit() {
  local work_status=$? cleanup_status=0
  if [ "$NGINX_VALIDATE_CREATED" -eq 1 ]; then
    kubectl_indramesh delete pod "$NGINX_VALIDATE_POD_NAME" -n "$HELM_NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
    kubectl_indramesh delete configmap "$NGINX_VALIDATE_CONFIGMAP_NAME" -n "$HELM_NAMESPACE" --ignore-not-found >/dev/null 2>&1 || true
  fi
  cleanup_private_tmp || cleanup_status=$?
  if [ "$work_status" -ne 0 ]; then
    exit "$work_status"
  fi
  exit "$cleanup_status"
}
trap rebuild_cleanup_on_exit EXIT

step "rebuild: IndraMesh k3s rebuild loop (selectors='${*:-all}')"

# ---------------------------------------------------------------------------
# Step 1: preflight — cluster reachable, platform detected, release present
# (D-14, D-23). Any failure here stops before any build (D-19).
# ---------------------------------------------------------------------------
step "rebuild: preflight — cluster reachable, release present (D-23)"
kubectl_indramesh get nodes >/dev/null || die "rebuild" "cannot reach the k3s cluster — run ./install_indramesh_k8s.sh first (D-23)"

detect_platform
[ "$PLATFORM_ARCH" != "unknown" ] || die "rebuild" "unable to detect the host architecture (D-14)"

# Helm binary (D-11/Pitfall 7): only acquire+verify the pinned archive when a
# compatible Helm isn't already on PATH — mirrors tests/bootstrap/helm_bin.sh's
# own short-circuit (never a bare `helm`, never an unconditional download).
HELM_INSTALLED_VERSION="$(helm_installed_version)"
if [ "$(helm_install_decision "$HELM_INSTALLED_VERSION")" != "keep" ]; then
  step "rebuild: acquiring the verified pinned Helm artifact (D-12)"
  acquire_artifacts "$PLATFORM_ARCH" "$TMP_DIR" "$CHECKSUMS_FILE" "$VERSIONS_FILE" || exit 1
fi
HELM_BIN="$(helm_binary_path "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" "$TMP_DIR")" || exit 1

if _helm_release_is_absent "$HELM_BIN"; then
  HELM_RELEASE_STATE=0
else
  HELM_RELEASE_STATE=$?
fi
case "$HELM_RELEASE_STATE" in
  0) die "rebuild" "no existing Helm release '$HELM_RELEASE_NAME' found — run ./install_indramesh_k8s.sh first (D-23)" ;;
  1) ;; # release present — the only state this wrapper reconciles
  *) exit 1 ;; # hard failure, already diagnosed by _helm_release_is_absent
esac

# ---------------------------------------------------------------------------
# Step 2: build selected images — ONE shared tag per run (D-09/D-10: the
# build marker is the run, not the individual image).
# ---------------------------------------------------------------------------
TAG="$(tool_image_tag)" || exit 1
step "rebuild: building selected images with tag $TAG (D-13/D-14/D-15)"
for repo in "${SELECTED_REPOS[@]}"; do
  if [ "$repo" = "indramesh-frontend-static" ]; then
    build_image "$repo" "$TAG" "$NO_CACHE_FLAG" --build-arg "VITE_ENVIRONMENT=$APP_ENVIRONMENT" || exit 1
  else
    build_image "$repo" "$TAG" "$NO_CACHE_FLAG" || exit 1
  fi
done

# ---------------------------------------------------------------------------
# Step 3: import + exact-match verify every selected image (D-16/D-17)
# before any Helm step touches the release.
# ---------------------------------------------------------------------------
step "rebuild: importing + verifying selected images (D-16/D-17)"
for repo in "${SELECTED_REPOS[@]}"; do
  import_image_stream "$repo:$TAG" || exit 1
  IMPORTED_REF="$(normalize_image_ref "$repo:$TAG")"
  containerd_exact_ref "$IMPORTED_REF" \
    || { preflight_error "rebuild: imported image not found in containerd: $IMPORTED_REF (D-17)"; exit 1; }
done

# ---------------------------------------------------------------------------
# Step 4: nginx runtime assets (nginx selector only) — the disposable
# in-cluster `nginx -t` validation (D-07, Pitfall 1: this can never run
# host-side — nginx resolves upstream DNS eagerly). The validation Pod mounts
# the cert-manager leaf Secret (indramesh-local-tls, D-03) so it validates the
# exact TLS material Helm will deploy.
# ---------------------------------------------------------------------------
# WR-01: read the currently-deployed release values into a private contract
# file (fail-closed, mirroring deployed_refs_from_pods's discipline): a
# non-NotFound read failure aborts before Helm rather than silently
# re-asserting an empty contract and letting --reset-values revert operator
# overrides. The D-23 release-present gate above guarantees the release
# exists, so ANY failure here is a hard error.
DEPLOYED_VALUES_FILE="$TMP_DIR/deployed-values.yaml"
if ! sudo env KUBECONFIG="$K3S_KUBECONFIG" "$HELM_BIN" get values "$HELM_RELEASE_NAME" -n "$HELM_NAMESPACE" -o yaml > "$DEPLOYED_VALUES_FILE" 2>"$TMP_DIR/helm-get-values.err"; then
  preflight_error "rebuild: failed to read the deployed release values (WR-01): $(cat "$TMP_DIR/helm-get-values.err")"
  exit 1
fi

if [ "$BUILD_NGINX" -eq 1 ]; then
  # The validation input is the nginx.conf Helm will deploy, rendered with the
  # deployed values above — never the raw chart file (render_chart_nginx_conf).
  NGINX_CONF_FILE="$TMP_DIR/nginx-validate.conf"
  step "rebuild: rendering the chart's nginx.conf with the deployed values (D-07)"
  render_chart_nginx_conf "$HELM_BIN" "$CHART_DIR" "$DEPLOYED_VALUES_FILE" "$NGINX_CONF_FILE" || exit 1

  step "rebuild: creating the disposable nginx -t validation ConfigMap (D-07)"
  kubectl_indramesh create configmap "$NGINX_VALIDATE_CONFIGMAP_NAME" -n "$HELM_NAMESPACE" \
    --from-file=nginx.conf="$NGINX_CONF_FILE" \
    --dry-run=client -o yaml | kubectl_indramesh apply -f - || exit 1
  NGINX_VALIDATE_CREATED=1

  RENDERED_POD_MANIFEST="$TMP_DIR/nginx-validate-pod.yaml"
  sed "s/__TAG__/$TAG/" "$NGINX_VALIDATE_MANIFEST" > "$RENDERED_POD_MANIFEST"

  step "rebuild: applying the disposable in-cluster nginx -t validation Pod (D-07)"
  kubectl_indramesh apply -f "$RENDERED_POD_MANIFEST" || exit 1

  step "rebuild: waiting for nginx -t validation to complete (D-07, bounded $NGINX_VALIDATE_TIMEOUT)"
  if ! kubectl_indramesh wait --for=jsonpath='{.status.phase}'=Succeeded "pod/$NGINX_VALIDATE_POD_NAME" \
    -n "$HELM_NAMESPACE" --timeout="$NGINX_VALIDATE_TIMEOUT"; then
    NGINX_VALIDATE_PHASE="$(kubectl_indramesh get pod "$NGINX_VALIDATE_POD_NAME" -n "$HELM_NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || true)"
    preflight_error "rebuild: nginx -t validation failed (phase=${NGINX_VALIDATE_PHASE:-unknown}) — config or TLS material is invalid; existing pods are untouched (D-07)"
    kubectl_indramesh logs "$NGINX_VALIDATE_POD_NAME" -n "$HELM_NAMESPACE" >&2 || true
    exit 1
  fi
  step "rebuild: nginx -t validation succeeded (D-07)"
fi

# ---------------------------------------------------------------------------
# Step 5: write the private image-tags values file — the FULL RUNNING
# CONTRACT (D-22/D-25, Pitfall 9). `--reset-values` resets the release to
# chart defaults before any --values file applies, so a focused run must
# re-assert every non-selected workload's CURRENTLY-DEPLOYED tag (read
# fail-closed) plus nginx.enabled/tlsContentHash whenever the nginx
# Deployment exists — otherwise a focused run would roll non-selected
# images back to the phase-257 chart defaults and silently disable nginx.
# WR-01: --reset-values ALSO discards every NON-image operator override
# (stateful resource profile, nginxTrustedIp, corsAllowedOrigins,
# apps.*.resources, apps.*.wait) that the image-tags subtree does not
# re-assert. The full running contract therefore additionally carries the
# currently-deployed release values (helm get values, fail-closed) so the
# rebuild re-asserts them byte-for-byte — passed BEFORE the image-tags file
# so Helm's deep merge restores the deployed contract while the right-most
# image-tags file still moves the selected tags.
# ---------------------------------------------------------------------------
IMAGE_TAGS_FILE="$TMP_DIR/image-tags.yaml"
step "rebuild: writing the private image-tags values file — full running contract (D-22/D-25)"

if [ "$BUILD_BACKEND" -eq 1 ]; then
  # D-22: backend selection unconditionally moves BOTH Backend and Worker to
  # this run's fresh tag together — no separate worker key exists
  # (worker-deployment.yaml consumes apps.backend.image.*, Pitfall 6), and no
  # backend/worker drift check runs in this branch (review finding C1): the
  # pre-run worker ref is about to be replaced regardless, so comparing it
  # against a tag only just built this run can never be a meaningful check.
  BACKEND_TAG="$TAG"
else
  deployed_refs_from_pods "$HELM_NAMESPACE" indramesh-backend indramesh-worker > "$TMP_DIR/backend-worker-refs.txt" || exit 1
  mapfile -t BACKEND_WORKER_REFS < "$TMP_DIR/backend-worker-refs.txt"
  [ "${#BACKEND_WORKER_REFS[@]}" -eq 2 ] \
    || { preflight_error "rebuild: could not resolve both the backend and worker currently-deployed image refs (D-25)"; exit 1; }
  BACKEND_DEPLOYED_REF="${BACKEND_WORKER_REFS[0]}"
  WORKER_DEPLOYED_REF="${BACKEND_WORKER_REFS[1]}"
  # Drift check (scoped to backend-NOT-selected only, D-22/review finding
  # C1): a mismatch means the release is already drifted and must be
  # repaired deliberately — never silently re-pinned by this run's
  # non-selected-repo re-assertion of backend's currently-deployed tag.
  [ "$BACKEND_DEPLOYED_REF" = "$WORKER_DEPLOYED_REF" ] \
    || { preflight_error "rebuild: backend/worker deployed image refs differ ('$BACKEND_DEPLOYED_REF' vs '$WORKER_DEPLOYED_REF') — the release is already drifted; repair deliberately before rerunning (D-22)"; exit 1; }
  BACKEND_TAG="${BACKEND_DEPLOYED_REF##*:}"
fi

if [ "$BUILD_FRONTEND" -eq 1 ]; then
  FRONTEND_TAG="$TAG"
else
  deployed_refs_from_pods "$HELM_NAMESPACE" indramesh-frontend > "$TMP_DIR/frontend-ref.txt" || exit 1
  FRONTEND_DEPLOYED_REF="$(cat "$TMP_DIR/frontend-ref.txt")"
  [ -n "$FRONTEND_DEPLOYED_REF" ] \
    || { preflight_error "rebuild: could not resolve the currently-deployed frontend image ref (D-25)"; exit 1; }
  FRONTEND_TAG="${FRONTEND_DEPLOYED_REF##*:}"
fi

# nginx block: written whenever the nginx Deployment currently exists OR
# nginx is selected — never silently disabled/deleted by a focused run.
NGINX_DEPLOYMENT_EXISTS=0
kubectl_indramesh get deployment nginx -n "$HELM_NAMESPACE" >/dev/null 2>&1 && NGINX_DEPLOYMENT_EXISTS=1

NGINX_WRITE_BLOCK=0
NGINX_TAG=""
NGINX_TLS_HASH=""
if [ "$BUILD_NGINX" -eq 1 ]; then
  NGINX_WRITE_BLOCK=1
  NGINX_TAG="$TAG"
elif [ "$NGINX_DEPLOYMENT_EXISTS" -eq 1 ]; then
  NGINX_WRITE_BLOCK=1
  deployed_refs_from_pods "$HELM_NAMESPACE" nginx > "$TMP_DIR/nginx-ref.txt" || exit 1
  NGINX_DEPLOYED_REF="$(cat "$TMP_DIR/nginx-ref.txt")"
  [ -n "$NGINX_DEPLOYED_REF" ] \
    || { preflight_error "rebuild: nginx Deployment exists but its deployed image ref could not be resolved (D-25)"; exit 1; }
  NGINX_TAG="${NGINX_DEPLOYED_REF##*:}"
fi
if [ "$NGINX_WRITE_BLOCK" -eq 1 ]; then
  # D-03: the cert-manager leaf Secret owns the TLS material, so the deployed
  # checksum/secret annotation is the ONLY tlsContentHash source — the
  # manual/reinstall annotation path survives (D-02). The bracket-notation
  # form below is deliberate (review finding C3): a backslash-escaped '\/' is
  # NOT a valid client-go jsonpath escape for a slash-containing key — only
  # ['checksum/secret'] correctly addresses it. A not-yet-deployed nginx
  # (NotFound) contributes an empty hash (the chart default).
  if ! NGINX_TLS_HASH="$(kubectl_indramesh get deployment nginx -n "$HELM_NAMESPACE" \
    -o "jsonpath={.spec.template.metadata.annotations['checksum/secret']}" 2>&1)"; then
    case "$NGINX_TLS_HASH" in
      *NotFound*|*"not found"*)
        NGINX_TLS_HASH=""
        ;;
      *)
        preflight_error "rebuild: failed to read the nginx Deployment's checksum/secret annotation (D-08): $NGINX_TLS_HASH"
        exit 1
        ;;
    esac
  fi
fi

{
  printf 'apps:\n'
  # Phase 272/K8SFIX-04: the single ENVIRONMENT signal is part of the full
  # running contract — without re-asserting it here, `--reset-values` would
  # roll the in-cluster backend/worker ENVIRONMENT back to the chart default.
  printf '  environment: %s\n' "$APP_ENVIRONMENT"
  printf '  maintenance:\n'
  printf '    cutover: %s\n' "$MAINTENANCE_CUTOVER"
  printf '  backend:\n'
  printf '    image:\n'
  printf '      tag: %s\n' "$BACKEND_TAG"
  printf '  frontend:\n'
  printf '    image:\n'
  printf '      tag: %s\n' "$FRONTEND_TAG"
  if [ "$NGINX_WRITE_BLOCK" -eq 1 ]; then
    printf '  nginx:\n'
    printf '    enabled: true\n'
    printf '    image:\n'
    printf '      tag: %s\n' "$NGINX_TAG"
    printf '    tlsContentHash: %s\n' "$NGINX_TLS_HASH"
  fi
} > "$IMAGE_TAGS_FILE"

# ---------------------------------------------------------------------------
# Step 6: one Helm reconciliation — the full pass only, with the deployed
# values contract and the image-tags file appended right-most in that order
# (D-24/D-25, WR-01): Helm deep-merges --values files left-to-right with the
# last one winning per key, so the deployed contract re-asserts every
# non-image operator override against --reset-values while the right-most
# image-tags file still moves the selected tags. Never the stateful pass
# (D-23 guarantees the release exists); never `--set`.
# ---------------------------------------------------------------------------
step "rebuild: reconciling the Helm release — full pass with the deployed contract + image-tags (D-24/D-25)"
reconcile_helm_release_full_pass "$CHART_DIR" "$HELM_BIN" "$HELM_UPGRADE_TIMEOUT" "$DEPLOYED_VALUES_FILE" "$IMAGE_TAGS_FILE" || exit 1

# ---------------------------------------------------------------------------
# Step 7: verify rollout + the Phase 256 pod-image ref-chain for every
# AFFECTED Deployment (D-10/D-24). A mismatch stops before cleanup.
# ---------------------------------------------------------------------------
AFFECTED_DEPLOYMENTS=()
if [ "$BUILD_BACKEND" -eq 1 ]; then
  AFFECTED_DEPLOYMENTS+=(indramesh-backend indramesh-worker)
fi
if [ "$BUILD_FRONTEND" -eq 1 ]; then
  AFFECTED_DEPLOYMENTS+=(indramesh-frontend)
fi
if [ "$BUILD_NGINX" -eq 1 ]; then
  AFFECTED_DEPLOYMENTS+=(nginx)
fi
if [ ${#AFFECTED_DEPLOYMENTS[@]} -eq 0 ]; then
  AFFECTED_DEPLOYMENTS=(indramesh-backend indramesh-worker indramesh-frontend nginx)
fi

declare -A EXPECTED_REPO_FOR_DEPLOYMENT=(
  [indramesh-backend]=indramesh-backend
  [indramesh-worker]=indramesh-backend
  [indramesh-frontend]=indramesh-frontend-static
  [nginx]=indramesh-nginx
)
declare -A EXPECTED_TAG_FOR_DEPLOYMENT=(
  [indramesh-backend]="$BACKEND_TAG"
  [indramesh-worker]="$BACKEND_TAG"
  [indramesh-frontend]="$FRONTEND_TAG"
  [nginx]="$NGINX_TAG"
)

step "rebuild: verifying rollout + build markers (D-10/D-24)"
for dep in "${AFFECTED_DEPLOYMENTS[@]}"; do
  kubectl_indramesh rollout status "deployment/$dep" -n "$HELM_NAMESPACE" --timeout="$ROLLOUT_TIMEOUT" || exit 1
  EXPECTED_REF="docker.io/library/${EXPECTED_REPO_FOR_DEPLOYMENT[$dep]}:${EXPECTED_TAG_FOR_DEPLOYMENT[$dep]}"
  IMAGE_FIELD="$(kubectl_indramesh get deployment "$dep" -n "$HELM_NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}')"
  ACTUAL_REF="$(normalize_image_ref "$IMAGE_FIELD")"
  [ "$ACTUAL_REF" = "$EXPECTED_REF" ] \
    || { preflight_error "rebuild: deployment/$dep image field '$IMAGE_FIELD' (normalized '$ACTUAL_REF') != expected canonical ref '$EXPECTED_REF' (D-10/D-24)"; exit 1; }
  step "rebuild: deployment/$dep verified at $EXPECTED_REF (ref-chain proof, D-10)"
done

# ---------------------------------------------------------------------------
# Step 8: cleanup — only after step 7 is fully green (D-18). Reads ALL four
# Deployments (nginx NotFound-tolerated) so a non-selected currently-deployed
# tag is always subtracted and never removed.
# ---------------------------------------------------------------------------
deployed_refs_from_pods "$HELM_NAMESPACE" indramesh-backend indramesh-worker indramesh-frontend nginx > "$TMP_DIR/all-deployed-refs.txt" || exit 1
mapfile -t ALL_DEPLOYED_REFS < "$TMP_DIR/all-deployed-refs.txt"
step "rebuild: cleaning up obsolete tool-owned images (D-18)"
cleanup_obsolete_tool_images "${ALL_DEPLOYED_REFS[@]}" || exit 1

step "rebuild: complete — deployed refs:"
for dep in "${AFFECTED_DEPLOYMENTS[@]}"; do
  echo "  $dep -> docker.io/library/${EXPECTED_REPO_FOR_DEPLOYMENT[$dep]}:${EXPECTED_TAG_FOR_DEPLOYMENT[$dep]}"
done
echo "rebuild: next — kubectl get pods -n $HELM_NAMESPACE (or ./start/./stop to manage the k3s service)"
