#!/bin/bash
set -e

echo "Running database migrations..."
alembic upgrade head

echo "Bootstrapping plugin database role..."
# Phase 287.1 (PLUGDB-07, D-03/D-04): the PostgreSQL-enforced plugin/core
# boundary. Runs after the core migration — so a fresh core revision's tables
# are covered by the per-table grants — and before the plugin runner. With the
# role credentials configured a failure here is fatal under `set -e` and stops
# the script before the runner; with no credentials it logs and exits 0 (the
# documented weakened fallback, D-09/D-10).
python -m app.plugin_system.bootstrap_role

echo "Running plugin database migrations..."
# Phase 287 (PLUGDB-03/D-16): a SEPARATE command after the core migration. The
# core `alembic upgrade head` above stays fatal under `set -e`; the runner is
# designed to exit 0 on a per-plugin failure so one broken plugin never aborts
# the Compose start.
python -m app.plugin_system.migrate

echo "Starting application..."
# Phase 275 (COMPOSE-PROD-01, D-07/D-08): only the exact ENVIRONMENT=production
# value starts Uvicorn without --reload; every missing, development, or unknown
# value retains the current reload command. Migrations above stay unconditional.
# Phase 298 (D-18): --no-access-log disables Uvicorn's own plaintext access log
# in every mode; the central logging layer emits exactly one structured `access`
# record per request (RequestContextMiddleware). Keep this flag appended — the
# deploy-wiring test reads these exact command shapes.
if [ "${ENVIRONMENT:-}" = "production" ]; then
  exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --no-access-log
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --no-access-log