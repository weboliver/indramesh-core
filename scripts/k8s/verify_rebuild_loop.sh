#!/bin/bash
# verify_rebuild_loop.sh — bounded live evidence gate for the IMG-03 rebuild
# loop (D-07, D-09, D-10, D-18, D-24). Deliberately separate from rebuild.sh —
# a deliberate verify step, not part of routine task commits (AGENTS.md
# testing discipline: run only what a task's own <verify> specifies).
#
# Proves, against the REAL dev cluster:
#   - all four Deployments (backend, worker, frontend, nginx) roll out and
#     their Ready pods' image field matches the Deployment template's image
#     field (the ref-chain proof, D-10/D-24) and the new build-marker tag
#     format (D-09)
#   - the nginx Pod is Ready 1/1 (probes green on the HTTPS /healthz path)
#   - cleanup evidence: containerd carries no stray tool-owned ref except the
#     currently-deployed ones (D-18), and the phase-NNN baselines plus the
#     OpenSearch wrapper image (the search tier's pinned baseline after Phase
#     312.1; the retired docker.elastic.co image no longer exists on a host
#     installed after that phase) are untouched (research Pitfall 5)
#   - no-reinstall evidence: the Helm release is still present and the nginx
#     Deployment still exists (no `helm uninstall` ever ran)
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
. "$SCRIPT_DIR/lib/common.sh"
. "$SCRIPT_DIR/lib/platform.sh"
. "$SCRIPT_DIR/lib/artifacts.sh"
. "$SCRIPT_DIR/lib/helm.sh"

NAMESPACE="indramesh"
BUILD_MARKER_REGEX='^docker\.io/library/indramesh-(backend|frontend-static|nginx):git-[0-9a-f]{7,40}-[0-9]{14}$'

kubectl_indramesh() { sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"; }
verify_error() { echo "[verify-rebuild-loop] ERROR: $*" >&2; }

normalize_image_ref() {  # copied verbatim from the proven D-10 seam
  local ref="$1"
  case "${ref%%:*}" in
    */*) printf '%s' "$ref" ;;
    *) printf 'docker.io/library/%s' "$ref" ;;
  esac
}

assert_rollout_ref_chain() {  # assert_rollout_ref_chain <deployment>
  local dep="$1" template_image ready_images normalized_template img normalized_ready
  kubectl_indramesh rollout status "deployment/$dep" -n "$NAMESPACE" --timeout=180s \
    || { verify_error "deployment/$dep rollout status is not available"; return 1; }
  template_image="$(kubectl_indramesh get deployment "$dep" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}')"
  normalized_template="$(normalize_image_ref "$template_image")"
  [[ "$normalized_template" =~ $BUILD_MARKER_REGEX ]] \
    || { verify_error "deployment/$dep template image '$normalized_template' does not match the build-marker format (D-09)"; return 1; }
  ready_images="$(kubectl_indramesh get pods -n "$NAMESPACE" -l "app.kubernetes.io/name=$dep" \
    -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.spec.containers[0].image}{"\n"}{end}' 2>/dev/null || true)"
  [ -n "$ready_images" ] || { verify_error "deployment/$dep has no Running pods to verify"; return 1; }
  while IFS= read -r img; do
    [ -n "$img" ] || continue
    normalized_ready="$(normalize_image_ref "$img")"
    [ "$normalized_ready" = "$normalized_template" ] \
      || { verify_error "deployment/$dep pod image '$normalized_ready' != Deployment template image '$normalized_template' (ref-chain, D-10)"; return 1; }
  done <<<"$ready_images"
  echo "verify: deployment/$dep pod image == Deployment template image == $normalized_template (ref-chain proof)"
}

main() {
  kubectl_indramesh get nodes >/dev/null || { verify_error "root kubeconfig cannot read cluster nodes"; return 1; }

  local dep
  for dep in indramesh-backend indramesh-worker indramesh-frontend nginx; do
    assert_rollout_ref_chain "$dep" || return 1
  done

  local nginx_ready
  nginx_ready="$(kubectl_indramesh get pods -n "$NAMESPACE" -l app.kubernetes.io/name=nginx \
    -o jsonpath='{range .items[*]}{.status.containerStatuses[0].ready}{"\n"}{end}' 2>/dev/null || true)"
  case "$nginx_ready" in
    *true*) echo "verify: nginx pod Ready 1/1 (HTTPS /healthz probes green)" ;;
    *) verify_error "nginx pod is not Ready 1/1"; return 1 ;;
  esac

  local list_out
  list_out="$(sudo k3s ctr -n k8s.io images list)" \
    || { verify_error "failed to list containerd images (D-18 evidence)"; return 1; }
  local deployed_refs
  deployed_refs="$(
    for dep in indramesh-backend indramesh-worker indramesh-frontend nginx; do
      img="$(kubectl_indramesh get deployment "$dep" -n "$NAMESPACE" -o jsonpath='{.spec.template.spec.containers[0].image}' 2>/dev/null || true)"
      [ -n "$img" ] && normalize_image_ref "$img"
    done
  )"
  local tool_ref_regex='^docker\.io/library/indramesh-(backend|frontend-static|nginx):git-[0-9a-f]{7,40}-[0-9]{14}$'
  local ref stray=0
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    [[ "$ref" =~ $tool_ref_regex ]] || continue
    case "$deployed_refs" in
      *"$ref"*) ;;
      *) verify_error "stray tool-owned image not cleaned up: $ref (D-18)"; stray=1 ;;
    esac
  done < <(printf '%s\n' "$list_out" | awk '{print $1}')
  [ "$stray" -eq 0 ] || return 1
  echo "verify: no stray tool-owned images remain — cleanup (D-18) subtracted only the currently-deployed refs"

  for baseline in "docker.io/library/indramesh-backend:phase-257" "docker.io/library/indramesh-frontend-static:phase-257" "docker.io/library/indramesh-stateful-opensearch:3.9.0-r1"; do
    case "$list_out" in
      *"$baseline"*) echo "verify: baseline $baseline is untouched" ;;
      *) verify_error "baseline image missing from containerd: $baseline"; return 1 ;;
    esac
  done

  detect_platform
  local helm_installed helm_bin
  helm_installed="$(helm_installed_version)"
  if [ "$(helm_install_decision "$helm_installed")" = "keep" ]; then
    helm_bin="helm"
  else
    local tmp_dir
    tmp_dir="$(mktemp -d "${TMPDIR:-/tmp}/indramesh-verify-rebuild.XXXXXX")"
    trap 'rm -rf "$tmp_dir"' RETURN
    acquire_artifacts "$PLATFORM_ARCH" "$tmp_dir" "$SCRIPT_DIR/checksums.sha256" "$SCRIPT_DIR/versions.env" || return 1
    helm_bin="$(helm_binary_path "$PLATFORM_ARCH" "$VERIFIED_HELM_ARCHIVE" "$tmp_dir")" || return 1
  fi
  sudo env KUBECONFIG="$K3S_KUBECONFIG" "$helm_bin" status indramesh -n "$NAMESPACE" >/dev/null \
    || { verify_error "Helm release 'indramesh' is not present — a reinstall or uninstall may have occurred"; return 1; }
  kubectl_indramesh get deployment nginx -n "$NAMESPACE" >/dev/null \
    || { verify_error "nginx Deployment is missing — no-reinstall evidence failed"; return 1; }
  echo "verify: Helm release 'indramesh' present and nginx Deployment exists — no reinstall/uninstall occurred"

  echo "verify: Phase 258 rebuild loop passed"
}

main "$@"
