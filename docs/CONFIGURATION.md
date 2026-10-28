<!-- generated-by: gsd-doc-writer -->
# Configuration

IndraMesh Core uses environment variables for application and container settings. The root
`.env.example` is the only environment template; `docker-compose.yml` loads `.env` and supplies
container-network overrides.

```bash
cp .env.example .env
```

Mandatory values (`JWT_SECRET_KEY`, `FERNET_KEY`, `CORS_ALLOWED_ORIGINS`, plus every database,
Valkey and OpenSearch password) ship as `CHANGE_ME` placeholders and are guarded by Compose
(`${VAR:?…}`). The quickstart installer `./install_indramesh_docker.sh` generates a real value for
each before the stack starts. Those guards reject a *missing or blank* value only — they do not
recognise the placeholder: a hand-copied `.env` left with `CHANGE_ME` renders and starts the whole
project, with publicly known credentials, so replace every placeholder (run the installer, or fill
them by hand). CORS and proxy settings are checked at import time; JWT and Fernet settings are
checked in the application lifespan. Never source `.env` as a shell script, and never commit it.

## Environment Variables

“Required” means required by the application or by the selected deployment path. Variables marked
optional have a code or Compose default unless stated otherwise.

The `DB_*` block (`DB_HOST`, `DB_PORT`, `DB_USER`, `DB_PASSWORD`, `DB_NAME`) is the single authored
source of database credentials: values are set once there and every consumer derives from it.
`DB_PORT` is the **host** port Compose publishes (`"${DB_PORT:-5434}:5432"`), and Compose
overwrites `DB_HOST`/`DB_PORT` to `postgres`/`5432` inside backend, worker, and cutover. The
`postgres` service's own `POSTGRES_*` variables and the backend/worker/cutover `DB_*` values are
derived from the same keys, never authored separately. In Kubernetes only `DB_PASSWORD` reaches
the cluster: the installer writes it into the `indramesh-database` Secret under both
`POSTGRES_PASSWORD` and `DB_PASSWORD`, while `DB_USER`/`DB_NAME` stay Helm chart values
(`postgres.user`/`postgres.database`, defaults `postgres`/`indramesh`) — the `.env` `DB_NAME` value
does not switch an existing namespace's database. An existing `.env` migrates by renaming its old
`POSTGRES_*` credential keys onto this block, keeping the same values. An installation created
before the current durable-state layout must be rebuilt rather than upgraded in place; the procedure
for upgrading a current installation is in [OPERATIONS.md § Upgrade](./OPERATIONS.md#upgrade).

| Variable | Required | Default | Description |
|---|---|---|---|
| `DATABASE_URL` | No | constructed | Explicit async SQLAlchemy URL. It overrides all `DB_*` components. |
| `DB_HOST` | No | `postgres` | Database host used when constructing `DATABASE_URL`. Compose overrides it with `postgres` for backend and worker. |
| `DB_PORT` | No | `5432` | Database port used when constructing `DATABASE_URL`. Compose overrides it with `5432` for backend and worker. |
| `DB_USER` | No | `postgres` | Database user used when constructing `DATABASE_URL`. |
| `DB_PASSWORD` | No | `postgres` | Database password used when constructing `DATABASE_URL`. |
| `DB_NAME` | No | `app` | Database name used when constructing `DATABASE_URL`. |
| `DB_POOL_SIZE` | No | `10` | SQLAlchemy async-engine pool size. Compose and Helm pin the backend value to `10`. |
| `DB_MAX_OVERFLOW` | No | `20` | Connections allowed above `DB_POOL_SIZE`. |
| `DB_POOL_RECYCLE` | No | `1800` | Seconds before a pooled connection is recycled. |
| `DB_POOL_PRE_PING` | No | `true` | Enables connection validation before pool checkout. |
| `PLUGIN_DB_USER` | Conditional | `indramesh_plugin` when a password is configured | Dedicated plugin migration login role, not the runtime `DB_USER`. Must match `^[A-Za-z_][A-Za-z0-9_]{0,62}$`. Setting a user without a password aborts deployment. |
| `PLUGIN_DB_PASSWORD` | **For restricted plugin migrations** | unset | Password for the dedicated migration role. The shipped Jira plugin requires this boundary; both credentials absent allow only plugins that do not require a restricted role to use the privileged fallback. |
| `INDRAMESH_PLUGINS_DIR` | No | expanded `~/.indramesh/plugins/` | Directory-scan discovery root. Compose mounts `./plugins` read-only at `/opt/indramesh/plugins`; Helm uses `plugins.mountPath`, with the same path by default. |
| `VALKEY_PASSWORD` | Compose | — | Password passed to Valkey’s `--requirepass` and embedded in the Compose `VALKEY_URL`. |
| `VALKEY_URL` | No | `redis://valkey:6379/0` | Cache/queue connection URL used by the cache and the ARQ worker. The `redis://` scheme is the wire-protocol name and stays; the key, the service and the volume are the `valkey` names. Compose supplies an authenticated container-network URL. |
| `OPENSEARCH_URL` | No | `http://opensearch:9200` | OpenSearch endpoint used by the backend and worker. |
| `OPENSEARCH_USERNAME` | No | `admin` | OpenSearch basic-auth username — the security plugin's admin user. |
| `OPENSEARCH_PASSWORD` | Compose | `""` in application code | OpenSearch password. Compose forwards it to the search service as `OPENSEARCH_INITIAL_ADMIN_PASSWORD`, which that service reads **only on the first start of an empty `opensearch_data` volume**; changing it later does not rotate the admin password (see [The search admin password](#the-search-admin-password)). |
| `JIRA_URL` | No (legacy) | `http://jira:8080` in root template | No longer read by Core; the Jira plugin uses its per-connection URL instead. Retained alongside the dev Jira container and still passed through by Compose. |
| `BACKEND_PORT` | Compose | — | Host port mapped to backend port `8000`. |
| `FRONTEND_PORT` | Compose | — | Host port mapped to the Compose frontend port `5173`. |
| `HTTP_PORT` | No | `8180` | Host port mapped to Nginx’s HTTP listener. |
| `HTTPS_PORT` | No | `8443` | Host port mapped to Nginx’s HTTPS listener. |
| `COMPOSE_SUBNET` | No | `172.28.0.0/16` | Compose `default`-network subnet. The Docker installer checks it against existing Docker networks and host routes and, while both it and `NGINX_TRUSTED_IP` are at their defaults, writes a free range and a matching nginx address into `.env`; an operator-set value is only checked. Set by hand, keep `NGINX_TRUSTED_IP` inside the chosen range. |
| `VALKEY_PORT` | No | `6379` | Loopback host port mapped to Valkey. |
| `OPENSEARCH_PORT` | No | `9200` | Loopback host port mapped to OpenSearch. |
| `CERT_UID` / `CERT_GID` | No | `1000` | Compose Nginx development-certificate ownership settings. |
| `COMPOSE_PROJECT_NAME` | No | top-level Compose name `indramesh` | Optional Compose project-name override. Prefer leaving it unset so container naming remains consistent with repository scripts. |
| `LOCAL_UID` / `LOCAL_GID` | No | `1001` | Build arguments for the Compose backend/worker image user. |
| `ENVIRONMENT` | No | `production` in backend settings | `development` enables report-only development CSP and request timing. Only exact `production` starts Compose backend without `--reload` and builds/previews the frontend static bundle; all other or missing values use the Compose development commands. |
| `VITE_ENVIRONMENT` | No | derived from `ENVIRONMENT` | Frontend build-time flag. Compose passes `ENVIRONMENT`; the Kubernetes frontend image receives it as a build argument. It is statically embedded by Vite, not read at runtime. |
| `JWT_SECRET_KEY` | **Yes** | — | HS256 JWT signing secret. The backend raises `RuntimeError` when it is absent. |
| `FERNET_KEY` | **Yes** | — | Fernet key used to encrypt service-account bearer tokens and plugin secret configuration fields. It must be a valid Fernet key; retain it to decrypt existing stored ciphertext. |
| `FERNET_KEY_OLD` | No | unset | The previous `FERNET_KEY`, held only while a key rotation runs. When set, new writes use `FERNET_KEY` while values encrypted under the old key stay readable. It must be a valid Fernet key; an invalid value aborts startup. Remove it once `python -m app.maintenance rotate-fernet-key` exits `0` — see [DEPLOYMENT.md § Fernet Key Rotation](./DEPLOYMENT.md#fernet-key-rotation). |
| `CORS_ALLOWED_ORIGINS` | **Yes** | — | Comma-separated trusted origins. It cannot be empty or include `*`. |
| `NGINX_TRUSTED_IP` | **Yes** | `172.28.0.10` (Compose) | Trusted proxy address for Uvicorn `ProxyHeadersMiddleware`, and the static `ipv4_address` Compose assigns to `nginx`. One variable feeds both, so they cannot drift; Helm defaults to the cluster pod CIDR in `apps.backend.nginxTrustedIp`. |
| `INDRAMESH_RUNTIME_MODE` | Deployment-managed | — | Must be `compose` or `kubernetes`; other values fail closed. Compose injects `compose`; the Helm helper injects `kubernetes`. |
| `ADMIN_PASSWORD` | **Yes** | — | Initial `admin` password, required in every runtime mode (Compose guards it with `${ADMIN_PASSWORD:?…}`). It is used **only** when the admin user is first created on a fresh database; changing it later has no effect. The installer generates and prints it once. |
| `RATE_LIMIT_ENABLED` | No | `true` | Enables the global SlowAPI limiter. The login limiter remains enabled regardless. `.env.example` sets it to `false` for development/testing. |
| `PUBLIC_API_RATE_LIMIT` | No | `200/minute` | Default public API per-token limit. Also the fallback default for `RATE_LIMIT_GLOBAL`. |
| `RATE_LIMIT_GLOBAL` | No | `PUBLIC_API_RATE_LIMIT` | Default global limiter rule. |
| `RATE_LIMIT_LOGIN` | No | `5/minute` | Limit for `POST /api/v1/auth/login`. |
| `RATE_LIMIT_PASSWORD_CHANGE` | No | `5/minute` | Limit for `POST /api/v1/users/me/change-password`. |
| `RATE_LIMIT_FALLBACK` | No | `50/minute` | In-memory fallback limit when the cache storage is unavailable. |
| `RATE_LIMIT_STORAGE_URI` | No | `VALKEY_URL` | SlowAPI counter-storage URL override. |
| `MAX_TOKEN_AGE_SECONDS` | No | disabled | Optional absolute token-age limit in seconds in addition to JWT expiration. |
| `CLOCK_SKEW_TOLERANCE_SECONDS` | No | `10` | JWT `nbf` validation tolerance in seconds. |
| `LLM_API_KEY` | No | unset | Enables the optional LLM client when non-empty. |
| `LLM_PROVIDER` | No | `mistral` | Provider preset that supplies the default base URL and structured mode: `mistral`, `openai`, or `openai_compatible` (which requires an explicit `LLM_BASE_URL`). |
| `LLM_BASE_URL` | No | unset — the provider preset supplies it | Optional endpoint override for the OpenAI-compatible client; it must include the `/v1` suffix. On Kubernetes the installer carries the `.env` value into the chart (see [Outbound connections on Kubernetes](#outbound-connections-on-kubernetes)). |
| `LLM_MODEL` | — | no default — required for a working client | Bare model id (for example `mistral-small-latest`); a legacy `mistral/` provider prefix is rejected loudly, never silently split. |
| `LLM_MODE` | No | provider preset (`mistral` → `json_schema`, `openai` → `tools`) | Structured-output mode override: `tools`, `json_schema`, `json`, or `md_json`. |
| `K8S_EGRESS_ALLOW` | No | unset | Kubernetes only: extra outbound targets for the backend and worker, as comma-separated `IP[/len]:port` entries (for example `10.0.5.20/32:8080` for an on-prem Jira). See [Outbound connections on Kubernetes](#outbound-connections-on-kubernetes). Compose ignores it. |
| `GRAPHQL_MAX_DEPTH` | No | `15` | Maximum GraphQL query nesting depth. Must be at least `1`. |
| `GRAPHQL_MAX_TOKENS` | No | `2000` | Maximum GraphQL document size in tokens; a larger document is rejected before execution. Must be at least `1`. |
| `GRAPHQL_MAX_ALIASES` | No | `20` | Maximum number of aliases in one GraphQL document (alias-amplification guard). Must be at least `1`. |
| `GRAPHQL_MAX_COST` | No | `20000` | Cost budget for one GraphQL operation; nested list fields are weighted. Must be at least `1`. |
| `GRAPHQL_NESTED_LIST_LIMIT` | No | `500` | Per-parent hard cap applied to nested list fields (for example `values`, `referencesFrom`, `children`). Must be at least `1`. |
| `GRAPHQL_STATEMENT_TIMEOUT` | No | `5000` | Postgres `statement_timeout` for GraphQL operations, in **milliseconds**. Must be at least `1`. |
| `GRAPHQL_EXECUTION_TIMEOUT` | No | `10.0` | Wall-clock backstop for one GraphQL operation, in **seconds**. Must be greater than `0` and must exceed `GRAPHQL_STATEMENT_TIMEOUT` — a pairing that does not is rejected at startup instead of starting silently; both bounds stay under nginx's 60 s read timeout. The OpenSearch search read bound is half this value, capped at 5 s. |
| `AQL_REF_CUTOFF` | No | `10000` | Maximum nested reference keys resolved in one AQL filter branch. |
| `AQL_MULTI_TYPE_CUTOFF` | No | `25` | Maximum ObjectTypes an AQL attribute-name condition may match. |
| `OBJECT_TYPE_GRAPH_CUTOFF` | No | `50` | Maximum nodes returned by an ObjectType relationship graph. |
| `OBJECT_GRAPH_CUTOFF` | No | `50` | Maximum nodes returned by an Object relationship graph. |
| `EXPORT_ROOT` | No | `/tmp/exports` | Allow-listed root for CSV export downloads. Compose and Helm mount/set this path. |
| `IMPORT_ROOT` | No | `/tmp/imports` | Allow-listed root for import-definition folders. Compose sets `/tmp/imports`; Helm sets `/data/imports/folders`, inside the `imports-storage` claim both the backend and worker mount at `/data/imports`. |
| `IMPORT_UPLOAD_DIR` | No | OS temporary directory | Directory for uploaded CSV files. It must be shared by backend and worker; Compose and Helm use `/data/imports`. |
| `IMPORT_MAX_FILE_SIZE` | No | `52428800` | Maximum CSV-upload size in bytes (50 MiB), enforced by the upload handlers for both import routes. Nginx allows a bounded `64 MiB` body for those two `POST` paths, so raising this past 64 MiB also means raising `client_max_body_size` in `nginx/nginx.conf` and `deploy/helm/indramesh/nginx.conf`. |
| `UI_TRANSLATION_EXPORT_ROOT` | No | `/workspace/frontend-locales` | Filesystem export root for UI translation dictionaries. The Compose backend sets and mounts it. |
| `ARQ_JOB_TIMEOUT_SECONDS` | No | `1800` | ARQ job timeout in seconds. `0` maps to `86400` seconds. |
| `DOCKER_SOCKET_PATH` | No | `/var/run/docker.sock` | Legacy Compose-mode log-discovery socket. Core no longer reads it (Phase 305): the log stack mounts the read-only `docker-socket-proxy` for Alloy instead, and the Compose `backend` block is deliberately kept free of the raw socket. |
| `KUBERNETES_SERVICE_HOST` / `KUBERNETES_SERVICE_PORT` | Kubernetes-injected | port `443` | In-cluster Kubernetes API connection settings. The log collector (Alloy) uses them to tail pod logs, not the backend or worker. |
| `SLOW_REQUEST_THRESHOLD_MS` | No | `500` | Request-duration threshold for development-mode slow-request logging. |
| `SLOW_QUERY_THRESHOLD_MS` | No | `100` | Query-duration threshold for development-mode DB profiling. |
| `STORAGE_SERVICE_URL` | No | `http://storage:8001` | Storage-service base URL used by the backend and worker. Compose and Helm set it to the internal `storage` service. |
| `STORAGE_BUCKET` | No | `indramesh` | Logical bucket used by backend storage clients and the storage service. |
| `STORAGE_BLOB_ROOT` | No | `/data/storage` | Filesystem root used by the storage service itself. Compose and Helm mount persistent storage there. |
| `TASK_LOG_MAX_LINES` | No | `10000` | Maximum stored lines per task run. The first half and the last half are kept; the discarded middle is marked with a dropped-lines line. Allowed `2`–`10000`. Helm: `apps.taskLog.maxLines`. |
| `TASK_LOG_RETENTION_DAYS` | No | `30` | Retention window in days for stored task-log lines. The worker purges older lines hourly; the Task row stays. Allowed `1`–`3650`. Helm: `apps.taskLog.retentionDays`. |

The nested-list weight of the `GRAPHQL_MAX_COST` estimator is the code constant
`NESTED_LIST_WEIGHT = 5` (`backend/app/graphql/limits.py`) — deliberately **not** an eighth
`GRAPHQL_*` env var, so the estimator cannot be widened arbitrarily. It answers a different question
than `GRAPHQL_NESTED_LIST_LIMIT`: the weight says what one nested list field is worth in the cost
budget, while the cap says how many rows one parent may return at all.

`MISTRAL_API_KEY` and `MISTRAL_API_BASE_URL` are commented legacy placeholders in the root
template. Backend code reads only the provider-neutral `LLM_*` variables (Phase 302, provider
neutrality); the Kubernetes secret installer still accepts a legacy `MISTRAL_API_KEY` secret key
as external API credential material for existing installs.

### Task log cap and retention (Phase 300)

Core stores a rollback-safe, bounded run log per task, read through
`GET /api/v1/tasks/{id}/logs` and downloadable as a `.log` snapshot (both gated by `tasks:read`).

- **Cap per run (`TASK_LOG_MAX_LINES`, default 10 000):** the first half and the last half of a
  run are kept; the discarded middle is marked with `----- N lines dropped (line limit reached) -----`
  in the API gap and in the download. Cap and retention have no settings UI (D-12).
- **Retention (`TASK_LOG_RETENTION_DAYS`, default 30):** the worker runs an internal
  `system_task_log_purge` job once at startup and then hourly. It deletes only lines older than the
  window, keeps the Task row (and its `task_log_state` row), and marks a fully purged task
  `expired`. **PENDING and RUNNING tasks are never purged**, so an active long run keeps its whole
  history. Deleting a Task still removes its log through the FK cascade.
- **DB-outage barrier:** the completion path waits for accepted lines to be persisted. On a
  temporary database outage the writer keeps the buffered lines and retries with bounded backoff;
  it never drops an accepted line silently, and the outage is visible in the process log. Lines
  explicitly discarded by the cap are counted as dropped, not as unsaved accepted lines.
- **Deliberate boundary — one worker process:** this phase assumes exactly one worker
  (`replicas: 1`) and does not implement multi-replica safety. A hard worker abort (crash, OOM,
  SIGKILL) loses the not-yet-persisted buffer lines; startup recovery writes a
  "log may be incomplete" line for the interrupted run. Multi-replica safety can be added later if
  `replicas > 1` is needed.

## Container Images and Server-Level Settings

### The database image and its two baked settings

Both install paths run the repository's own PostgreSQL image, built from
`deploy/k8s-images/stateful/postgres/Dockerfile`: the unchanged `postgres:18.4-alpine` base line with
Apache AGE and pgvector compiled from pinned sources, plus two server settings baked into the image.
The Compose service and the chart both run it under the project's phase tag, so both paths use the
same image.

| Setting | Value | Effect |
|---|---|---|
| `shared_preload_libraries` | contains `age` | Preloads the graph extension. An operator-configured entry (for example `pg_stat_statements`) is merged, never replaced. |
| `wal_level` | `logical` | Enforced on every start. |

Facts an operator needs before changing anything:

- The image applies them itself, on an **already initialized data directory** as well as on a fresh
  `initdb`; there is no `command:`/`args:` for them in `docker-compose.yml` or in the chart, and
  nothing has to be passed on the command line.
- They are **restart parameters** (postmaster-level settings, read only at server start). Changing
  them means a new image — or an explicit edit of the settings file inside the data directory —
  followed by a container/pod restart; a runtime `SET` cannot change them.
- This release creates **no replication slot, no publication and no replication role**. `wal_level =
  logical` therefore only retains WAL a little longer than the default; no WAL retention is
  configured for any consumer.
- The graph and vector extensions are created in the **application database only**, by the
  application's own migration chain (`alembic upgrade head`). The server's other databases are
  deliberately left untouched, which is what makes cluster-wide preloading safe.

### The search admin password

`OPENSEARCH_PASSWORD` must satisfy the rule the image's demo security configuration enforces: 8–100
characters with at least one uppercase letter, one lowercase letter, one digit and one special
character, and not similar to the user name. A value that does not match makes the container exit at
start and is refused by the installers' fail-closed gates.

The value is consumed as `OPENSEARCH_INITIAL_ADMIN_PASSWORD` on the **first** start of an empty
`opensearch_data` volume; from then on the security index holds the hash. Changing
`OPENSEARCH_PASSWORD` afterwards does not rotate the admin password — rotation is a security-plugin
operation outside this release's scope.

### Volumes and claims

| Tier | Compose volume | Cluster claim |
|---|---|---|
| Database | `postgres18_data` | `data-postgres-0` |
| Cache/queue | `valkey_data` | `data-valkey-0` |
| Search | `opensearch_data` | `data-opensearch-0` |
| Storage | `storage_data` | `data-storage-0` |

On a fresh stack the cache/queue and search volumes start **empty** by design: their contents are
derived state (the index is rebuilt from PostgreSQL, the queue is recreated). The database volume is
the one carried over unchanged. An installation created before the current durable-state layout must
be rebuilt rather than upgraded in place, and its retired volumes and claims are removed by hand.

## Config File Format

There is no separate application YAML or JSON configuration file. These repository files provide
deployment configuration and plugin declarations in addition to environment variables:

- **`docker-compose.yml`** is the public, production-shaped base: PostgreSQL, Valkey, OpenSearch,
  backend, worker, storage, frontend and Nginx, plus the maintenance-profile `cutover` service. The
  log stack (Loki, Alloy and the read-only `docker-socket-proxy`) sits behind the `logging` and
  `observability` profiles and Grafana behind `observability`, so it is not part of the default set.
  It uses `.env` and then overrides internal service addresses and shared filesystem paths.
  `docker-compose.dev.yml` (the mirrored dev overlay with the source bind mounts),
  `docker-compose.jira.yml` (the non-mirrored, development-only Jira service and its volume) and
  `docker-compose.e2e.yml` (the non-mirrored, development-only `e2e-tests` Playwright runner behind
  the `e2e` profile) are layered on top through the private `.env`'s `COMPOSE_FILE`.
- **`nginx/nginx.conf`** is a read-only static Nginx configuration. It provides TLS, HTTP-to-HTTPS
  redirecting, proxy routing, body-size limits, and Nginx rate-limit zones. It does not interpolate
  environment variables. The base file serves the compiled SPA with the strict CSP; the development
  policy lives in `nginx/nginx.dev.conf`, which `docker-compose.dev.yml` mounts over it.
- **`deploy/helm/indramesh/values.yaml`** holds non-secret Kubernetes defaults. Helm workload
  templates inject secret values through Kubernetes `Secret` references rather than chart values.
- **`plugins/<name>/plugin.toml`** declares plugin metadata, capabilities, optional configuration
  schema, and frontend/database references. Operator-supplied plugin configuration is stored in
  PostgreSQL, not written back to this manifest.

Helm's top-level `bootstrap` controls the endpoint contract; `postgres`, `valkey`, and
`opensearch` control stateful services; `apps` controls application workloads and migration
settings; `plugins` controls plugin storage and migration-role identity. `networkPolicies`,
`backup`, `monitoring`, and `headlamp` configure network isolation and operational services.

The following Compose excerpt illustrates the override order: `env_file` loads `.env`, while
`environment` provides the service-network values that the backend actually receives.

```yaml
backend:
  env_file:
    - .env
  environment:
    - INDRAMESH_RUNTIME_MODE=compose
    - DB_HOST=postgres
    - DB_PORT=5432
    - DB_USER=${DB_USER}
    - DB_PASSWORD=${DB_PASSWORD}
    - DB_NAME=${DB_NAME}
    - VALKEY_URL=redis://:${VALKEY_PASSWORD}@valkey:6379/0
    - STORAGE_SERVICE_URL=http://storage:8001
```

## Required vs Optional Settings

For a direct backend run, the following values must be present before application startup:

- **`JWT_SECRET_KEY`** must be non-empty.
- **`FERNET_KEY`** must be non-empty and accepted by `cryptography.fernet.Fernet`.
- **`CORS_ALLOWED_ORIGINS`** must contain at least one non-wildcard origin.
- **`NGINX_TRUSTED_IP`** must be non-empty.

`INDRAMESH_RUNTIME_MODE` must resolve to exactly `compose` or `kubernetes`; Compose and Helm
provide it rather than relying on `.env`. `ADMIN_PASSWORD` is required in **every** runtime mode:
Compose guards it with `${ADMIN_PASSWORD:?…}` and the backend fails closed in Compose and Kubernetes
alike. The database, Valkey, OpenSearch, frontend port, and backend port settings are
required for a functioning Compose stack; use the values in `.env.example` as the local baseline.

Startup validation is implemented in `backend/app/main.py`,
`backend/app/services/runtime_mode.py`, and `backend/app/services/init_service.py`:

| Setting | Validation failure |
|---|---|
| `JWT_SECRET_KEY` | `JWT_SECRET_KEY environment variable is required but not set.` |
| `FERNET_KEY` | `FERNET_KEY environment variable is required but not set.` or `FERNET_KEY is invalid: ...` |
| `FERNET_KEY_OLD` | `FERNET_KEY_OLD is invalid: ...` — it is only set during a key rotation; either supply the previous key in the same format as `FERNET_KEY` or unset it. |
| `CORS_ALLOWED_ORIGINS` | Missing value: `CORS_ALLOWED_ORIGINS environment variable is required but not set.` Empty parsed list is also rejected; a wildcard produces `CORS_ALLOWED_ORIGINS must not contain '*'.` |
| `NGINX_TRUSTED_IP` | `NGINX_TRUSTED_IP environment variable is required but not set.` |
| `INDRAMESH_RUNTIME_MODE` | `INDRAMESH_RUNTIME_MODE must be 'compose' or 'kubernetes' ...` |
| `ADMIN_PASSWORD` | Initial-user creation fails closed when it is missing or empty, in every runtime mode, naming `ADMIN_PASSWORD` and the `.env`/Secret source. Setting it does not reset an existing admin password. |

`AppSettings` validates GraphQL depth and all numeric AQL/graph cutoffs as integers at least `1`.
An invalid value raises a Pydantic validation error when settings are constructed. CORS validation
trims a comma-separated list and rejects empty lists and `*`; it does not certify that the listed
origins are reachable or trustworthy. Proxy validation checks presence, not the deployment's
network topology.

### Plugin migration-role requirements

`backend/app/plugin_system/bootstrap_role.py` resolves this credential matrix (whitespace-only
values count as absent):

| `PLUGIN_DB_USER` | `PLUGIN_DB_PASSWORD` | Result |
|---|---|---|
| Absent | Absent | Bootstrap skipped; runner warns that Core tables are unprotected on the privileged fallback. Plugins requiring a restricted role are refused. |
| Absent | Present | Restricted role named `indramesh_plugin`. |
| Present | Present | Restricted role with the validated configured name. |
| Present | Absent | Fatal: `PLUGIN_DB_USER is set but PLUGIN_DB_PASSWORD is missing; configure both or neither.` |

Configured bootstrap/GRANT failures or privilege drift abort deployment; there is no silent
fallback. The migration role owns no Core objects and is limited to `SELECT` and `REFERENCES` on
Core tables, while it can create plugin tables. This isolates migration revisions, not runtime
plugin code: runtime database access still uses the application connection.

Compose's `backend/start.sh` runs Core migrations, role bootstrap, then plugin migrations before
Uvicorn. Helm's `templates/migration-job.yaml` bootstraps the role alongside Core migrations and
runs plugin migrations in a separate post-install/post-upgrade Job. Plugin-attributable migration
failures isolate that plugin rather than failing the entire deploy.

For Kubernetes, set non-secret `plugins.dbUser` and supply `PLUGIN_DB_PASSWORD` in the
`indramesh-database` Secret. With `plugins.dbUser` empty (the chart default), neither credential is
rendered into migration Jobs—even if the Secret contains a password. The shipped
`plugins/jira/plugin.toml` declares `require_restricted_role = true`, so leaving the boundary
unconfigured prevents Jira plugin migration/admission. See
[Plugin files, configuration, and lifecycle](./DEPLOYMENT.md#plugin-files-configuration-and-lifecycle).

## Defaults

The backend derives its database URL as follows when `DATABASE_URL` is unset:

```text
postgresql+asyncpg://{DB_USER}:{DB_PASSWORD}@{DB_HOST}:{DB_PORT}/{DB_NAME}
```

`AppSettings` in `backend/app/config.py` supplies the default `ENVIRONMENT`, GraphQL, AQL, and
graph-limit values in the table. `StorageSettings` supplies the storage service URL and bucket.
Other defaults are applied where the variable is read—for example, in the database, cache,
rate-limit, import/export, JWT, worker, and task-log service modules.

| Default group | Source |
|---|---|
| Database URL and pool parameters | `backend/app/db/__init__.py` |
| LLM settings/client construction | `backend/app/config.py`; API display values in `backend/app/api/config.py` |
| Global/public/login limiter configuration | `backend/app/services/rate_limit.py`; route-specific rules in `backend/app/api/auth.py` and `backend/app/api/users.py` |
| JWT age and clock-skew settings | `backend/app/services/auth_service.py` |
| Plugin discovery directory | `backend/app/plugin_system/__init__.py` |
| Plugin migration-role name | `backend/app/plugin_system/bootstrap_role.py` |

Code defaults are not necessarily template values: the root template uses host-facing
`DB_HOST=localhost`, `DB_PORT=5434`, and a host-facing cache URL, while Compose injects container
addresses. If a non-empty `DATABASE_URL` is present, it still overrides those injected `DB_*`
components; remove a stale host-only URL before using the container stack.

When `LLM_BASE_URL` is unset, `app/llm` falls back to the selected `LLM_PROVIDER` preset's base URL
(`mistral` → `https://api.mistral.ai/v1`, `openai` → `https://api.openai.com/v1`); a provider whose
preset carries no URL (`openai_compatible`) fails loudly instead. On Kubernetes the same non-secret
values reach the pods through `apps.backend.llm.*` and `apps.worker.llm.*`; the installer writes
them from the `.env` whenever it sets any `LLM_*` variable besides the key.

`TLS_CERT_DIR` is documented as a potential certificate-directory override in `.env.example`, but
it is not consumed by Compose: the Nginx certificate mount is fixed at `./nginx/certs`. Likewise,
the static Nginx redirect mapping only translates the default host-port pair `8180` → `8443`; if
you change those ports, update `nginx/nginx.conf` as well.

## Plugin Configuration

The validated manifest contract is in `backend/app/plugin_system/manifest.py`. Required metadata
keys are `name`, `version`, `author`, `api_version`, and `capabilities`; directory discovery also
needs `module`. Optional keys include `config_schema`, `frontend`, `database`, `license` (an SPDX
identifier or expression such as `Apache-2.0`; declared by every shipped plugin but not required
for a third-party plugin), `permissions`, `enables_http_graphql` (default `false`),
`requires_worker_update` (default `false`), `requires_log_store` (default `false`), `requires_llm`
(default `false`), and `active_by_default` (default `true`, consulted only on first discovery).
Unknown keys are rejected.

- **`requires_log_store` / `requires_llm`:** declare that the plugin only works with a log store or
  with a configured Core LLM client (`LLM_*`). The plugin installers print the remedy when the
  requirement is missing; for `requires_llm` the Plugins admin page additionally shows the Core's
  LLM status (`not_configured` / `misconfigured`, with the key-free reason) next to the plugin,
  because such a plugin hides its AI actions without an LLM. `ai-translation` and
  `ai-schema-wizard` declare `requires_llm`; `log-explorer` declares `requires_log_store`.

- **`[frontend]`:** `bundle` plus an optional `extension_points` list.
- **`[database]`:** `migrations_dir` must be relative and traversal-free; `version_table` must be
  `alembic_version_<normalized_plugin_name>`. `require_restricted_role` defaults to `false` but
  the Jira manifest opts in. Plugin-owned tables use the `plugin_<normalized_plugin_name>_` prefix.
- **`[config_schema]`:** inline JSON Schema for persisted plugin settings. Use ordinary nested
  object properties for secret fields and mark them with `format = "password"`.

For example, this optional schema section can be added to a plugin manifest:

```toml
[config_schema]
type = "object"

[config_schema.properties.endpoint]
type = "string"
default = "https://service.example.invalid"

[config_schema.properties.token]
type = "string"
format = "password"
```

Users with `plugins:manage` permission read and update loaded-plugin settings through
`GET /api/v1/plugins/{name}/config` and `PUT /api/v1/plugins/{name}/config`. PUT accepts a JSON
object containing `config`; invalid schema/payload validation returns HTTP `422`. A plugin not
currently loaded returns `404`, because its schema is unavailable.

`backend/app/plugin_system/validation.py` applies schema defaults at read time only; it does not
persist them or satisfy required PUT fields automatically. Non-secret PUT fields replace the
submitted configuration rather than being merged with omitted old fields.

`backend/app/plugin_system/crypto.py` encrypts password-marked values individually in
`plugins.config` JSONB using the shared `FERNET_KEY` and a `v1:` ciphertext prefix. GET masks
stored secrets with `__INDRAMESH_SECRET_UNCHANGED__`. On PUT, that sentinel, an omitted secret,
or `null` preserves existing ciphertext after payload validation; another accepted value replaces
it with newly encrypted plaintext. Nested object-property secrets are supported. Secret markers
under arrays or schema composition and literal-dot property names are rejected rather than
silently persisted unencrypted; `$ref` schemas are also rejected by the config validator.

Do not change `FERNET_KEY` casually: existing plugin and service-account ciphertext depends on it.
An undecryptable plugin value becomes `[INVALID_TOKEN]`, not a recovered credential. A real change
is a rotation, not an edit: follow [Fernet Key Rotation](./DEPLOYMENT.md#fernet-key-rotation), which
keeps the old key readable under `FERNET_KEY_OLD` until
`python -m app.maintenance rotate-fernet-key` exits `0`. The Jira
plugin's connection settings are separate from global manifest-schema settings; `JIRA_URL` is not
a substitute for configuring those connections.

## Per-Environment Overrides

- **Compose development:** layer the mirrored `docker-compose.dev.yml` in the private `.env`'s
  `COMPOSE_FILE` — the overlay selects the Vite dev server (HMR), the development CSP and the
  source bind mounts. Set `ENVIRONMENT=development` as well to keep Uvicorn reload and request
  timing; that also disables the global rate limiter for test traffic.
- **Compose production (the shipped default):** `.env.example` sets `ENVIRONMENT=production`, so a
  fresh clone runs the production shape — `backend/start.sh` runs Uvicorn without `--reload` and the
  frontend container serves the image-built bundle through `vite preview`. Rebuild the backend and
  frontend images after changing it.
- **Kubernetes:** Helm supplies `INDRAMESH_RUNTIME_MODE=kubernetes`, database and service DNS
  values, and non-secret values from `values.yaml`. `scripts/k8s/lib/secrets.sh` reconciles
  sensitive `.env` values into Kubernetes Secrets before Helm workloads consume them. Set
  `apps.environment`, `apps.backend.corsAllowedOrigins`, and `apps.backend.nginxTrustedIp` to the
  deployment’s trusted values through a private values override.
  `scripts/k8s/lib/secrets.sh` creates absent Secrets and compares existing data; it does not
  overwrite existing Secrets on rerun. Missing newly configured keys or differing credentials
  require explicit operator action. The `indramesh-external-api` Secret (`LLM_API_KEY`) is
  optional: an install without a key starts without it, and the first installer run that finds a
  key in the `.env` creates the Secret and restarts the backend and worker so they pick it up.
- **Secrets:** use distinct database, Valkey, OpenSearch, JWT, Fernet, admin, and LLM
  credentials for each environment. Do not commit `.env` files or place secret values in Helm
  values files.

### Outbound connections on Kubernetes

The Helm chart denies all traffic in the `indramesh` namespace by default and then admits what each
workload needs. For outbound connections from the backend and worker — the LLM provider and the
Jira instances the Jira plugin talks to — it ships two rules:

- **Public addresses on port 443** (`networkPolicies.publicEgress`, on by default). Jira Cloud and
  the hosted LLM presets (`mistral`, `openai`) work without further steps. Private, loopback,
  link-local and CGNAT ranges are excluded, so this rule never opens the cluster or the local
  network. Set `networkPolicies.publicEgress.enabled: false` in a private values file to close it.
- **Explicit targets** (`networkPolicies.externalEgress`), one `{cidr, port}` entry each. The
  installer fills it from the `.env`:
  - `K8S_EGRESS_ALLOW` lists targets by hand, for example an on-prem Jira Data Center:
    `K8S_EGRESS_ALLOW=10.0.5.20/32:8080`. Several entries are comma-separated.
  - When `LLM_BASE_URL` points at a private address (a local Ollama or vLLM server, for example
    `http://192.168.1.50:11434/v1`), or at a public address on a port other than 443, the
    installer resolves the host and adds the matching `/32` entry itself.

A NetworkPolicy matches IP addresses, not host names. A host that resolves only inside the cluster
(a Service such as `ollama.ai.svc.cluster.local`) cannot be resolved by the installer: add its pod
or Service range to `K8S_EGRESS_ALLOW`. Rerun `./install_indramesh_k8s.sh` after changing any of
these values.

A missing rule shows up as a connection failure: the Jira connection test reports "Jira could not
be reached." and the LLM status reports an unreachable provider or a timeout. On Kubernetes both
messages point at this egress configuration.

Confirm trusted frontend origins and the real proxy source address/CIDR for each deployment;
Compose's static address and Helm's default pod CIDR are repository defaults, not evidence of a
different cluster's topology.
