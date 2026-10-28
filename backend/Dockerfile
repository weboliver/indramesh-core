FROM python:3.12-slim

RUN apt-get update && apt-get upgrade -y && apt-get install -y curl && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY plugin-requirements.txt .
RUN if grep -qvE '^[[:space:]]*(#|$)' plugin-requirements.txt; then pip install --no-cache-dir -r plugin-requirements.txt; fi

COPY . .

RUN chmod +x start.sh docker-entrypoint.sh

# Phase 97 — Non-root container (CONTAINER-01, closes F-88-02-11)
# The /app bind mount from docker-compose.yml overrides chown at runtime,
# so write operations on the mounted /app require the host user's UID.
# The appuser exists primarily for process-level privilege reduction.
# Phase 235 — APP_UID/APP_GID build args (default 1001, byte-identical to
# prior behavior when unset) let a developer whose host UID differs from
# 1001 rebuild with a matching UID so every appuser-owned path — the named
# volumes below AND the new frontend/locales bind mount — is
# self-consistently owned by one UID, never a runtime mismatch
# (235-PLAN-REVIEW.md Finding 1: no compose-level `user:` override).
#
# Phase 235 (CR-01 code review fix) — the frontend/locales bind mount keeps the
# host's ownership regardless of APP_UID/APP_GID (bind mounts are never chown'd
# by Docker, unlike the named volumes below), so on a host whose UID doesn't
# happen to match the built image, the export feature would still fail with a
# permission error. docker-entrypoint.sh (invoked only by the backend service —
# see docker-compose.yml) runs as root long enough to reconcile that one path's
# ownership, then drops to appuser via setpriv before exec'ing the real command.
# Only the backend service overrides `entrypoint`/`user` in docker-compose.yml
# to use it; every other consumer of this image (worker, a plain `docker run`)
# is unaffected and keeps running directly as appuser below.
ARG APP_UID=1001
ARG APP_GID=1001
RUN groupadd -g ${APP_GID} appuser \
 && useradd -m -u ${APP_UID} -g appuser appuser

# /tmp/exports, /tmp/imports, /data/imports and /data/storage are named volumes
# (docker-compose.yml) shared between backend/worker and the storage service —
# Docker populates a freshly created empty volume from the image's directory
# content, so pre-creating them here with appuser ownership means a first-run
# volume isn't left root-owned (appuser can't write).
#
# Phase 255 (D-10, T-255-05): Kubernetes runs with an arbitrary cluster-assigned
# non-root UID in group 0, so the same declared write paths additionally get
# group-0 read/write/execute. The owner (appuser, UID 1001) stays intact for
# Compose; `chown appuser:0` + `chmod -R g=u` (group permissions mirror the
# owner's) lets any UID in group 0 write exactly these enumerated paths
# without broadening anything outside the inventory. The Python temporary
# directory defaults to /tmp (world-writable sticky 1777), which arbitrary
# UIDs can already write; under a read-only root it must be mounted
# explicitly (declared in deploy/helm/indramesh/templates/_helpers.tpl).
#
# Phase 278 (Pitfall 2): /data/storage is the storage service's blob root — a
# fresh storage_data volume would otherwise be root-owned and every store call
# would 500 with PermissionError.
RUN mkdir -p /tmp/exports /tmp/imports /data/imports /data/storage \
 && chown -R appuser:0 /tmp/exports /tmp/imports /data/imports /data/storage \
 && chmod -R g=u /tmp/exports /tmp/imports /data/imports /data/storage

USER ${APP_UID}

CMD ["./start.sh"]
