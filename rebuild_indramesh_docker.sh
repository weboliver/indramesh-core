#!/bin/bash
# rebuild_indramesh_docker.sh — quick rebuild+recreate for the Docker Compose dev stack,
# selector-compatible with rebuild_indramesh_k8s.sh ([backend|frontend] [--no-cache]) but
# far simpler: Compose builds and (re)creates containers directly against the local
# docker daemon, no image-tag/Helm reconciliation needed (that machinery in
# scripts/k8s/rebuild.sh exists only because k3s has no registry to push to).
#
# Per CLAUDE.md: backend/worker hot-reload .py changes via their source volume mount —
# only rebuild them for new dependencies or Dockerfile/compose.yml changes. The frontend
# Vite dev server also hot-reloads most changes, EXCEPT ones that only take effect at
# Vite's own startup (e.g. a new/changed VITE_* env passthrough in docker-compose.yml,
# Phase 272/K8SFIX-03) — those need this restart to pick up.
#
# backend/worker/frontend are each independently selectable (unlike
# rebuild_indramesh_k8s.sh, where `backend` bundles worker because both k8s
# Deployments share one image — in Compose they're separate services/containers,
# so no bundling here); no selector rebuilds all three.
#
# backend, worker and storage each build the same backend sources into their own
# per-service image (`indramesh-backend`, `indramesh-worker`, `indramesh-storage`),
# sharing the layer cache. Selecting `backend` or `worker` therefore builds and
# recreates all three, so they never run different code. Selecting `frontend` builds
# and recreates `frontend`; no selector does all four. The hot-reload source mounts
# come from docker-compose.dev.yml, not from the public base file.
#
# nginx.conf proxies to `backend`/`frontend` by hostname and re-resolves those names
# at runtime from Docker's embedded DNS (`resolver` plus `server … resolve;` in both
# upstream blocks, Phase 323 D-07). A rebuilt service is recreated under a NEW
# container IP, and nginx follows it within the resolver's ten-second validity
# window — so this script never restarts or reloads nginx. Nothing is needed for a
# worker-only rebuild either, since nginx never proxies to the worker.
set -euo pipefail

usage() { echo "usage: $(basename "$0") [backend|worker|frontend] [--no-cache]" >&2; exit 2; }

SELECTOR="${1:-}"
case "${2:-}" in
  "") NO_CACHE_FLAG=() ;;
  --no-cache) NO_CACHE_FLAG=(--no-cache) ;;
  *) usage ;;
esac

declare -a RECREATE_TARGETS
case "$SELECTOR" in
  "")       RECREATE_TARGETS=(backend worker storage frontend) ;;
  backend)  RECREATE_TARGETS=(backend worker storage) ;;
  worker)   RECREATE_TARGETS=(backend worker storage) ;;
  frontend) RECREATE_TARGETS=(frontend) ;;
  *) usage ;;
esac

echo "rebuild: building ${RECREATE_TARGETS[*]}${NO_CACHE_FLAG:+ (--no-cache)}"
docker compose build "${NO_CACHE_FLAG[@]}" "${RECREATE_TARGETS[@]}"

echo "rebuild: recreating ${RECREATE_TARGETS[*]}"
docker compose up -d "${RECREATE_TARGETS[@]}"

echo "rebuild: done"
docker compose ps "${RECREATE_TARGETS[@]}"
