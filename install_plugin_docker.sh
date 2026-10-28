#!/bin/bash
# install_plugin_docker.sh — root entrypoint (mirrors install_plugin_k8s.sh /
# install_indramesh_docker.sh). Thin wrapper; all orchestration lives in
# scripts/docker/install_plugin.sh — the same convention its Kubernetes twin
# already follows. The root path stays stable for the documented one-liners in
# every plugin README and for the publish allowlist.
#
# Usage: ./install_plugin_docker.sh <source-dir-or-url> [plugin-name] [--ref <ref>]
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/scripts/docker/install_plugin.sh" "$@"
