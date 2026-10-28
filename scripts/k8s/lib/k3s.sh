#!/bin/bash
# k3s.sh — verified k3s server installation and readiness observation for the
# Phase 254 k3s bootstrap (D-10, D-11, D-12, D-15; T-254-01, T-254-03,
# T-254-04). Sourceable; requires common.sh (step/preflight_error/private_tmp),
# artifacts.sh (load_versions/k3s_install_decision/VERIFIED_*), and platform.sh
# (PLATFORM_ARCH).
#
# Installs the Plan 03 verified staged server binary through the verified
# tagged official installer with INSTALL_K3S_SKIP_DOWNLOAD=true so no unchecked
# upstream binary download can bypass the digest gate (T-254-01). A compatible
# or newer installation is preserved (never downgraded) per D-11; a missing or
# older-compatible installation is installed/upgraded to the pinned version.
# The durable server configuration is reproduced identically on every call so a
# rerun cannot lose k3s settings (research Pitfall 2). An installed-but-inactive
# unit is started first (ensure_k3s_running) so the D-18 reinstall flow after a
# host or WSL restart does not wait out the readiness window on a dead service;
# after installation the module observes the k3s systemd unit, the Kubernetes
# API, and a Ready node with bounded polling (D-15); any timeout returns
# non-zero so the installer stops before Helm reconciliation.
set -euo pipefail

# Bounded readiness window (D-15): total budget = attempts * interval seconds.
K3S_READY_ATTEMPTS="${K3S_READY_ATTEMPTS:-90}"
K3S_READY_INTERVAL="${K3S_READY_INTERVAL:-2}"

# Permanent install location for the verified k3s binary — must be a path
# that survives reboot (never a private_tmp()/mktemp scratch dir, which on
# many hosts is a tmpfs RAM disk wiped on every restart). This is also
# k3s's own official INSTALL_K3S_BIN_DIR default, matching the documented
# air-gapped-install pattern: pre-place the verified binary here, then run
# the installer with INSTALL_K3S_SKIP_DOWNLOAD=true so it treats it as
# already downloaded and writes the systemd unit's ExecStart to this same
# permanent path.
K3S_BIN_DIR="${INDRAMESH_K3S_BIN_DIR:-/usr/local/bin}"

k3s_installed_version() {  # k3s_installed_version [binary] — echo "" when absent; fail closed on unparseable (see artifacts.sh header contract)
  local bin="${1:-k3s}"
  local out ver
  if ! command -v "$bin" >/dev/null 2>&1; then
    echo ""
    return 0
  fi
  out="$("$bin" --version 2>/dev/null || true)"
  ver="$(printf '%s\n' "$out" | sed -nE 's/.*\b(v[0-9]+\.[0-9]+\.[0-9]+(\+k3s[0-9]+)?).*/\1/p' | head -n1)"
  if [ -n "$ver" ] && k3s_version_parseable "$ver"; then
    echo "$ver"
  else
    preflight_error "existing k3s binary's version could not be parsed from '$out' — refusing to reinstall blindly (D-11)"
    return 1
  fi
}

install_or_reuse_k3s() {  # install_or_reuse_k3s <verified_installer> <verified_binary> <arch>
  # Preserve compatible/newer, upgrade an older compatible patch to the pin,
  # install a missing server; never downgrade (D-11). The verified binary is
  # pre-placed at the permanent K3S_BIN_DIR (never a reboot-ephemeral tmp
  # path — see K3S_BIN_DIR above) and installed through the verified tagged
  # official installer with INSTALL_K3S_SKIP_DOWNLOAD=true (D-12, T-254-01).
  local installer="${1:?verified installer path required}"
  local binary="${2:?verified binary path required}"
  local arch="${3:?arch required}"
  local installed decision
  load_versions
  installed="$(k3s_installed_version)" || {
    preflight_error "k3s: refusing to install/upgrade over an existing binary whose version could not be determined (D-11)"
    return 1
  }
  decision="$(k3s_install_decision "$installed")"
  case "$decision" in
    keep|preserve)
      step "k3s: existing ${installed:-server} is compatible — preserving (D-11)"
      return 0
      ;;
    install|upgrade)
      [ -f "$installer" ] || { preflight_error "verified k3s installer missing: $installer (T-254-01)"; return 1; }
      [ -f "$binary" ] || { preflight_error "verified k3s server binary missing: $binary (T-254-01)"; return 1; }
      sudo install -m 0755 "$binary" "$K3S_BIN_DIR/k3s"
      step "k3s: installing pinned $K3S_VERSION via the verified official installer (INSTALL_K3S_SKIP_DOWNLOAD=true)"
      # Reproduce the same minimal server configuration on every call so a
      # rerun cannot lose installer-supplied settings (research Pitfall 2).
      # Pass only the four INSTALL_K3S_* variables the installer needs
      # (targeted sudo only, D-14) instead of blanket-preserving the whole
      # calling environment with `sudo -E`. The kubeconfig keeps k3s's own
      # secure default (root-only, mode 600) rather than being loosened to
      # world-readable; _wait_for_kube_api/_wait_for_ready_node below and
      # helm.sh's reconcile_helm_release use targeted `sudo env KUBECONFIG=`
      # calls (K3S_KUBECONFIG, common.sh) instead.
      sudo env \
        INSTALL_K3S_VERSION="$K3S_VERSION" \
        INSTALL_K3S_SKIP_DOWNLOAD=true \
        INSTALL_K3S_BIN_DIR="$K3S_BIN_DIR" \
        INSTALL_K3S_EXEC="server" \
        sh "$installer"
      return 0
      ;;
  esac
  preflight_error "k3s: unknown install decision '$decision'"
  return 1
}

ensure_k3s_running() {  # ensure_k3s_running — start the unit when it is installed but inactive (D-15)
  # A compatible installation whose systemd unit is inactive (host reboot,
  # WSL restart, or an earlier `./stop`) is not a usable cluster: the reuse
  # path above deliberately leaves the unit untouched, so the reinstall flow
  # would otherwise wait out the entire readiness window and fail on a dead
  # service. Start it here; the bounded readiness observation follows.
  if systemctl is-active --quiet k3s 2>/dev/null; then
    return 0
  fi
  step "k3s: systemd unit inactive — starting it"
  if ! sudo systemctl start k3s; then
    preflight_error "k3s systemd unit is inactive and could not be started — run: systemctl status k3s"
    return 1
  fi
  return 0
}

_wait_for_systemd_k3s() {  # _wait_for_systemd_k3s — bounded systemd active polling
  local i
  for i in $(seq 1 "$K3S_READY_ATTEMPTS"); do
    if systemctl is-active --quiet k3s 2>/dev/null; then
      step "k3s: systemd unit active"
      return 0
    fi
    sleep "$K3S_READY_INTERVAL"
  done
  preflight_error "k3s systemd unit did not become active within ${K3S_READY_ATTEMPTS}s — run: systemctl status k3s"
  return 1
}

_wait_for_kube_api() {  # _wait_for_kube_api — bounded Kubernetes API readiness
  local i
  for i in $(seq 1 "$K3S_READY_ATTEMPTS"); do
    if sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get --raw /readyz >/dev/null 2>&1; then
      step "k3s: Kubernetes API ready"
      return 0
    fi
    sleep "$K3S_READY_INTERVAL"
  done
  preflight_error "Kubernetes API not ready within ${K3S_READY_ATTEMPTS}s — run: sudo KUBECONFIG=$K3S_KUBECONFIG kubectl get --raw /readyz"
  return 1
}

_wait_for_ready_node() {  # _wait_for_ready_node — bounded Ready-node observation
  local i
  for i in $(seq 1 "$K3S_READY_ATTEMPTS"); do
    if sudo env KUBECONFIG="$K3S_KUBECONFIG" kubectl get nodes --no-headers 2>/dev/null | grep -q ' Ready '; then
      step "k3s: node Ready"
      return 0
    fi
    sleep "$K3S_READY_INTERVAL"
  done
  preflight_error "no Ready node within ${K3S_READY_ATTEMPTS}s — run: sudo KUBECONFIG=$K3S_KUBECONFIG kubectl get nodes"
  return 1
}

wait_for_k3s_readiness() {  # wait_for_k3s_readiness — bounded systemd + API + node readiness (D-15)
  _wait_for_systemd_k3s || return 1
  _wait_for_kube_api || return 1
  _wait_for_ready_node || return 1
  step "k3s: server, API, and node Ready"
  return 0
}
