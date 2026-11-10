#!/bin/bash
# install.sh — Docker quickstart orchestration (Phase 311; D-05..D-11).
# Orchestration only: sources the lib/ modules and drives one explicit
# fail-fast sequence. It never installs a package, never escalates (no sudo —
# Docker/Compose/openssl are prerequisites, D-11), never prints a secret value
# except the generated admin password reported once on the run that generated it
# (D-10), and never touches a repository-root .env it did not create.
#
# Sequence:
#   1. preflight: daemon reachable, Compose >= 2.20, memory (warn), the
#      container-visible vm.max_map_count (warn), host ports free
#   2. create .env from .env.example when it does not exist (never overwrite)
#   3. pick or verify a free default-network subnet (COMPOSE_SUBNET and
#      NGINX_TRUSTED_IP, written to .env only while both are at their defaults)
#   4. fill the placeholders (compare-before-write, D-06) — nothing is started
#      while a placeholder is still present
#   5. docker compose up -d --build
#   6. wait for the backend healthcheck, then for nginx
#   7. report the URL and the admin password
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# INDRAMESH_DOCKER_ROOT is a hermetic-test seam; the documented invocation runs
# this script from a repository clone and never sets it.
ROOT_DIR="${INDRAMESH_DOCKER_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export ROOT_DIR

# shellcheck source=./lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# shellcheck source=./lib/secrets.sh
. "$SCRIPT_DIR/lib/secrets.sh"

ENV_FILE="${INDRAMESH_DOCKER_ENV_FILE:-$ROOT_DIR/.env}"
ENV_EXAMPLE="$ROOT_DIR/.env.example"
HEALTH_TIMEOUT="${INDRAMESH_DOCKER_HEALTH_TIMEOUT:-300}"
NGINX_TIMEOUT="${INDRAMESH_DOCKER_NGINX_TIMEOUT:-120}"

[ -f "$ENV_EXAMPLE" ] || die "install" ".env.example not found at $ENV_EXAMPLE — run this from a repository clone"
[ -f "$ROOT_DIR/docker-compose.yml" ] || die "install" "docker-compose.yml not found at $ROOT_DIR/docker-compose.yml — run this from a repository clone"

private_tmp SCRATCH_DIR
trap cleanup_private_tmp_on_exit EXIT

step "install: IndraMesh Docker Compose quickstart"

step "install: preflight"
docker_preflight_daemon || exit 1
docker_preflight_compose_version || exit 1
docker_preflight_memory
docker_preflight_max_map_count "$ROOT_DIR" "$ENV_EXAMPLE" "$SCRATCH_DIR" || exit 1
docker_preflight_ports "$ENV_FILE" "$ENV_EXAMPLE" || exit 1

step "install: environment file"
if [ -f "$ENV_FILE" ]; then
  echo "  .env already exists — nothing is overwritten; only empty or placeholder values are filled"
else
  umask 077
  cp "$ENV_EXAMPLE" "$ENV_FILE" || die "install" "could not create $ENV_FILE from .env.example"
  chmod 600 "$ENV_FILE" 2>/dev/null || true
  echo "  created .env from .env.example"
fi
# Phase 322 (D-02, Pitfall 6): a 1.0.0 .env gains ONLY the missing DB_APP_* lines
# here, before the fill — fill_env_placeholders' closing loop refuses a key the
# file does not carry, so the upgrade would abort on the first rerun otherwise.
# Append-only: every existing line (DB_USER/DB_PASSWORD included) is untouched.
append_missing_app_db_keys "$ENV_FILE" "$ENV_EXAMPLE" || exit 1

step "install: default network"
docker_preflight_subnet "$ENV_FILE" || exit 1

step "install: generating secrets"
fill_env_placeholders "$ENV_FILE" || exit 1
echo "  filled ${GENERATED_SECRET_COUNT} secret value(s)"

step "install: starting the stack (docker compose up -d --build)"
# `</dev/null`: the build inside `up` may otherwise inherit and consume the
# caller's stdin (the same class as the truncation in lib/secrets.sh — see
# .planning/WINDOWS.md). Nothing here reads stdin, so closing it is safe.
( cd "$ROOT_DIR" && docker compose up -d --build </dev/null ) || die "install" "docker compose up -d --build failed"

step "install: waiting for readiness"
wait_for_backend_healthy "$ROOT_DIR" "$HEALTH_TIMEOUT" || exit 1

# Upgrade finding (1.0.0 -> 1.0.1 on host B): with the containerd image store
# (Docker Engine 29+, Docker Desktop) every `--build` produces a NEW image index
# — BuildKit's provenance attestation carries the build time — even when the
# platform manifest is byte-identical. Compose compares the manifest, keeps the
# running container, and the tag moves on to the new index: the container then
# runs on an untagged image and every container list shows it as a bare
# `sha256:…` (seen for opensearch, whose content did not change). Recreating
# exactly the containers whose image no longer is the one their tag names puts
# each of them back on its tagged image; a cold install finds none.
_stale=""
for _cid in $( cd "$ROOT_DIR" && docker compose ps -q </dev/null 2>/dev/null ); do
  _svc="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.service"}}' "$_cid" 2>/dev/null || true)"
  _ref="$(docker inspect -f '{{.Config.Image}}' "$_cid" 2>/dev/null || true)"
  _running="$(docker inspect -f '{{.Image}}' "$_cid" 2>/dev/null || true)"
  [ -n "$_svc" ] && [ -n "$_ref" ] && [ -n "$_running" ] || continue
  _current="$(docker image inspect -f '{{.Id}}' "$_ref" 2>/dev/null || true)"
  [ -n "$_current" ] && [ "$_current" != "$_running" ] || continue
  _stale="$_stale $_svc"
done
if [ -n "$_stale" ]; then
  step "install: recreating services whose image tag now names a rebuilt image:$_stale"
  # shellcheck disable=SC2086 # Compose service names, one per word
  ( cd "$ROOT_DIR" && docker compose up -d --no-deps --force-recreate $_stale </dev/null ) \
    || die "install" "recreating$_stale failed"
  wait_for_backend_healthy "$ROOT_DIR" "$HEALTH_TIMEOUT" || exit 1
fi

# Phase 318 (host-B finding): the worker scans the plugin directory at its own
# start. On a cold install it starts together with the backend, i.e. while
# `backend/start.sh` is still running the plugin migration runner, and isolates
# every plugin whose migrations are not applied yet as "Plugin database
# migrations are not applied … restart or update the worker" — jira and
# ai-translation in the observed run. Their task types then report
# `dispatchable: false` until someone restarts the worker by hand, which is a
# broken first run for a shipped plugin. Restarting the worker now that the
# backend reported healthy makes the scan see the migrated state; this is the
# same order `scripts/docker/install_plugin.sh` uses for the same reason
# (dd9aed454, afa63ba46).
step "install: restarting the worker so its plugin registry scans the migrated state"
( cd "$ROOT_DIR" && docker compose restart worker </dev/null ) \
  || die "install" "docker compose restart worker failed"

# Upgrade finding (1.0.0 -> 1.0.1 on host B): nginx and loki read their
# configuration from a SINGLE-FILE bind mount. The upgrade's `git checkout`
# replaces that file with a new one, while a running container stays attached to
# the old file — and `up -d` above does not recreate either service, because
# neither its image nor its service definition changed. nginx then kept proxying
# the SPA to the 1.0.0 upstream (`frontend:5173`) and answered every page with
# 502; `nginx -s reload` re-reads the same stale file. Recreating attaches the
# checked-out file. A cold install pays one nginx restart; loki is recreated only
# when the `logging` profile actually runs it.
step "install: recreating nginx so it reads the checked-out configuration"
( cd "$ROOT_DIR" && docker compose up -d --no-deps --force-recreate nginx </dev/null ) \
  || die "install" "recreating nginx failed"
if ( cd "$ROOT_DIR" && docker compose ps --status running --services </dev/null 2>/dev/null ) | grep -qx loki; then
  step "install: recreating loki so it reads the checked-out configuration"
  ( cd "$ROOT_DIR" && docker compose up -d --no-deps --force-recreate loki </dev/null ) \
    || die "install" "recreating loki failed"
fi

HTTP_PORT_VALUE="$(docker_resolve_port "$ENV_FILE" "$ENV_EXAMPLE" HTTP_PORT 8180)"
HTTPS_PORT_VALUE="$(docker_resolve_port "$ENV_FILE" "$ENV_EXAMPLE" HTTPS_PORT 8443)"
wait_for_nginx "$HTTP_PORT_VALUE" "$NGINX_TIMEOUT" || exit 1

step "install: done"
echo "IndraMesh is running."
echo "  URL:    https://localhost:${HTTPS_PORT_VALUE}"
echo "          The bundled certificate is self-signed — accept the browser warning."
echo "  Login:  admin"
if [ -n "${GENERATED_ADMIN_PASSWORD:-}" ]; then
  echo "  Password: ${GENERATED_ADMIN_PASSWORD}"
  echo "          Generated by this run. It is used only when the admin user is first"
  echo "          created; changing ADMIN_PASSWORD in .env later has no effect — change"
  echo "          the password in the UI instead."
else
  echo "  Password: already set before this run — see ADMIN_PASSWORD in .env"
fi
echo "  (The log stack is profile-gated: enable it with COMPOSE_PROFILES=logging in .env.)"
