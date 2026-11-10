#!/bin/bash
# install_plugin.sh — Docker plugin-install orchestration (PLGI-01, Phase 311.1
# D-10..D-15). Orchestration only: it sources the shared acquisition/staging
# helper and drives one explicit fail-fast sequence. Mirrors the sibling
# scripts/docker/install.sh: the root entry point (install_plugin_docker.sh) is
# a thin exec wrapper over this file.
#
# Why a script exists at all: the plugin bind mount (`./plugins:/opt/indramesh/
# plugins:ro` in docker-compose.yml — a HOST bind mount, not a volume) needs no
# container interaction to receive files, only a `cp` on the host. Picking them
# up does: PluginRegistry discovery (app/plugin_system/__init__.py's
# init_plugin_registry) runs exactly once, at process startup (app/main.py's
# lifespan, app/worker.py's startup_recovery) — there is no live filesystem
# watch, so the restart below is mandatory even though the files are already
# visible inside the running containers the instant they land on the host.
#
# Fixed order (the same order on both stacks, asserted from the recorded command
# log by tests/bootstrap/test_install_plugin.sh):
#   1. identify and validate the source (local directory or allow-listed URL)
#   2. clone a URL source shallowly into a private temporary directory
#   3. stage the tree through the exclusion pass (version-control/CI metadata)
#   4. derive the destination name from the manifest, or take the CLI override
#   5. verify the declared api_version with the Core's own rule in the container
#   6. decide from the installed version: no destination installs, a higher one
#      replaces it, the same one is a no-op and a lower one is refused. No force
#      flag anywhere; a destination without a readable three-part version (for
#      example a directory created by hand) is refused
#   7. copy the STAGED tree to <name>.new, move the installed plugin aside to
#      <name>.old, then move <name>.new into place. <name>.old marks an
#      unfinished update, so a rerun after an abort finishes it either way
#   8. restart backend and worker so both Registry instances re-scan, then remove
#      <name>.old as the last writing step
#
# Compose scope (D-15, T-311.1-12): every `docker compose` invocation runs with
# the working directory set to ROOT_DIR — resolved from this script's own
# location, never from the caller's — and with a closed stdin. An install
# performed in a clone can therefore never restart, or otherwise touch, the
# Compose project the caller's shell happens to resolve.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# INDRAMESH_DOCKER_ROOT is a hermetic-test seam (identical to
# scripts/docker/install.sh); the documented invocation runs this script from a
# repository clone and never sets it.
ROOT_DIR="${INDRAMESH_DOCKER_ROOT:-$(cd "$SCRIPT_DIR/../.." && pwd)}"
export ROOT_DIR

# shellcheck source=./lib/common.sh
. "$SCRIPT_DIR/lib/common.sh"
# The ONE shared source-acquisition/staging helper, resolved relative to this
# file (same pattern the stack libraries use), so it always comes from the
# checkout that is executing rather than from a caller's environment.
# shellcheck source=../lib/plugin_source.sh
. "$(cd "$SCRIPT_DIR/.." && pwd)/lib/plugin_source.sh"

PLUGINS_DIR="$ROOT_DIR/plugins"

usage() {
  echo "usage: $(basename "$0") <source-dir-or-url> [plugin-name] [--ref <ref>]" >&2
  echo "  <source-dir-or-url>  a local directory containing plugin.toml, or a repository URL" >&2
  echo "  [plugin-name]        destination directory name under ./plugins" >&2
  echo "                       (default: the manifest's own name field)" >&2
  echo "  [--ref <ref>]        tag or branch to clone (URL sources only)" >&2
  exit 2
}

# ---------------------------------------------------------------------------
# Argument parsing. A source may never begin with a dash, and it is refused
# before it could be read as an option anywhere (T-311.1-15).
# ---------------------------------------------------------------------------
SRC=""
DEST_NAME=""
REF=""
while [ "$#" -gt 0 ]; do
  case "$1" in
    --ref)
      shift
      [ "$#" -gt 0 ] || die "install_plugin" "--ref needs a value (a tag or branch name)"
      REF="$1"
      ;;
    --ref=*) REF="${1#--ref=}" ;;
    -*)
      die "install_plugin" "unexpected option '$1' — a source must not begin with a dash; pass a local plugin directory or a repository URL"
      ;;
    *)
      if [ -z "$SRC" ]; then
        SRC="$1"
      elif [ -z "$DEST_NAME" ]; then
        DEST_NAME="$1"
      else
        die "install_plugin" "unexpected extra argument '$1'"
      fi
      ;;
  esac
  shift
done
[ -n "$SRC" ] || usage

# ---------------------------------------------------------------------------
# The private temporary directory is registered on the exit trap FIRST, so the
# clone/stage scratch space is removed on every path — success or failure — and
# never survives an abort. plugin_source_cleanup is a no-op when the directory
# was never created.
# ---------------------------------------------------------------------------
trap plugin_source_cleanup EXIT

step "install_plugin: identifying the source"
KIND="$(plugin_source_kind "$SRC")" || exit 1

plugin_source_mktemp >/dev/null

SRC_TREE="$SRC"
if [ "$KIND" = "url" ]; then
  step "install_plugin: cloning the plugin repository (shallow, host-side)"
  SRC_TREE="${PLUGIN_SOURCE_TMP_DIR}/source"
  plugin_clone "$SRC" "$SRC_TREE" "$REF" || exit 1
fi

step "install_plugin: staging the plugin (version-control and CI metadata excluded)"
plugin_stage "$SRC_TREE" "${PLUGIN_SOURCE_TMP_DIR}/staged" >/dev/null || exit 1

step "install_plugin: deriving the destination name from the manifest"
if [ -n "$DEST_NAME" ]; then
  plugin_validate_name "$DEST_NAME" || exit 1
  NAME="$DEST_NAME"
else
  NAME="$(plugin_derive_name "$STAGED_DIR")" || exit 1
fi

# D-13: the API check precedes the destination check on both stacks, and it is
# a refusal on anything but a clean compatible verdict.
step "install_plugin: verifying the plugin API version in the running Core (before any copy)"
DECLARED_API="$(plugin_manifest_api_version "$STAGED_DIR")" || exit 1
plugin_verify_api_docker "$ROOT_DIR" "$DECLARED_API" || exit 1

DEST="$PLUGINS_DIR/$NAME"
step "install_plugin: checking the installed version"
# A leftover staging directory from an aborted run is never trusted; the copy
# below recreates it. `<name>.old` is NOT removed here: it is the marker of an
# unfinished update and is only removed after the restarts succeed.
rm -rf "$DEST.new"

IS_INSTALL=1
IS_UPDATE=0
if [ -e "$DEST" ]; then
  IS_INSTALL=0
  INSTALLED_VERSION="$(plugin_manifest_version "$DEST")" \
    || die "install_plugin" "$DEST already exists but carries no readable three-part version in its plugin.toml — refusing to replace a directory this script did not install (remove it deliberately first if you mean to replace it)"
  NEW_VERSION="$(plugin_manifest_version "$STAGED_DIR")" || exit 1
  ACTION="$(plugin_update_action "$INSTALLED_VERSION" "$NEW_VERSION")" || exit 1
  case "$ACTION" in
    refuse)
      die "install_plugin" "$DEST is at version $INSTALLED_VERSION and the source carries $NEW_VERSION — refusing a downgrade (plugin migrations only run forward)"
      ;;
    same)
      if [ ! -e "$DEST.old" ]; then
        step "install_plugin: $NAME is already at version $NEW_VERSION — nothing to do"
        echo "  The installed plugin is unchanged: no copy and no restart was performed."
        exit 0
      fi
      step "install_plugin: resuming an unfinished update of $NAME to version $NEW_VERSION"
      ;;
    replace)
      step "install_plugin: updating $NAME from version $INSTALLED_VERSION to $NEW_VERSION"
      IS_UPDATE=1
      ;;
  esac
fi

if [ "$IS_INSTALL" = 1 ] || [ "$IS_UPDATE" = 1 ]; then
  step "install_plugin: copying the staged plugin to $DEST.new"
  mkdir -p "$PLUGINS_DIR"
  # The STAGED tree, never the source as found: the exclusion pass above is what
  # keeps version-control and CI metadata out of the plugin directory. The copy
  # lands in `<name>.new`, so a half-written tree is never visible as a plugin.
  cp -R "$STAGED_DIR" "$DEST.new" || die "install_plugin" "copying $STAGED_DIR -> $DEST.new failed"
  if [ "$IS_INSTALL" = 0 ]; then
    rm -rf "$DEST.old"
    mv "$DEST" "$DEST.old" || die "install_plugin" "moving $DEST aside to $DEST.old failed"
  fi
  mv "$DEST.new" "$DEST" || die "install_plugin" "moving $DEST.new into place failed"
fi

step "install_plugin: restarting the backend so PluginRegistry re-scans and the plugin migrations run"
( cd "$ROOT_DIR" && docker compose restart backend </dev/null ) || exit 1
# Phase 318 (host-B finding): the worker must NOT restart in the same breath as
# the backend. The backend applies this plugin's database migrations during
# startup; a worker that scans first isolates the plugin as "migrations are not
# applied" and keeps it there until the next restart — reproduced on a fresh
# release stack (2 of 5 plugins had no worker_loaded_at, the admin page said
# "Worker restart required"). Wait for the backend to report healthy first.
_backend_cid="$( cd "$ROOT_DIR" && docker compose ps -q backend </dev/null 2>/dev/null | head -n1 )"
if [ -n "$_backend_cid" ]; then
  _waited=0
  while [ "$_waited" -lt 120 ]; do
    _health="$( cd "$ROOT_DIR" && docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$_backend_cid" 2>/dev/null || true )"
    if [ "$_health" = "healthy" ]; then break; fi
    sleep 2
    _waited=$((_waited + 2))
  done
  step "install_plugin: backend reported '${_health:-unknown}' after ${_waited}s — restarting the worker"
fi

step "install_plugin: restarting the worker so its registry re-scans the migrated plugin"
( cd "$ROOT_DIR" && docker compose restart worker </dev/null ) || exit 1
# A restart re-attaches the containers and can hand the backend a NEW IP, while
# nginx resolved its upstream once at its own start. Every /api call then answers
# 502 until nginx is reloaded — measured twice on a fresh release stack
# ("connect() failed … upstream: http://172.30.0.7:8000" while the backend sat on
# .8). A reload re-resolves the name; it is a no-op when the name still points at
# the same address, and a missing nginx must not fail the install.
( cd "$ROOT_DIR" && docker compose exec -T nginx nginx -s reload </dev/null ) >/dev/null 2>&1 || true

# The update marker goes last: while `<name>.old` exists the update is not
# finished, so a rerun after an abort at any earlier step finishes it instead of
# reporting "nothing to do". Kept in place until every restart above succeeded.
step "install_plugin: removing the update marker $DEST.old"
rm -rf "$DEST.old"

step "install_plugin: done — check the Plugins admin page"
echo "  GET /api/v1/plugins/$NAME reports the plugin's state once backend and worker are back."
# Phase 318 (host-B clean-host finding): a plugin that needs an optional core
# feature must say so at install time — but only when the feature is actually
# missing. The log stack is profile-gated and off by default, so log-explorer
# would otherwise land "active" and answer "not configured" with no hint about
# what to change (and a blind hint would be wrong for an operator who enabled it).
LOG_STORE_ON=false
case "${COMPOSE_PROFILES:-}" in *logging*) LOG_STORE_ON=true ;; esac
[ -n "${LOKI_QUERY_URL:-}" ] && LOG_STORE_ON=true
if [ "$LOG_STORE_ON" = false ] && [ -f "$ROOT_DIR/.env" ]; then
  case "$(grep -E '^COMPOSE_PROFILES=' "$ROOT_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2-)" in
    *logging*) LOG_STORE_ON=true ;;
  esac
  grep -qE '^LOKI_QUERY_URL=.+' "$ROOT_DIR/.env" 2>/dev/null && LOG_STORE_ON=true
fi
if [ "$(plugin_manifest_value "$DEST" requires_log_store)" = "true" ] && [ "$LOG_STORE_ON" = false ]; then
  echo
  echo "  NOTE: $NAME needs a log store, and the log stack is OFF by default."
  echo "        Enable it in the root .env with COMPOSE_PROFILES=logging (Loki + Alloy),"
  echo "        or point LOKI_QUERY_URL at an external Loki, then run:"
  echo "          docker compose -p \${COMPOSE_PROJECT_NAME:-indramesh} up -d"
  echo "        Until then the plugin stays installed but reports 'not configured'."
fi
# Phase 318 (host-B no-LLM finding): an AI plugin hides its actions while the
# Core has no LLM key, so without this note it lands "active" and simply shows
# nothing. The key is read from the shell or the root .env; a set key prints no
# note (whether the provider accepts it is the Plugins admin page's job).
LLM_KEY_SET=false
[ -n "${LLM_API_KEY:-}" ] && LLM_KEY_SET=true
if [ "$LLM_KEY_SET" = false ] && [ -f "$ROOT_DIR/.env" ]; then
  grep -qE '^LLM_API_KEY=.+' "$ROOT_DIR/.env" 2>/dev/null && LLM_KEY_SET=true
fi
if [ "$(plugin_manifest_value "$DEST" requires_llm)" = "true" ] && [ "$LLM_KEY_SET" = false ]; then
  echo
  echo "  NOTE: $NAME needs an LLM, and none is configured (LLM_API_KEY is empty)."
  echo "        Set LLM_API_KEY and LLM_MODEL (see the LLM block in .env.example) in"
  echo "        the root .env, then run:"
  echo "          docker compose -p \${COMPOSE_PROJECT_NAME:-indramesh} up -d backend worker"
  echo "        Until then the plugin stays installed but shows no AI actions."
fi
