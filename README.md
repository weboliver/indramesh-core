<!-- generated-by: gsd-doc-writer -->
# IndraMesh Core

IndraMesh Core is a self-hosted platform for defining, managing, and distributing hierarchical company data through a REST API and a React administration interface. Developer and AI tooling — the GraphQL studio, log exploration, AI schema generation, and AI translation — ships as plugins beside the lean core.

Project homepage: **[indramesh.com](https://indramesh.com)**.

## What This Is

IndraMesh Core models structured data such as CMDB-style asset inventories, master data, and related entities. Its hierarchy spans Source, Schema, ObjectType, Object, attributes, values, references, statuses, import/export records, tasks, targets, and service accounts, and it exposes a read-only internal GraphQL API plus a separately authenticated public REST and GraphQL API for integrations.

IndraMesh Core is deliberately lean: it owns the data model, the APIs, RBAC, search, tasks and schedules, and the logging pipeline. Everything that only views, generates, or connects data lives in a plugin — including Jira Assets connectivity. Plugins integrate through a connector architecture; Jira is one connector, not the platform's identity.

## Lean Core and Plugins

**Core keeps:**

- The 17-level hierarchical data model (Source → Schema → ObjectType → Object → ObjectValue → ...) with its CRUD surface
- The REST API under `/api/v1`, the GraphQL API under `/api/v1/graphql`, and saved queries
- RBAC, AQL/OpenSearch search, tasks and schedules, the per-task log, and the logging pipeline
- The provider-neutral LLM client (`LLM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODE`) exposed to plugins through the SDK

**Plugins add:**

- The GraphQL studio editor (GraphiQL + Monaco), server-log exploration, AI schema generation, and AI translation suggestions — the four first-party plugins below
- External-system connectivity, starting with the Jira Assets connector

The full component and data-flow detail lives in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).

## Logging

Each application process writes categorized, correlated lines to stdout (`LOG_FORMAT=json` by default, switchable to `logfmt` or `console`; categories `app`, `access`, `security`, `task`, `connector.*`, `plugin.*`, `db`; `request_id`/`task_id` correlation). In the bundled stack Grafana Alloy collects the container logs through a read-only docker-socket-proxy and writes them to the bundled Loki (14-day retention) — or to an operator's own log target via `ALLOY_SINK=otlp`/`otlphttp` (Graylog, Elasticsearch/OpenSearch, Splunk, Syslog and other OTLP intakes), with Grafana available under the `observability` profile. Independently of the pipeline, Core persists a per-task log for every task run and shows it in Task detail (`TASK_LOG_MAX_LINES`, `TASK_LOG_RETENTION_DAYS`). See [docs/LOGGING-INFRASTRUCTURE.md](docs/LOGGING-INFRASTRUCTURE.md) for the operator model and every `ALLOY_*`/`OTLP_*`/`LOKI_*` knob.

## First-Party Plugins

Every first-party plugin is `active_by_default = true` once installed into `plugins/` (the bundled `example` plugin is opt-in):

| Plugin | Purpose |
|---|---|
| [graphql-studio](https://github.com/weboliver/indramesh-plugin-graphql-studio) | GraphiQL/Monaco editor for the Core GraphQL API (editor page and Tools nav item); saved queries stay Core, and the HTTP GraphQL endpoint is gated on this plugin being active in production. Permission: `graphql-studio:use`. |
| [log-explorer](https://github.com/weboliver/indramesh-plugin-log-explorer) | Queries and follows server logs from the bundled Loki (or an external sink); replaces the former Core System Log viewer. Permission: `system-logs:read`. |
| [ai-schema-wizard](https://github.com/weboliver/indramesh-plugin-ai-schema-wizard) | Generates source-free schema drafts from a free-text description in the Core schema wizard through the Core LLM facade. Permission: `ai-schema-wizard:generate`. |
| [ai-translation](https://github.com/weboliver/indramesh-plugin-ai-translation) | AI translation suggestions for schema data and UI strings, plus the two hidden bulk task types. Permissions: `ai-translation:data`, `ai-translation:ui`. |
| [jira](https://github.com/weboliver/indramesh-plugin-jira) | Jira Assets connector: `jira.sync-pull`, `jira.sync-push`, `jira.sync-export`, plugin-owned link and sync-state tables, and its own admin UI. |
| [example](plugins/example/README.md) | Opt-in (`active_by_default = false`) demonstration of the plugin extension points — registration-only, no heavy editor. |

The first-party plugins are published from their own repositories. Each plugin's own README states
the Core release line it requires and the one command that installs it on either stack:

- [indramesh-plugin-graphql-studio](https://github.com/weboliver/indramesh-plugin-graphql-studio)
- [indramesh-plugin-log-explorer](https://github.com/weboliver/indramesh-plugin-log-explorer)
- [indramesh-plugin-ai-schema-wizard](https://github.com/weboliver/indramesh-plugin-ai-schema-wizard)
- [indramesh-plugin-ai-translation](https://github.com/weboliver/indramesh-plugin-ai-translation)
- [indramesh-plugin-jira](https://github.com/weboliver/indramesh-plugin-jira)

## Installing and Updating Plugins

Plugins are installed into an **already-running** stack. One command does it, on either stack, and the
source may be a repository URL or a local directory (a URL source also accepts `--ref <tag-or-branch>`):

```bash
# Compose stack — from the Core checkout root
./install_plugin_docker.sh https://github.com/weboliver/indramesh-plugin-ai-translation

# Local single-node k3s — from the Core checkout root, with KUBECONFIG set
./install_plugin_k8s.sh https://github.com/weboliver/indramesh-plugin-ai-translation
```

The installer copies the plugin in, runs the plugin's migrations when its manifest declares
`[database]`, and restarts `backend` and `worker` — plugin discovery runs exactly once at process
startup, so that restart is what registers the plugin. Verify it under **Plugins** in the admin UI, or
`GET /api/v1/plugins/<name>` (`state: active`).

**Updating is a deliberate replace, never an overwrite.** Both installers refuse a destination that
already exists and neither offers a force flag, so an update removes the installed plugin
*deliberately* first and then installs again:

```bash
# Compose stack — in the checkout
rm -rf plugins/ai-translation
./install_plugin_docker.sh https://github.com/weboliver/indramesh-plugin-ai-translation
```

On k3s the plugin directory lives on the `plugins-storage` claim, which the application pods mount
read-only on purpose — use the repository's writable helper pod for the removal, then install again:

```bash
K="sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml kubectl"   # or export KUBECONFIG
IMG="$($K get deploy indramesh-backend -n indramesh -o jsonpath='{.spec.template.spec.containers[0].image}')"
sed "s|__IMAGE__|$IMG|" scripts/k8s/plugin-copy-pod.yaml | $K apply -f -
$K wait --for=condition=Ready pod/indramesh-plugin-copy -n indramesh --timeout=60s
$K exec -n indramesh indramesh-plugin-copy -- rm -rf /opt/indramesh/plugins/ai-translation
$K delete pod indramesh-plugin-copy -n indramesh --wait=false
./install_plugin_k8s.sh https://github.com/weboliver/indramesh-plugin-ai-translation
```

**Raise the plugin's `version` when you publish an update.** The plugin's frontend bundle is fetched
with the manifest version as its cache key, so an update that keeps the same version can keep serving
the previous bundle in an open browser. A version bump makes the update visible on the next page load
and verifiable through `GET /api/v1/plugins/<name>`.

**Two k3s prerequisites** apply to plugins that need more than the plugin itself (both are described
under *Kubernetes Installation* above): a plugin that owns a database needs the plugin DB role
(`PLUGIN_DB_USER`/`PLUGIN_DB_PASSWORD` in `.env`; the installer carries the role name into the chart),
and a plugin whose generation calls an external AI provider needs that provider to be reachable
from the cluster — a hosted provider on port 443 is by default, a private or non-443 endpoint needs
an explicit egress entry.

## Stack

- **Backend:** FastAPI, async SQLAlchemy, PostgreSQL, Valkey, and OpenSearch
- **Worker:** ARQ jobs and APScheduler schedules
- **Frontend:** React and Vite
- **Infrastructure:** Docker Compose with Nginx as the reverse proxy, plus the log stack (Loki, Alloy, docker-socket-proxy)
- **Testing:** pytest for the backend and Playwright for end-to-end tests

## Prerequisites

- **Git**, **Docker Engine** with the **Compose v2 plugin** (Compose `2.20` or newer — the stack relies on `depends_on.required`), and **`openssl`**.
- About **4 GB of memory** available to the Docker daemon; OpenSearch alone reserves up to a 2 GB heap. Nothing else is needed on the host: no Python and no Node installation is required, because the backend image and the frontend bundle are built inside Docker.

**No prebuilt images are published for 1.0.0.** The backend image and the frontend bundle are built locally from the repository on the first run, so the first start compiles rather than pulls and therefore takes noticeably longer than a pull would.

### WSL2 hosts

On Windows, install **Docker Engine inside the WSL2 distribution** rather than Docker Desktop, and enable **systemd** in the distribution's configuration (`/etc/wsl.conf`) so the Docker service can start. Keep the clone **inside the Linux filesystem** (for example `~/indramesh-core`), not on a Windows drive under `/mnt/c`, and give the distribution enough memory through the Windows **WSL configuration** (`.wslconfig`) — the daemon's memory budget is the distribution's, not the Windows host's total RAM. The kernel's `vm.max_map_count` must meet OpenSearch's floor; the installer checks the value the **containers** see and prints the fix command (native and WSL2 forms) instead of applying it, because that setting needs privileges.

## Installation

Clone the public repository and change into it:

```bash
git clone https://github.com/weboliver/indramesh-core.git
cd indramesh-core
```

Then run the installer:

```bash
./install_indramesh_docker.sh
```

That one command is the whole quickstart. It creates `.env` from `.env.example`, generates every secret (database, Valkey, OpenSearch, plugin role, JWT signing key, Fernet key and the initial admin password), builds and starts the stack, waits until the backend is healthy **and** nginx answers — so there is no silent outage while the first migrations run — and finally prints the login URL and the admin password. Since the images are built locally from the repository, the first run is the slow one.

## Quick Start

1. Run `./install_indramesh_docker.sh` as above and wait for `IndraMesh is running.`
2. Open **`https://localhost:8443`**. The bundled certificate is self-signed, so accept the browser warning. The ports `8180`/`8443` are deliberate: they keep this stack from colliding with a Kubernetes setup on `80`/`443`.
3. Log in as `admin` with the password the installer printed.

The default Compose file brings up the **production-shaped** stack: the frontend image is built with `ENVIRONMENT=production`, nginx serves the compiled SPA from it, and the SPA document carries the strict Content-Security-Policy — the import-map hash instead of an inline/eval allowance, and no WebSocket source in `connect-src`.

For development, layer the mirrored overlay. It restores the Vite dev server with HMR and the development CSP, and adds the source bind mounts that give the backend its hot reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d
```

`ADMIN_PASSWORD` is used **only** when the admin user is first created. Changing it in `.env` afterwards has no effect — the password is changed in the UI.

A cold quickstart on a clean host, from `git clone` through the first successful login, is measured as:

**2 min 21 s** — measured on 2026-10-24: WSL2 Ubuntu 24.04.5 LTS on an Intel Core i7-9750H at 2.60 GHz with 12 logical cores and 19 GiB RAM, Docker Engine 29.7.2, Docker Compose v2. The time runs from `./install_indramesh_docker.sh` to a reachable login page with the Core stack only; installing the five first-party plugins afterwards adds about **1 min 45 s**, because each install restarts the backend and the worker. The running stack settles at **≈ 2.3 GiB**, of which OpenSearch alone is ≈ 1.6 GiB (`docker stats`, 2026-10-24) — the memory above that leaves room for the build.

The installer prints its own per-step timings, so anyone re-measuring can see where the time goes.

### Troubleshooting

- **`Pool overlaps with other one on this address space`** — the Compose subnet clashes with an existing network. `./install_indramesh_docker.sh` picks a free range by itself while `COMPOSE_SUBNET` and `NGINX_TRUSTED_IP` are at their defaults; with a value you set yourself, set `COMPOSE_SUBNET` to a free range and `NGINX_TRUSTED_IP` to an address inside it, then re-run the installer.
- **Trusted-proxy / `NGINX_TRUSTED_IP` mismatch** — `NGINX_TRUSTED_IP` must stay equal to the value the backend reads; it is the same value nginx carries as its static address, so the two move together. Change both only through that one variable.

## Kubernetes Installation

The same repository also carries a Kubernetes quickstart: a self-contained, single-node **k3s**
cluster on the machine that runs it, with the application images built locally and imported into
that cluster. It supports native Ubuntu or Debian (`amd64` or `arm64`) and Ubuntu under WSL2.

**Prerequisites.** Git, `openssl`, and a running **Docker Engine** with the **Compose v2 plugin**.
Docker is required because the application images are built locally from the repository and
imported into the cluster — no registry is involved and no prebuilt image is pulled. Give the
machine at least 6 GiB of memory (8 GiB is comfortable); the installer refuses a smaller budget
for the stateful services.
Host-specific preparation (the WSL2 `systemd` prerequisite, the Docker Desktop mount hazard, the
memory floor and the Windows-side access) is described in
[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#kubernetes-host-prerequisites).

Clone the public repository and change into it:

```bash
git clone https://github.com/weboliver/indramesh-core.git
cd indramesh-core
```

Then run the one command:

```bash
./install_indramesh_k8s.sh
```

That is the whole quickstart. The installer creates `.env` from `.env.example` when none exists and
generates every secret in it (database, plugin role, Valkey, OpenSearch, JWT signing key, Fernet
key and the initial admin password), installs or reuses the cluster, provisions TLS through
cert-manager, builds and imports the application images, reconciles the Helm release, imports the
retained certificate authority into this host's trust store, and closes by printing the login URL
and — only on the run that generated it — the admin password.

Open **`https://localhost`** and log in as `admin` with the password the installer printed. On
WSL2 the Windows browser reaches the same URL through WSL's localhost forwarding; nothing on the
Windows side needs administrator rights. Traefik publishes the app on port 80 and port 443. The
Compose stack above uses `8180`/`8443`, so both stacks can run on the same machine side by side.
The bundled certificate comes from the installer's own certificate authority, which it imports
into this host's trust store; elsewhere, accept the browser warning or import that authority. The
certificate carries `localhost`, `127.0.0.1` and, on a native Linux host, the host's own IPv4
address: other machines on the network reach the stack at `https://<host-ip>`. The closing banner
prints that address, and `INDRAMESH_HOST_IP` overrides or (set empty) disables it.

`ADMIN_PASSWORD` is used **only** when the admin user is first created. Changing it in `.env`
afterwards has no effect — the password is changed in the UI.

**Not installed by default.** The Kubernetes quickstart is lean: it installs the application, the
stateful services, cert-manager and TLS. Three components are opt-in, each through a switch in
`.env` that must be set to exactly the string `true`:

| Switch | Default | Turns on |
|---|---|---|
| `MONITORING_ENABLED` | `false` | `kube-prometheus-stack` (Prometheus and Grafana) in the `monitoring` namespace |
| `HEADLAMP_ENABLED` | `false` | [Headlamp](https://headlamp.dev/), the browser-based cluster UI, under `/headlamp/` |
| `LOGINFRA_ENABLED` | `false` | The log stack (Loki and the Alloy collector) beside the monitoring stack |

Switching a component off only stops managing it: nothing is removed, no release is deleted and no
data is touched. A switched-off run is a lean run, not a cleanup.

A cold Kubernetes quickstart on a clean host, from `git clone` through the first successful login,
is measured as:

**4 min 20 s** — measured cold on 2026-10-24: WSL2 Ubuntu 24.04.5 LTS on an Intel Core i7-9750H at 2.60 GHz with 12 logical cores and 19 GiB RAM, Docker Engine 29.7.2, Docker Compose v2, k3s v1.36.4+k3s1. The previous cluster, its volumes and the image pool were removed first, so the cluster install, the image builds and the image imports all happened inside the measured interval; the three opt-in components (`MONITORING_ENABLED`, `HEADLAMP_ENABLED`, `LOGINFRA_ENABLED`) stayed off, so this is the lean quickstart. The application pods settle at **≈ 1.9 GiB** (`kubectl top pods -n indramesh`, 2026-10-24) — leave the room above that free for the build.

Those are the measured values, not targets: they come from the cold quickstart of 2026-10-24. The installer prints its own per-step timings, so anyone re-measuring can see where the time goes.

**Outbound connections from the cluster.** The chart denies all traffic by default and then admits
what each workload needs. The backend and worker may reach **public addresses on port 443**
(`networkPolicies.publicEgress`, on by default), so Jira Cloud and the hosted LLM providers work
without further steps; private, loopback, link-local and CGNAT ranges stay closed. A target on a
private address or on another port — an on-prem Jira Data Center, a local Ollama or vLLM server —
needs an explicit entry: list it in `K8S_EGRESS_ALLOW` in the `.env` (`IP[/len]:port`,
comma-separated), and the installer also adds a private `LLM_BASE_URL` host by itself. Rerun
`./install_indramesh_k8s.sh` after changing these values. A missing rule shows up as "Jira could not be
reached." or as an unreachable LLM provider, and on Kubernetes both messages point at this
configuration. The details are in [docs/CONFIGURATION.md](docs/CONFIGURATION.md), section *Outbound
connections on Kubernetes*. On the Compose stack no such rule exists, because that stack has ordinary
outbound network access.

## What the Installer Changes on Your Host

The installer mutates the host it runs on. It never removes anything itself, and a rerun is
idempotent: an existing cluster, existing data and an existing `.env` are reused, never reset.
Every change, with the way back:

| Change | Removal path |
|---|---|
| The **k3s** cluster itself — the server binary, its systemd service and its embedded containerd. | `/usr/local/bin/k3s-uninstall.sh`, the uninstaller k3s installs alongside the server. |
| The **bootstrap packages**, apt-installed when missing: `curl`, `ca-certificates`, `tar`, `coreutils`, `gnupg` — plus `socat`, installed for the local-forward bridge below. | `sudo apt-get remove curl ca-certificates tar coreutils gnupg socat` (or only the ones you do not want to keep). |
| The **kernel setting** OpenSearch needs: `vm.max_map_count`, persisted as `/etc/sysctl.d/90-indramesh-max-map-count.conf`. | Delete that file and run `sudo sysctl --system`. |
| The **WSL configuration**: `[boot]` `systemd=true` in `/etc/wsl.conf`. A change here stops the run with exit code 3, because it needs a WSL restart (`wsl --shutdown`) followed by a rerun. | Remove the entry from `/etc/wsl.conf` and restart WSL. |
| The **WSL memory floor**: `[wsl2]` `memory=` in the Windows-side `%USERPROFILE%\.wslconfig` (an already larger value is left alone). | Remove that line and restart WSL. |
| The **local-forward service**: the `indramesh-local-forward@.service` systemd template unit at `/etc/systemd/system/indramesh-local-forward@.service`, with its environment file `/etc/indramesh/local-forward.env`; it relays `127.0.0.1:80` and `127.0.0.1:443` to the cluster's Traefik load balancer, which is what makes `https://localhost` work. | `sudo systemctl disable --now indramesh-local-forward@80.service indramesh-local-forward@443.service`, then delete both files and run `sudo systemctl daemon-reload`. |
| The **trust-store import**: the retained CA root is copied to `/usr/local/share/ca-certificates/indramesh-local-ca.crt` and registered with `update-ca-certificates`; on a WSL2 host the same certificate also goes into the Windows user's `CurrentUser\Root` store (or `LocalMachine\Root` when the terminal happens to run elevated). | Delete `indramesh-local-ca.crt` and run `sudo update-ca-certificates --fresh`; remove the certificate in the Windows certificate manager. |

The cluster-side resources (the namespace, the Secrets, the Helm release and its retained PVCs)
live inside that cluster — remove them through the cluster (`helm uninstall indramesh -n indramesh`)
before running the uninstaller above.

## Usage Examples

Internal resource endpoints require a bearer token and enforce permissions. Login at `/api/v1/auth/login` does not require an existing token; `/health` and `/api/v1/` are also unauthenticated.

**Authenticate and list sources:**

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -H "Content-Type: application/x-www-form-urlencoded" \
  -d "username=admin" \
  -d "password=<your-admin-password>"

curl http://localhost:8000/api/v1/sources/ \
  -H "Authorization: Bearer <access_token>"
```

The login response contains the token to use in the second request:

```json
{"access_token":"<access_token>","token_type":"bearer"}
```

Listing sources returns a JSON array of sources visible to the caller (`[]` when none are visible).

**Trigger a Jira Assets pull:**

First enable the Jira plugin, configure a Jira connection for the Source under **Jira connections**, and select an active service account bound to that Source with a Jira bearer token. The backend and worker must have a compatible active Jira plugin; the caller needs `tasks:create` and `sync:trigger` permissions. Replace the UUID placeholders with your configured Source and service account IDs.

```bash
curl -X POST http://localhost:8000/api/v1/tasks/ \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"tasktype":"jira.sync-pull","source_id":"<source-uuid>","service_account_id":"<service-account-uuid>","current_step":null,"plugin_parameters":{}}'
```

Expected result: HTTP `201` with a Task object, including its `id` and `tasktype: "jira.sync-pull"`; execution runs asynchronously. Read its status at `GET /api/v1/tasks/<task-id>`. To select a single Jira schema, set `plugin_parameters.jira_schema_id` to its Jira schema ID.

Jira synchronization now uses plugin task types `jira.sync-pull`, `jira.sync-push`, and `jira.sync-export` through the Tasks API. The former core `/api/v1/sync/*` endpoints are removed; `JIRA_URL` is not the plugin's connection configuration.

**Query the internal GraphQL API:**

```bash
curl -X POST http://localhost:8000/api/v1/graphql \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ health { status version } }"}'
```

Expected result: a GraphQL `data.health` object with `status: "ok"` and the runtime package version (or `"unknown"` if package metadata is unavailable).

See [docs/API.md](docs/API.md) for the REST, GraphQL, and public API references.

## Development

```bash
# Development stack: base file + mirrored overlay (Vite dev server with HMR,
# development CSP, source bind mounts for backend hot reload)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build

# The default stack (base file only) serves the built SPA — production shape:
docker compose up -d --build

# Follow service logs
docker compose logs -f backend

# Run the frontend dev server outside Compose
cd frontend
npm install
npm run dev
```

The base `docker-compose.yml` is production-shaped: the frontend image is built with `ENVIRONMENT=production` and nginx serves the compiled SPA under the strict Content-Security-Policy. HMR exists only with `docker-compose.dev.yml`, which overrides the build and the container command back to the Vite dev server and mounts `nginx/nginx.dev.conf` for the development CSP. The same overlay supplies the `./backend:/app` mount, so backend source files hot reload in the development shape only. Stop the Compose frontend before running another dev server on the same host port. Run Compose commands only from the main checkout, not from a Git worktree. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the complete local workflow.

## Testing

Every change is expected to come with evidence that it works: the commands you ran and what you
observed. State that in your pull request, together with what changed and why — and if the change
touches a documented API surface, say so explicitly, because that surface is versioned.

The automated suites that check this project mechanically are maintained in the full development
checkout and are not part of this published tree, which carries the runtime product and the
documents for running it. What that means for a contribution — the scope a change is expected to
keep, and how it is described — is stated in [CONTRIBUTING.md](CONTRIBUTING.md).

## Documentation

- [Getting started](docs/GETTING-STARTED.md)
- [Architecture](docs/ARCHITECTURE.md)
- [Log infrastructure](docs/LOGGING-INFRASTRUCTURE.md)
- [API reference](docs/API.md)
- [Configuration](docs/CONFIGURATION.md)
- [Development](docs/DEVELOPMENT.md)
- [Deployment](docs/DEPLOYMENT.md)
- [Raspberry Pi](docs/RASPBERRY-PI.md)
- [Operations](docs/OPERATIONS.md)
- [Stability and versioning](docs/STABILITY.md)

## Stability & Versioning

IndraMesh Core follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The promise
covers the **documented API** — the `/api/v1` REST endpoints described in the API reference and in
the public API contract, the public GraphQL endpoint, and saved-query execution — together with the
documented environment-variable names, the machine-readable error codes of that API and the
invocation of the public scripts. Everything else is explicitly outside it, including the plugin
SDK, the database schema and the core tables, the granularity and payload of internal hook events,
the numeric limit values, and the Helm chart's values and templates.

Only the latest 1.x release line is supported, and deprecations follow a fixed rule: anything
deprecated in a 1.x release keeps working until the next major release, announced in the changelog
and in the API documentation. IndraMesh Core 1.0 supports two installation models — **Docker
Compose** and a **local single-node k3s** install created by the bundled chart and the Kubernetes
installer. The chart is built OpenShift-*compatible* but has **never been verified** on OpenShift
or OKD, so 1.0 does not cover those targets: treat such a deployment as unverified work.

The full declaration, with every exclusion named individually and the version boundary stated, is
[docs/STABILITY.md](docs/STABILITY.md).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE) with the [IndraMesh Core Plugin Exception](LICENSE-PLUGIN-EXCEPTION): plugins that use Core only through its plugin interface may carry any license. Commercial licenses available for organizations that cannot use AGPL — contact me. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for dependency notices. “Jira” and “Jira Assets” are trademarks of Atlassian Pty Ltd; IndraMesh Core is independent and not affiliated with, endorsed by, or sponsored by Atlassian.
