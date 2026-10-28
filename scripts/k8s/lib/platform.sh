#!/bin/bash
# platform.sh — supported-host and WSL2 prerequisite adapter for the Phase 254
# k3s bootstrap (D-05, D-07, D-09, D-13, D-22; T-254-03, T-254-04). Sourceable;
# requires common.sh for step/preflight_error/die. Native Ubuntu/Debian
# amd64/arm64 hosts only receive the named persistent vm.max_map_count sysctl; WSL2 hosts
# additionally reconcile /etc/wsl.conf systemd support and the user .wslconfig
# 8GB memory floor while preserving unrelated settings (D-07, D-22). Any WSL
# change returns WSL_RESTART_EXIT so the installer demands a restart-and-rerun
# instead of continuing under stale VM settings (D-07).
set -euo pipefail

WSL_RESTART_EXIT=3
MAX_MAP_COUNT=1048576
WSL_MEMORY_FLOOR=8GB

PLATFORM_OS=""
PLATFORM_ARCH=""
PLATFORM_WSL=0
HOST_IP=""

detect_platform() {  # detect_platform [os_release_file] — set PLATFORM_OS/PLATFORM_ARCH/PLATFORM_WSL
  local os_release="${1:-/etc/os-release}"
  local arch kernel
  PLATFORM_OS="unknown"
  PLATFORM_ARCH="unknown"
  PLATFORM_WSL=0
  if [ -f "$os_release" ]; then
    PLATFORM_OS="$(sed -n 's/^ID=//p' "$os_release" | tr -d '"' | head -n1)"
  fi
  arch="$(uname -m 2>/dev/null || true)"
  case "$arch" in
    x86_64|amd64) PLATFORM_ARCH="amd64" ;;
    aarch64|arm64) PLATFORM_ARCH="arm64" ;;
  esac
  kernel="$(uname -r 2>/dev/null || true)"
  case "$kernel" in
    *[Mm]icrosoft*) PLATFORM_WSL=1 ;;
  esac
}

detect_host_ip() {  # detect_host_ip — set HOST_IP from INDRAMESH_HOST_IP, else the native host's primary IPv4
  # Set but empty, INDRAMESH_HOST_IP switches IP access off. WSL2 has no
  # LAN-reachable address of its own, so it gets one only through the override.
  local ip="${INDRAMESH_HOST_IP-}"
  HOST_IP=""
  if [ -z "${INDRAMESH_HOST_IP+x}" ] && [ "$PLATFORM_WSL" -eq 0 ]; then
    ip="$(ip -4 route get 1.1.1.1 2>/dev/null | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1 || true)"
  fi
  [ -n "$ip" ] || return 0
  if [[ ! "$ip" =~ ^([0-9]{1,3}\.){3}[0-9]{1,3}$ ]]; then
    preflight_error "host IP '$ip' is not an IPv4 address — set INDRAMESH_HOST_IP to one, or to an empty value to switch IP access off"
    return 1
  fi
  HOST_IP="$ip"
}

require_supported_platform() {  # require_supported_platform [os_release_file] — D-13: fail before mutation
  detect_platform "${1:-}"
  case "$PLATFORM_OS" in
    ubuntu|debian) ;;
    *)
      preflight_error "unsupported OS '$PLATFORM_OS' — only Ubuntu and Debian are supported (D-13)"
      return 1
      ;;
  esac
  case "$PLATFORM_ARCH" in
    amd64|arm64) ;;
    *)
      preflight_error "unsupported architecture '$PLATFORM_ARCH' — only amd64/arm64 are supported (D-13)"
      return 1
      ;;
  esac
  return 0
}

apply_sysctl_max_map_count() {  # apply_sysctl_max_map_count [sysctl_dir] — persist+apply vm.max_map_count (D-07)
  local sysctl_dir="${1:-/etc/sysctl.d}"
  local conf="$sysctl_dir/90-indramesh-max-map-count.conf"
  local current
  # sudo for the read: under a restrictive sudo umask the drop-in is root-only.
  if [ -f "$conf" ] && sudo grep -q "^vm.max_map_count[[:space:]]*=[[:space:]]*$MAX_MAP_COUNT" "$conf" 2>/dev/null; then
    current="$(sysctl -n vm.max_map_count 2>/dev/null || echo 0)"
    if [ "$current" -ge "$MAX_MAP_COUNT" ]; then
      step "host: vm.max_map_count satisfied ($current)"
      return 0
    fi
  fi
  step "host: persisting vm.max_map_count=$MAX_MAP_COUNT to $conf"
  printf 'vm.max_map_count=%s\n' "$MAX_MAP_COUNT" | sudo tee "$conf" >/dev/null
  sudo chmod 0644 "$conf"
  sudo sysctl -p "$conf" >/dev/null
}

wsl_conf_has_systemd() {  # wsl_conf_has_systemd <conf> — 0 when [boot] has effective systemd=true
  awk '
    /^[[:space:]]*\[/ { section=$0; gsub(/[[:space:]]/,"",section) }
    section == "[boot]" && /^[[:space:]]*systemd[[:space:]]*=[[:space:]]*true([[:space:]]|$)/ { found=1 }
    END { exit found ? 0 : 1 }
  ' "$1"
}

reconcile_wsl_conf() {  # reconcile_wsl_conf <conf_file> — ensure [boot] systemd=true, preserve others; 0 unchanged, 1 changed
  local conf="${1:-/etc/wsl.conf}"
  local tmp
  if [ ! -f "$conf" ]; then
    step "wsl: creating $conf with systemd support"
    printf '[boot]\nsystemd=true\n' | sudo tee "$conf" >/dev/null
    return 1
  fi
  if wsl_conf_has_systemd "$conf"; then
    step "wsl: $conf already has systemd=true"
    return 0
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/wslconf.XXXXXX")"
  awk '
    /^[[:space:]]*\[/ {
      if (section == "[boot]" && !wrote) { print "systemd=true"; wrote=1 }
      section = $0; gsub(/[[:space:]]/,"",section)
      if (section == "[boot]") have_boot=1
      print; next
    }
    section == "[boot]" && /^[[:space:]]*systemd/ { print "systemd=true"; wrote=1; next }
    { print }
    END {
      if (have_boot && !wrote) print "systemd=true"
      if (!have_boot) { print "[boot]"; print "systemd=true" }
    }
  ' "$conf" > "$tmp"
  step "wsl: reconciling systemd=true in $conf"
  sudo tee "$conf" >/dev/null < "$tmp"
  rm -f "$tmp"
  return 1
}

wslconfig_memory_floor_ok() {  # wslconfig_memory_floor_ok <file> — 0 when [wsl2] memory >= 8GB (D-22)
  # Accepts both GB and MB units, case-insensitively — .wslconfig's memory=
  # setting is not guaranteed to be uppercase GB, and an operator-set MB or
  # lowercase value must never be misreported as below the floor (which would
  # cause reconcile_wslconfig to silently downgrade it to memory=8GB).
  awk '
    /^[[:space:]]*\[/ { section=$0; gsub(/[[:space:]]/,"",section) }
    section == "[wsl2]" && /^[[:space:]]*memory[[:space:]]*=/ {
      val=$0; sub(/^[[:space:]]*memory[[:space:]]*=[[:space:]]*/,"",val)
      gsub(/[[:space:]]/,"",val)
      if (val ~ /^[0-9]+([Gg][Bb])$/) { num=val+0; if (num>=8) ok=1 }
      else if (val ~ /^[0-9]+([Mm][Bb])$/) { num=val+0; if (num>=8192) ok=1 }
    }
    END { exit ok ? 0 : 1 }
  ' "$1"
}

reconcile_wslconfig() {  # reconcile_wslconfig <file> — ensure [wsl2] memory floor, preserve others; 0 unchanged, 1 changed
  local file="${1:-$HOME/.wslconfig}"
  local tmp
  if [ ! -f "$file" ]; then
    step "wsl: creating $file with the 8GB memory floor"
    printf '[wsl2]\nmemory=8GB\n' > "$file"
    return 1
  fi
  if wslconfig_memory_floor_ok "$file"; then
    step "wsl: $file already satisfies the 8GB memory floor"
    return 0
  fi
  tmp="$(mktemp "${TMPDIR:-/tmp}/wslconfig.XXXXXX")"
  awk -v floor="$WSL_MEMORY_FLOOR" '
    /^[[:space:]]*\[/ {
      if (section == "[wsl2]" && !wrote) { print "memory=" floor; wrote=1 }
      section = $0; gsub(/[[:space:]]/,"",section)
      if (section == "[wsl2]") have_wsl2=1
      print; next
    }
    section == "[wsl2]" && /^[[:space:]]*memory[[:space:]]*=/ { print "memory=" floor; wrote=1; next }
    { print }
    END {
      if (have_wsl2 && !wrote) print "memory=" floor
      if (!have_wsl2) { print "[wsl2]"; print "memory=" floor }
    }
  ' "$file" > "$tmp"
  step "wsl: reconciling memory=$WSL_MEMORY_FLOOR in $file"
  mv "$tmp" "$file"
  return 1
}

resolve_windows_wslconfig_path() {  # resolve_windows_wslconfig_path — the real Windows-side .wslconfig WSL2 reads (%USERPROFILE%\.wslconfig), not $HOME/.wslconfig inside the WSL2 Linux filesystem, which WSL2 never consults for VM-wide settings like the memory floor (D-22)
  local win_home
  command -v powershell.exe >/dev/null 2>&1 || return 1
  command -v wslpath >/dev/null 2>&1 || return 1
  win_home="$(powershell.exe -NoProfile -NonInteractive -Command '$env:USERPROFILE' 2>/dev/null | tr -d '\r\n')"
  [ -n "$win_home" ] || return 1
  win_home="$(wslpath -u "$win_home" 2>/dev/null | tr -d '\r\n')"
  [ -n "$win_home" ] || return 1
  printf '%s/.wslconfig\n' "$win_home"
}

check_wsl_mount_sanity() {  # check_wsl_mount_sanity [mounts_file] — WSL2-only D-13 preflight gate: a mount line with more than the standard 6 whitespace-separated /proc/mounts fields crashes k3s's kubelet ContainerManager startup validation ("system validation failed - wrong number of fields"), hanging k3s in an endless crash-loop that looks like ./start/./install_indramesh_k8s.sh never completing. Commonly caused by Docker Desktop's WSL integration mounting its Windows install path (e.g. /Docker/host) with an unescaped space embedded in the mount options string.
  local mounts_file="${1:-/proc/mounts}"
  local bad_line
  [ -f "$mounts_file" ] || return 0
  bad_line="$(awk 'NF>6 {print; exit}' "$mounts_file")"
  if [ -n "$bad_line" ]; then
    preflight_error "a malformed mount entry was found in $mounts_file (more than 6 fields), which crashes k3s's kubelet startup with 'system validation failed - wrong number of fields' — this hangs k3s in an endless restart loop, not a one-time failure. This is commonly Docker Desktop's WSL integration mounting its Windows install path (often /Docker/host) with an unescaped space in the mount options. Fix: disable WSL Integration for this distro in Docker Desktop (Settings > Resources > WSL Integration), or as an immediate workaround run: sudo umount -l <mountpoint> for the offending entry, then rerun ./install_indramesh_k8s.sh. See docs/DEPLOYMENT.md for details. Offending line: $bad_line"
    return 1
  fi
  return 0
}

reconcile_wsl_prerequisites() {  # reconcile_wsl_prerequisites [wsl_conf] [wslconfig] — 0 unchanged, WSL_RESTART_EXIT on change
  local wsl_conf="${1:-/etc/wsl.conf}"
  local wslconfig="${2:-}"
  local changed=0
  if [ -z "$wslconfig" ]; then
    wslconfig="$(resolve_windows_wslconfig_path)" || wslconfig="$HOME/.wslconfig"
  fi
  if ! reconcile_wsl_conf "$wsl_conf"; then
    changed=1
  fi
  if ! reconcile_wslconfig "$wslconfig"; then
    changed=1
  fi
  if [ "$changed" -eq 1 ]; then
    echo "WSL configuration changed: restart WSL (wsl --shutdown or reboot Windows) and rerun ./install_indramesh_k8s.sh (D-07)" >&2
    return "$WSL_RESTART_EXIT"
  fi
  step "wsl: prerequisites already satisfied (systemd + memory floor)"
  return 0
}

platform_prepare() {  # platform_prepare [os_release] [wsl_conf] [wslconfig] [sysctl_dir] [mounts_file]
  local os_release="${1:-/etc/os-release}"
  local wsl_conf="${2:-/etc/wsl.conf}"
  local wslconfig="${3:-}"
  local sysctl_dir="${4:-/etc/sysctl.d}"
  local mounts_file="${5:-/proc/mounts}"
  require_supported_platform "$os_release" || return 1
  apply_sysctl_max_map_count "$sysctl_dir"
  if [ "$PLATFORM_WSL" -eq 1 ]; then
    check_wsl_mount_sanity "$mounts_file" || return 1
    reconcile_wsl_prerequisites "$wsl_conf" "$wslconfig" || return "$?"
  fi
  return 0
}