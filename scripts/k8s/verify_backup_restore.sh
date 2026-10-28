#!/bin/bash
# verify_backup_restore.sh — Phase 260's scripted, non-destructive BAK-02
# restore runbook (D-19..D-22). It creates a disposable marker probe in the
# real indramesh database, triggers a fresh backup dump via the indramesh-backup
# CronJob's own proven pg_dump path so the marker is captured, unconditionally
# drops the source-side probe (P-260-02, independent of D-22's separate
# success/failure retention rule for the verification database), restores the
# newest backup into a brand-new indramesh_restore_check database on the SAME
# running Postgres instance (never the production indramesh DB, T-260-07), and
# verifies the restore with a marker-id match plus a public-schema table-count
# and core-table-presence structure check (D-20) — never a bare pg_restore
# exit-code check (research Anti-Pattern: pg_restore's default mode can exit
# non-zero on mere warnings).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

NAMESPACE="indramesh"
RELEASE_NAME="indramesh"
K3S_KUBECONFIG="${INDRAMESH_K3S_KUBECONFIG:-/etc/rancher/k3s/k3s.yaml}"
MARKER_ID="INDRAMESH_PHASE_260_$(date +%s)_$$"
POD_NAME="indramesh-backup-restore-check"

kubectl_indramesh() { sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"; }
verify_error() { echo "[verify-backup-restore] ERROR: $*" >&2; }

JOB_NAME=""

# P-260-02: the source-side probe table is dropped from the real `indramesh`
# database unconditionally, on success or failure, before this function's
# caller ever inspects a pass/fail result. `DROP TABLE IF EXISTS` is
# idempotent, so calling this more than once (explicit call plus the EXIT
# trap below) is always safe.
drop_source_probe() {
  kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -d indramesh -v ON_ERROR_STOP=1 \
    -c "DROP TABLE IF EXISTS _indramesh_backup_restore_probe;" >/dev/null 2>&1 || true
}

# Disposable tooling only (the ephemeral restore-check Pod and the ad-hoc
# backup Job created by this run) — never the D-22 diagnostic evidence, which
# lives in the retained indramesh_restore_check database on failure, not in
# either Kubernetes object. Also re-runs the source-side probe drop as a
# defensive safety net for any exit path before the explicit call below runs.
cleanup_ephemeral() {
  local work_status=$?
  drop_source_probe
  kubectl_indramesh delete pod "$POD_NAME" -n "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  [ -z "$JOB_NAME" ] || kubectl_indramesh delete job "$JOB_NAME" -n "$NAMESPACE" --ignore-not-found --wait=false >/dev/null 2>&1 || true
  exit "$work_status"
}
trap cleanup_ephemeral EXIT

main() {
  echo "verify-backup-restore: marker=$MARKER_ID"

  # Guard: a lingering indramesh_restore_check from a prior failed run is D-22's
  # retained diagnostic evidence — never silently drop it out from under an
  # unresolved investigation.
  local existing_check
  existing_check="$(kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -tAc "SELECT 1 FROM pg_database WHERE datname='indramesh_restore_check'" | tr -d '[:space:]')"
  if [ "$existing_check" = "1" ]; then
    verify_error "indramesh_restore_check already exists — a prior run's diagnostic evidence is present; inspect and drop it manually before re-running (D-22)"
    return 1
  fi

  # (1) Baseline public-schema table count in the real indramesh DB, captured
  # BEFORE the probe table exists (D-20 structure check).
  local baseline_count
  baseline_count="$(kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -d indramesh -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]')"
  echo "verify-backup-restore: baseline public table count in indramesh = $baseline_count"

  # (2) Create the disposable marker probe (D-20) — data this script creates
  # itself, in this same run, never a pre-existing/manually-created row
  # (CLAUDE.md "never test against data you didn't create yourself").
  kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -d indramesh -v ON_ERROR_STOP=1 -c \
    "CREATE TABLE IF NOT EXISTS _indramesh_backup_restore_probe (id text primary key, created_at timestamptz not null default now()); INSERT INTO _indramesh_backup_restore_probe(id) VALUES ('$MARKER_ID');"

  # (3) Trigger a fresh dump so the marker is captured — reuses the
  # indramesh-backup CronJob's own proven dump path rather than reimplementing
  # pg_dump (D-15/D-04).
  JOB_NAME="indramesh-backup-restore-check-$(date +%s)"
  kubectl_indramesh create job --from=cronjob/indramesh-backup -n "$NAMESPACE" "$JOB_NAME"
  local wait_status=0
  kubectl_indramesh wait --for=condition=complete "job/$JOB_NAME" -n "$NAMESPACE" --timeout=700s || wait_status=$?

  # (4) Immediate, unconditional source-side probe cleanup (P-260-02) — runs
  # regardless of step 3's outcome, strictly before the pass/fail branch on
  # marker/structure verification below.
  drop_source_probe

  if [ "$wait_status" -ne 0 ]; then
    verify_error "backup job $JOB_NAME did not complete within the bounded timeout — job pod logs follow (D-17's bounded-failure contract)"
    kubectl_indramesh logs "job/$JOB_NAME" -n "$NAMESPACE" >&2 || true
    return 1
  fi

  # (5) Resolve the live Postgres image reference by construction rather than
  # hardcoding a tag, mirroring verify_stateful_services.sh's ref-chain-proof
  # pattern — guarantees pg_restore is run from the exact same image/version
  # as the Postgres server that produced the dump (D-15).
  local postgres_image_ref
  postgres_image_ref="$(kubectl_indramesh get statefulset postgres -n "$NAMESPACE" \
    -o jsonpath='{.spec.template.spec.containers[0].image}')"
  echo "verify-backup-restore: restore-check pod image = $postgres_image_ref"

  # (6) Apply the one-off restore-check Pod: same app.kubernetes.io/name:
  # indramesh-backup label as the CronJob's pod (reuses the existing
  # allow-backup-egress/allow-postgres-ingress NetworkPolicy pair, no new
  # policy needed, T-260-09), restricted security context, fsGroup 70/
  # OnRootMismatch (matches D-06's precedent so it can read files the
  # fixed-UID-70 backup Job wrote), PGPASSWORD via secretKeyRef only (D-18),
  # a read-only mount of postgres-backups, and a writable /tmp for pg_restore
  # scratch space under readOnlyRootFilesystem (Pitfall 3).
  kubectl_indramesh apply -f - <<PODEOF
apiVersion: v1
kind: Pod
metadata:
  name: $POD_NAME
  namespace: $NAMESPACE
  labels:
    app.kubernetes.io/name: indramesh-backup
spec:
  restartPolicy: Never
  securityContext:
    fsGroup: 70
    fsGroupChangePolicy: OnRootMismatch
  containers:
    - name: restore-check
      image: "$postgres_image_ref"
      command: ["sleep", "3600"]
      securityContext:
        runAsNonRoot: true
        allowPrivilegeEscalation: false
        readOnlyRootFilesystem: true
        seccompProfile:
          type: RuntimeDefault
        capabilities:
          drop:
            - ALL
      env:
        - name: PGPASSWORD
          valueFrom:
            secretKeyRef:
              name: indramesh-database
              key: POSTGRES_PASSWORD
      volumeMounts:
        - name: backups
          mountPath: /backups
          readOnly: true
        - name: restore-tmp
          mountPath: /tmp
  volumes:
    - name: backups
      persistentVolumeClaim:
        claimName: postgres-backups
        readOnly: true
    - name: restore-tmp
      emptyDir: {}
PODEOF

  if ! kubectl_indramesh wait --for=condition=Ready "pod/$POD_NAME" -n "$NAMESPACE" --timeout=180s; then
    verify_error "restore-check pod did not become Ready — describe follows"
    kubectl_indramesh describe pod "$POD_NAME" -n "$NAMESPACE" >&2 || true
    return 1
  fi

  # (7) createdb + pg_restore into a brand-new indramesh_restore_check database
  # on the SAME running Postgres instance (D-19/D-21) — never the production
  # indramesh database. pg_restore's exit status is recorded but is never the
  # sole pass/fail signal (research Anti-Pattern: its default, non-
  # --exit-on-error mode can return non-zero on mere warnings) — the marker
  # and structure checks below are the authoritative verification (D-20).
  local dump_file restore_status=0
  dump_file="$(kubectl_indramesh exec "$POD_NAME" -n "$NAMESPACE" -- \
    sh -ec 'ls -t /backups/indramesh-*.dump | head -1')"
  echo "verify-backup-restore: restoring $dump_file into indramesh_restore_check"
  kubectl_indramesh exec "$POD_NAME" -n "$NAMESPACE" -- \
    createdb -h postgres -U postgres indramesh_restore_check
  kubectl_indramesh exec "$POD_NAME" -n "$NAMESPACE" -- \
    pg_restore -h postgres -U postgres -d indramesh_restore_check "$dump_file" || restore_status=$?
  echo "verify-backup-restore: pg_restore exit status = $restore_status (informational only, never the sole pass/fail signal)"

  # (8) Verify on postgres-0's local socket (no password needed, the same
  # convention pg_isready already relies on): the exact marker id, the
  # public-schema table count (baseline + 1 for the probe table present at
  # dump time), and the presence of the core application tables (D-20).
  local marker_found restored_table_count core_tables_present expected_table_count
  marker_found="$(kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -d indramesh_restore_check -tAc "SELECT id FROM _indramesh_backup_restore_probe WHERE id = '$MARKER_ID'" | tr -d '[:space:]')"
  restored_table_count="$(kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -d indramesh_restore_check -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public'" | tr -d '[:space:]')"
  core_tables_present="$(kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
    psql -U postgres -d indramesh_restore_check -tAc "SELECT count(*) FROM information_schema.tables WHERE table_schema='public' AND table_name IN ('sources','schemas','object_types','objects')" | tr -d '[:space:]')"
  expected_table_count=$((baseline_count + 1))

  echo "verify-backup-restore: marker_found=$marker_found expected=$MARKER_ID restored_tables=$restored_table_count expected_tables=$expected_table_count core_tables_present=$core_tables_present expected_core_tables=4"

  # (9) On success: drop indramesh_restore_check (D-22). On failure: retain it
  # and print a diagnostic naming the database for manual follow-up (D-22).
  if [ "$marker_found" = "$MARKER_ID" ] && [ "$restored_table_count" = "$expected_table_count" ] && [ "$core_tables_present" = "4" ]; then
    kubectl_indramesh exec postgres-0 -n "$NAMESPACE" -- \
      psql -U postgres -v ON_ERROR_STOP=1 -c "DROP DATABASE indramesh_restore_check;"
    echo "RESTORE VERIFICATION: PASS"
    return 0
  else
    verify_error "marker/structure verification failed — indramesh_restore_check retained for diagnosis (D-22)"
    echo "RESTORE VERIFICATION: FAILED — indramesh_restore_check retained for diagnosis"
    return 1
  fi
}

main
