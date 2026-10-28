#!/bin/sh
set -e

# Phase 235 (CR-01 fix): the ./frontend/locales bind mount keeps the host's UID/GID,
# which almost never matches the image's appuser (default 1001) — the export feature
# would get a permission error on essentially any dev host. Running as root here lets
# us open up write access before dropping to appuser for the actual process, so the
# export works regardless of host UID/GID with no manual LOCAL_UID/LOCAL_GID step.
# chmod, not chown: chown would flip the bind-mounted directory to appuser-owned on
# the HOST side too (same inode), locking the host user out of editing these files
# directly without sudo — a worse trade than the permission error it fixes.
if [ "$(id -u)" = "0" ]; then
  if [ -d /workspace/frontend-locales ]; then
    chmod -R a+rwX /workspace/frontend-locales 2>/dev/null || true
  fi
  # setpriv drops uid/gid but does not touch HOME — left at /root it makes
  # libraries that probe $HOME (e.g. asyncpg's default SSL key lookup at
  # ~/.postgresql/postgresql.key) hit a permission error as non-root.
  export HOME=/home/appuser
  exec setpriv --reuid=appuser --regid=appuser --init-groups "$@"
fi

exec "$@"
