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

Every application process writes categorized, correlated lines to stdout, and Core keeps a per-task log for every task run. In the bundled stack Grafana Alloy collects the container logs into Loki or into your own log target. See [docs/LOGGING-INFRASTRUCTURE.md](docs/LOGGING-INFRASTRUCTURE.md).

## First-Party Plugins

The first-party plugins are `active_by_default = true` once installed into `plugins/` — except the
two opt-in ones: the bundled `example` (registration-only demo) and `graphql-studio`, whose
activation opens the internal GraphQL endpoint on a production stack and is therefore an
administrator's deliberate step:

| Plugin | Purpose |
|---|---|
| [graphql-studio](https://github.com/weboliver/indramesh-plugin-graphql-studio) | GraphiQL/Monaco editor for the Core GraphQL API (editor page and Tools nav item); saved queries stay Core. Opt-in (`active_by_default = false`): a fresh installation stays inactive until an administrator activates it, and only that activation opens the internal HTTP GraphQL endpoint on production; `/public/api/v1/graphql` is the plugin-independent integration surface. Permission: `graphql-studio:use`. |
| [log-explorer](https://github.com/weboliver/indramesh-plugin-log-explorer) | Queries and follows server logs from the bundled Loki (or an external sink); replaces the former Core System Log viewer. Permission: `system-logs:read`. |
| [ai-schema-wizard](https://github.com/weboliver/indramesh-plugin-ai-schema-wizard) | Generates source-free schema drafts from a free-text description in the Core schema wizard through the Core LLM facade. Permission: `ai-schema-wizard:generate`. |
| [ai-translation](https://github.com/weboliver/indramesh-plugin-ai-translation) | AI translation suggestions for schema data and UI strings, plus the two hidden bulk task types. Permissions: `ai-translation:data`, `ai-translation:ui`. |
| [jira](https://github.com/weboliver/indramesh-plugin-jira) | Jira Assets connector: `jira.sync-pull`, `jira.sync-push`, `jira.sync-export`, plugin-owned link and sync-state tables, and its own admin UI. |
| [example](plugins/example/README.md) | Opt-in (`active_by_default = false`) demonstration of the plugin extension points — registration-only, no heavy editor. |

The first-party plugins are published from their own repositories. Each plugin's own README states
the Core release line it requires and the one command that installs it on either stack.

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

Updating is a deliberate replace, never an overwrite: remove the installed plugin first, then install it again. The removal commands for both stacks are in [Updating a Plugin](docs/DEPLOYMENT.md#updating-a-plugin).

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

`ADMIN_PASSWORD` is used **only** when the admin user is first created. Changing it in `.env` afterwards has no effect — the password is changed in the UI.

A cold quickstart on a clean host was measured at **2 min 21 s** ([details](docs/DEPLOYMENT.md#measured-quickstart-times)). If something goes wrong, see [Common Setup Issues](docs/GETTING-STARTED.md#common-setup-issues).

## Kubernetes Installation

The same repository also carries a Kubernetes quickstart: a self-contained, single-node **k3s** cluster on the machine that runs it, with the application images built locally and imported into that cluster. It supports native Ubuntu or Debian (`amd64` or `arm64`) and Ubuntu under WSL2. It needs Git, `openssl`, a running Docker Engine with the Compose v2 plugin, and at least 6 GiB of memory (8 GiB is comfortable).

From the cloned repository:

```bash
./install_indramesh_k8s.sh
```

Open **`https://localhost`** and log in as `admin` with the password the installer printed. Monitoring, Headlamp and the log stack are opt-in. A cold run was measured at **4 min 20 s** ([details](docs/DEPLOYMENT.md#measured-quickstart-times)).

**This installer changes the host it runs on**, for example with the k3s cluster, bootstrap packages, a kernel setting and a local port forward. [What the Installer Changes on Your Host](docs/DEPLOYMENT.md#what-the-installer-changes-on-your-host) lists every change with the way back.

Host preparation, opt-in components and TLS are described in [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md#k3s-and-helm), and targets on private addresses in [Outbound connections on Kubernetes](docs/CONFIGURATION.md#outbound-connections-on-kubernetes).

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

See [docs/API.md](docs/API.md) for the REST, GraphQL, and public API references, including how to [start a Jira pull](docs/API.md#jira-bridging-jira-plugin) and how to [query GraphQL](docs/API.md#graphql-api-apiv1graphql).

## Development

The default stack is production-shaped. For development, layer the mirrored overlay, which adds the Vite dev server with HMR, the development CSP and backend hot reload:

```bash
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d --build
```

Run Compose commands only from the main checkout, not from a Git worktree. See [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md) for the complete local workflow.

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

IndraMesh Core follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The promise covers the documented API; only the latest 1.x release line is supported. IndraMesh Core 1.0 supports **Docker Compose** and a **local single-node k3s** install. OpenShift and OKD have **never been verified**. The full declaration is [docs/STABILITY.md](docs/STABILITY.md).

## How This Was Built

IndraMesh Core was created through vibe programming, primarily with Claude Opus 5.5 and DeepSeek 4.1 Flash.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for contribution guidelines. The automated test suites are maintained in the full development checkout and are not part of this published tree.

## License

Licensed under the [GNU Affero General Public License v3.0](LICENSE) with the [IndraMesh Core Plugin Exception](LICENSE-PLUGIN-EXCEPTION): plugins that use Core only through its plugin interface may carry any license. Commercial licenses available for organizations that cannot use AGPL — contact me. See [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) for dependency notices. “Jira” and “Jira Assets” are trademarks of Atlassian Pty Ltd; IndraMesh Core is independent and not affiliated with, endorsed by, or sponsored by Atlassian.
