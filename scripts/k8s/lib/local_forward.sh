#!/bin/bash
# local_forward.sh — self-host reachability bridge behind https://localhost on
# every host, native and WSL2 (BOOT-03 gap-closure, 259-05 follow-up). Sourceable; requires common.sh (step/preflight_error/
# require_sudo) and reachability.sh (_reachability_discover_lb_ip).
#
# Problem (confirmed live): k3s's default ServiceLB (klipper-lb) redirects
# Traefik's LoadBalancer ports via an iptables PREROUTING DNAT rule.
# PREROUTING never sees host-originated loopback traffic, and a Windows
# process connecting to its own LAN-facing interface IP is short-circuited
# by the Windows TCP stack before that traffic ever reaches WSL's shared
# interface — so neither 127.0.0.1 nor the node's real IP is reachable from
# a Windows browser running on the SAME physical machine as the k3s node
# under WSL2. A `kubectl port-forward` bound to 127.0.0.1 proved this live:
# it is a REAL bound socket, and WSL2 mirrored networking's localhost-
# forwarding correctly bridges Windows to it — just never to klipper-lb's
# NAT-only rule.
#
# Fix: a persistent local TCP relay (socat), managed as a systemd template
# unit, listening on 127.0.0.1:<port> and forwarding to the Traefik
# LoadBalancer's discovered IP. Reconciled on every installer run — the
# backend IP is re-discovered and rewritten only if it changed; the unit
# file is written once and left alone otherwise.
set -euo pipefail

INDRAMESH_LOCAL_FORWARD_ENV_FILE="${INDRAMESH_LOCAL_FORWARD_ENV_FILE:-/etc/indramesh/local-forward.env}"
INDRAMESH_LOCAL_FORWARD_UNIT_FILE="${INDRAMESH_LOCAL_FORWARD_UNIT_FILE:-/etc/systemd/system/indramesh-local-forward@.service}"
INDRAMESH_LOCAL_FORWARD_PORTS="80 443"

_local_forward_unit_content() {  # _local_forward_unit_content — the systemd template unit body
  cat <<'UNIT'
[Unit]
Description=IndraMesh local TCP forward (127.0.0.1:%i -> Traefik LB) for self-host reachability (BOOT-03 gap-closure)
After=network.target

[Service]
Type=simple
EnvironmentFile=/etc/indramesh/local-forward.env
ExecStart=/usr/bin/socat -d TCP-LISTEN:%i,bind=127.0.0.1,fork,reuseaddr TCP:${INDRAMESH_LB_IP}:%i
Restart=always
RestartSec=2
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict

[Install]
WantedBy=multi-user.target
UNIT
}

ensure_socat_installed() {  # ensure_socat_installed — idempotent apt install.
  # socat is a single, well-known TCP relay tool; not part of the D-13
  # bootstrap allowlist (curl/tar/coreutils/ca-certificates), so it is
  # installed here explicitly and only for this self-host reachability
  # bridge, never through require_bootstrap_package's allowlist gate.
  if command -v socat >/dev/null 2>&1; then
    return 0
  fi
  require_sudo || return 1
  step "local-forward: installing socat (BOOT-03 gap-closure self-host bridge)"
  sudo apt-get update || {
    preflight_error "local-forward: apt-get update failed — cannot install socat"
    return 1
  }
  sudo apt-get install --yes --no-install-recommends socat || {
    preflight_error "local-forward: failed to install socat"
    return 1
  }
  return 0
}

_local_forward_port_conflict() {  # _local_forward_port_conflict <port> —
  # prints the offending `ss -ltnp` line and returns 0 if 127.0.0.1:<port>
  # (or the wildcard 0.0.0.0:<port>/*:<port>, which shadows it) is already
  # held by a process OTHER than our own socat relay; returns 1 (no
  # conflict — free, or already held by our own instance) otherwise.
  #
  # Confirmed live (2026-09-03): reconcile_local_forward previously tried
  # the bind unconditionally and just let systemd's Restart=always retry
  # loop mask the real cause — the Docker Compose dev stack's Nginx
  # (docker-proxy) had already claimed 0.0.0.0:80/443, and the resulting
  # failure surfaced nowhere near the installer (a *separate* `docker
  # compose up nginx` run failed instead, hours later, with no link back to
  # this bridge). This check fails closed BEFORE attempting the bind so the
  # real cause is named immediately, here, at provisioning time.
  #
  # `ss` (iproute2) is assumed present — it ships by default on every
  # Ubuntu variant this installer supports; if it is somehow missing, this
  # check fails OPEN (returns 1, no conflict reported) rather than blocking
  # the install over a diagnostic nicety — the actual bind attempt right
  # after this call is still the authoritative fail-closed gate.
  local port="$1" line
  command -v ss >/dev/null 2>&1 || return 1
  line="$(sudo ss -ltnp 2>/dev/null | awk -v p=":${port}" '$4 ~ (p"$") {print; exit}')"
  [ -z "$line" ] && return 1
  case "$line" in
    *'"socat"'*) return 1 ;;
    *)
      printf '%s\n' "$line"
      return 0
      ;;
  esac
}

reconcile_local_forward() {  # reconcile_local_forward <lb_ip> — idempotent
  # systemd-managed TCP relay from 127.0.0.1:{80,443} to <lb_ip>:{80,443}.
  # Rewrites the backend IP (and restarts) only when it actually changed;
  # the unit file is written once. Fail-closed on any step.
  local lb_ip="${1:?LB IP required}"
  local port desired_env current_env desired_unit current_unit
  local env_changed=0 unit_changed=0

  ensure_socat_installed || return 1
  require_sudo || return 1

  sudo mkdir -p "$(dirname "$INDRAMESH_LOCAL_FORWARD_ENV_FILE")" || {
    preflight_error "local-forward: failed to create $(dirname "$INDRAMESH_LOCAL_FORWARD_ENV_FILE")"
    return 1
  }

  desired_env="INDRAMESH_LB_IP=$lb_ip"
  current_env=""
  [ -f "$INDRAMESH_LOCAL_FORWARD_ENV_FILE" ] && current_env="$(sudo cat "$INDRAMESH_LOCAL_FORWARD_ENV_FILE" 2>/dev/null || true)"
  if [ "$current_env" != "$desired_env" ]; then
    printf '%s\n' "$desired_env" | sudo tee "$INDRAMESH_LOCAL_FORWARD_ENV_FILE" >/dev/null || {
      preflight_error "local-forward: failed to write $INDRAMESH_LOCAL_FORWARD_ENV_FILE"
      return 1
    }
    env_changed=1
  fi

  desired_unit="$(_local_forward_unit_content)"
  current_unit=""
  [ -f "$INDRAMESH_LOCAL_FORWARD_UNIT_FILE" ] && current_unit="$(sudo cat "$INDRAMESH_LOCAL_FORWARD_UNIT_FILE" 2>/dev/null || true)"
  if [ "$current_unit" != "$desired_unit" ]; then
    printf '%s' "$desired_unit" | sudo tee "$INDRAMESH_LOCAL_FORWARD_UNIT_FILE" >/dev/null || {
      preflight_error "local-forward: failed to write $INDRAMESH_LOCAL_FORWARD_UNIT_FILE"
      return 1
    }
    unit_changed=1
  fi

  if [ "$unit_changed" -eq 1 ]; then
    sudo systemctl daemon-reload || {
      preflight_error "local-forward: systemctl daemon-reload failed"
      return 1
    }
  fi

  for port in $INDRAMESH_LOCAL_FORWARD_PORTS; do
    sudo systemctl enable "indramesh-local-forward@${port}.service" >/dev/null 2>&1 || {
      preflight_error "local-forward: failed to enable indramesh-local-forward@${port}.service"
      return 1
    }
    if [ "$env_changed" -eq 1 ] || [ "$unit_changed" -eq 1 ] \
       || ! systemctl is-active --quiet "indramesh-local-forward@${port}.service" 2>/dev/null; then
      local conflict_line
      if conflict_line="$(_local_forward_port_conflict "$port")"; then
        preflight_error "local-forward: port ${port} is already held by another process — NOT indramesh-local-forward's own socat relay: ${conflict_line}. Refusing to start indramesh-local-forward@${port}.service and race it. This is commonly the Docker Compose dev stack's Nginx (docker-proxy) still running — stop it first (e.g. 'docker compose stop nginx' or the full './stop'-equivalent for whichever stack is holding the port) and re-run the installer. (BOOT-03 gap-closure port-conflict guard)"
        return 1
      fi
      sudo systemctl restart "indramesh-local-forward@${port}.service" || {
        preflight_error "local-forward: failed to (re)start indramesh-local-forward@${port}.service"
        return 1
      }
    fi
  done
  step "local-forward: 127.0.0.1:{80,443} -> $lb_ip reconciled (BOOT-03 gap-closure)"
  return 0
}
