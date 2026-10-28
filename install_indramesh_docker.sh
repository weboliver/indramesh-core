#!/bin/bash
# install_indramesh_docker.sh — root quickstart entrypoint (Phase 311; D-05).
# Thin root-relative wrapper that execs the maintained installer; the documented
# standard command is ./install_indramesh_docker.sh.
# It must stay a wrapper: all orchestration lives in scripts/docker/install.sh.
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/docker/install.sh" "$@"
