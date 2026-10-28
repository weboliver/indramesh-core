# Log Infrastructure

Operator manual for the IndraMesh log stack (Phase 301): the bundled **Loki**
log store, the **Grafana Alloy** collector, the read-only **docker-socket-proxy**,
the optional **Grafana** UI, and how to forward logs to an external **OTLP**
receiver. It covers Compose and the k3s installer. Application code is not
touched — ingesting, parsing and shipping are configuration only.

## Architecture

```
Compose services (json-file driver, 10m x 3 rotation)
        │  container log files
        ▼
docker-socket-proxy  ──(read-only Docker API)──►  Alloy
 (CONTAINERS/NETWORKS/PING only, POST=0)          discovery.docker
                                                  discovery.relabel (service, env)
                                                  loki.source.docker
                                                  loki.process "app"  (json + logfmt)
                                                        │
                              ┌─────────────────────────┴─────────────────────────┐
                              ▼                                                    ▼
                     loki.write "bundled"                          otelcol.receiver.loki "bridge"
                     (http://loki:3100/loki/api/v1/push)                        │
                              │                                       otelcol.processor.batch
                              ▼                                                    ▼
                        Loki (TSDB, 14d)                        otelcol.exporter.otlp     (gRPC)
                              ▲                                 otelcol.exporter.otlphttp (HTTP + auth)
                              │                                                    │
                        Grafana (observability profile)                    external OTLP intake
```

Networks:

- **`logging_internal`** (`internal: true`, no external routing): Loki,
  docker-socket-proxy, and the Alloy/Grafana endpoints. Loky publishes **no
  host port** — it is reachable only from this network.
- **`logging_query`** (`internal: true`, Phase 305): the bounded server-side
  query path — Loki is dual-homed onto it and the backend reaches `loki:3100`
  only here. No published port, no nginx route (Carry-01).
- **`default`**: Alloy is dual-homed (egress to an external OTLP target) and
  Grafana can be published on the host loopback.

The backend service is deliberately **not** attached to `logging_internal`: it
cannot reach `docker-socket-proxy`. Collection happens through the Docker
socket proxy; the `log-explorer` plugin queries Loki over the dedicated
`logging_query` network only.

## Services & profiles

| Service | Profile | Purpose |
|---|---|---|
| `loki` | `logging`, `observability` | Bundled log store (monolithic, TSDB v13, 14-day retention). |
| `alloy` | `logging`, `observability` | Collector: Docker discovery → parse → bundled Loki and/or external OTLP. |
| `docker-socket-proxy` | `logging`, `observability` | Read-only Docker API (`CONTAINERS=1 NETWORKS=1 PING=1 POST=0`, all other sections `0`). Alloy never mounts the raw socket. |
| `grafana` | `observability` | Log UI on `127.0.0.1:3000` with a provisioned Loki datasource. |

The three log services (`loki`, `alloy`, `docker-socket-proxy`) are **not** part
of the **default service set**: a plain `docker compose up -d` starts only the
application services, and the log stack joins the render only when the `logging`
profile is active. No application service depends on them, so the dev stack and
the E2E run start with the profiles off. Activating the profile is what turns the
bundled log stack on:

```bash
# bundled log stack only (loki + alloy + docker-socket-proxy)
docker compose --profile logging up -d

# the log stack plus Grafana
docker compose --profile observability up -d
```

Grafana is only present when the `observability` profile is active. A root `.env`
can set `COMPOSE_PROFILES=logging` to activate the profile for every Compose
command — the develop checkout does this so its stack keeps feeding the
`log-explorer` plugin.

There is no nginx route to Grafana or Loki in Compose (a static upstream would
502 whenever the profile is off). The k8s deployment keeps its own ingress.

## Environment model

Every knob is a **root `.env`** value. Compose injects them into the `alloy`
service; the k8s installer mirrors the same model into Helm values. All keys are
optional — the defaults below are the shipped behaviour.

| Variable | Default | Semantics |
|---|---|---|
| `ALLOY_SINK` | `loki` | Selects the checked-in sink fragment: `loki`, `otlp`, `otlphttp`, or `none`. Only a file path is substituted — env never renders arbitrary Alloy config (D-02). |
| `OTLP_ENDPOINT` | *(empty)* | External OTLP target. gRPC: `host:port` (e.g. `graylog:4317`). HTTP: full base URL (e.g. `https://graylog:4318`); `logs_endpoint` defaults to `endpoint + "/v1/logs"`. |
| `OTLP_AUTHORIZATION` | *(empty)* | Value of the `Authorization` header for the **otlphttp** fragment only (send pre-base64-encoded Basic). gRPC never carries credentials (Pitfall 9). |
| `OTLP_TLS_INSECURE` | `false` | gRPC only. Unset/`false` = **TLS** (the same default in Compose **and** k8s). Only the explicit string `true` enables plaintext gRPC. |
| `LOKI_URL` | `http://loki:3100/loki/api/v1/push` | Bundled `loki.write` push endpoint. |
| `LOKI_QUERY_URL` | `http://loki:3100` | Loki **query base** the `log-explorer` plugin reads server-side (a bare base, **no** `/push` or `/loki/api/v1` path). Compose default `http://loki:3100`; the Kubernetes chart ships **empty** and the installer writes `http://loki.monitoring.svc.cluster.local:3100` only when `LOGINFRA_ENABLED=true`, because a lean cluster has no Loki — empty makes the plugin report `not_configured` instead of `unreachable`. An external Loki target is set here by the operator. Every value takes effect on the next backend (re)creation. |
| `LOG_EXPLORER_EXTERNAL_ONLY` | `false` | Declared "server logs go only to the external sink" switch (`true` only — set it in the external-sink recipe below: activate the `logging` profile, point `LOKI_QUERY_URL` at the external Loki, set this to `true`). Never inferred from unreachability. |
| `GRAFANA_EXPLORE_URL` | *(empty)* | Browser-reachable Grafana base enabling the "open in Grafana" deep link; empty = no link. |
| `GRAFANA_LOKI_DATASOURCE` | *(empty)* | Loki datasource UID used by the deep link; empty = no link. |
| `LOKI_RETENTION_PERIOD` | `336h` (14 days) | Loki retention window; feeds the Loki compactor's complete retention triple. |
| `LOGINFRA_ENABLED` | `false` | k8s installer switch (opt-in since 311.1) that renders the whole log stack or nothing. |

`LOGINFRA_ENABLED` is read by the **k3s installer only**: the Compose stack is unaffected by it and
gets its own log containers by activating the `logging` profile (`COMPOSE_PROFILES=logging`,
311 D-16). Set it to `true` in the root `.env` to install Loki and Alloy on k3s.

`sys.env()` in the checked-in fragments reads **only** `OTLP_ENDPOINT`,
`OTLP_AUTHORIZATION`, `OTLP_TLS_INSECURE` and `LOKI_URL`. No other name is
interpolated anywhere. The `LOKI_QUERY_URL` / `LOG_EXPLORER_*` /
`GRAFANA_*` values are read by the **backend process** (the `log-explorer`
plugin), never interpolated into Alloy config.

If a Loki server is ever shared by more than one IndraMesh instance, each
instance must use a **distinct `ENVIRONMENT`** value (both stacks) and — in
k8s — live in its own namespace: the plugin derives its mandatory query scope
from `ENVIRONMENT` plus the k8s `POD_NAMESPACE` Downward-API value, and a
collision would expose another instance's streams.

### Selecting a sink

The alloy `command` concatenates the base fragment with exactly one sink:

```
cat /etc/alloy/base-docker.alloy /etc/alloy/sinks/${ALLOY_SINK:-loki}.alloy
```

A typo in `ALLOY_SINK` makes `cat` fail, so Alloy never starts and stays in a
visible restart loop (`docker compose logs alloy`). It cannot silently fall back
to a different sink.

| `ALLOY_SINK` | Ships to bundled Loki | Ships to external OTLP |
|---|---|---|
| `loki` (default) | yes | no |
| `otlp` | yes (tee) | yes, gRPC |
| `otlphttp` | yes (tee) | yes, HTTP |
| `none` | no | no (parse only) |

### Tee semantics (D-03)

`otlp` and `otlphttp` are **tee** fragments: `forward_to` lists both
`loki.write.bundled.receiver` and `otelcol.receiver.loki.bridge.receiver`.
Setting an external target **never** silences the bundled Loki on its own —
Alloy still writes into `loki.write "bundled"`. Use `ALLOY_SINK=none` if you want
neither destination.

### Shipping to an external Loki (the external-only recipe)

Deactivating the `logging` profile stops the whole bundled log stack — `alloy`
included — so the retired "bundled Loki off while an external sink stays on"
arrangement no longer applies. To keep the collector and the query path while the
bundled Loki is not the query target, activate the `logging` profile and point
the query base at the external Loki:

```bash
# root .env
COMPOSE_PROFILES=logging
LOKI_QUERY_URL=https://loki.example.com
LOG_EXPLORER_EXTERNAL_ONLY=true
```

`LOG_EXPLORER_EXTERNAL_ONLY=true` is the declaration that server logs go only to
the external sink; it is never inferred from unreachability. The former
deploy-replicas off-switch that used to express this arrangement is gone.

### gRPC vs HTTP — credentials (Pitfall 9)

`otelcol.exporter.otlp` uses gRPC, which **cannot** carry sensitive credentials
over an insecure channel. The gRPC fragment therefore has no headers at all and
is TLS by default. Use `otlphttp` for header/token auth:

- `otlphttp` sets `headers = { "Authorization" = sys.env("OTLP_AUTHORIZATION") }`
  and follows the endpoint scheme (`http`/`https`) for TLS — there is no
  `insecure` switch on this fragment.
- An empty `OTLP_AUTHORIZATION` sends an **empty** `Authorization` header. Some
  intakes reject that; either set a value, terminate TLS at a proxy, or omit the
  header by choosing a different sink. No fragment branches on env (D-02).

### Missing `OTLP_ENDPOINT`

`otlp`/`otlphttp` with an unset `OTLP_ENDPOINT` gives the exporter an empty
endpoint, so Alloy fails loudly at startup instead of forwarding into the void.
There is no silent fallback.

## Rotation, retention and the RAM budget

- **Docker log rotation (D-06):** every Compose service uses the `json-file`
  driver with `max-size=10m` and `max-file=3` — `10m × 3` per service, including
  third-party images. There is no capacity-based driver and no per-service
  override.
- **Loki retention (D-04):** 14 days (`336h`), identical in Compose and k8s and
  overridable with `LOKI_RETENTION_PERIOD`. Retention requires the complete
  triple — `limits_config.retention_period` + `compactor.retention_enabled:
  true` + `compactor.delete_request_store: filesystem` — otherwise it silently
  does not apply.

### RAM measurement methodology (D-05)

Measure **after** the stack is up, with the containers settled:

```bash
docker compose up -d
docker stats --no-stream loki alloy docker-socket-proxy
```

`docker stats --no-stream` prints a single instantaneous sample per container —
that is the measurement instrument. The values are a point-in-time snapshot of
`loki`, `alloy` and `docker-socket-proxy`, not a guarantee.

**Target corridor:** ~450 MiB combined for `loki + alloy + docker-socket-proxy`
on the WSL2 development host. This is a goal, not a hard limit. The stack is
opt-in: it runs only while the `logging` (or `observability`) profile is active,
so a host that does not need it never pays the memory cost.

**Measured values (mandatory before phase acceptance):**

| Container | `docker stats --no-stream` (MiB) |
|---|---|
| loki | _TODO: gemessen eintragen_ |
| alloy | _TODO: gemessen eintragen_ |
| docker-socket-proxy | _TODO: gemessen eintragen_ |

> **BLOCKING:** this document carries the `_TODO: gemessen eintragen_`
> placeholders until the Orchestrator fills in the measured
> `docker stats --no-stream` values at wave-merge. Phase 301 is **not accepted**
> while any placeholder remains (LOGINFRA-05). The executor must not fabricate
> measurements.

## Canary / redaction proof

`scripts/logging/verify_loki_canary.sh` is the executable proof that normal
lines reach Loki byte-exactly while the secret-like 298 canary stays absent. It
emits a marker (and a canary in a `password` extra field) on the backend
container's own PID-1 stdout via `docker compose run --rm --no-deps`, then
queries `loki:3100/loki/api/v1/query_range` from a throwaway `python:3.12-slim`
container joined to `indramesh_logging_internal`. It exits non-zero if the
marker is missing or the canary appears.

Run it from the **main checkout** against a running stack — never from a Git
worktree, and never as part of an executor task:

```bash
bash scripts/logging/verify_loki_canary.sh
```

## Kubernetes

The k3s installer provisions the same stack beside `kube-prometheus-stack`:
`lib/loki.sh` installs the signed, pinned Loki community chart and
`lib/alloy.sh` installs the SHA-256-pinned Grafana Alloy chart, both in the
`monitoring` namespace. The existing kube-prometheus-stack Grafana is reused
(no second Grafana) and gets the `indramesh-loki` datasource.

### Installer environment model (identical keys to Compose)

Every knob is read from the **root `.env`** by the installer (`dotenv_value`),
so Compose and k8s share one source of truth (LOGINFRA-04, D-02):

| Variable | Default | k8s semantics |
|---|---|---|
| `LOGINFRA_ENABLED` | `false` | Gate around BOTH log-stack releases; the log stack is opt-in. `false` (the default) skips the whole provisioning (Loki + Alloy) and **removes nothing** — existing releases, PVCs and the Grafana datasource stay exactly as they are. Set it to `true` to install the log stack. |
| `ALLOY_SINK` | `loki` | Selects the checked-in sink fragment (`loki`/`otlp`/`otlphttp`/`none`); the installer assembles `base-kubernetes.alloy` + `sinks/<sink>.alloy` into the Alloy ConfigMap. An unknown value fails closed before Helm. |
| `OTLP_ENDPOINT` | *(empty)* | External OTLP target (gRPC `host:port` / HTTP base URL). |
| `OTLP_AUTHORIZATION` | *(empty)* | `otlphttp` header value (never sent over plaintext gRPC). |
| `OTLP_TLS_INSECURE` | `false` | Shared default: unset/`false` = **TLS** in Compose and k8s alike; only the explicit string `true` enables plaintext gRPC (D-02 mirror). |
| `LOKI_URL` | `http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push` | Bundled push endpoint (`alloy.extraEnv`). |
| `LOKI_RETENTION_PERIOD` | `336h` | Loki retention override (feeds the complete retention triple). |
| `LOG_ENV` (written by the installer from `ENVIRONMENT`) | `production` | The `env` label Alloy attaches to every line. |

The `log-explorer` query path is **chart config** (`apps.backend`):
`lokiQueryUrl` (empty by default; the installer writes
`http://loki.monitoring.svc.cluster.local:3100` when it provisions the log stack)
renders `LOKI_QUERY_URL` into the backend Deployment env, and
`logExplorer.externalOnly` / `logExplorer.grafanaExploreUrl` /
`logExplorer.grafanaLokiDatasource` render `LOG_EXPLORER_EXTERNAL_ONLY` /
`GRAFANA_EXPLORE_URL` / `GRAFANA_LOKI_DATASOURCE`. Only the backend receives
these values — the worker builds the plugin registry but never serves plugin
routes. The k8s scope additionally uses the `POD_NAMESPACE` Downward-API env
(`indramesh.kubernetesEnv`), which always equals the Alloy-emitted `namespace`
label.

### Turning individual releases off

`LOGINFRA_ENABLED=false` is the whole-stack switch described above. Individual
releases are switched off independently — the D-03 "separate switch" contract —
with a direct Helm uninstall from the main checkout (never from a Git worktree):

```bash
export KUBECONFIG=$HOME/.kube/config
# stop shipping to the bundled Loki only (Alloy keeps parsing; an external
# OTLP sink stays active — its loki.write writes then fail and are retried):
helm uninstall loki -n monitoring
# stop collecting entirely:
helm uninstall alloy -n monitoring
```

An uninstall removes only that release's own objects; the Grafana datasource
stays in place and points into empty space (Review-Korrektur 5).

### Collector shape and RBAC (D-10)

Alloy runs as a **1-replica, non-root Deployment** (`runAsNonRoot`, uid 65534,
no privilege escalation) and tails pod logs through the **Kubernetes API**
(`loki.source.kubernetes`), never via a privileged DaemonSet with a hostPath
mount on `/var/log/pods`. The installer applies a minimal **namespaced** RBAC
manifest (`scripts/k8s/alloy-rbac.yaml`, Role + RoleBinding
`indramesh-alloy-log-reader` in `indramesh`: `pods` get/list/watch and
`pods/log` get, bound to the `alloy` ServiceAccount in `monitoring`) **before**
the Helm install. The pod watch is restricted to the `indramesh` namespace in
the Alloy config itself; there is no cluster-wide log permission.

### Network policies (D-12 pair)

The Alloy NetworkPolicy denies **all ingress** (`ingress: null`) and permits
egress only to:

- `kube-dns` (namespace `kube-system`, pod label `k8s-app: kube-dns`, UDP+TCP 53),
- the discovered `kube-apiserver` ClusterIP (`/32`, TCP 443) **and** a port-only leg (TCP 443 and 6443,
  no address constraint). The port-only leg is what actually admits the discovery: k3s's NetworkPolicy
  implementation matches egress after the DNAT, so a policy naming only the ClusterIP lets Alloy start,
  report healthy and collect nothing (Phase 318). It is deliberately not bound to an IP — a WSL2 node's
  API endpoint is its `eth0` address and changes on every restart, and a multi-control-plane cluster has
  more than one endpoint. Trade-off, stated openly: any destination is reachable on those two ports; all
  other egress stays denied.
- the bundled Loki (namespace `monitoring`, pod label
  `app.kubernetes.io/name: loki`, TCP 3100),
- and — **only when `ALLOY_SINK` is `otlp`/`otlphttp`** — the external OTLP
  target, as `0.0.0.0/0` on the single TCP port derived from `OTLP_ENDPOINT`
  (explicit `host:port`, otherwise `https`→443 / `http`→80). Tightening that
  destination to an operator-specific CIDR is left to the operator (OQ4).

Loki's own chart NetworkPolicy admits ingress from Alloy in the `monitoring`
namespace (and its metrics port from `monitoring`). Phase 305 adds the matched
`backend`→Loki pair for the `log-explorer` plugin: the source-side egress
clause lives in the indramesh chart's `allow-backend-worker-egress`, and the
monitoring-side ingress half is the checked-in installer-owned
`scripts/k8s/loki-backend-ingress-netpol.yaml` (NetworkPolicies are additive,
so the chart's Alloy ingress policy stays intact) applied by `lib/loki.sh`
after the pinned Loki install. Loki stays without a published port.

### Operations notes

- **Restart behaviour (A1-like):** Alloy stores its tailing positions in
  `alloy.storagePath: /tmp/alloy`, which is **ephemeral** — by default data is
  lost between restarts and API-based tailing simply re-follows from the current
  point. A restart can therefore miss lines written while Alloy was down; add a
  small persistent volume if that gap matters.
- **Image air-gap (A2-like):** with `configReloader.enabled=false` and
  `crds.create=false`, the only image the release needs is `grafana/alloy`
  (tag override `v1.20.1`).
- **Hermetic check (in-phase):** `bash tests/bootstrap/helm_bin.sh template indramesh
  deploy/helm/indramesh` renders the chart — including the backend→Loki egress
  clause, the `POD_NAMESPACE` Downward-API env and the backend `LOKI_QUERY_URL`
  entry — without a cluster and without a bare `helm` binary on PATH. The live
  NetworkPolicy-pair proof stays `deferred: milestone-regression`.
- **Live verification** (orchestrator/user, from the main checkout):
  `KUBECONFIG=$HOME/.kube/config kubectl get pods -n monitoring` (Loki + Alloy
  Running and Ready), `kubectl get netpol -n monitoring` (the pair above), and a
  Grafana Loki query against the `indramesh-loki` datasource.
