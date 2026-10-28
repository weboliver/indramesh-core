#!/bin/bash
# rebuild_indramesh_k8s.sh — root rebuild entrypoint (D-20). Thin root-relative
# wrapper that execs the maintained orchestration; the documented standard
# command is ./rebuild_indramesh_k8s.sh [backend|frontend|nginx].
# It must stay a wrapper: all orchestration lives in scripts/k8s/rebuild.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/k8s/rebuild.sh" "$@"
