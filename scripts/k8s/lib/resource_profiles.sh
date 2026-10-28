#!/bin/bash
# resource_profiles.sh — deterministic, non-secret stateful resource profiles.
# Source after common.sh. Profiles reserve enough overhead above OpenSearch's
# 2Gi heap and can be selected explicitly by an operator.
set -euo pipefail

STATEFUL_PROFILE=""
STATEFUL_PROFILE_VALUES_FILE=""
STATEFUL_MEMORY_MIB=""

_memory_to_mib() {  # _memory_to_mib <8Gi|8192Mi|KiB count>
  local value="${1:-}" number unit
  if [[ "$value" =~ ^[0-9]+$ ]]; then
    echo "$((value / 1024))"
    return 0
  fi
  if [[ "$value" =~ ^([0-9]+)([Gg]([Ii][Bb]?)?|[Gg])$ ]]; then
    echo "$((BASH_REMATCH[1] * 1024))"
    return 0
  fi
  if [[ "$value" =~ ^([0-9]+)([Mm]([Ii][Bb]?)?|[Mm])$ ]]; then
    echo "${BASH_REMATCH[1]}"
    return 0
  fi
  return 1
}

detect_host_memory_mib() {
  local memory_input="${INDRAMESH_HOST_MEMORY:-}"
  if [ -z "$memory_input" ]; then
    memory_input="$(awk '/^MemTotal:/ {print $2; exit}' /proc/meminfo 2>/dev/null || true)"
  fi
  _memory_to_mib "$memory_input" || {
    preflight_error "stateful host memory '$memory_input' is malformed — use an integer KiB count, MiB, or GiB"
    return 1
  }
}

select_resource_profile() {
  local requested="${INDRAMESH_STATEFUL_PROFILE:-auto}" memory
  memory="$(detect_host_memory_mib)" || return 1
  STATEFUL_MEMORY_MIB="$memory"
  if [ "$memory" -lt 6144 ]; then
    preflight_error "stateful host memory ${memory}MiB is below the supported 6144MiB minimum"
    return 1
  fi
  case "$requested" in
    auto)
      if [ "$memory" -ge 16384 ]; then
        STATEFUL_PROFILE="standard"
      else
        STATEFUL_PROFILE="compact"
      fi
      ;;
    compact|standard)
      STATEFUL_PROFILE="$requested"
      ;;
    *)
      preflight_error "unknown INDRAMESH_STATEFUL_PROFILE '$requested' — use auto, compact, or standard"
      return 1
      ;;
  esac
  step "resources: selected stateful profile '$STATEFUL_PROFILE' from ${memory}MiB (override: ${requested})"
}

write_resource_profile_values() {  # write_resource_profile_values <path>
  local destination="${1:?profile values path required}"
  [ -n "$STATEFUL_PROFILE" ] || { preflight_error "stateful profile was not selected"; return 1; }
  case "$STATEFUL_PROFILE" in
    compact)
      cat > "$destination" <<'YAML'
postgres:
  resources: {requests: {cpu: 250m, memory: 256Mi}, limits: {cpu: "1", memory: 1Gi}}
valkey:
  resources: {requests: {cpu: 100m, memory: 128Mi}, limits: {cpu: 500m, memory: 512Mi}}
opensearch:
  resources: {requests: {cpu: "1", memory: 2Gi}, limits: {cpu: "2", memory: 3Gi}}
YAML
      ;;
    standard)
      cat > "$destination" <<'YAML'
postgres:
  resources: {requests: {cpu: 500m, memory: 512Mi}, limits: {cpu: "2", memory: 2Gi}}
valkey:
  resources: {requests: {cpu: 250m, memory: 256Mi}, limits: {cpu: "1", memory: 1Gi}}
opensearch:
  resources: {requests: {cpu: "2", memory: 3Gi}, limits: {cpu: "3", memory: 4Gi}}
YAML
      ;;
  esac
  STATEFUL_PROFILE_VALUES_FILE="$destination"
}
