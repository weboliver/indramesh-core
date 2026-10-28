#!/bin/bash
# secrets.sh — Docker quickstart secret lifecycle (Phase 311; D-06, D-07, D-09,
# D-10; shared core in Phase 311.1 D-01). Sourceable; requires common.sh (and
# ROOT_DIR for the container-based key generation).
#
# Contract:
#   * Placeholder detection, generation and fill walk the SECRET key set — wider
#     than the Compose-guarded set, because PLUGIN_DB_PASSWORD also becomes
#     CHANGE_ME and carries no Compose guard, yet a placeholder left there would
#     be a publicly known plugin-role password.
#   * Compare-before-write (D-06): only a value that is empty or the literal
#     placeholder is filled. An already-set value is never rewritten, so a
#     Postgres volume initialised with the old password stays openable.
#   * Every placeholder comparison runs the value through _normalize_env_value
#     (common.sh — the single definition): a trailing CR or one layer of
#     surrounding quotes is removed first, so `CHANGE_ME\r` and `"CHANGE_ME"`
#     are recognised as placeholders instead of passing as already-set values
#     (D-07). A filled value is always written back unquoted and without a CR.
#   * The file is rebuilt and moved into place, so it always holds either the
#     previous or the new complete content — an interrupted or concurrent run
#     cannot leave a half-written .env. It is written under a restrictive umask.
#   * No value ever reaches stdout except the generated ADMIN_PASSWORD, reported
#     once by the caller on the run that generated it (D-10).
#
# Phase 311.1 D-01/D-01a: the generation core — the mandatory key set, the
# placeholder constant, the generator dispatch, the placeholder pre-scan and the
# fill routine — lives ONCE in scripts/lib/secrets.sh and is sourced below,
# BASH_SOURCE-relative. This library defines none of them any more: a second
# definition here is exactly the drift that produced the empty-DB_PASSWORD
# fail-open, and it must not come back. The Docker installer's call sequence
# (scripts/docker/install.sh) is unchanged.

# Double-source guard: sourcing this library twice is a no-op.
if [ -n "${_INDRAMESH_DOCKER_SECRETS_SH_LOADED:-}" ]; then
  return 0
fi
_INDRAMESH_DOCKER_SECRETS_SH_LOADED=1

_DOCKER_SECRETS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./common.sh
. "$_DOCKER_SECRETS_DIR/common.sh"
# The shared generation core, resolved relative to this file (same pattern as
# common.sh -> k8s/lib/common.sh above).
# shellcheck source=../../lib/secrets.sh
. "$(cd "$_DOCKER_SECRETS_DIR/../.." && pwd)/lib/secrets.sh"
