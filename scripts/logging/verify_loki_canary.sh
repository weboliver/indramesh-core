#!/usr/bin/env bash
# verify_loki_canary.sh — in-network proof for LOGINFRA-02 / success criterion 2
# (RESEARCH Pitfall 10): a normal emitted line must reach the bundled Loki
# byte-exactly, while the secret-like 298 canary value must stay ABSENT because
# the upstream redaction blanks it before the collector ever sees it.
#
# Pitfall 10: Loki deliberately publishes NO host port (LOGINFRA-01/Pitfall 7),
# so a host process cannot query it. The probe therefore runs INSIDE the
# `indramesh_logging_internal` Compose network from a throwaway
# `python:3.12-slim` container. The backend itself is NOT attached to that
# network — by design it can never reach `loki:3100`; only Alloy (which pushes
# into Loki) and Grafana are dual-homed onto it.
#
# Emission (Review-Korrektur 2026-10-06): the marker line is emitted on the
# container's OWN PID-1 stdout via `docker compose run --rm --no-deps backend
# python -c ...` — NOT via `docker compose exec`, whose process stdout goes to
# the exec client rather than the container's json-file logstream (Alloy would
# never see it, so the positive proof would always fail and the canary absence
# would be trivially true). The `run` container carries the same Compose labels
# (`com.docker.compose.project=indramesh`, `com.docker.compose.service=backend`),
# so `discovery.docker` collects it and the `service="backend"` query matches.
#
# This is an Orchestrator / wave-merge live check against a running stack from
# the MAIN checkout. It is never executed by an executor (Carry-Ops): creating
# containers and measuring RAM is an operator/orchestrator action.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$ROOT"

COMPOSE_PROJECT="${COMPOSE_PROJECT:-indramesh}"
LOG_NETWORK="${COMPOSE_PROJECT}_logging_internal"
LOKI_QUERY_URL="http://loki:3100/loki/api/v1/query_range"
POLL_ATTEMPTS="${POLL_ATTEMPTS:-30}"
POLL_INTERVAL="${POLL_INTERVAL:-6}"
# discovery.docker refresh_interval defaults to 1m, so the emission container
# must outlive at least one refresh for Alloy to register it. 90s > 60s.
EMIT_LIFETIME_SECONDS="${EMIT_LIFETIME_SECONDS:-90}"

if ! command -v docker >/dev/null 2>&1; then
  echo "FATAL: docker is required (this is an operator/orchestrator live check)." >&2
  exit 1
fi
if ! command -v uuidgen >/dev/null 2>&1; then
  echo "FATAL: uuidgen is required to generate the marker and canary values." >&2
  exit 1
fi
if [ ! -f "$ROOT/.env" ]; then
  echo "FATAL: $ROOT/.env is missing — docker compose needs it to run the backend service." >&2
  exit 1
fi

canary="CANARY-$(uuidgen | tr -d -)"
marker="LOKI-COLLECTION-PROBE-$(uuidgen | tr -d -)"

# Per-process emission: one INFO record whose msg IS the marker, plus a
# secret-like extra field carrying the canary. The 298 RedactionFilter blanks
# the `password` value before the JSON renderer, so the marker must survive and
# the canary must not. Then idle so discovery.docker registers the container.
EMIT_PY='
import logging, os, time
from app.logging import configure_logging

configure_logging("backend")
logging.getLogger("app.canary").info(
    os.environ["MARKER_VAL"], extra={"password": os.environ["CANARY_VAL"]}
)
print("canary-probe emitted", flush=True)
time.sleep(int(os.environ.get("EMIT_LIFETIME_SECONDS", "90")))
'

# In-network query: stdlib-only so it runs in the bare python:3.12-slim image.
# Exit 0 = marker present, exit 2 = canary leaked, exit 1 = marker not yet seen.
POLL_PY='
import datetime, json, os, sys, urllib.parse, urllib.request

url = os.environ["LOKI_QUERY_URL"]
marker = os.environ["MARKER_VAL"]
canary = os.environ["CANARY_VAL"]

def query(expr):
    now = datetime.datetime.now(datetime.timezone.utc)
    start = (now - datetime.timedelta(minutes=15)).strftime("%Y-%m-%dT%H:%M:%SZ")
    end = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    params = urllib.parse.urlencode(
        {"query": expr, "start": start, "end": end, "limit": "200", "direction": "backward"}
    )
    with urllib.request.urlopen(url + "?" + params, timeout=10) as resp:
        data = json.load(resp)
    lines = []
    for stream in (data.get("data", {}) or {}).get("result", []) or []:
        for _ts, line in stream.get("values", []) or []:
            lines.append(line)
    return lines

canary_hits = query("{service=\"backend\"} |= \"%s\"" % canary)
if canary_hits:
    print("CANARY_LEAK")
    sys.exit(2)
marker_hits = query("{service=\"backend\"} |= \"%s\"" % marker)
if marker_hits:
    print("MARKER_FOUND")
    sys.exit(0)
print("MARKER_PENDING")
sys.exit(1)
'

echo "== Loki canary probe =="
echo "marker: $marker"
echo "emitting on the backend PID-1 stdout for ${EMIT_LIFETIME_SECONDS}s (discovery.docker refresh is 1m)..."
# The run-client output is deliberately discarded; only the container's own
# logstream matters (see the Review-Korrektur note above).
LOG_FORMAT=json docker compose run --rm --no-deps -T \
  -e LOG_FORMAT=json \
  -e MARKER_VAL="$marker" \
  -e CANARY_VAL="$canary" \
  -e EMIT_LIFETIME_SECONDS="$EMIT_LIFETIME_SECONDS" \
  backend python -c "$EMIT_PY" >/dev/null 2>&1

echo "polling Loki via the throwaway container on $LOG_NETWORK..."
attempt=0
while [ "$attempt" -lt "$POLL_ATTEMPTS" ]; do
  attempt=$((attempt + 1))
  poll_out=""
  if poll_out="$(docker run --rm --network "$LOG_NETWORK" \
      -e LOKI_QUERY_URL="$LOKI_QUERY_URL" \
      -e MARKER_VAL="$marker" \
      -e CANARY_VAL="$canary" \
      python:3.12-slim python -c "$POLL_PY" 2>&1)"; then
    echo "OK: marker found in Loki and canary absent after $attempt attempt(s)."
    exit 0
  else
    rc=$?
  fi
  if [ "$rc" -eq 2 ]; then
    echo "FAIL: the canary value reached Loki (redaction broke) — probe: $poll_out" >&2
    exit 1
  fi
  echo "[$attempt/$POLL_ATTEMPTS] marker not yet in Loki — retrying in ${POLL_INTERVAL}s"
  sleep "$POLL_INTERVAL"
done

echo "FAIL: marker [$marker] was not found in Loki after $POLL_ATTEMPTS attempts (${POLL_INTERVAL}s each)." >&2
echo "Diagnose: docker compose logs alloy; docker compose logs loki; verify ALLOY_SINK and that the 'logging' profile is active (COMPOSE_PROFILES=logging)." >&2
exit 1
