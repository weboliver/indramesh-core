FROM node:24-alpine

WORKDIR /app
RUN apk upgrade --no-cache && chown node:node /app

# Phase 97 — Non-root container (CONTAINER-01, closes F-88-02-11)
# node:22-alpine ships a built-in 'node' user at uid/gid 1000, which matches
# the host dev user on most single-user Linux setups (first non-system uid).
# Reusing it (instead of a custom 'appuser' at 1001) keeps the bind-mounted
# ./frontend:/app writable — Vite bundles vite.config.js into a temp
# `.timestamp-*.mjs` file next to it on every dev-server start, which needs
# write access to that host-owned directory, not just node_modules.
# npm install runs as node directly (COPY --chown below) instead of root +
# a separate recursive `chown -R node_modules` afterward — with ~43k files
# in node_modules that chown alone took 5 minutes on some hosts (WSL2/overlayfs).
# `.npmrc` ships with the manifests, not with the later `COPY . .`: it carries
# maxsockets=3 (commit 4b77c337), and npm's default of 15 parallel registry
# connections stalls indefinitely under WSL2's mirrored networking mode. Copied
# after the install it would arrive too late to cap anything.
COPY --chown=node:node package*.json .npmrc ./
USER node
# Second half of the WSL2 stall mitigation, not a speed optimisation — see the
# matching comment in deploy/k8s-images/frontend/Dockerfile. `.npmrc` caps the
# concurrent registry connections; the cache removes most of the requests.
# uid/gid 1000 = the node user that owns this install; without them the mount
# is root-owned and unwritable here.
RUN --mount=type=cache,target=/home/node/.npm,uid=1000,gid=1000 npm install

# Phase 275 (COMPOSE-PROD-02, D-01..D-05): an exact ENVIRONMENT=production image
# build bakes the compiled bundle into /srv/frontend-dist — an image-owned
# directory OUTSIDE the ./frontend:/app bind mount (which would hide runtime
# artifacts), created before dropping back to the non-root node user. Every
# other (or absent) build-arg value skips the build and keeps the development
# image. The runtime command previews the same directory on port 5173 only for
# the exact production value (D-05: existing Vite preview, no new dependency)
# and falls back to the current Vite dev server for everything else.
ARG ENVIRONMENT
USER root
RUN mkdir -p /srv/frontend-dist && chown node:node /srv/frontend-dist
USER node

COPY --chown=node:node . .

RUN if [ "${ENVIRONMENT:-}" = "production" ]; then npm run build -- --outDir /srv/frontend-dist; fi

EXPOSE 5173

CMD if [ "${ENVIRONMENT:-}" = "production" ]; then exec npm run preview -- --host 0.0.0.0 --port 5173 --outDir /srv/frontend-dist; else exec npm run dev -- --host; fi