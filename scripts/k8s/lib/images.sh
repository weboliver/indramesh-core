#!/bin/bash
# images.sh — registry-free local image build/import/verify/cleanup library
# for IMG-02 (D-09..D-18). Sourceable; requires common.sh (step/
# preflight_error/K3S_KUBECONFIG) and platform.sh (PLATFORM_ARCH) to already
# be sourced by the caller — mirrors stateful_storage.sh's convention of
# never self-sourcing its own dependencies. Every function is a thin caller
# of the Phase 256 proven seams (scripts/k8s/verify_stateful_services.sh
# stream/verify/ref-chain patterns). Consumed by the rebuild orchestration
# wrapper (Plan 258-03). Never retains a tar archive (D-16), never deletes a
# non-tool-owned or currently-deployed image (D-18).
set -euo pipefail

# Own script directory, private-prefixed so sourcing this file never
# clobbers a caller's own SCRIPT_DIR (several entrypoint scripts in this
# repo — helm_bin.sh, verify_stateful_services.sh — define that name too).
_IMAGES_SH_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$_IMAGES_SH_DIR/../../.." && pwd)"

# D-13: single central mapping — context + Dockerfile per image, nothing
# inferred from a shared context. Repository names are the stable k8s names
# (D-12, matching deploy/helm/indramesh/values.yaml apps.*.image.repository for
# the three app images and postgres.image/valkey.image/opensearch.image for the
# three stateful wrappers); only the build tag changes. Values are fully
# resolved absolute paths so build_image never has to guess whether a Dockerfile
# entry is relative to ROOT_DIR or to its own context.
#
# The three stateful wrapper repos are in this mapping for the same reason the
# app images are: they are local-only names (no public registry carries them)
# and the chart pins them with imagePullPolicy IfNotPresent, so a bare host can
# only get them by building and importing them. Leaving them out made every
# fresh-clone k3s quickstart end in postgres-0/valkey-0 ImagePullBackOff — and a
# fourth wrapper without an entry would end the same way for opensearch-0.
declare -A IMAGE_BUILD_SPECS=(
  [indramesh-backend:context]="$ROOT_DIR/backend"
  [indramesh-backend:dockerfile]="$ROOT_DIR/backend/Dockerfile"
  [indramesh-frontend-static:context]="$ROOT_DIR"
  [indramesh-frontend-static:dockerfile]="$ROOT_DIR/deploy/k8s-images/frontend/Dockerfile"
  [indramesh-nginx:context]="$ROOT_DIR/deploy/k8s-images/nginx"
  [indramesh-nginx:dockerfile]="$ROOT_DIR/deploy/k8s-images/nginx/Dockerfile"
  [indramesh-stateful-postgres:context]="$ROOT_DIR/deploy/k8s-images/stateful/postgres"
  [indramesh-stateful-postgres:dockerfile]="$ROOT_DIR/deploy/k8s-images/stateful/postgres/Dockerfile"
  [indramesh-stateful-valkey:context]="$ROOT_DIR/deploy/k8s-images/stateful/valkey"
  [indramesh-stateful-valkey:dockerfile]="$ROOT_DIR/deploy/k8s-images/stateful/valkey/Dockerfile"
  [indramesh-stateful-opensearch:context]="$ROOT_DIR/deploy/k8s-images/stateful/opensearch"
  [indramesh-stateful-opensearch:dockerfile]="$ROOT_DIR/deploy/k8s-images/stateful/opensearch/Dockerfile"
)

tool_image_tag() {  # tool_image_tag — echo git-<sha>-<YYYYmmddHHMMSS> (D-09).
  # Both components are built internally from the repo state and the clock,
  # never from operator input (T-258-06).
  local sha ts
  sha="$(git -C "$ROOT_DIR" rev-parse --short=7 HEAD)" \
    || { preflight_error "images: failed to resolve the current git SHA for the build tag (D-09)"; return 1; }
  ts="$(date -u +%Y%m%d%H%M%S)"
  printf 'git-%s-%s\n' "$sha" "$ts"
}

build_image() {  # build_image <repo> <tag> [--no-cache] [--build-arg K=V...] — D-13/D-14/D-15
  # Extra trailing arguments pass through to `docker build` verbatim (Phase
  # 272/K8SFIX-03): rebuild.sh/install.sh feed `--build-arg
  # VITE_ENVIRONMENT=<ENVIRONMENT>` from the root .env so the k8s frontend
  # image bakes the same single variable the Compose stack uses.
  local repo="$1" tag="$2"
  shift 2
  local context="${IMAGE_BUILD_SPECS[${repo}:context]:-}"
  local dockerfile="${IMAGE_BUILD_SPECS[${repo}:dockerfile]:-}"
  [ -n "$context" ] && [ -n "$dockerfile" ] \
    || { preflight_error "images: no build spec for repo '$repo' (D-13)"; return 1; }
  local extra_arg
  local args=(-t "$repo:$tag" --platform "linux/$PLATFORM_ARCH")
  for extra_arg in "$@"; do
    [ -n "$extra_arg" ] || continue
    args+=("$extra_arg")
  done
  args+=(-f "$dockerfile" "$context")
  step "images: building $repo:$tag (context=$context, dockerfile=$dockerfile)"
  docker build "${args[@]}" </dev/null \
    || { preflight_error "images: docker build failed for $repo (D-14/D-15)"; return 1; }
}

import_image_stream() {  # import_image_stream <local_ref> — D-16: stream
  # docker image save straight into ctr images import, never a retained tar
  # archive. Fail-fast on either side of the pipe via the caller's pipefail.
  docker image save "$1" | sudo k3s ctr -n k8s.io images import -
}

containerd_exact_ref() {  # containerd_exact_ref <ref> — D-17 exact-match
  # existence check; the k3s containerd DIGEST column is the canonical
  # target digest and there is no `ctr images inspect`
  # (verify_stateful_services.sh:40-49). awk's $1==ref is a whole-field
  # equality test — never a substring/prefix match.
  sudo k3s ctr -n k8s.io images list | awk -v ref="$1" '
    $1==ref { found=1 }
    END { exit (found ? 0 : 1) }
  '
}

normalize_image_ref() {  # normalize_image_ref <ref> — canonicalize a bare
  # image name to its default-registry form (copied from
  # verify_stateful_services.sh:199-205, the proven D-10 build-marker seam).
  local ref="$1"
  case "${ref%%:*}" in
    */*) printf '%s' "$ref" ;;                 # already registry- or namespace-qualified
    *) printf 'docker.io/library/%s' "$ref" ;; # bare image name -> default docker hub library
  esac
}

deployed_refs_from_pods() {  # deployed_refs_from_pods <namespace> <deployment...>
  # D-18: fail-closed reader of the currently-deployed image ref per named
  # Deployment's pod template. A NotFound deployment contributes no ref
  # (definitive absence is safe to subtract nothing for — no live tag
  # exists to protect); any other failure (non-zero exit, empty/malformed
  # output) fails closed (research Pitfall 5: never subtract on an
  # indeterminate read).
  local namespace="$1" dep image_field
  shift
  for dep in "$@"; do
    if ! image_field="$(sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get deployment "$dep" -n "$namespace" \
      -o jsonpath='{.spec.template.spec.containers[0].image}' 2>&1)"; then
      case "$image_field" in
        *NotFound*|*"not found"*) continue ;;
        *)
          preflight_error "images: failed to read deployment/$dep image field (D-18): ${image_field:-no output}"
          return 1
          ;;
      esac
    fi
    [ -n "$image_field" ] \
      || { preflight_error "images: deployment/$dep image field is empty (D-18)"; return 1; }
    printf '%s\n' "$(normalize_image_ref "$image_field")"
  done
}

cleanup_obsolete_tool_images() {  # cleanup_obsolete_tool_images <deployed_ref...>
  # D-18: remove only tool-owned images (the three indramesh app repos, tagged
  # in the git-<sha>-<ts> tool format) that are not currently deployed.
  # phase-NNN baselines, stateful images, and docker.elastic.co never match
  # the full canonical-ref regex. Never uses `ctr content` surgery.
  local deployed=("$@")
  local tool_ref_regex='^docker\.io/library/indramesh-(backend|frontend-static|nginx):git-[0-9a-f]{7,40}-[0-9]{14}$'
  local list_out ref d is_deployed
  if ! list_out="$(sudo k3s ctr -n k8s.io images list)"; then
    preflight_error "images: failed to list containerd images for cleanup (D-18)"
    return 1
  fi
  while IFS= read -r ref; do
    [ -n "$ref" ] || continue
    [[ "$ref" =~ $tool_ref_regex ]] || continue
    is_deployed=0
    for d in "${deployed[@]:-}"; do
      [ -n "$d" ] || continue
      [ "$ref" = "$d" ] && { is_deployed=1; break; }
    done
    [ "$is_deployed" -eq 1 ] && continue
    step "images: removing obsolete tool image $ref (D-18)"
    sudo k3s ctr -n k8s.io images remove "$ref" \
      || { preflight_error "images: failed to remove obsolete tool image $ref (D-18)"; return 1; }
  done < <(printf '%s\n' "$list_out" | awk '{print $1}')
  return 0
}
