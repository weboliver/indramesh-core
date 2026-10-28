#!/bin/bash
# install_plugin.sh — install a plugin onto the shared `plugins-storage` claim
# from a local directory OR a repository URL, then restart backend+worker so
# their PluginRegistry picks it up (PLGI-01, Phase 311.1 D-10..D-14). Mirrors
# rebuild.sh's disposable-Pod pattern (nginx-validate-pod.yaml: committed
# manifest, sed-rendered into a private tmpdir, applied, waited on with a
# bounded timeout, deleted unconditionally on exit) rather than an inline
# --overrides blob.
#
# Why a script exists at all instead of documenting raw kubectl commands
# (2026-09-22 finding): backend-deployment.yaml, worker-deployment.yaml and
# migration-job.yaml's plugin-migration Job all mount plugins-storage
# `readOnly: true` — no running Pod can be written into. Placing a plugin
# therefore needs its own writable Pod, a `kubectl cp`, a teardown, and a
# restart of both processes (PluginRegistry discovery runs exactly once, at
# process startup — app/main.py's lifespan, app/worker.py's startup_recovery;
# there is no live filesystem watch). That is four kubectl invocations an
# operator has to get exactly right, in exactly that order, every time.
#
# The acquisition/staging half — source identification, the shallow host-side
# clone, the version-control/CI exclusion pass, the manifest-derived name, the
# API-version verdict and the private temporary directory — lives ONCE in
# scripts/lib/plugin_source.sh, shared with the Docker path, so the two stacks
# cannot drift apart. This file owns only what is stack-specific.
#
# Sequence (fail-fast at every step; the same order as the Docker path):
#   1. identify the source: local directory holding plugin.toml, or an
#      allow-listed repository URL; refuse anything else
#   2. clone a URL source shallowly into the private temporary directory
#   3. stage the tree through the exclusion pass (STAGED_DIR is what gets copied)
#   4. derive the destination name from the manifest, or take the CLI override,
#      and validate it under the loader's own character class (T-282-04)
#   5. verify the declared api_version against the DEPLOYED backend, which runs
#      the Core's own rule — before any destination or copy work (D-13)
#   6. preflight the destination: cluster reachable, claim slot free
#   7. discover the CURRENTLY DEPLOYED backend image (never a hardcoded tag) and
#      render the disposable copy-Pod from it; apply it, wait for Running
#      (bounded), `kubectl cp` the STAGED tree in, delete the Pod
#   8. run the plugin migration runner in the live backend pod (k8s never
#      self-migrates, unlike Compose's start.sh) — BEFORE the restart, so one
#      restart suffices
#   9. rollout restart backend + worker, wait for both bounded
set -euo pipefail

usage() {
  echo "usage: $(basename "$0") <source-dir-or-url> [plugin-name] [--ref <ref>]" >&2
  echo "  <source-dir-or-url>  a local directory containing plugin.toml, or a repository URL" >&2
  echo "  [plugin-name]        destination directory name under plugins-storage" >&2
  echo "                       (default: the manifest's own name field)" >&2
  echo "  [--ref <ref>]        tag or branch to clone (URL sources only)" >&2
  exit 2
}

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LIB_DIR="$SCRIPT_DIR/lib"
K8S_DIR="$SCRIPT_DIR"
ROOT_DIR="$(cd "$SCRIPT_DIR/../.." && pwd)"

. "$LIB_DIR/common.sh"
. "$LIB_DIR/namespace.sh"
# The ONE shared acquisition/staging helper (Phase 311.1 D-10..D-12, D-13).
. "$ROOT_DIR/scripts/lib/plugin_source.sh"

[ -f "$ROOT_DIR/install_plugin_k8s.sh" ] \
  || die "install_plugin" "ROOT_DIR resolved to '$ROOT_DIR' — expected the repository root"

kubectl_indramesh() { sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"; }

COPY_POD_NAME="indramesh-plugin-copy"
COPY_POD_MANIFEST="${INDRAMESH_PLUGIN_COPY_MANIFEST:-$K8S_DIR/plugin-copy-pod.yaml}"
COPY_POD_TIMEOUT="${INDRAMESH_PLUGIN_COPY_TIMEOUT:-60s}"
ROLLOUT_TIMEOUT="${INDRAMESH_PLUGIN_ROLLOUT_TIMEOUT:-180s}"

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
# Cleanup: the copy Pod, its rendered-manifest tmpdir AND the private
# clone/stage directory are always removed, success or failure (mirrors
# rebuild.sh's NGINX_VALIDATE_POD_NAME trap). All three variables are declared
# empty here and populated below — the trap body is single-quoted (delayed
# expansion), so it always sees their value at EXIT time, and the guards keep
# every removal a no-op on a path that never created the directory.
# ---------------------------------------------------------------------------
TMP_DIR=""
cleanup() {
  kubectl_indramesh delete pod "$COPY_POD_NAME" -n "$NAMESPACE_NAME" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  plugin_source_cleanup || true
  [ -n "${TMP_DIR:-}" ] && rm -rf "$TMP_DIR"
}
trap cleanup EXIT

# ---------------------------------------------------------------------------
# Acquisition and staging (shared helper), in the fixed order.
# ---------------------------------------------------------------------------
plugin_source_mktemp >/dev/null

step "install_plugin: identifying the source"
KIND="$(plugin_source_kind "$SRC")" || exit 1

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

# D-13: the verdict comes from the deployed backend — the same Core that will
# load the plugin after the restart — and anything but a clean compatible one
# aborts here, before any destination or copy work.
step "install_plugin: verifying the plugin API version against the deployed backend (before any copy)"
DECLARED_API="$(plugin_manifest_api_version "$STAGED_DIR")" || exit 1
plugin_verify_api_k8s "$DECLARED_API" || exit 1

step "install_plugin: checking the destination is free"
if kubectl_indramesh get pod "$COPY_POD_NAME" -n "$NAMESPACE_NAME" >/dev/null 2>&1; then
  step "install_plugin: removing a leftover copy Pod from a previous run"
  kubectl_indramesh delete pod "$COPY_POD_NAME" -n "$NAMESPACE_NAME" --wait=true >/dev/null 2>&1 || true
fi
if kubectl_indramesh exec -n "$NAMESPACE_NAME" deploy/indramesh-backend -- \
    test -e "/opt/indramesh/plugins/$NAME" 2>/dev/null; then
  die "install_plugin" "/opt/indramesh/plugins/$NAME already exists on the claim — remove it deliberately first if you mean to replace it (this script never overwrites)"
fi

step "install_plugin: discovering the currently deployed backend image"
BACKEND_IMAGE="$(kubectl_indramesh get deployment indramesh-backend -n "$NAMESPACE_NAME" \
  -o jsonpath='{.spec.template.spec.containers[0].image}')"
[ -n "$BACKEND_IMAGE" ] || die "install_plugin" "could not read the deployed backend image — is the release healthy?"
step "install_plugin: using $BACKEND_IMAGE (guaranteed restricted-PSA-compatible: the same image backend/worker already run under)"

TMP_DIR="$(mktemp -d)"
RENDERED_POD_MANIFEST="$TMP_DIR/plugin-copy-pod.yaml"
sed "s#__IMAGE__#$BACKEND_IMAGE#" "$COPY_POD_MANIFEST" > "$RENDERED_POD_MANIFEST"

step "install_plugin: applying the disposable writable copy Pod"
kubectl_indramesh apply -f "$RENDERED_POD_MANIFEST" || exit 1

step "install_plugin: waiting for the copy Pod to be Running (bounded $COPY_POD_TIMEOUT)"
if ! kubectl_indramesh wait --for=jsonpath='{.status.phase}'=Running "pod/$COPY_POD_NAME" \
    -n "$NAMESPACE_NAME" --timeout="$COPY_POD_TIMEOUT"; then
  preflight_error "install_plugin: copy Pod never reached Running — see events below"
  kubectl_indramesh describe pod "$COPY_POD_NAME" -n "$NAMESPACE_NAME" >&2 || true
  exit 1
fi

# The STAGED tree (shared helper), never the source as found: the exclusion pass
# is what keeps version-control and CI metadata off the claim (D-12).
step "install_plugin: copying the staged plugin to plugins-storage:/opt/indramesh/plugins/$NAME"
kubectl_indramesh cp "$STAGED_DIR" "$NAMESPACE_NAME/$COPY_POD_NAME:/opt/indramesh/plugins/$NAME" \
  || die "install_plugin" "kubectl cp failed — the copy Pod's logs/events may explain why"

step "install_plugin: removing the copy Pod"
kubectl_indramesh delete pod "$COPY_POD_NAME" -n "$NAMESPACE_NAME" --wait=false >/dev/null 2>&1 || true

# 2026-09-22 finding: unlike Compose (backend/start.sh runs `alembic upgrade
# head && python -m app.plugin_system.migrate` on EVERY container start, so a
# bare `docker compose restart` re-migrates for free), the k8s backend
# Deployment never self-migrates (PLUGDB-03: "Backend- und Worker-Startup
# führen keine Migrationen aus") — migrations run ONLY from the dedicated
# Helm pre-upgrade/post-upgrade Jobs, which a plain restart never triggers.
# Without this step, a plugin declaring `[database]` lands in `error`
# ("migrations pending") on the very next backend/worker startup, correctly
# refused by the Phase 287-02 activation gate — not a bug in that gate, a real
# gap in only copying + restarting. Run the SAME command the post-upgrade
# migration Job runs, via exec in the just-populated backend pod (already has
# DB_* and INDRAMESH_PLUGINS_DIR; the readOnly plugins mount is irrelevant here
# — the runner only reads plugin files and writes to Postgres). Ordered BEFORE
# the restart below: the activation gate runs once at startup, so migrating
# first means one restart suffices instead of two. Safe to run even for a
# plugin with no [database] section — the runner discovers zero
# database-owning plugins for it and does nothing extra.
step "install_plugin: running pending plugin database migrations (mirrors the post-upgrade migration Job)"
kubectl_indramesh exec -n "$NAMESPACE_NAME" deploy/indramesh-backend -- \
  python -m app.plugin_system.migrate \
  || die "install_plugin" "plugin migration runner failed at the core level — see the backend pod's logs (this is distinct from a single plugin's own migration failing, which the runner always isolates and exits 0 for)"

# Phase 318 (host-B finding): backend first and WAIT, then the worker. The
# backend applies this plugin's migrations during startup; a worker that scans
# while they are still running isolates the plugin as "migrations are not
# applied" until the next restart — reproduced on a fresh stack (2 of 5 plugins).
step "install_plugin: restarting the backend so PluginRegistry re-scans and the plugin migrations run"
kubectl_indramesh rollout restart deployment/indramesh-backend -n "$NAMESPACE_NAME"
kubectl_indramesh rollout status deployment/indramesh-backend -n "$NAMESPACE_NAME" --timeout="$ROLLOUT_TIMEOUT" \
  || die "install_plugin" "backend rollout did not complete in time"

step "install_plugin: restarting the worker so its registry re-scans the migrated plugin"
kubectl_indramesh rollout restart deployment/indramesh-worker -n "$NAMESPACE_NAME"
kubectl_indramesh rollout status deployment/indramesh-worker -n "$NAMESPACE_NAME" --timeout="$ROLLOUT_TIMEOUT" \
  || die "install_plugin" "worker rollout did not complete in time"

step "install_plugin: done — check the Plugins admin page, or:"
echo "  KUBECONFIG=\$HOME/.kube/config kubectl exec -n $NAMESPACE_NAME deploy/indramesh-backend -- curl -s http://localhost:8000/api/v1/plugins/$NAME"
# Phase 318 (host-B clean-host finding): a plugin that needs an optional core
# feature must say so at install time — but only when the feature is actually
# missing. The k8s log stack is opt-in (LOGINFRA_ENABLED), so log-explorer would
# otherwise land "active" and answer "not configured" with no hint about what to
# change (and a blind hint would be wrong for an operator who enabled it).
LOG_STORE_ON=false
case "${LOGINFRA_ENABLED:-}" in true) LOG_STORE_ON=true ;; esac
if [ "$LOG_STORE_ON" = false ] && [ -f "$ROOT_DIR/.env" ]; then
  case "$(grep -E '^LOGINFRA_ENABLED=' "$ROOT_DIR/.env" 2>/dev/null | tail -n1 | cut -d= -f2-)" in
    true) LOG_STORE_ON=true ;;
  esac
fi
if [ "$(plugin_manifest_value "$STAGED_DIR" requires_log_store)" = "true" ] && [ "$LOG_STORE_ON" = false ]; then
  echo
  echo "  NOTE: $NAME needs a log store, and the k3s log stack is OFF by default."
  echo "        Set LOGINFRA_ENABLED=true in .env and re-run ./install_indramesh_k8s.sh"
  echo "        (that installs Loki + Alloy and points the plugin at them), or set"
  echo "        apps.backend.lokiQueryUrl to an external Loki in your values file."
  echo "        Until then the plugin stays installed but reports 'not configured'."
fi
# Phase 318 (host-B no-LLM finding): an AI plugin hides its actions while the
# Core has no LLM key, so without this note it lands "active" and simply shows
# nothing. The key is read from the shell or the root .env, the source the
# installer copies into the indramesh-external-api Secret.
LLM_KEY_SET=false
[ -n "${LLM_API_KEY:-}" ] && LLM_KEY_SET=true
if [ "$LLM_KEY_SET" = false ] && [ -f "$ROOT_DIR/.env" ]; then
  grep -qE '^LLM_API_KEY=.+' "$ROOT_DIR/.env" 2>/dev/null && LLM_KEY_SET=true
fi
if [ "$(plugin_manifest_value "$STAGED_DIR" requires_llm)" = "true" ] && [ "$LLM_KEY_SET" = false ]; then
  echo
  echo "  NOTE: $NAME needs an LLM, and none is configured (LLM_API_KEY is empty)."
  echo "        Set LLM_API_KEY in .env and re-run ./install_indramesh_k8s.sh (the key"
  echo "        lands in the indramesh-external-api Secret; provider and model come"
  echo "        from LLM_PROVIDER / LLM_MODEL / LLM_BASE_URL in .env). A hosted provider on"
  echo "        port 443 needs no egress step; a private endpoint is admitted by the"
  echo "        installer or through K8S_EGRESS_ALLOW (docs/CONFIGURATION.md)."
  echo "        Until then the plugin stays installed but shows no AI actions."
fi
