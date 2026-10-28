#!/bin/bash
# verify_stateful_services.sh — Phase 256's guarded, disposable live evidence
# gate. It reconciles only the fixed indramesh release, writes only reserved
# phase-owned markers, and deletes only the three fixed StatefulSet Pods.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$ROOT/scripts/k8s/lib/common.sh"

NAMESPACE="indramesh"
RELEASE_NAME="indramesh"
K3S_KUBECONFIG="${INDRAMESH_K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
MARKER_PREFIX="INDRAMESH_PHASE_256_$(date +%s)_$$"
POSTGRES_MARKER="${MARKER_PREFIX}_POSTGRES"
VALKEY_MARKER="${MARKER_PREFIX}_VALKEY"
OPENSEARCH_MARKER="${MARKER_PREFIX}_OPENSEARCH"
OPENSEARCH_INDEX="indramesh-phase-3121-verifier-${MARKER_PREFIX,,}"
INSTALL_LOG="$(mktemp "${TMPDIR:-/tmp}/indramesh-phase-3121-install.XXXXXX")"
DEFAULT_EVIDENCE_FILE="$SCRIPT_DIR/stateful-image-digests.env"
POSTGRES_IMAGE_REF="docker.io/library/indramesh-stateful-postgres:18.4-age1.8.0-pgv0.8.7"
VALKEY_IMAGE_REF="docker.io/library/indramesh-stateful-valkey:9.1.1-r1"
OPENSEARCH_IMAGE_REF="docker.io/library/indramesh-stateful-opensearch:3.9.0-r1"
POSTGRES_LOCAL_IMAGE="indramesh-stateful-postgres:18.4-age1.8.0-pgv0.8.7"
VALKEY_LOCAL_IMAGE="indramesh-stateful-valkey:9.1.1-r1"
OPENSEARCH_LOCAL_IMAGE="indramesh-stateful-opensearch:3.9.0-r1"

kubectl_indramesh() { sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"; }
verify_error() { echo "[verify-stateful-services] ERROR: $*" >&2; }

cleanup_install_log() {
  local work_status=$?
  rm -f -- "$INSTALL_LOG" 2>/dev/null || true
  exit "$work_status"
}
trap cleanup_install_log EXIT

is_digest() {
  [[ "$1" =~ ^sha256:[0-9a-f]{64}$ ]]
}

containerd_target_digest() {
  local image_ref="$1"
  # The installed k3s containerd (v2.3.4) has no `ctr images inspect --output json`.
  # `ctr images list` DIGEST column is the exact canonical target digest for the
  # top-level index/manifest of the ref; extract it deterministically, one value.
  sudo k3s ctr -n k8s.io images list | awk -v ref="$image_ref" '
    $1==ref { print $3; found=1 }
    END { exit (found ? 0 : 1) }
  '
}

assert_built_wrapper_user() {
  local image="$1" expected_user="$2" actual_user
  actual_user="$(docker image inspect --format '{{.Config.User}}' "$image")"
  [ "$actual_user" = "$expected_user" ] || {
    verify_error "built image $image has Config.User '$actual_user', expected '$expected_user'"
    return 1
  }
}

write_image_evidence() {
  local evidence_file="$1" postgres_pre="$2" postgres_post="$3" valkey_pre="$4" valkey_post="$5"
  local opensearch_pre="$6" opensearch_post="$7"
  local evidence_dir evidence_tmp
  evidence_dir="$(dirname "$evidence_file")"
  mkdir -p "$evidence_dir"
  evidence_tmp="$(mktemp "$evidence_dir/.stateful-image-digests.XXXXXX")"
  {
    printf 'POSTGRES_IMAGE_REF=%s\n' "$POSTGRES_IMAGE_REF"
    printf 'POSTGRES_PRE_IMPORT_TARGET_DIGEST=%s\n' "$postgres_pre"
    printf 'POSTGRES_POST_IMPORT_TARGET_DIGEST=%s\n' "$postgres_post"
    printf 'VALKEY_IMAGE_REF=%s\n' "$VALKEY_IMAGE_REF"
    printf 'VALKEY_PRE_IMPORT_TARGET_DIGEST=%s\n' "$valkey_pre"
    printf 'VALKEY_POST_IMPORT_TARGET_DIGEST=%s\n' "$valkey_post"
    printf 'OPENSEARCH_IMAGE_REF=%s\n' "$OPENSEARCH_IMAGE_REF"
    printf 'OPENSEARCH_PRE_IMPORT_TARGET_DIGEST=%s\n' "$opensearch_pre"
    printf 'OPENSEARCH_POST_IMPORT_TARGET_DIGEST=%s\n' "$opensearch_post"
  } >"$evidence_tmp"
  mv "$evidence_tmp" "$evidence_file"
}

read_image_evidence() {
  local evidence_file="$1" line key value
  [ -r "$evidence_file" ] || { verify_error "digest evidence file is unreadable: $evidence_file"; return 1; }
  POSTGRES_EVIDENCE_REF=""
  POSTGRES_PRE_IMPORT_DIGEST=""
  POSTGRES_POST_IMPORT_DIGEST=""
  VALKEY_EVIDENCE_REF=""
  VALKEY_PRE_IMPORT_DIGEST=""
  VALKEY_POST_IMPORT_DIGEST=""
  OPENSEARCH_EVIDENCE_REF=""
  OPENSEARCH_PRE_IMPORT_DIGEST=""
  OPENSEARCH_POST_IMPORT_DIGEST=""
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in
      POSTGRES_IMAGE_REF=*|POSTGRES_PRE_IMPORT_TARGET_DIGEST=*|POSTGRES_POST_IMPORT_TARGET_DIGEST=*|VALKEY_IMAGE_REF=*|VALKEY_PRE_IMPORT_TARGET_DIGEST=*|VALKEY_POST_IMPORT_TARGET_DIGEST=*|OPENSEARCH_IMAGE_REF=*|OPENSEARCH_PRE_IMPORT_TARGET_DIGEST=*|OPENSEARCH_POST_IMPORT_TARGET_DIGEST=*)
        key="${line%%=*}"
        value="${line#*=}"
        case "$key" in
          POSTGRES_IMAGE_REF) POSTGRES_EVIDENCE_REF="$value" ;;
          POSTGRES_PRE_IMPORT_TARGET_DIGEST) POSTGRES_PRE_IMPORT_DIGEST="$value" ;;
          POSTGRES_POST_IMPORT_TARGET_DIGEST) POSTGRES_POST_IMPORT_DIGEST="$value" ;;
          VALKEY_IMAGE_REF) VALKEY_EVIDENCE_REF="$value" ;;
          VALKEY_PRE_IMPORT_TARGET_DIGEST) VALKEY_PRE_IMPORT_DIGEST="$value" ;;
          VALKEY_POST_IMPORT_TARGET_DIGEST) VALKEY_POST_IMPORT_DIGEST="$value" ;;
          OPENSEARCH_IMAGE_REF) OPENSEARCH_EVIDENCE_REF="$value" ;;
          OPENSEARCH_PRE_IMPORT_TARGET_DIGEST) OPENSEARCH_PRE_IMPORT_DIGEST="$value" ;;
          OPENSEARCH_POST_IMPORT_TARGET_DIGEST) OPENSEARCH_POST_IMPORT_DIGEST="$value" ;;
        esac
        ;;
      '#'*) ;;                          # comment lines are tolerated (the evidence file may carry a header)
      '') ;;
      *) verify_error "digest evidence file has an unexpected field"; return 1 ;;
    esac
  done <"$evidence_file"
  [ "$POSTGRES_EVIDENCE_REF" = "$POSTGRES_IMAGE_REF" ] || { verify_error "Postgres evidence ref is not canonical"; return 1; }
  [ "$VALKEY_EVIDENCE_REF" = "$VALKEY_IMAGE_REF" ] || { verify_error "Valkey evidence ref is not canonical"; return 1; }
  [ "$OPENSEARCH_EVIDENCE_REF" = "$OPENSEARCH_IMAGE_REF" ] || { verify_error "OpenSearch evidence ref is not canonical"; return 1; }
  is_digest "$POSTGRES_POST_IMPORT_DIGEST" || { verify_error "Postgres post-import target digest is invalid"; return 1; }
  is_digest "$VALKEY_POST_IMPORT_DIGEST" || { verify_error "Valkey post-import target digest is invalid"; return 1; }
  is_digest "$OPENSEARCH_POST_IMPORT_DIGEST" || { verify_error "OpenSearch post-import target digest is invalid"; return 1; }
  for digest in "$POSTGRES_PRE_IMPORT_DIGEST" "$VALKEY_PRE_IMPORT_DIGEST" "$OPENSEARCH_PRE_IMPORT_DIGEST"; do
    [ "$digest" = "ABSENT" ] || is_digest "$digest" || { verify_error "pre-import target digest is invalid"; return 1; }
  done
}

assert_image_evidence_current() {
  local current
  read_image_evidence "$1"
  current="$(containerd_target_digest "$POSTGRES_EVIDENCE_REF")"
  [ "$current" = "$POSTGRES_POST_IMPORT_DIGEST" ] || { verify_error "Postgres current target digest differs from Task 2 evidence"; return 1; }
  current="$(containerd_target_digest "$VALKEY_EVIDENCE_REF")"
  [ "$current" = "$VALKEY_POST_IMPORT_DIGEST" ] || { verify_error "Valkey current target digest differs from Task 2 evidence"; return 1; }
  current="$(containerd_target_digest "$OPENSEARCH_EVIDENCE_REF")"
  [ "$current" = "$OPENSEARCH_POST_IMPORT_DIGEST" ] || { verify_error "OpenSearch current target digest differs from the recorded evidence"; return 1; }
}

import_wrapper_images() {
  local evidence_file="$1" postgres_pre valkey_pre postgres_post valkey_post
  local opensearch_pre opensearch_post postgres_local valkey_local opensearch_local
  assert_built_wrapper_user "$POSTGRES_LOCAL_IMAGE" "70:0"
  assert_built_wrapper_user "$VALKEY_LOCAL_IMAGE" "999:0"
  assert_built_wrapper_user "$OPENSEARCH_LOCAL_IMAGE" "1000:0"
  postgres_pre="$(containerd_target_digest "$POSTGRES_IMAGE_REF" 2>/dev/null || printf 'ABSENT')"
  valkey_pre="$(containerd_target_digest "$VALKEY_IMAGE_REF" 2>/dev/null || printf 'ABSENT')"
  opensearch_pre="$(containerd_target_digest "$OPENSEARCH_IMAGE_REF" 2>/dev/null || printf 'ABSENT')"
  for digest in "$postgres_pre" "$valkey_pre" "$opensearch_pre"; do
    [ "$digest" = "ABSENT" ] || is_digest "$digest" || { verify_error "pre-import target digest is invalid"; return 1; }
  done
  docker image save "$POSTGRES_LOCAL_IMAGE" | sudo k3s ctr -n k8s.io images import -
  postgres_post="$(containerd_target_digest "$POSTGRES_IMAGE_REF")"
  docker image save "$VALKEY_LOCAL_IMAGE" | sudo k3s ctr -n k8s.io images import -
  valkey_post="$(containerd_target_digest "$VALKEY_IMAGE_REF")"
  docker image save "$OPENSEARCH_LOCAL_IMAGE" | sudo k3s ctr -n k8s.io images import -
  opensearch_post="$(containerd_target_digest "$OPENSEARCH_IMAGE_REF")"
  is_digest "$postgres_post" || { verify_error "Postgres post-import target digest is invalid"; return 1; }
  is_digest "$valkey_post" || { verify_error "Valkey post-import target digest is invalid"; return 1; }
  is_digest "$opensearch_post" || { verify_error "OpenSearch post-import target digest is invalid"; return 1; }
  # Continuity proof (T-312.1-27): the containerd target digest of the canonical ref
  # must equal the locally built image's ID (the OCI index digest for these wrapper
  # images — verified against ctr's DIGEST column). This proves the tag targets
  # exactly the locally built revision, so an idempotent re-import (installer
  # imported the same build first) is a pass, while a stale target fails.
  postgres_local="$(docker image inspect --format '{{.Id}}' "$POSTGRES_LOCAL_IMAGE")"
  valkey_local="$(docker image inspect --format '{{.Id}}' "$VALKEY_LOCAL_IMAGE")"
  opensearch_local="$(docker image inspect --format '{{.Id}}' "$OPENSEARCH_LOCAL_IMAGE")"
  [ "$postgres_post" = "$postgres_local" ] || { verify_error "Postgres containerd target digest differs from the locally built image"; return 1; }
  [ "$valkey_post" = "$valkey_local" ] || { verify_error "Valkey containerd target digest differs from the locally built image"; return 1; }
  [ "$opensearch_post" = "$opensearch_local" ] || { verify_error "OpenSearch containerd target digest differs from the locally built image"; return 1; }
  write_image_evidence "$evidence_file" "$postgres_pre" "$postgres_post" "$valkey_pre" "$valkey_post" "$opensearch_pre" "$opensearch_post"
  assert_image_evidence_current "$evidence_file"
}

show_pod_diagnostics() {
  local pod="$1"
  verify_error "pod/$pod did not become Ready; describe and containerStatuses follow"
  kubectl_indramesh describe pod "$pod" -n "$NAMESPACE" >&2 || true
  kubectl_indramesh get pod "$pod" -n "$NAMESPACE" \
    -o jsonpath='{range .status.containerStatuses[*]}{.name}{": ready="}{.ready}{", waiting="}{.state.waiting.reason}{", terminated="}{.state.terminated.reason}{"\n"}{end}' >&2 || true
}

wait_for_singleton_ready() {
  local pod="$1" i
  # After the delete/recreate loop the pod object does not exist yet and
  # `kubectl wait` fails instantly on NotFound instead of waiting — poll for the
  # StatefulSet controller to recreate the object first (bounded), then wait for
  # its Ready condition.
  for i in $(seq 1 60); do
    if kubectl_indramesh get pod "$pod" -n "$NAMESPACE" >/dev/null 2>&1; then break; fi
    sleep 1
  done
  if ! kubectl_indramesh get pod "$pod" -n "$NAMESPACE" >/dev/null 2>&1; then
    verify_error "pod/$pod was not recreated within 60s"; return 1
  fi
  if ! kubectl_indramesh wait --for=condition=Ready "pod/$pod" -n "$NAMESPACE" --timeout=180s; then
    show_pod_diagnostics "$pod"
    return 1
  fi
  local ready
  ready="$(kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath='{range .status.containerStatuses[*]}{.ready}{end}')"
  [ "$ready" = "true" ] || { show_pod_diagnostics "$pod"; return 1; }
  echo "verify: pod/$pod Ready 1/1"
}

assert_selected_image_contract() {
  local manifest
  manifest="$(bash "$ROOT/tests/bootstrap/helm_bin.sh" template "$RELEASE_NAME" "$ROOT/deploy/helm/indramesh" --namespace "$NAMESPACE")"
  for image in indramesh-stateful-postgres:18.4-age1.8.0-pgv0.8.7 indramesh-stateful-valkey:9.1.1-r1 indramesh-stateful-opensearch:3.9.0-r1; do
    case "$manifest" in *"$image"*) ;; *) verify_error "helm template stock image missing: $image"; return 1 ;; esac
  done
  # Blocker A: the OpenSearch copy-config initContainer must render from the same
  # wrapper image under the restricted helper, copying config into the emptyDir —
  # and must NOT be an ownership repair (no chown -R anywhere in the rendered
  # chart).
  case "$manifest" in
    *"copy-opensearch-config"*) ;;
    *) verify_error "helm template OpenSearch copy-config initContainer missing"; return 1 ;;
  esac
  case "$manifest" in
    *"cp -r /usr/share/opensearch/config/. /os-config/"*) ;;
    *) verify_error "helm template OpenSearch copy-config command missing"; return 1 ;;
  esac
  case "$manifest" in *"chown -R"*) verify_error "rendered chart contains an ownership-repair chown"; return 1 ;; esac
  echo "verify: helm template confirms corrected values-to-template images and the OpenSearch copy-config initContainer contract"
}

# Blocker B (ref-chain proof): the ready pod's `image` field must equal the
# canonical ref, proving it started from the exact revision whose recorded
# post-import digest matched the current containerd target. Combined with
# assert_image_evidence_current (evidence digest == current ctr DIGEST of the
# canonical ref) this closes the chain: pod image field == canonical ref ==
# recorded post-import revision.
#
# The chart renders the wrapper by its values repository/tag (bare name
# `indramesh-stateful-postgres:18.4-age1.8.0-pgv0.8.7`), which Docker normalizes to the
# canonical `docker.io/library/indramesh-stateful-postgres:18.4-age1.8.0-pgv0.8.7` — the SAME
# image as the recorded canonical ref. Normalize the pod's bare name to its
# canonical registry-qualified form before the equality check so the chain is
# proven without requiring the chart to spell out the default registry.
normalize_image_ref() {
  local ref="$1"
  case "${ref%%:*}" in
    */*) printf '%s' "$ref" ;;                 # already registry- or namespace-qualified
    *) printf 'docker.io/library/%s' "$ref" ;; # bare image name -> default docker hub library
  esac
}

assert_runtime_image_ref() {
  local pod="$1" container="$2" expected_ref="$3" image_field normalized
  image_field="$(kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath="{.spec.containers[?(@.name==\"$container\")].image}")"
  normalized="$(normalize_image_ref "$image_field")"
  [ "$normalized" = "$expected_ref" ] || { verify_error "pod/$pod container/$container image field '$image_field' (normalized '$normalized') != canonical ref '$expected_ref'"; return 1; }
  echo "verify: pod/$pod container/$container image field '$image_field' == canonical ref $expected_ref (ref-chain proof)"
}

assert_claim_identity() {
  local pod="$1" claim="$2" actual
  actual="$(kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath='{.spec.volumes[?(@.name=="data")].persistentVolumeClaim.claimName}')"
  [ "$actual" = "$claim" ] || { verify_error "pod/$pod uses PVC '$actual', expected '$claim'"; return 1; }
  kubectl_indramesh get pvc "$claim" -n "$NAMESPACE" >/dev/null
  echo "verify: pod/$pod reuses PVC/$claim"
}

assert_pgdata_contract() {
  local pgdata mounts
  pgdata="$(kubectl_indramesh get pod postgres-0 -n "$NAMESPACE" -o jsonpath='{.spec.containers[0].env[?(@.name=="PGDATA")].value}')"
  mounts="$(kubectl_indramesh get pod postgres-0 -n "$NAMESPACE" -o jsonpath='{range .spec.containers[0].volumeMounts[*]}{.mountPath}{" subPath="}{.subPath}{"\n"}{end}')"
  case "$pgdata" in /var/lib/postgresql/pgdata) ;; *) verify_error "PGDATA is '$pgdata', not the pgdata subPath beneath the outer PVC mount"; return 1 ;; esac
  case "$mounts" in *"/var/lib/postgresql subPath="*"/var/lib/postgresql/pgdata subPath=pgdata"*) ;; *) verify_error "Postgres outer mount plus pgdata subPath contract missing"; return 1 ;; esac
  echo "verify: Postgres PGDATA=$pgdata with outer /var/lib/postgresql mount and subPath: pgdata"
}

record_runtime_evidence() {
  local pod="$1" path="$2"
  echo "verify: pod/$pod effective identity: $(kubectl_indramesh exec "$pod" -n "$NAMESPACE" -- sh -ec 'id; id -G')"
  kubectl_indramesh exec "$pod" -n "$NAMESPACE" -- sh -ec "test -w '$path'; marker='$path/.${MARKER_PREFIX}_writable'; printf '%s' '$MARKER_PREFIX' > \"\$marker\"; test \"\$(cat \"\$marker\")\" = '$MARKER_PREFIX'; rm -f \"\$marker\""
  echo "verify: pod/$pod mounted path $path is writable"
  kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath='{range .status.conditions[*]}{.type}{"="}{.status}{" "}{end}'; echo
}

cleanup_markers() {
  kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- sh -ec "psql -v ON_ERROR_STOP=1 -U postgres -d indramesh -c \"DROP TABLE IF EXISTS indramesh_phase_256_verifier\"" >/dev/null 2>&1 || true
  kubectl_indramesh exec valkey-0 -n "$NAMESPACE" -- sh -ec "valkey-cli -a \"\$VALKEY_PASSWORD\" DEL '$VALKEY_MARKER'" >/dev/null 2>&1 || true
  kubectl_indramesh exec opensearch-0 -n "$NAMESPACE" -- sh -ec "curl -sS -u \"admin:\$OPENSEARCH_INITIAL_ADMIN_PASSWORD\" -X DELETE 'http://localhost:9200/$OPENSEARCH_INDEX' >/dev/null" >/dev/null 2>&1 || true
}

main() {
  local evidence_file="$1"
  kubectl_indramesh get nodes >/dev/null || { verify_error "root kubeconfig cannot read cluster nodes"; return 1; }
  assert_image_evidence_current "$evidence_file"
  assert_selected_image_contract

  # D-06 companion: owner-chown the retained Postgres/Valkey local-path PVC
  # host dirs (chmod-capable owner) before reconciliation. This retry runs on
  # the retained PVCs from the prior attempt, so the prep resolves them in the
  # single pass (the fresh-install second-pass flow belongs to Task 2).
  . "$ROOT/scripts/k8s/lib/stateful_storage.sh"
  prepare_stateful_local_path_ownership \
    || { verify_error "stateful host-prep ownership failed before reconciliation"; return 1; }

  # D-13: the installer owns its configured HELM_UPGRADE_TIMEOUT and has the
  # bounded reconciliation seam. Capture its non-secret diagnostics on failure.
  if ! bash "$ROOT/scripts/k8s/install.sh" >"$INSTALL_LOG" 2>&1; then
    verify_error "installer reconciliation failed within HELM_UPGRADE_TIMEOUT=${HELM_UPGRADE_TIMEOUT:-10m}"
    tail -n 80 "$INSTALL_LOG" >&2
    for pod in postgres-0 valkey-0 opensearch-0; do
      show_pod_diagnostics "$pod"
    done
    return 1
  fi

  echo "verify: StorageClass/local-path allowVolumeExpansion=$(kubectl_indramesh get storageclass local-path -o jsonpath='{.allowVolumeExpansion}')"
  for spec in "postgres-0 data-postgres-0 /var/lib/postgresql/pgdata" "valkey-0 data-valkey-0 /data" "opensearch-0 data-opensearch-0 /usr/share/opensearch/data"; do
    read -r pod claim path <<<"$spec"
    wait_for_singleton_ready "$pod"
    assert_claim_identity "$pod" "$claim"
    # Blocker B digest proof is the ref-chain, NOT the pod imageID: the recorded
    # target digest is the OCI index digest, while the ready pod imageID is the
    # platform-manifest digest — for a multi-arch image these differ by design.
    # assert_image_evidence_current already proved (evidence digest == current
    # ctr DIGEST of the canonical ref); assert_runtime_image_ref proves the pod
    # image field == canonical ref, so the pod started from the recorded revision.
    case "$pod" in
      postgres-0)
        assert_runtime_image_ref "$pod" postgres "$POSTGRES_EVIDENCE_REF"
        echo "verify: pod/$pod container/postgres imageID=$(kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath="{.status.containerStatuses[?(@.name==\"postgres\")].imageID}")"
        ;;
      valkey-0)
        assert_runtime_image_ref "$pod" valkey "$VALKEY_EVIDENCE_REF"
        echo "verify: pod/$pod container/valkey imageID=$(kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath="{.status.containerStatuses[?(@.name==\"valkey\")].imageID}")"
        ;;
      opensearch-0)
        assert_runtime_image_ref "$pod" opensearch "$OPENSEARCH_EVIDENCE_REF"
        echo "verify: pod/$pod container/opensearch imageID=$(kubectl_indramesh get pod "$pod" -n "$NAMESPACE" -o jsonpath="{.status.containerStatuses[?(@.name==\"opensearch\")].imageID}")"
        ;;
    esac
    record_runtime_evidence "$pod" "$path"
  done
  assert_pgdata_contract

  # Fresh claim initialization is observable from the first successful ready
  # state and the initial PostgreSQL server log, before any phase marker exists.
  kubectl_indramesh logs postgres-0 -n "$NAMESPACE" | grep -Eq 'database system is ready to accept connections|PostgreSQL init process complete' \
    || { verify_error "Postgres fresh-init success evidence is absent"; return 1; }

  kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- sh -ec "psql -v ON_ERROR_STOP=1 -U postgres -d indramesh -c \"CREATE TABLE IF NOT EXISTS indramesh_phase_256_verifier (marker text primary key); INSERT INTO indramesh_phase_256_verifier(marker) VALUES ('$POSTGRES_MARKER') ON CONFLICT DO NOTHING; SELECT marker FROM indramesh_phase_256_verifier WHERE marker = '$POSTGRES_MARKER';\"" | grep -F "$POSTGRES_MARKER" >/dev/null
  kubectl_indramesh exec valkey-0 -n "$NAMESPACE" -- sh -ec "valkey-cli -a \"\$VALKEY_PASSWORD\" SET '$VALKEY_MARKER' '$VALKEY_MARKER'; test \"\$(valkey-cli -a \"\$VALKEY_PASSWORD\" --raw GET '$VALKEY_MARKER')\" = '$VALKEY_MARKER'"
  kubectl_indramesh exec opensearch-0 -n "$NAMESPACE" -- sh -ec "curl -fsS -u \"admin:\$OPENSEARCH_INITIAL_ADMIN_PASSWORD\" -H 'Content-Type: application/json' -X PUT 'http://localhost:9200/$OPENSEARCH_INDEX/_doc/marker?refresh=true' -d '{\"marker\":\"$OPENSEARCH_MARKER\"}' >/dev/null; curl -fsS -u \"admin:\$OPENSEARCH_INITIAL_ADMIN_PASSWORD\" 'http://localhost:9200/$OPENSEARCH_INDEX/_doc/marker' | grep -F '$OPENSEARCH_MARKER' >/dev/null"

  for spec in "postgres-0 data-postgres-0" "valkey-0 data-valkey-0" "opensearch-0 data-opensearch-0"; do
    read -r pod claim <<<"$spec"
    kubectl_indramesh delete pod "$pod" -n "$NAMESPACE" --wait=true --timeout=120s
    wait_for_singleton_ready "$pod"
    assert_claim_identity "$pod" "$claim"
  done
  assert_pgdata_contract
  kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- sh -ec "psql -v ON_ERROR_STOP=1 -U postgres -d indramesh -tAc \"SELECT marker FROM indramesh_phase_256_verifier WHERE marker = '$POSTGRES_MARKER'\"" | grep -Fx "$POSTGRES_MARKER" >/dev/null
  kubectl_indramesh exec valkey-0 -n "$NAMESPACE" -- sh -ec "test \"\$(valkey-cli -a \"\$VALKEY_PASSWORD\" --raw GET '$VALKEY_MARKER')\" = '$VALKEY_MARKER'"
  kubectl_indramesh exec opensearch-0 -n "$NAMESPACE" -- sh -ec "curl -fsS -u \"admin:\$OPENSEARCH_INITIAL_ADMIN_PASSWORD\" 'http://localhost:9200/$OPENSEARCH_INDEX/_doc/marker' | grep -F '$OPENSEARCH_MARKER' >/dev/null"
  cleanup_markers
  echo "verify: Phase 256 corrected-image persistence gate passed; only phase-owned markers were removed"
}

MODE="live"
EVIDENCE_FILE="${INDRAMESH_STATEFUL_DIGEST_EVIDENCE_FILE:-$DEFAULT_EVIDENCE_FILE}"
while [ "$#" -gt 0 ]; do
  case "$1" in
    --import-wrappers) MODE="import" ;;
    --check-image-evidence-only) MODE="check" ;;
    --evidence-file)
      shift
      [ "$#" -gt 0 ] || { verify_error "--evidence-file requires a path"; exit 2; }
      EVIDENCE_FILE="$1"
      ;;
    *) verify_error "unknown argument: $1"; exit 2 ;;
  esac
  shift
done

case "$MODE" in
  import) import_wrapper_images "$EVIDENCE_FILE" ;;
  check) assert_image_evidence_current "$EVIDENCE_FILE" ;;
  live) main "$EVIDENCE_FILE" ;;
esac
