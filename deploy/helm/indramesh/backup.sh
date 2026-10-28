#!/bin/sh
# backup.sh — dump/verify/atomic-rename (D-04, research Pattern 2) for the
# indramesh-backup CronJob (BAK-01/BAK-02). Embedded into the indramesh-backup-
# script ConfigMap via .Files.Get (the exact nginx-configmap.yaml idiom used
# for nginx.conf) and mounted read-only at /scripts/backup.sh, run as
# `sh /scripts/backup.sh`.
#
# Sourceable for hermetic testing: BACKUP_SH_SOURCE_ONLY=1 defines
# run_dump/retention_sweep/main without invoking main
# (tests/bootstrap/test_backup.sh). Plain POSIX sh only — no bashisms, no
# GNU-only find/sed extensions — so it runs unmodified against the Postgres
# image's busybox /bin/sh.
#
# Environment (set by templates/backup-cronjob.yaml from the backup: values
# block): PGHOST, PGPORT, PGUSER, PGPASSWORD, PGDATABASE (standard libpq
# variables consumed implicitly by pg_dump/pg_restore, D-12/D-18),
# BACKUP_DIR, BACKUP_MAX_AGE_DAYS, BACKUP_MAX_COUNT (consumed by
# retention_sweep).
set -eu

# run_dump — writes a pg_dump -Fc archive to a temp name, verifies it with
# pg_restore --list (an independent signal beyond pg_dump's own exit code —
# see 260-RESEARCH.md Pitfall 1), and only then atomically renames it to the
# final indramesh-<UTC-timestamp>.dump name (D-04/D-13). A killed/failed run
# leaves only the orphaned .tmp-* file — diagnostic evidence, never renamed,
# never deleted automatically (D-04).
run_dump() {
  ts="$(date -u +%Y%m%dT%H%M%SZ)"
  tmp="$BACKUP_DIR/.tmp-indramesh-${ts}.dump"
  final="$BACKUP_DIR/indramesh-${ts}.dump"

  echo "backup: dumping indramesh database to $tmp"
  pg_dump -Fc -f "$tmp"

  echo "backup: verifying archive integrity of $tmp"
  pg_restore --list "$tmp" >/dev/null

  echo "backup: renaming $tmp -> $final"
  mv "$tmp" "$final"
}

# retention_sweep <dir> <max_age_days> <max_count> — deletes only complete
# indramesh-*.dump files (never .tmp-indramesh-* or foreign files, D-04) that are
# older than max_age_days OR fall beyond the newest max_count files by
# lexicographic rank (D-02's union-of-both-bounds rule). The single
# lexicographically newest indramesh-*.dump file is never deleted, even if it is
# also older than max_age_days or the directory contains only that one file
# (D-05's last-good-dump floor). Lexicographic order equals chronological
# order per the D-13 UTC-timestamp filename format. Only POSIX-portable find/
# sed idioms are used (confirmed against the pinned Alpine/busybox image,
# research Assumption A3) — never GNU-only extensions such as `head -n -N`.
retention_sweep() {
  dir="$1"
  max_age_days="$2"
  max_count="$3"

  all_sorted="$(find "$dir" -maxdepth 1 -type f -name 'indramesh-*.dump' 2>/dev/null | sort)"
  [ -n "$all_sorted" ] || return 0

  total="$(printf '%s\n' "$all_sorted" | wc -l)"
  [ "$total" -gt 1 ] || return 0

  newest="$(printf '%s\n' "$all_sorted" | tail -n 1)"
  # every candidate for deletion excludes the newest file unconditionally.
  candidates="$(printf '%s\n' "$all_sorted" | sed '$d')"

  age_candidates="$(find "$dir" -maxdepth 1 -type f -name 'indramesh-*.dump' -mtime "+$max_age_days" 2>/dev/null | sort)"
  age_set="$(printf '%s\n' "$age_candidates" | grep -vFx "$newest" 2>/dev/null || true)"

  candidate_count="$(printf '%s\n' "$candidates" | wc -l)"
  excess=$((candidate_count - (max_count - 1)))
  count_set=""
  if [ "$excess" -gt 0 ]; then
    count_set="$(printf '%s\n' "$candidates" | sed -n "1,${excess}p")"
  fi

  union="$(printf '%s\n%s\n' "$age_set" "$count_set" | sed '/^$/d' | sort -u)"
  [ -n "$union" ] || return 0

  printf '%s\n' "$union" | while IFS= read -r f; do
    [ -n "$f" ] || continue
    echo "backup: retention removing $f"
    rm -f "$f"
  done
}

# main — D-07: retention runs only after a successful dump, in the same job
# run. set -eu already means a failing run_dump aborts the script before
# retention_sweep is ever reached; run_dump is never wrapped in an
# error-swallowing conditional.
main() {
  run_dump
  retention_sweep "$BACKUP_DIR" "${BACKUP_MAX_AGE_DAYS:-30}" "${BACKUP_MAX_COUNT:-14}"
}

if [ "${BACKUP_SH_SOURCE_ONLY:-}" != "1" ]; then
  main
fi
