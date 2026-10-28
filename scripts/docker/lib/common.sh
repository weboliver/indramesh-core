#!/bin/bash
# common.sh — Docker quickstart helper layer (Phase 311; D-05..D-11).
# Sourceable; requires bash, coreutils, python3, openssl and Docker on PATH.
#
# Boundary (D-11): the Docker path INSTALLS NOTHING and NEVER ESCALATES. Docker,
# Compose and openssl are prerequisites of the quickstart, not this installer's
# responsibility, so none of the k3s apt/sudo helpers are used from here. What is
# reused verbatim from scripts/k8s/lib/common.sh — a single source of truth — is
# the reporting/parsing layer: die / step / preflight_error, dotenv_keys,
# validate_env_schema, require_env_value and the private-temp helper with its
# cleanup. No apt path, no sudo path, no cluster path.
#
# Everything in here is read-only with respect to the host: the daemon is
# queried, ports are bind-probed on the loopback, and the two advisory checks
# (memory, vm.max_map_count) print a recommendation and never run it.
set -euo pipefail

_DOCKER_LIB_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
_K8S_LIB_DIR="$(cd "$_DOCKER_LIB_DIR/../../k8s/lib" && pwd)"
# shellcheck source=../../k8s/lib/common.sh
. "$_K8S_LIB_DIR/common.sh"

# ---------------------------------------------------------------------------
# Reading one literal value out of an env file (Phase 311.1 D-01a).
#
# The value normaliser `_normalize_env_value` and the stack-neutral reader
# `env_file_value <file> <key>` are defined ONCE in the base library
# (scripts/k8s/lib/common.sh, sourced above) so the shared secret-generation
# core (scripts/lib/secrets.sh) can reach them from either stack without
# depending on this Docker library. The Docker reader below survives only as a
# thin delegating wrapper so `docker_resolve_port`'s two call sites and every
# other Docker caller behave byte-identically and nothing on this path has to
# change. A second copy of the normaliser is exactly how the placeholder
# comparison drifted apart once already (Phase 311.1 D-01) — it is not coming
# back here.
# ---------------------------------------------------------------------------

docker_env_value() {  # docker_env_value <env_file> <key> — echo the literal value, normalised ("" when absent)
  env_file_value "$@"
}

# ---------------------------------------------------------------------------
# Stdin isolation for Docker. Every `docker` invocation in this installer path
# carries `</dev/null`, because a run/build that inherits the caller's stdin
# silently consumes it — and the caller can be a `while read` loop over the env
# file (the truncation defect in lib/secrets.sh, see .planning/WINDOWS.md). The
# query-only subcommands below (`info`, `compose version`, `compose ps`,
# `compose config`) do not attach stdin today, but the redirect is applied
# uniformly so the property cannot be lost by a later edit.
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Port resolution. Precedence mirrors Compose's own interpolation order
# (shell environment > project .env), with the example file consulted while the
# project .env does not exist yet — Compose needs it, and so does a preflight
# that runs before the environment file is created.
# ---------------------------------------------------------------------------
docker_resolve_port() {  # docker_resolve_port <env_file> <fallback_env_file> <var> <default>
  local env_file="$1" fallback="$2" var="$3" default="$4" value=""
  value="${!var:-}"
  if [ -z "$value" ] && [ -n "$env_file" ] && [ -f "$env_file" ]; then
    value="$(docker_env_value "$env_file" "$var")"
  fi
  if [ -z "$value" ] && [ -n "$fallback" ] && [ -f "$fallback" ]; then
    value="$(docker_env_value "$fallback" "$var")"
  fi
  [ -n "$value" ] || value="$default"
  printf '%s' "$value"
}

# ---------------------------------------------------------------------------
# Preflight (D-11). The daemon and Compose-version checks abort; memory and
# vm.max_map_count are advisory only (they print the fix and never run it).
# ---------------------------------------------------------------------------

DOCKER_MIN_COMPOSE_MAJOR=2
DOCKER_MIN_COMPOSE_MINOR=20
DOCKER_MEMORY_FLOOR_BYTES=$((4 * 1024 * 1024 * 1024))
DOCKER_MAX_MAP_COUNT_FLOOR=262144
DOCKER_MAX_MAP_COUNT_RECOMMENDED=1048576

docker_preflight_daemon() {  # 0 when the Docker daemon answers
  if docker info </dev/null >/dev/null 2>&1; then
    echo "[preflight] the Docker daemon is reachable"
    return 0
  fi
  preflight_error "the Docker daemon is not reachable — start it (e.g. 'sudo systemctl start docker', or start Docker Desktop / the WSL distro it runs in) and rerun ./install_indramesh_docker.sh"
  return 1
}

_compose_version_numbers() {  # echo "<major> <minor>" of the installed Compose CLI
  local raw=""
  raw="$(docker compose version </dev/null 2>/dev/null || true)"
  printf '%s\n' "$raw" | grep -oE '[0-9]+(\.[0-9]+)+' | head -n1 \
    | awk -F. '{print $1 " " $2}'
}

docker_preflight_compose_version() {  # 0 when Compose >= 2.20 (depends_on.required)
  local nums major minor
  nums="$(_compose_version_numbers)"
  major="${nums%% *}"
  minor="${nums##* }"
  if [ -z "$major" ] || [ -z "$minor" ]; then
    preflight_error "could not determine the Docker Compose version ('docker compose version' produced no version string) — Docker Compose >= ${DOCKER_MIN_COMPOSE_MAJOR}.${DOCKER_MIN_COMPOSE_MINOR} is required (depends_on.required)"
    return 1
  fi
  # Numeric per-component comparison: a string compare would reject a current
  # Compose such as v5.6.0 against "2.20".
  if [ "$major" -gt "$DOCKER_MIN_COMPOSE_MAJOR" ] \
    || { [ "$major" -eq "$DOCKER_MIN_COMPOSE_MAJOR" ] && [ "$minor" -ge "$DOCKER_MIN_COMPOSE_MINOR" ]; }; then
    echo "[preflight] Docker Compose ${major}.${minor} satisfies the >= ${DOCKER_MIN_COMPOSE_MAJOR}.${DOCKER_MIN_COMPOSE_MINOR} floor"
    return 0
  fi
  preflight_error "Docker Compose ${major}.${minor} is too old — >= ${DOCKER_MIN_COMPOSE_MAJOR}.${DOCKER_MIN_COMPOSE_MINOR} is required (depends_on.required was introduced in Compose ${DOCKER_MIN_COMPOSE_MAJOR}.${DOCKER_MIN_COMPOSE_MINOR})"
  return 1
}

docker_preflight_memory() {  # advisory: warn below ~4 GiB, never abort
  local bytes gib
  bytes="$(docker info --format '{{.MemTotal}}' </dev/null 2>/dev/null || true)"
  case "$bytes" in
    ''|*[!0-9]*)
      echo "[preflight] WARNING: could not read the Docker daemon's memory total — make sure at least ~4 GiB are available to Docker" >&2
      return 0
      ;;
  esac
  gib="$(awk -v b="$bytes" 'BEGIN { printf "%.1f", b / 1073741824 }')"
  if [ "$bytes" -lt "$DOCKER_MEMORY_FLOOR_BYTES" ]; then
    echo "[preflight] WARNING: the Docker daemon reports ${gib} GiB, below the ~4 GiB this stack wants (OpenSearch alone reserves 2 GiB). Under WSL2 the limit is the DISTRO's memory, not the Windows host's RAM: set 'memory=' under [wsl2] in the Windows-side .wslconfig and run 'wsl --shutdown'. Continuing." >&2
  else
    echo "[preflight] the Docker daemon reports ${gib} GiB (>= ~4 GiB)"
  fi
  return 0
}

_port_bindable() {  # _port_bindable <host> <port> — 0 when the port can still be bound
  python3 - "$1" "$2" <<'PY'
import socket
import sys

host, port = sys.argv[1], int(sys.argv[2])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    sock.bind((host, port))
except OSError:
    sys.exit(1)
finally:
    sock.close()
sys.exit(0)
PY
}

docker_own_published_ports() {  # echo the host ports this project's own containers publish (one per line)
  # Best-effort: on a fresh clone there is no project .env yet, so `compose ps`
  # may fail — that is exactly the case where no container of ours can exist.
  docker compose ps --format json </dev/null 2>/dev/null | python3 -c '
import json
import sys

ports = set()
for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    for publisher in (obj.get("Publishers") or []):
        published = publisher.get("PublishedPort")
        if published:
            ports.add(int(published))
print("\n".join(str(p) for p in sorted(ports)))
' 2>/dev/null || true
}

# <variable>:<compose-default>:<published-host> — the host side mirrors the
# compose file: nginx on every interface, everything else on the loopback.
DOCKER_PORT_SPECS=(
  "HTTP_PORT:8180:0.0.0.0"
  "HTTPS_PORT:8443:0.0.0.0"
  "DB_PORT:5434:127.0.0.1"
  "VALKEY_PORT:6379:127.0.0.1"
  "OPENSEARCH_PORT:9200:127.0.0.1"
  "BACKEND_PORT:8000:127.0.0.1"
  "FRONTEND_PORT:5173:127.0.0.1"
)

docker_preflight_ports() {  # docker_preflight_ports <env_file> [fallback_env_file]
  local env_file="${1:-}" fallback="${2:-}"
  local own spec var default host value
  own="$(docker_own_published_ports)"
  for spec in "${DOCKER_PORT_SPECS[@]}"; do
    var="${spec%%:*}"
    default="$(printf '%s' "${spec#*:}" | cut -d: -f1)"
    host="${spec##*:}"
    value="$(docker_resolve_port "$env_file" "$fallback" "$var" "$default")"
    # Probe the loopback even for the nginx ports: anything already bound on
    # 0.0.0.0 would make the loopback bind fail too, so this catches both.
    [ "$host" = "0.0.0.0" ] && host="127.0.0.1"
    if printf '%s\n' "$own" | grep -qx "$value"; then
      echo "[preflight] port ${value} (${var}) is held by this project's own running container — safe on a re-run"
      continue
    fi
    if ! _port_bindable "$host" "$value"; then
      preflight_error "host port ${value} is already in use — set ${var} to a free port in .env and rerun ./install_indramesh_docker.sh"
      return 1
    fi
  done
  echo "[preflight] host ports are free"
  return 0
}

docker_preflight_max_map_count() {  # docker_preflight_max_map_count <project_dir> <example_env> [scratch_dir]
  # Advisory only (D-11): print the fix, never run an escalation command.
  local project_dir="$1" example_env="$2" scratch="${3:-}"
  local render_dir="$project_dir" image value=""
  if [ ! -f "$project_dir/.env" ]; then
    if [ -z "$scratch" ]; then
      echo "[preflight] WARNING: OpenSearch needs vm.max_map_count >= ${DOCKER_MAX_MAP_COUNT_FLOOR} as seen by containers" >&2
      return 0
    fi
    render_dir="$scratch"
    cp "$example_env" "$render_dir/.env"
  fi
  # The stack pulls this image anyway, so the probe adds no download of its own.
  image="$(docker compose --project-directory "$render_dir" -f "$project_dir/docker-compose.yml" config --images </dev/null 2>/dev/null \
    | grep -i 'opensearch' | head -n1 || true)"
  if [ -n "$image" ]; then
    value="$(docker run --rm --entrypoint cat "$image" /proc/sys/vm/max_map_count </dev/null 2>/dev/null | tr -dc '0-9' || true)"
  fi
  case "${value:-}" in
    ''|*[!0-9]*) value="" ;;
  esac
  if [ -n "$value" ] && [ "$value" -ge "$DOCKER_MAX_MAP_COUNT_FLOOR" ]; then
    echo "[preflight] vm.max_map_count as seen by containers: ${value} (>= ${DOCKER_MAX_MAP_COUNT_FLOOR})"
    return 0
  fi
  cat >&2 <<EOF
[preflight] WARNING: OpenSearch needs vm.max_map_count >= ${DOCKER_MAX_MAP_COUNT_FLOOR} as seen by CONTAINERS;
            the current value is ${value:-unknown}. This kernel setting is shared with the distro, so fix it where Docker
            runs — a Windows-side change alone does not reach the containers under WSL2:
              sudo sysctl -w vm.max_map_count=${DOCKER_MAX_MAP_COUNT_RECOMMENDED}
              echo 'vm.max_map_count=${DOCKER_MAX_MAP_COUNT_RECOMMENDED}' | sudo tee /etc/sysctl.d/90-indramesh-max-map-count.conf
              sudo sysctl --system
            On WSL2 run those three lines inside the WSL distro, then 'wsl --shutdown' in Windows and reopen the distro.
            This installer never runs sudo itself. Continuing.
EOF
  return 0
}

# ---------------------------------------------------------------------------
# Readiness wait (D-05). The backend healthcheck is the real readiness
# condition (it only reports healthy after the first-boot migrations), and nginx
# is polled afterwards the same way frontend/run-e2e.sh does.
# ---------------------------------------------------------------------------

_backend_health() {  # echo the backend service's Health/State from compose ps ("" when unknown)
  docker compose ps --format json </dev/null 2>/dev/null | python3 -c '
import json
import sys

for line in sys.stdin:
    line = line.strip()
    if not line:
        continue
    try:
        obj = json.loads(line)
    except ValueError:
        continue
    if obj.get("Service") == "backend":
        print(obj.get("Health") or obj.get("State") or "")
        break
' 2>/dev/null || true
}

wait_for_backend_healthy() {  # wait_for_backend_healthy <project_dir> [timeout_seconds]
  local project_dir="${1:?project directory required}"
  local timeout="${2:-300}" waited=0 interval=3 status=""
  while [ "$waited" -lt "$timeout" ]; do
    status="$(cd "$project_dir" && _backend_health)"
    if [ "$status" = "healthy" ]; then
      echo "  backend is healthy${waited:+ after ${waited}s}"
      return 0
    fi
    sleep "$interval"
    waited=$((waited + interval))
  done
  preflight_error "the backend did not report 'healthy' within ${timeout}s — inspect it with 'docker compose logs backend'"
  return 1
}

wait_for_nginx() {  # wait_for_nginx <http_port> [timeout_seconds]
  local port="${1:-8180}" timeout="${2:-120}" waited=0 interval=2 code=""
  while [ "$waited" -lt "$timeout" ]; do
    code="$(curl -s -o /dev/null -w '%{http_code}' "http://localhost:${port}/health" 2>/dev/null || true)"
    case "$code" in
      200|301|302)
        echo "  nginx is answering (HTTP ${code})"
        return 0
        ;;
    esac
    sleep "$interval"
    waited=$((waited + interval))
  done
  preflight_error "nginx did not answer on http://localhost:${port}/health within ${timeout}s — inspect it with 'docker compose logs nginx backend'"
  return 1
}

# ---------------------------------------------------------------------------
# Default-network address space (Phase 318, clean-host finding). The Compose
# `default` network takes COMPOSE_SUBNET (172.28.0.0/16) and pins nginx to
# NGINX_TRUSTED_IP (172.28.0.10) — the address the backend trusts as its proxy.
# On a host where that range is already taken (another Docker network, a VPN,
# the WSL NAT) `docker compose up` failed with "Pool overlaps with other one on
# this address space", and the override needed two coupled hand edits. The
# preflight compares the configured subnet with every Docker network and host
# IPv4 route; when the operator left both values at their defaults it picks the
# first free candidate and writes both keys into the .env, otherwise it stops
# naming the conflict.
# ---------------------------------------------------------------------------
DOCKER_DEFAULT_SUBNET="172.28.0.0/16"
DOCKER_DEFAULT_NGINX_IP="172.28.0.10"
DOCKER_SUBNET_CANDIDATES=(
  172.28.0.0/16 172.29.0.0/16 172.30.0.0/16 172.31.0.0/16
  10.210.0.0/16 10.211.0.0/16 10.212.0.0/16 10.213.0.0/16
  192.168.240.0/20 192.168.224.0/20
)

_docker_taken_ranges() {  # _docker_taken_ranges <own_network> — echo "<owner> <cidr>" for every range in use
  local own="$1" ids networks="" own_subnets
  ids="$(docker network ls -q </dev/null 2>/dev/null || true)"
  if [ -n "$ids" ]; then
    # shellcheck disable=SC2086
    networks="$(docker network inspect --format '{{.Name}}{{range .IPAM.Config}} {{.Subnet}}{{end}}' $ids </dev/null 2>/dev/null || true)"
    printf '%s\n' "$networks" \
      | awk -v own="$own" 'NF && $1 != own { for (i = 2; i <= NF; i++) print "docker-network:" $1, $i }' || true
  fi
  # A running stack's own network puts its subnet into the host routing table
  # through its bridge, so a re-run must not count that route as a conflict.
  own_subnets="$(printf '%s\n' "$networks" | awk -v own="$own" '$1 == own { for (i = 2; i <= NF; i++) print $i }')"
  ip -o -4 route show 2>/dev/null \
    | awk -v own_subnets="$own_subnets" '
        BEGIN { n = split(own_subnets, s, "\n"); for (i = 1; i <= n; i++) if (s[i] != "") mine[s[i]] = 1 }
        $1 ~ /^[0-9.]+(\/[0-9]+)?$/ && !($1 in mine) { print "host-route:" $3, $1 }' || true
}

_env_set_key() {  # _env_set_key <env_file> <key> <value> — replace KEY=, else uncomment "# KEY=", else append
  python3 - "$1" "$2" "$3" <<'PY'
import os
import re
import sys
import tempfile

path, key, value = sys.argv[1:4]
with open(path, encoding="utf-8") as fh:
    lines = fh.read().splitlines(keepends=True)
line = f"{key}={value}\n"
for pattern in (rf"^\s*{key}=", rf"^\s*#\s*{key}="):
    hit = next((i for i, l in enumerate(lines) if re.match(pattern, l)), None)
    if hit is not None:
        lines[hit] = line
        break
else:
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines.append(line)
mode = os.stat(path).st_mode & 0o777
fd, tmp = tempfile.mkstemp(dir=os.path.dirname(os.path.abspath(path)))
with os.fdopen(fd, "w", encoding="utf-8") as fh:
    fh.writelines(lines)
os.chmod(tmp, mode)
os.replace(tmp, path)
PY
}

docker_preflight_subnet() {  # docker_preflight_subnet <env_file> — pick or verify a free default-network subnet
  local env_file="$1" project subnet nginx_ip subnet_explicit=0 nginx_default=0 taken verdict
  local new_subnet new_ip conflict_with
  project="${COMPOSE_PROJECT_NAME:-$(docker_env_value "$env_file" COMPOSE_PROJECT_NAME)}"
  project="${project:-indramesh}"
  subnet="${COMPOSE_SUBNET:-$(docker_env_value "$env_file" COMPOSE_SUBNET)}"
  nginx_ip="${NGINX_TRUSTED_IP:-$(docker_env_value "$env_file" NGINX_TRUSTED_IP)}"
  [ -n "$subnet" ] && subnet_explicit=1
  subnet="${subnet:-$DOCKER_DEFAULT_SUBNET}"
  if [ -z "$nginx_ip" ] || [ "$nginx_ip" = "$DOCKER_DEFAULT_NGINX_IP" ]; then
    nginx_default=1
  fi
  # A value from the shell outranks the .env for Compose, so it can only be
  # checked, never rewritten.
  [ -n "${COMPOSE_SUBNET:-}" ] && nginx_default=0
  [ -n "${NGINX_TRUSTED_IP:-}" ] && nginx_default=0
  taken="$(_docker_taken_ranges "${project}_default")"
  verdict="$(DOCKER_TAKEN_RANGES="$taken" python3 - "$subnet" "$nginx_ip" "$subnet_explicit" "$nginx_default" "${DOCKER_SUBNET_CANDIDATES[@]}" <<'PY'
import ipaddress
import os
import sys

subnet, nginx_ip, explicit, nginx_default = sys.argv[1], sys.argv[2], sys.argv[3] == "1", sys.argv[4] == "1"
candidates = sys.argv[5:]
taken = []
for line in os.environ.get("DOCKER_TAKEN_RANGES", "").splitlines():
    owner, _, cidr = line.strip().rpartition(" ")
    try:
        taken.append((owner, ipaddress.ip_network(cidr, strict=False)))
    except ValueError:
        continue


def conflict(net):
    for owner, other in taken:
        if other.version == net.version and net.overlaps(other):
            return f"{owner} ({other})"
    return None


try:
    net = ipaddress.ip_network(subnet)
except ValueError:
    print(f"error COMPOSE_SUBNET '{subnet}' is not a valid network address (for example 172.29.0.0/16)")
    sys.exit(0)
hit = conflict(net)
if hit is None:
    gateway_ok = True
    try:
        gateway_ok = ipaddress.ip_address(nginx_ip) in net if nginx_ip else True
    except ValueError:
        gateway_ok = False
    if gateway_ok:
        print("ok")
    elif nginx_default:
        print(f"nginx {net.network_address + 10}")
    else:
        print(f"error NGINX_TRUSTED_IP {nginx_ip} lies outside COMPOSE_SUBNET {net} — set an address inside it (for example {net.network_address + 10})")
    sys.exit(0)
if explicit or not nginx_default:
    print(f"error COMPOSE_SUBNET {net} overlaps {hit} — set COMPOSE_SUBNET to a free range and NGINX_TRUSTED_IP to an address inside it")
    sys.exit(0)
for cand in candidates:
    c = ipaddress.ip_network(cand)
    if conflict(c) is None:
        print(f"switch {c} {c.network_address + 10} {hit}")
        sys.exit(0)
print(f"error COMPOSE_SUBNET {net} overlaps {hit}, and none of the built-in alternatives is free — set COMPOSE_SUBNET and NGINX_TRUSTED_IP in .env")
PY
)" || { preflight_error "the default-network check could not run (python3)"; return 1; }
  case "$verdict" in
    ok)
      echo "[preflight] the default network ${subnet} is free"
      ;;
    nginx\ *)
      _env_set_key "$env_file" NGINX_TRUSTED_IP "${verdict#nginx }" || return 1
      echo "[preflight] NGINX_TRUSTED_IP set to ${verdict#nginx } inside COMPOSE_SUBNET ${subnet} (written to .env)"
      ;;
    switch\ *)
      read -r _ new_subnet new_ip conflict_with <<< "$verdict"
      _env_set_key "$env_file" COMPOSE_SUBNET "$new_subnet" || return 1
      _env_set_key "$env_file" NGINX_TRUSTED_IP "$new_ip" || return 1
      echo "[preflight] the default network ${subnet} is taken by ${conflict_with} — using ${new_subnet} (nginx ${new_ip}); both written to .env"
      ;;
    *)
      preflight_error "${verdict#error }"
      return 1
      ;;
  esac
  return 0
}
