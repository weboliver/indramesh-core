#!/bin/sh
# indramesh-postgres-entrypoint.sh — wrapper entrypoint of the own Postgres
# image (Phase 312.1, D-10/D-12, PGAGE-02). The image's ENTRYPOINT points here;
# the official entrypoint still does all the normal work (initdb on a fresh
# directory, then exec the server with the original arguments).
#
# Why the write must happen HERE and not later: shared_preload_libraries and
# wal_level are POSTMASTER-level restart parameters — they are only read while
# the postmaster starts. Writing them before the server starts makes them
# effective on this very start, without a second restart and without any
# operator-visible `command:`/`args:` anywhere (D-12). From then on they are
# ordinary restart-time settings of the data directory.
set -eu

: "${PGDATA:?PGDATA must be set (the image default or the pod's data-directory path)}"

# The one shared definition of the settings, also sourced by the initdb script.
. /usr/local/lib/indramesh-pg-settings.sh

if [ -s "$PGDATA/PG_VERSION" ]; then
  # Initialized data directory (an existing Compose volume, an existing
  # Kubernetes PVC): apply the settings idempotently before the server starts.
  indramesh_apply_server_settings "$PGDATA"
fi
# A fresh directory carries no PG_VERSION yet: initdb runs below inside the
# official entrypoint, and /docker-entrypoint-initdb.d/00-indramesh-server-settings.sh
# applies the same settings to it right after initdb.

exec /usr/local/bin/docker-entrypoint.sh "$@"
