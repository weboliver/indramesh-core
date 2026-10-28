#!/bin/bash
# install_plugin_k8s.sh — root entrypoint (mirrors install_indramesh_k8s.sh /
# rebuild_indramesh_k8s.sh). Thin wrapper; all orchestration lives in
# scripts/k8s/install_plugin.sh.
#
# Usage: ./install_plugin_k8s.sh <source-dir-or-url> [plugin-name] [--ref <ref>]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/k8s/install_plugin.sh" "$@"
