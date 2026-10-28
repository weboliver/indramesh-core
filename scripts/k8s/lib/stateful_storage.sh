#!/bin/bash
# stateful_storage.sh — host-level ownership preparation for the Phase 256
# stateful local-path PVCs (D-06 companion; T-256-12). Sourceable; requires
# common.sh for step/preflight_error and K3S_KUBECONFIG.
#
# MECHANICAL REASON (confirmed live finding, 2026-09-01): a pod-level fsGroup
# sets only GROUP ownership on the volume and does NOT grant `chmod`. POSIX
# requires the caller to own the file (or hold CAP_FOWNER) to change modes —
# local-path PVC roots are 0777 root-owned, so the Postgres wrapper's initdb
# `chmod 00700 /var/lib/postgresql/pgdata` fails with EPERM unless the data
# dir is owned by the wrapper UID. The D-06 boundary relaxation is fsGroup-only
# (group write); this module is its documented companion: a host-level,
# idempotent, targeted-sudo chown of exactly the resolved Postgres/Valkey
# local-path PVC host dirs to the wrapper UIDs (70:70, 999:999). It never
# touches the Elasticsearch PVC or any other path, and never runs inside the
# pod (no privileged container, no ownership-repair initContainer).
#
# FRESH-INSTALL FLOW (D-16 intra-run fixup; confirmed live finding,
# 2026-09-01): on a genuinely fresh install the PVCs do not exist until
# volumeClaimTemplates create them on the first ("stateful-only") Helm pass,
# and kubelet creates the pgdata subPath dir as root on first mount — so
# ownership cannot be prepared before that pass. `install.sh` runs the
# stateful-only pass WITHOUT `--wait` for exactly this reason, then calls
# `fresh_install_stateful_ownership_fixup` (below) to bounded-poll for the
# now-created PVCs to bind, chown their host dirs, and force-restart
# `postgres-0` if it already crash-looped before the fix landed — all inside
# the SAME installer invocation, before the second ("full") Helm pass blocks
# on readiness. A bare rerun of the whole installer is no longer the primary
# recovery path (it remains a safe fallback: `prepare_stateful_local_path_
# ownership`'s pre-Helm call site in install.sh still resolves and chowns any
# retained PVC left over from an interrupted/older run, and the installer's
# upgrade-or-install semantics stay idempotent and non-destructive).
set -euo pipefail

STATEFUL_NAMESPACE="${INDRAMESH_STATEFUL_NAMESPACE:-indramesh}"

# D-16 fresh-install fixup bounds (env-injectable so hermetic tests can zero
# the poll interval instead of sleeping for real). The default 60s/2s budget
# is comfortably inside local-path-provisioner's near-instant PVC binding.
STATEFUL_PVC_BIND_POLL_TIMEOUT_SECONDS="${INDRAMESH_STATEFUL_PVC_BIND_POLL_TIMEOUT_SECONDS:-60}"
STATEFUL_PVC_BIND_POLL_INTERVAL_SECONDS="${INDRAMESH_STATEFUL_PVC_BIND_POLL_INTERVAL_SECONDS:-2}"

# D-16 fresh-install fixup retry bounds (confirmed live finding, 2026-09-01):
# a chown that lands before kubelet has ever mounted the Pod's `subPath`
# volumes (e.g. Postgres's `pgdata`) does NOT cover them — kubelet creates a
# missing `subPath` directory as root, as part of ITS OWN mount step, on the
# pod's actual (re)start, not before. On a truly fresh install pass 1 returns
# almost immediately (no --wait), so the first chown routinely lands before
# the pod's first mount attempt. `fresh_install_stateful_ownership_fixup`
# therefore loops chown-then-check-ready-then-restart-if-not, so every
# restart's freshly created subPath directory gets caught by the NEXT
# iteration's chown, bounded rather than relying on Kubernetes' own
# CrashLoopBackOff backoff (which can reach ~5 min per retry).
STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS="${INDRAMESH_STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS:-4}"
STATEFUL_OWNERSHIP_FIXUP_RETRY_INTERVAL_SECONDS="${INDRAMESH_STATEFUL_OWNERSHIP_FIXUP_RETRY_INTERVAL_SECONDS:-5}"

# Per-service ownership map: PVC name -> uid:gid owner. Only these two
# services are ever prepared; the Elasticsearch PVC is deliberately absent
# (T-256-12).
STATEFUL_OWNERSHIP_PREP=(
  "data-postgres-0|70:70"
  "data-valkey-0|999:999"
)

# Local-path provisioner roots that host dirs may be resolved to (k3s bundled
# local-path and the standalone local-path-provisioner defaults). Any resolved
# path outside these roots, or nested below a root, is rejected — fail closed
# (T-256-12).
STATEFUL_LOCAL_PATH_ROOTS=(
  /var/lib/rancher/k3s/storage
  /var/lib/k3s/storage
  /opt/local-path-provisioner
)

stateful_kubectl() {  # stateful_kubectl <kubectl args...> — targeted-sudo kubectl seam
  sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl "$@"
}

resolve_stateful_pvc_host_dir() {  # resolve_stateful_pvc_host_dir <pvc> — echo the bound PV host
  # dir, or "" when no bound PV exists yet (fresh first pass / Pending claim).
  # A PVC NotFound is "not created yet" (fresh install); any other kubectl
  # error fails closed (D-04) instead of being silently treated as absent.
  local pvc="$1" pv host_path get_out
  if ! get_out="$(stateful_kubectl get pvc "$pvc" -n "$STATEFUL_NAMESPACE" -o jsonpath='{.spec.volumeName}' 2>&1)"; then
    case "$get_out" in
      *NotFound*|*"not found"*) return 0 ;;
      *)
        preflight_error "failed to read PVC/$pvc (stateful-storage): ${get_out:-no output}"
        return 1
        ;;
    esac
  fi
  pv="$get_out"
  [ -n "$pv" ] || return 0  # claim exists but no volume yet (Pending)
  if ! host_path="$(stateful_kubectl get pv "$pv" -o jsonpath='{.spec.hostPath.path}{.spec.local.path}' 2>&1)"; then
    preflight_error "failed to read PV/$pv bound to PVC/$pvc (stateful-storage): ${host_path:-no output}"
    return 1
  fi
  [ -n "$host_path" ] || return 0
  echo "$host_path"
}

_validate_stateful_host_dir() {  # _validate_stateful_host_dir <pvc> <path> — 0 only for a direct
  # `pvc-*` child of a known local-path root (T-256-12)
  local pvc="$1" path="$2" root rest ok=1
  case "$path" in
    *'/../'*|*/..) preflight_error "refusing path traversal in host dir '$path' for PVC/$pvc (T-256-12)"; return 1 ;;
  esac
  for root in "${STATEFUL_LOCAL_PATH_ROOTS[@]}"; do
    case "$path" in
      "$root"/pvc-*)
        rest="${path#"$root"/}"
        case "$rest" in
          */*) ;;          # nested below the root — reject
          pvc-*) ok=0 ;;   # direct pvc- child — accept
        esac
        ;;
    esac
  done
  [ "$ok" -eq 0 ] || { preflight_error "refusing to chown non-local-path host dir '$path' for PVC/$pvc (T-256-12)"; return 1; }
  return 0
}

prepare_stateful_local_path_ownership() {  # prepare_stateful_local_path_ownership [poll_timeout_seconds]
  # idempotent host chown of the resolved Postgres/Valkey local-path PVC host
  # dirs to their wrapper UIDs. Never touches any other path.
  #
  # poll_timeout_seconds defaults to 0 (today's exact single-shot behavior):
  # a PVC with no bound PV yet gets an explicit diagnostic and a clean skip,
  # relied on by install.sh's pre-Helm call site (nothing to resolve before
  # Helm has run at all) and by the D-02-rerun recovery path (resolves and
  # chowns retained PVCs from a prior run). A nonzero value bounded-polls for
  # each unresolved PVC to bind before giving up — used by the post-pass-1
  # fresh-install fixup below, where local-path's PVC binding is near-instant
  # but not synchronous with a non-`--wait` `helm upgrade` returning.
  local poll_timeout="${1:-0}"
  local entry pvc owner host_path skipped=0 prepared=0 waited
  for entry in "${STATEFUL_OWNERSHIP_PREP[@]}"; do
    pvc="${entry%%|*}"
    owner="${entry##*|}"
    host_path="$(resolve_stateful_pvc_host_dir "$pvc")" || return 1
    if [ -z "$host_path" ] && [ "$poll_timeout" -gt 0 ]; then
      waited=0
      while [ -z "$host_path" ] && [ "$waited" -lt "$poll_timeout" ]; do
        sleep "$STATEFUL_PVC_BIND_POLL_INTERVAL_SECONDS"
        waited=$((waited + STATEFUL_PVC_BIND_POLL_INTERVAL_SECONDS))
        host_path="$(resolve_stateful_pvc_host_dir "$pvc")" || return 1
      done
    fi
    if [ -z "$host_path" ]; then
      step "stateful-storage: PVC/$pvc has no bound local-path PV yet — ownership cannot be prepared before the first Helm pass; the first pass creates and retains the PVC, and a rerun (second pass, D-02) resolves and chowns it"
      skipped=1
      continue
    fi
    _validate_stateful_host_dir "$pvc" "$host_path" || return 1
    step "stateful-storage: chowning PVC/$pvc host dir '$host_path' to $owner (chmod-capable owner)"
    sudo chown -R "$owner" "$host_path"
    prepared=1
  done
  if [ "$prepared" -eq 0 ] && [ "$skipped" -eq 1 ]; then
    step "stateful-storage: no retained PVCs resolved yet — fresh install first pass; the documented second install pass (D-02 rerun) prepares ownership"
  fi
  return 0
}

_stateful_pod_is_ready() {  # _stateful_pod_is_ready <pod> — 0 (true) only for an existing Ready pod
  local pod="$1" ready
  ready="$(stateful_kubectl get pod "$pod" -n "$STATEFUL_NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)"
  [ "$ready" = "true" ]
}

_restart_not_ready_stateful_pod() {  # _restart_not_ready_stateful_pod <pod> — delete <pod> only if it
  # exists and is not Ready (D-16 fresh-install fixup). A StatefulSet-owned
  # pod is recreated immediately against its existing PVC — no data touched.
  # Idempotent no-op for a healthy pod (D-02 rerun case) or a not-yet-created
  # pod. Not a T-254-02/T-254-04 "destructive verb": it never deletes a PVC
  # or a release, only forces a self-healing restart of one StatefulSet pod.
  local pod="$1" ready
  ready="$(stateful_kubectl get pod "$pod" -n "$STATEFUL_NAMESPACE" \
    -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || true)"
  [ -n "$ready" ] || return 0
  [ "$ready" = "true" ] && return 0
  step "stateful-storage: restarting not-Ready pod/$pod after ownership fix (D-16 fresh-install fixup)"
  stateful_kubectl delete pod "$pod" -n "$STATEFUL_NAMESPACE" --ignore-not-found \
    || { preflight_error "failed to restart pod/$pod after ownership fix (stateful-storage)"; return 1; }
}

fresh_install_stateful_ownership_fixup() {  # fresh_install_stateful_ownership_fixup — D-16 post-pass-1
  # hook: bounded-poll for the just-created stateful PVCs to bind, chown
  # their host dirs, then loop chown-check-restart up to
  # STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS times so a subPath directory
  # kubelet creates lazily during a (re)start (e.g. Postgres's `pgdata`,
  # which a chown landing before the pod's first mount attempt cannot cover)
  # gets caught by a later iteration's chown — bounded, rather than relying
  # on Kubernetes' own CrashLoopBackOff backoff. Only postgres-0 is ever
  # restarted: Redis/Valkey and Elasticsearch never hit the initdb chmod
  # problem this fixes.
  local attempt
  for attempt in $(seq 1 "$STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS"); do
    prepare_stateful_local_path_ownership "$STATEFUL_PVC_BIND_POLL_TIMEOUT_SECONDS" || return 1
    if _stateful_pod_is_ready postgres-0; then
      return 0
    fi
    step "stateful-storage: postgres-0 not Ready after ownership-fix attempt $attempt/$STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS (D-16 fresh-install fixup)"
    _restart_not_ready_stateful_pod postgres-0 || return 1
    if [ "$attempt" -lt "$STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS" ]; then
      sleep "$STATEFUL_OWNERSHIP_FIXUP_RETRY_INTERVAL_SECONDS"
    fi
  done
  step "stateful-storage: postgres-0 still not Ready after $STATEFUL_OWNERSHIP_FIXUP_MAX_ATTEMPTS bounded ownership-fix attempts — leaving it to the full pass's bounded --wait (D-16 fresh-install fixup)"
  return 0
}
