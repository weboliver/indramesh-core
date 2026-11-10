# deploy/k8s-images/frontend/Dockerfile — production-only static frontend image
# for the k3s release AND the Compose base stack (APP-01, D-01..D-04,
# T-257-07/08; Phase 323 D-14 gives Compose the same definition). Multi-stage
# build: deterministic `npm ci` against the committed frontend lockfile,
# `vite build`, then Caddy's compiled runtime serving the SPA from /srv on
# unprivileged port 8080 (D-03). The Compose dev image (frontend/Dockerfile) and
# its Vite dev command are intentionally unchanged; port 5173 stays the
# Compose/Vite dev-overlay contract.

# ---- build stage: deterministic lockfile install + production build ----
FROM node:22-alpine AS build

WORKDIR /build

# D-04: npm ci consumes the committed lockfile — deterministic install, never
# a speculative npm install (T-257-07 keeps the supply chain pinned to the
# committed package-lock.json).
#
# `.npmrc` is copied HERE, alongside the lockfile, and not left to the
# `COPY frontend/ ./` below: it carries `maxsockets=3` (commit 4b77c337), and
# npm's default of 15 parallel registry connections stalls indefinitely under
# WSL2's mirrored networking mode — the symptom is `npm ci` going silent right
# after the graphiql ERESOLVE peer warnings and never returning. Copied after
# the install, the cap would arrive one step too late to have any effect.
# Build-stage only: the Caddy runtime stage below inherits nothing from here,
# so no npm config reaches the shipped image.
COPY frontend/package.json frontend/package-lock.json frontend/.npmrc ./
# The npm cache mount is the SECOND half of the WSL2 stall mitigation, not a
# speed optimisation — do not remove it. `.npmrc` caps how many registry
# connections run at once; the cache decides how many requests are made at all.
# With a warm cache most tarballs come off local disk, so the number of
# stall-prone connections collapses. Removing it while keeping `.npmrc`
# reproduced the hang immediately (2026-09-22, twice: a live rebuild and a
# verification build that sat at 3.0s of npm output indefinitely). Judging this
# mount by wall-clock alone is misleading: run times swing 38s-357s because the
# long runs ARE the partial stall.
# It is a BuildKit cache, never an image layer — the install stays exactly as
# deterministic as the lockfile makes it.
RUN --mount=type=cache,target=/root/.npm npm ci

COPY frontend/ ./
# Phase 272/K8SFIX-03: VITE_ENVIRONMENT ist ein Build-Time-ARG, kein festes ENV — Vite bäckt
# den Wert unveränderlich in das statische Bundle ein (Caddy liefert nur den fertigen dist/-Output
# aus, kein Laufzeit-Zugriff auf Prozess-Env möglich). Default bleibt "production", damit sich das
# heutige k8s-Deployment nicht ändert; ein künftiges Kubernetes-Deployment mit
# ENVIRONMENT=development (siehe PROJECT.md Key Decisions: ENVIRONMENT/INDRAMESH_RUNTIME_MODE sind
# orthogonal, "Kubernetes" darf nicht dauerhaft mit "production" gleichgesetzt werden) baut dasselbe
# Dockerfile stattdessen mit `--build-arg VITE_ENVIRONMENT=development`.
ARG VITE_ENVIRONMENT=production
ENV VITE_ENVIRONMENT=${VITE_ENVIRONMENT}
RUN npm run build

# ---- runtime stage: Caddy static server (D-01/D-02, T-257-07) ----
# Manifest-verified dedicated static server: SPA fallback for client routes,
# ordinary file_server for dot-suffixed assets (genuinely missing assets stay
# HTTP 404), and a probe-only health responder — see the sibling Caddyfile.
FROM caddy:2.11.9-alpine

# compiled assets + the routing contract
COPY --from=build /build/dist /srv
COPY deploy/k8s-images/frontend/Caddyfile /etc/caddy/Caddyfile

# Live finding (2026-09-01): the upstream caddy image sets the file capability
# cap_net_bind_service on /usr/bin/caddy (so it CAN bind privileged ports as
# non-root). Under the shared restricted securityContext
# (allowPrivilegeEscalation: false + capabilities.drop: [ALL], i.e. the kernel's
# no_new_privs=1), exec() of a binary carrying a file capability beyond the
# process's permitted set is refused outright: "exec /usr/bin/caddy: operation
# not permitted". We only ever bind the unprivileged port 8080 (D-03), so the
# capability is never needed — stripping it lets the restricted context exec
# the binary normally.
RUN apk upgrade --no-cache \
 && apk add --no-cache libcap \
 && setcap -r /usr/bin/caddy \
 && apk del libcap

# The upstream caddy image declares no USER (it would run as root); the shared
# Restricted PSA/SCC context requires a non-root image user. The files are
# world-readable and port 8080 is unprivileged, so any cluster-assigned UID can
# serve — arbitrary-UID compatible.
USER 10001

# D-03: unprivileged internal HTTP port; 5173 is never exposed from this image.
EXPOSE 8080
