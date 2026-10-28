#!/bin/bash
# install_indramesh_k8s.sh — root bootstrap entrypoint (D-01). Thin root-relative
# wrapper that execs the maintained installer; the documented standard
# command is ./install_indramesh_k8s.sh.
# It must stay a wrapper: all orchestration lives in scripts/k8s/install.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/k8s/install.sh" "$@"