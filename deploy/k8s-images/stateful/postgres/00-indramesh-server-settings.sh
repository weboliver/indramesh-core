#!/bin/sh
# 00-indramesh-server-settings.sh — fresh-initialization path of the own Postgres
# image's baked-in server settings (Phase 312.1, D-12, PGAGE-02).
#
# This file is installed into /docker-entrypoint-initdb.d, which the official
# entrypoint executes ONLY during the first initdb of a FRESH data directory
# (it starts a socket-only temporary server and runs the directory's scripts).
# Every later start goes through the wrapper entrypoint instead, which applies
# the same settings to the initialized directory.
#
# The settings values live in exactly one place: the sourced library.
set -eu

: "${PGDATA:?PGDATA must be set (the image default or the pod's data-directory path)}"

. /usr/local/lib/indramesh-pg-settings.sh

indramesh_apply_server_settings_fresh_init "$PGDATA"
