<!-- generated-by: gsd-doc-writer -->
# Deployment

IndraMesh Core supports a Docker Compose stack for local development and previewing
production-mode behavior, plus a single-node k3s deployment managed through Helm.

## Deployment Targets

| Target | Configuration | Purpose |
|---|---|---|
| Docker Compose | `docker-compose.yml` | Local development and production-mode preview. |
| k3s with Helm | `deploy/helm/indramesh/` | The repository-managed deployment target. |

The chart also has a capability-gated OpenShift passthrough Route in
`templates/openshift-route.yaml`. That is a routing template, not a complete
OpenShift installer, and the supplied lifecycle scripts target single-node k3s
rather than OpenShift.

**OpenShift/OKD is a known limitation, not a supported target.** The chart is built to be
OpenShift-*compatible* — its manifests are meant to be admissible under a restricted SCC — but it
has **never been verified** on OpenShift or OKD: no release has been installed, exercised or
upgraded on either. IndraMesh Core 1.0 supports Docker Compose and single-node k3s only. Treat an
OpenShift/OKD deployment as unverified work rather than as a supported configuration.

### Docker Compose

The Compose file pins the project name to `indramesh`; a CLI `-p` or
`COMPOSE_PROJECT_NAME` override takes precedence. It defines these services:

| Service | Purpose |
|---|---|
| `postgres` | PostgreSQL database, running the repository's own image (Apache AGE + pgvector). |
| `valkey` | Valkey cache and ARQ queue backend (the upstream `valkey/valkey` image). |
| `jira` | Jira Service Management instance used by the Compose stack. |
| `opensearch` | Search index, running the repository's group-readable OpenSearch wrapper image. |
| `backend` | FastAPI REST and GraphQL API. |
| `worker` | ARQ worker process. |
| `storage` | Internal filesystem-backed storage service. |
| `frontend` | React/Vite frontend. |
| `nginx` | TLS termination and reverse proxy. |
| `cutover` | One-shot destructive-upgrade maintenance, enabled only through the `maintenance` profile. |

The backend, worker, and frontend source directories are bind-mounted. Python and
frontend source edits therefore normally do not require an image rebuild. Rebuild
when Dockerfiles, dependencies, or Vite build-time variables change.

Two stateful services build from tracked Dockerfiles instead of pulling an upstream
tag: `postgres` runs the repository's own PostgreSQL image (Apache AGE and pgvector
compiled onto the unchanged `postgres:18.4-alpine` base line, plus the two baked
server settings — see
[CONFIGURATION.md](./CONFIGURATION.md#the-database-image-and-its-two-baked-settings)),
and `opensearch` runs a group-readable wrapper around the upstream OpenSearch image —
the same wrapper the cluster path imports. The cache/queue service runs the upstream
`valkey/valkey` tag directly, and no service carries a `command:`/`args:` for a server
setting.

The named volumes are `postgres18_data`, `valkey_data`, `opensearch_data` and
`storage_data`, plus the log-stack volumes behind their profiles. On the cluster the
same tiers use the claims `data-postgres-0`, `data-valkey-0`, `data-opensearch-0` and
`data-storage-0`. The cache/queue and search volumes start **empty** on a fresh stack
(their contents are derived state and are rebuilt), while the database volume is the one
carried over unchanged. An installation created before the current durable-state layout
must be rebuilt rather than upgraded in place, with its retired volumes removed by hand;
the procedure for upgrading a current installation is in
[OPERATIONS.md § Upgrade](./OPERATIONS.md#upgrade).

Run Compose service operations only from the main checkout, never a Git worktree:
the shared project name can otherwise replace running containers with worktree
bind mounts. The read-only Docker socket mounted into the backend supports log
discovery; a read-only mount is not a Docker API authorization boundary.

`ENVIRONMENT=production` changes the Compose backend command to non-reload Uvicorn
and builds the frontend into `/srv/frontend-dist`, served with Vite preview. Other
or unset values use the development commands. This is a repository-supported
preview mode; the hardened static frontend image is the k3s-specific Caddy image.

### k3s and Helm

The k3s installer supports native Ubuntu or Debian on `amd64` or `arm64`, and
Ubuntu under WSL2 on either architecture. The canonical chart is
`deploy/helm/indramesh`, released as `indramesh` in the `indramesh` namespace.
The installer's supported hosts, its restart-and-rerun behaviour and the two
Windows-side conflicts are described under
[Kubernetes Host Prerequisites](#kubernetes-host-prerequisites) below.

#### Kubernetes Host Prerequisites

The installer runs on a fresh supported host. It provisions only an allowlisted set of base
packages (`curl`, `ca-certificates`, `tar`, `coreutils`, `gnupg`); **Docker must already be
installed and running on the host** — the installer uses Docker to build and save the local
application images and never installs Docker or provisions a daemon itself.

| Host | Architecture | Notes |
|---|---|---|
| Native Ubuntu Linux | `amd64` | Standard desktop or server install |
| Native Ubuntu Linux | `arm64` | Includes Ubuntu on a Raspberry Pi |
| Native Debian Linux | `amd64` / `arm64` | Includes Raspberry Pi OS — see [RASPBERRY-PI.md](RASPBERRY-PI.md) for the two Pi-specific steps |
| Ubuntu under WSL2 | `amd64` / `arm64` | WSL2-specific configuration is a conditional adapter, not a platform requirement |

Other distributions are not supported, and the installer refuses to proceed on them. That check
runs as part of host preparation, after the bootstrap base packages may already have been
installed, so an unsupported host can be minimally mutated before it is rejected.

**Memory floor: 6GB.** Before the Helm reconciliation the installer reads the host's memory budget
and selects one of two documented stateful profiles — `compact` from 6 GiB through 15 GiB,
`standard` from 16 GiB — and a malformed or below-6-GiB budget fails before Helm runs. The floor
is the search tier's: in compact mode the search service keeps a 3 GiB container limit above its
2 GiB maximum Java heap, so a host below the floor cannot carry the search tier together with the
rest of the stateful set on one node. The choice is visible and reproducible through the
non-secret `INDRAMESH_STATEFUL_PROFILE` variable (`compact` or `standard`), which the installer
writes into the values file it passes to Helm.

**WSL2 hosts need systemd.** On WSL2 the installer reconciles `systemd=true` in `/etc/wsl.conf`
and the memory floor in the user's `.wslconfig`, preserving every unrelated setting it finds. Both
are host edits it may make, and it reports them. They take effect only when the WSL VM restarts,
so when the installer changes a WSL setting it stops with a distinct restart-and-rerun outcome
instead of continuing under stale settings: restart WSL (`wsl --shutdown` from Windows, or reboot
Windows), reopen the terminal in the checkout, and rerun `./install_indramesh_k8s.sh`. **Native
Ubuntu never receives WSL2 configuration edits.**

**Reaching the stack.** The stack answers at `https://localhost`: a local relay
(`indramesh-local-forward@{80,443}.service`) listens on `127.0.0.1` and forwards to Traefik on
every host. On WSL2 the Windows browser reaches it through WSL's localhost forwarding (on by
default, `localhostForwarding` in `.wslconfig`), so no Windows hosts entry, port proxy or
administrator permission is involved. On a native host other machines reach the stack at
`https://<host-ip>`; the installer adds that address to the certificate.

Two Windows-side conditions can break the bootstrap on WSL2, and the installer can only fail fast
on them rather than work around them: Docker Desktop installed under a path containing a space
(k3s's kubelet fails to start when that path is mounted into the distro with the space unescaped),
and IIS installed on the Windows host (its `http.sys` driver claims ports 80/443 and answers there
before traffic ever reaches WSL2). Rule both out before running the installer, and again if the
installer or `./start` appears to hang or a browser request returns an unexpected result.

A fresh clone reaches a logged-in UI with one command: `./install_indramesh_k8s.sh` creates `.env`
from `.env.example` when the file is absent, generates every secret in it, installs or reuses the
cluster, provisions TLS through cert-manager, builds and imports the local application images into
the cluster and reconciles the Helm release. The quickstart is lean by default: the monitoring
stack, Headlamp and the log stack are installed only when their switch in the root `.env` is
exactly the string `true` — `MONITORING_ENABLED`, `HEADLAMP_ENABLED` and `LOGINFRA_ENABLED`,
all defaulting to `false`. Switching one off stops managing that component; nothing is removed.

The chart renders PostgreSQL, Valkey, OpenSearch, and the internal storage
service as StatefulSets. It renders the backend, worker, and frontend as
workloads when `apps.enabled` is true; Nginx and ingress routes are additionally
gated by `apps.nginx.enabled`. The chart also includes a pre-upgrade Alembic
migration Job, a daily PostgreSQL backup CronJob, and the opt-in Prometheus
integration and Headlamp deployment gated by the switches above.

The stateful tier is admitted by its own network policies (`allow-valkey-ingress`,
`allow-opensearch-ingress`, `allow-postgres-ingress` and the storage policy), with the
matching egress rules on the backend/worker axis, so a renamed tier and its policy move
together. The search tier's StatefulSet runs the wrapper image imported from the same
Dockerfile Compose builds, and its admin credential is the `OPENSEARCH_PASSWORD` value
in the `indramesh-opensearch` Secret — the same password rule described in
[CONFIGURATION.md](./CONFIGURATION.md#the-search-admin-password) applies, and the
installer's fail-closed gate refuses a `.env`/Secret whose `VALKEY_PASSWORD` or search
credential is missing or non-compliant, naming the offending group/key.

Outbound connections from the backend and worker — the LLM provider and the Jira
instances the Jira plugin connects to — are admitted to public addresses on port 443
by default; an on-prem Jira or a local model server needs an explicit entry
(`K8S_EGRESS_ALLOW`), see
[Outbound connections on Kubernetes](./CONFIGURATION.md#outbound-connections-on-kubernetes).

Because the stateful wrapper images are tag-pinned rather than digest-pinned, a tag move
invalidates any recorded image digests. `scripts/k8s/verify_stateful_services.sh` is the
only writer of the evidence file beside it: after moving an image tag, re-run the
verifier from the main checkout so the file records the digests actually imported.

Jira is not included in the k3s chart.

## Legacy Jira Core Cut-over (One-way)

Core revision `295_core_jira_legacy_schema_drop` performs a one-way schema drop: it deletes the legacy
Core Jira data and drops the retired columns, tables and enum. Both stacks must
therefore follow the same quiesce-first order and must never roll back to old
images.

**Supported order (both stacks):**

1. **Quiesce.** Stop the old API and worker and every external queue producer.
   A traffic/proxy stop is not a substitute — the old processes must actually
   terminate, or they keep writing jobs and cache values during the drop.
2. **Clear the queue.** Clear ARQ queue membership and job/in-progress/result/
   retry/abort state (the maintenance tool's `clear-queue` step). The same step
   then marks every non-permanent `PENDING`/`RUNNING` Task `FAILED` ("Cleared by
   maintenance cut-over (queue reset)"), because no job will ever pick it up
   again.
3. **Core migrate.** Run the Core Alembic revision under the Core role.
4. **Scoped cache purge.** Purge the internal entity/tag namespaces **and both
   public namespaces** — `cache:public:*` and the separately-named `public:*`.
   With `clear-queue`/`purge-cache` the auth token-blocklist, rate-limit
   counters, the public-token lookup cache and plugin lifecycle state are
   preserved.
5. **Final API/plugin ready**, then **start the worker**, then **reopen
   traffic** — in that staged order, on the final images and plugin files.

### Compose

`docker-compose.yml` defines a one-shot `cutover` service behind the
`maintenance` profile. It runs the backend image on the final bind-mounted code
and executes `python -m app.maintenance clear-queue` → `alembic upgrade head` →
`python -m app.plugin_system.bootstrap_role` → `python -m app.maintenance
purge-cache` as one fatal chain. It never runs the plugin runner, never calls
`docker compose down`, and never deletes a volume.

```bash
# 1. Quiesce the old producers (stop external producers too)
docker compose stop backend worker

# 2. One-shot cut-over on the final code
docker compose --profile maintenance run --rm cutover

# 3. Final API/plugin ready, then the worker
docker compose up -d backend
# Wait for /health and verify plugin readiness before the next command.
docker compose up -d worker
```

### k3s / Helm

The chart keeps the two-job migration split and adds two bounded pre-upgrade
maintenance hooks around the Core migration Job, ordered by ascending hook
order: `indramesh-maintenance-clear-queue` (before) → `indramesh-migration`
(Core migrate) → `indramesh-maintenance-purge-cache` (after). The plugin
migration runner stays in its own post-upgrade Job and no maintenance hook
mounts the plugin PVC.

Both maintenance hooks are opt-in: they render only when
`apps.maintenance.cutover` is `true` (default `false`). A routine upgrade
therefore never clears the ARQ queue or purges caches under running pods.
`scripts/k8s/rebuild.sh --cutover` writes the flag into the release values for
that run but does not clear it when the run finishes; it stays stored in the
release until the next ordinary `rebuild.sh` invocation (without `--cutover`)
re-asserts it as `false`. A direct `helm upgrade` that reuses those stored
values can render the destructive hooks again. A direct `helm upgrade` passes
`--set apps.maintenance.cutover=true` instead.

Scale the old app tier to zero and wait for termination **before** the upgrade.
Prevent external controllers and queue producers from undoing the quiesce.
`--set …replicas=0` inside the same `helm upgrade` does not work: the
pre-upgrade hooks fire before the new replica values take effect, so the old
pods would still be running.

```bash
# 1. Quiesce: scale old backend/worker to zero and wait
sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n indramesh scale deployment/indramesh-backend deployment/indramesh-worker --replicas=0
sudo env KUBECONFIG=/etc/rancher/k3s/k3s.yaml \
  kubectl -n indramesh wait --for=delete pod \
  -l 'app.kubernetes.io/name in (indramesh-backend,indramesh-worker)' --timeout=180s

# 2. Build/import the final images and run the single full Helm pass with the
#    cut-over hooks enabled; the ordered pre-upgrade hooks fire during this pass.
scripts/k8s/rebuild.sh backend --cutover
```

**Staging limitation:** this rebuild command reconciles backend and worker in
the same Helm pass. The chart's plugin migration hook runs only after ordinary
workloads are Ready; it does not enforce API/plugin-ready → worker-start order.
Keep traffic and external producers closed. An operator must establish the
worker-replica override and subsequent restart/reactivation procedure before
using this command for the destructive cut-over. A successful rollout alone
does not prove plugin readiness.

### Evidence to capture

- the final Core revision after the migration (`alembic current`) and the
  unchanged plugin chain head;
- absence of the dropped columns, tables and native enum in the live catalog;
- the `clear-queue`/`purge-cache` report counts (both public namespaces covered);
- backend and plugin health after the staged start;
- with the Jira plugin disabled, the graceful-absence checks (persisted Jira
  sources/targets stay listable and `jira.*` schedules appear paused).

**No rollback.** After this destructive drop there is no rollback to old
images; the schema and data are gone. Recovery from a failure is forward
correction (fix, redeploy, re-verify) with traffic held closed, never a
`helm rollback` or an old-image restart. A successful template parse, an
offline check or an unavailable Helm rehearsal is never deployment evidence.

## AI Translation Plugin Rollout (One-way Core Drop)

AI translation lives in the `ai-translation` plugin. Core drops the `translation_suggestions`
table with the drop-only Core revision `307_drop_translation_suggestions` (no data copy, no
historical rewrite). There is **no rollback to
old images** after this drop: the retired table and any legacy AI rows are gone, and recovery from a
failure is forward correction with traffic/queue held closed. Manual data/UI translation, the
language/fallback rules and the UI catalog are unaffected and keep working with the plugin disabled.

The Core drop and the legacy queue/status cleanup are separate, ordered steps. A short outage is
acceptable (project decision 2026-09-24: no live AI suggestion data exists); the worker is stopped
before the drop and cleanup, and restarted only after both have completed. Run all Compose commands
from the main checkout, never a git worktree, and never call `docker compose down`.

### Compose

`backend/start.sh` already runs the whole migration chain at backend startup, in this fatal-ordered
sequence: `alembic upgrade head` (the Core drop) → `python -m app.plugin_system.bootstrap_role` →
`python -m app.plugin_system.migrate` (the plugin's private `0001_suggestions` → `0002_runs`
chain).

```bash
# 1. Quiesce the worker so no legacy job runs during the drop.
docker compose stop worker

# 2. Deploy the final backend. start.sh runs alembic upgrade head (Core drop)
#    -> bootstrap_role -> plugin migrate.
./rebuild_indramesh_docker.sh backend

# 3. Remove the retired legacy AI job/status keys exactly once (allowlisted,
#    never a broad queue reset). Wait for the backend to be healthy first.
docker compose exec -T backend python -m app.maintenance cleanup-legacy-translations

# 4. Start the final worker on the new code.
docker compose up -d worker
```

The cleanup command is idempotent and scoped: it removes only the two retired ARQ job functions and
the exact old `translate_job:*` / `ui_translation_job:*` status key families, leaving unrelated
queued jobs and protected keys (token blocklist, rate limits, plugin lifecycle state) intact. It
never calls `clear-queue`/`purge-cache`. Its report is
`{"ok": true, "removed_jobs": N, "removed_keys": N, "skipped_undecodable": N, "failed_tasks": 0}`.

### Kubernetes (optional for this phase)

Kubernetes delivery uses the existing installer and migration Jobs — no new Helm hook or values flag
is added.

```bash
# 1. Quiesce the worker.
KUBECONFIG=$HOME/.kube/config kubectl -n indramesh scale deployment/indramesh-worker --replicas=0

# 2. Upgrade with the final images. The chart's existing pre-upgrade migration Job
#    runs the Core drop; the post-upgrade plugin Job runs the private chain.
KUBECONFIG=$HOME/.kube/config helm upgrade indramesh deploy/helm/indramesh -n indramesh

# 3. Copy the plugin onto the plugins PVC, run its private migration and restart
#    backend/worker (existing tool; refuses an existing destination).
KUBECONFIG=$HOME/.kube/config ./install_plugin_k8s.sh plugins/ai-translation

# 4. Remove the retired legacy AI job/status keys exactly once.
KUBECONFIG=$HOME/.kube/config kubectl -n indramesh exec deploy/indramesh-backend -- \
  python -m app.maintenance cleanup-legacy-translations

# 5. Restore the worker replicas.
KUBECONFIG=$HOME/.kube/config kubectl -n indramesh scale deployment/indramesh-worker --replicas=<n>
```

If a Kubernetes rollout is not performed for this phase, record it as **pending** in the phase
summary — never as passed.

### Evidence to capture

- `alembic current` shows the drop-only Core revision `307_drop_translation_suggestions` as the head;
- `alembic_version_ai_translation` reads `0002_runs`;
- the plugin row is active with no `database_error`;
- an authenticated
  `GET /api/v1/plugins/ai-translation/data/status?translation_type=object_type&entity_id=<disposable ObjectType id>`
  returns `200` with `{available, open_count}` (a bare call without both parameters is `422` and
  proves nothing);
- the task-type catalog lists `ai-translation.schema-bulk` and `ai-translation.ui-bulk` with
  `catalog_visible=false`;
- the one-shot `cleanup-legacy-translations` JSON report.

Never place credentials, provider text or proposal content in the recorded evidence.

## Read-only Saved-Query Revalidation

`python -m app.maintenance revalidate-saved-queries [--json]` is the read-only operator
tool for the GraphQL query limits: it loads every stored saved query in one statement,
runs the same analyzer the runtime enforces and the save path rejects with, and prints one
row per stored query — `id | name | cost | breached` (with `--json`, the same rows as a
JSON document). `cost` is the same measured value the `X-GraphQL-Cost` header reports and
`breached` names the limit axis (`tokens`, `aliases`, `depth`, `cost`) or `syntax` for a
stored document that cannot be parsed at all.

It never prints or logs the stored query text, never writes, never invalidates a cache and
deliberately does not initialise the cache service — it runs in a DB-only context, e.g.:

```bash
docker compose exec -T backend python -m app.maintenance revalidate-saved-queries
docker compose exec -T backend python -m app.maintenance revalidate-saved-queries --json
```

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Clean: no stored saved query breaches a limit axis. |
| `1` | At least one stored saved query would now be rejected (`breached` names the axis). |
| `2` | The run itself failed (e.g. the database was unreachable). |

The historical cut-over verbs (`clear-queue`, `purge-cache`, `cleanup-legacy-translations`,
`cutover`) keep their fail-closed `0`/`1` contract; `--json` is accepted for every command
and read by `revalidate-saved-queries` only.

## Fernet Key Rotation

`python -m app.maintenance rotate-fernet-key [--check]` finishes a `FERNET_KEY` rotation: it
re-encrypts every stored value still bound to `FERNET_KEY_OLD` under `FERNET_KEY`, then proves in a
fresh read that every stored value decrypts with `FERNET_KEY` alone. `--check` only reports and
never writes.

Every removal step below is conditioned on the rewrite exiting `0` with the verdict
`FERNET_KEY_OLD can be removed`. Keep a private copy of the old key until the final `--check`
passes — until then it is the only thing that can read the remaining ciphertext.

### Docker Compose

1. Generate a new key with the route `.env.example` documents (the project's own backend image, so
   no host Python dependency):

   ```bash
   docker compose build backend
   docker compose run --rm --no-deps --entrypoint python backend \
     -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

2. In `.env`, move the current `FERNET_KEY` value to `FERNET_KEY_OLD` and put the generated key in
   `FERNET_KEY`.
3. Recreate backend and worker:

   ```bash
   docker compose up -d backend worker
   ```

   A `docker compose restart` does not re-read the environment file, so the running processes would
   keep the old keys.
4. Prove what is still bound to the old key — this writes nothing:

   ```bash
   docker compose exec -T backend python -m app.maintenance rotate-fernet-key --check
   ```

5. Rewrite and verify:

   ```bash
   docker compose exec -T backend python -m app.maintenance rotate-fernet-key
   ```

   A safe run ends with `All <n> encrypted values decrypt with FERNET_KEY alone — FERNET_KEY_OLD can
   be removed.`
6. Only after that exit code `0` and verdict, delete `FERNET_KEY_OLD` from `.env`, recreate backend
   and worker again (`docker compose up -d backend worker`) and confirm with a final `--check`: it
   exits `0` and reports `No FERNET_KEY_OLD is configured; all <n> encrypted values decrypt with
   FERNET_KEY.`

### k3s / Helm

On k3s the keys live in the `indramesh-backend-auth` Secret, and a pod only reads a Secret change
when it is started again. The installer reconciles `FERNET_KEY` but does not manage
`FERNET_KEY_OLD` — add and remove that key yourself, and keep `.env` in step with the Secret: the
installer's compare-before-create check compares `.env` against the existing Secret, so a
`FERNET_KEY` that differs there fails the next installer run.

1. Set `FERNET_KEY` to the new key and add `FERNET_KEY_OLD` with the previous value in the
   `indramesh-backend-auth` Secret — for example with
   `KUBECONFIG=$HOME/.kube/config kubectl -n indramesh edit secret indramesh-backend-auth`, which
   keeps both values off the command line — and write the same two values into `.env`.
2. Roll out backend and worker so new pods read the updated Secret:

   ```bash
   KUBECONFIG=$HOME/.kube/config kubectl -n indramesh rollout restart deployment/indramesh-backend deployment/indramesh-worker
   ```

3. Prove what is still bound to the old key — this writes nothing:

   ```bash
   KUBECONFIG=$HOME/.kube/config kubectl -n indramesh exec deploy/indramesh-backend -- python -m app.maintenance rotate-fernet-key --check
   ```

4. Rewrite and verify:

   ```bash
   KUBECONFIG=$HOME/.kube/config kubectl -n indramesh exec deploy/indramesh-backend -- python -m app.maintenance rotate-fernet-key
   ```

5. Only after that exit code `0` and verdict, remove the key from the Secret — a JSON patch that
   carries no secret value on the command line:

   ```bash
   KUBECONFIG=$HOME/.kube/config kubectl -n indramesh patch secret indramesh-backend-auth --type json -p '[{"op":"remove","path":"/data/FERNET_KEY_OLD"}]'
   ```

   Remove the value from `.env` too, roll out backend and worker again and confirm with a final
   `--check` (`0`, no `FERNET_KEY_OLD` configured).

Rerunning the installer is not a rotation: it never changes an existing Secret value.

Exit codes:

| Code | Meaning |
|---|---|
| `0` | Safe: every stored value decrypts with `FERNET_KEY` alone and no foreign ciphertext was found — `FERNET_KEY_OLD` can be removed. |
| `1` | Not safe: the report names each location that still needs the old key (and any `plugin_*` column holding foreign ciphertext). `FERNET_KEY_OLD` must stay configured. |
| `2` | The run itself failed and was rolled back — for example the database was unreachable. Nothing was changed. |

The pass covers the ServiceAccount bearer-token columns (`source_bearer_token` and
`target_bearer_token`), which is also where the Jira plugin's connection credentials live, and every
secret field in `plugins.config`, including nested object and list values. It then sweeps the
`plugin_*` tables for token-shaped ciphertext that Core does not model; a hit is reported by table
and column and blocks the verdict instead of being rewritten. Values without the `v1:` version
prefix are counted and left alone, and the report carries only counts and locations — never a
plaintext, a stored value or a token fragment.

`--check` is accepted for every maintenance command and read by `rotate-fernet-key` only.

## Upgrading to a New Release

An upgrade from one 1.x release to a later one (for example `v1.0.0` → `v1.0.1`) is a source
update followed by a rebuild: the images are built locally from the checked-out release, so there
is no image to pull. Read the release notes first — they name any additional step a release needs.
Day-to-day operator procedures — backup and restore, the short upgrade checklist, and the
production-hardening checklist — are collected in [OPERATIONS.md](./OPERATIONS.md).

### Docker Compose

1. Back up the database, the `storage_data` volume and `.env` (see
   [Rollback Procedure](#rollback-procedure) for what each piece restores). A schema migration has
   no automatic downgrade, so the backup is the only way back.
2. Check out the new release in the existing clone. Untracked files — `.env` and plugin
   directories installed under `plugins/` — are left alone:

   ```bash
   git fetch --tags
   git checkout v1.0.1
   ```

3. Re-run the installer. On an existing installation it overwrites nothing in `.env` and fills only
   values that are empty or still hold the `CHANGE_ME` placeholder. A variable the new release
   **renamed or added** is *absent* from the old file rather than empty, and the fill cannot create
   it: the installer stops with the offending key named — for example
   `VALKEY_PASSWORD is missing or empty in .env after the fill … set it manually and rerun`. Set the
   named key in `.env` by hand — keeping every existing value, because `JWT_SECRET_KEY` and
   `FERNET_KEY` must survive or the values already encrypted in the database become unreadable — and
   run the installer again. It rebuilds every locally built image (including the database and search
   images), recreates the changed services and waits until the backend and nginx answer:

   ```bash
   ./install_indramesh_docker.sh
   ```

   The backend applies the database migrations itself when it starts (`alembic upgrade head`, then
   the plugin migrations — see [Build Pipeline](#build-pipeline)); there is no separate migration
   step.
4. Restart nginx so it resolves the recreated containers' new addresses:

   ```bash
   docker compose restart nginx
   ```

5. Log in and check that the application works. If a release note says the search
   index changed, run the full reindex (`POST /api/v1/search/reindex/all`, or the reindex task in
   the UI).

### k3s

1. Back up first: trigger a fresh dump from the `indramesh-backup` CronJob (or rely on the nightly
   one) and keep `.env` safe — see [Rollback Procedure](#rollback-procedure).
2. Check out the new release in the existing clone (`git fetch --tags && git checkout v1.0.1`).
3. Reconcile and redeploy:

   ```bash
   ./install_indramesh_k8s.sh && ./rebuild_indramesh_k8s.sh
   ```

   The installer validates `.env` against the new release (it stops and names any key the release
   added, so you can set it and rerun), reconciles the Secrets and the stateful services, and builds
   and imports the release's images. The rebuild performs one Helm upgrade with the new image tags;
   the chart's pre-upgrade Job runs the Core migrations and the post-upgrade Job runs the plugin
   migrations.
4. Check the rollout with `KUBECONFIG=$HOME/.kube/config kubectl get pods -n indramesh` and log in.

Installed plugins are versioned separately: update a plugin from its own repository with
`install_plugin_docker.sh` or `install_plugin_k8s.sh` (see the plugin's README) when its release
notes say it needs the new Core release.

**A re-install over an existing namespace keeps its secrets.** If the checkout's `.env` carries a
different value for a generated credential than the cluster's Secret, the installer stops in
preflight and names the Secret and key, for example:

```
[preflight] ERROR: secret 'indramesh-database' key 'POSTGRES_PASSWORD' differs from .env —
rotation requires an explicit operator action; a rerun never overwrites it (D-07)
```

This is deliberate: a rerun must never replace the password of a database that already holds data.
Two ways forward, and they are not equivalent:

- **Keep the data** — reuse the `.env` that belongs to this installation (the same file the first
  install generated). The installer then finds matching values and proceeds.
- **Start fresh** — remove the namespace with its volumes, then install again. Every stored object,
  plugin database and uploaded file is destroyed by this, so back up first:

  ```bash
  helm -n indramesh uninstall indramesh
  KUBECONFIG=$HOME/.kube/config kubectl delete namespace indramesh
  KUBECONFIG=$HOME/.kube/config kubectl get pvc -A | grep indramesh   # must print nothing
  ./install_indramesh_k8s.sh
  ```

## Build Pipeline

No repository CI/CD workflow is configured: there is no `.github/workflows/`
directory. No CI/CD pipeline detected. Builds and deployments are run
from an operator host.

### Docker Compose

```bash
# Build all images and start the default services
docker compose up -d --build

# Build one service image
docker compose build backend

# Rebuild and recreate selected services
./rebuild_indramesh_docker.sh backend
./rebuild_indramesh_docker.sh worker
./rebuild_indramesh_docker.sh frontend --no-cache
```

The rebuild wrapper restarts Nginx after a backend or frontend rebuild so its
upstreams resolve the replacement container addresses.

Database migrations run before the Compose backend starts through
`backend/start.sh` in this order:

1. `alembic upgrade head` (Core migration; fatal on failure).
2. `python -m app.plugin_system.bootstrap_role` (configured role/bootstrap failures are fatal).
3. `python -m app.plugin_system.migrate` (plugin-specific failures isolate that plugin).
4. Uvicorn starts; the worker does not run migrations.

Do not use the generic build/start commands for a database still requiring the
destructive cut-over: follow the quiesce-first runbook above instead.

### k3s

```bash
# Bootstrap or reconcile the cluster and Helm release
./install_indramesh_k8s.sh

# Build, import, and redeploy selected local images
./rebuild_indramesh_k8s.sh backend
./rebuild_indramesh_k8s.sh frontend
./rebuild_indramesh_k8s.sh nginx
```

The installer validates the root `.env`, reconciles Kubernetes Secrets, installs
or reuses k3s, provisions supporting components, imports required local images,
and applies the Helm chart. The rebuild command builds local images, imports them
into k3s containerd, then performs one Helm reconciliation and waits for affected
rollouts. Selecting `backend` updates both backend and worker because they share
an image.

For the k3s path, database migrations are owned by the chart's pre-upgrade Job;
the backend Deployment runs Uvicorn directly. The installer first applies a
stateful-only release, then a full upgrade: the Core pre-upgrade hook therefore
also runs on this bootstrap path. A bare first `helm install` does not trigger
that Core hook. Plugin migrations run in a separate post-install/post-upgrade
Job, after ordinary workloads are Ready. A plain pod restart runs neither Job.

The rebuild script preserves unselected image tags and most deployed non-image
values, validates Nginx in-cluster when selected, and removes obsolete
tool-owned images only after successful rollout checks. Two non-image values
are exceptions, written into the right-most values file under `--reset-values`:
`apps.environment` is re-derived from the host `.env` `ENVIRONMENT` (defaulting
to `production`), and `apps.maintenance.cutover` is set from that run's
`--cutover` flag. A stored override of either is therefore replaced by the
running invocation's value. It does not copy host plugin files
onto the plugin PVC or reconcile changed Secrets.

## Environment Setup

Use [CONFIGURATION.md](./CONFIGURATION.md) as the full variable reference. Start
from the tracked template and keep the resulting file local:

```bash
cp .env.example .env
```

Remove or comment the template's bare rebuild command lines before use; they are
not dotenv assignments. Never source `.env` as a shell script, commit it, or
reuse example credentials in production.

An installation created before the current durable-state layout must be rebuilt rather than upgraded
in place: there is no supported in-place upgrade for that layout, and the Kubernetes path is a
reinstall rather than an upgrade. The procedure for upgrading a current installation is in
[OPERATIONS.md § Upgrade](./OPERATIONS.md#upgrade).

Before running the application, configure at least these application-required
values with deployment-specific secrets and trusted network values:

- `JWT_SECRET_KEY`
- `FERNET_KEY`
- `CORS_ALLOWED_ORIGINS`
- `NGINX_TRUSTED_IP`

For Compose, also provide the PostgreSQL, Valkey, OpenSearch, and port values
used by `docker-compose.yml`. Compose injects its internal service addresses and
credentials into the backend and worker.

For k3s, the installer checks that every key in `.env.example` is present in
`.env`, then materializes sensitive values into Kubernetes Secrets. The chart
reads non-secret deployment settings from `values.yaml`; use a private Helm
values override for deployment-specific settings such as CORS origins and the
trusted Nginx source range. Do not put secrets in Helm values files.

Secret reconciliation in `scripts/k8s/lib/secrets.sh` is compare-before-create:
existing matching Secrets are untouched; missing keys or different values fail
and require explicit operator action. Rerunning the installer is not rotation.
Provide `ADMIN_PASSWORD` for Kubernetes initial admin creation. The chart also
references `LLM_API_KEY` from its external-API Secret even when the application
does not need an LLM client. Keep `FERNET_KEY` with recoverable backups: changing
it without re-encrypting stored values makes existing ciphertext unreadable. See
[Fernet Key Rotation](#fernet-key-rotation) for the only safe way to change it.

### Restricted plugin migrations

Configure `PLUGIN_DB_USER` and `PLUGIN_DB_PASSWORD` for Compose. In Helm, set the
non-secret `plugins.dbUser` and add `PLUGIN_DB_PASSWORD` to the
`indramesh-database` Secret. A password in that Secret alone does not activate
the boundary: both migration Jobs gate their credential injection on
`plugins.dbUser`.

The bootstrap role owns no Core objects and has only `SELECT`/`REFERENCES` on
Core tables plus permission to create plugin tables in `public`. Invalid
credentials, GRANT failures, or privilege drift abort deployment. With both
credentials absent, the runner warns and permits a privileged fallback only
for plugins that do not require the restricted role. The shipped Jira plugin
requires it and is refused without it. This isolates migration revisions,
**not runtime plugin code**, which uses the application database connection.
See [CONFIGURATION.md § Plugin migration-role requirements](./CONFIGURATION.md#plugin-migration-role-requirements).

### Plugin files, configuration, and lifecycle

- Compose mounts `./plugins` read-only into backend and worker at
  `/opt/indramesh/plugins`. Discovery happens at process startup, not via a live
  filesystem watcher; restart the relevant processes after plugin installation.
- Helm mounts `plugins-storage` read-only into backend, worker, and the plugin
  migration Job. It is a `ReadWriteOnce` local-path PVC; multi-node deployment
  requires a reviewed shared-storage/scheduling design. Core and maintenance
  Jobs do not mount it.
- Both stacks install a plugin with one command from the Core checkout root, from a public
  repository URL or from a local directory; a URL takes an optional ref (tag or branch) as a
  further argument, and the destination directory name comes from the plugin's own manifest:

  ```bash
  ./install_plugin_docker.sh https://github.com/weboliver/indramesh-plugin-graphql-studio
  ./install_plugin_k8s.sh https://github.com/weboliver/indramesh-plugin-graphql-studio
  ```

  Each plugin README states the Core release line it requires and shows the same two commands.
  Neither entry point overwrites an existing destination: replacing an installed plugin means
  removing its directory deliberately first and installing again.
- `install_plugin_k8s.sh` wraps `scripts/k8s/install_plugin.sh`: it copies a new
  plugin through a disposable writable pod, refuses to overwrite an existing
  directory, runs the runner in the live backend, then restarts backend/worker.
  **Restricted-role caveat:** the shipped backend Deployment does not inject
  plugin migration credentials. The helper's live-backend runner is therefore
  not equivalent to the credentialed Helm plugin Job for the Jira plugin.
  Use the migration Job with its configured role before admitting that plugin;
  do not weaken its manifest to work around missing credentials.
- Operator configuration is stored in PostgreSQL `plugins.config`, not the
  read-only manifest. `GET`/`PUT /api/v1/plugins/{name}/config` requires
  `plugins:manage`; fields marked `format = "password"` are Fernet-encrypted
  individually with `FERNET_KEY`, and reads mask them. See
  [CONFIGURATION.md](./CONFIGURATION.md#plugin-configuration) for update semantics.
- `PUT /api/v1/plugins/{name}/state` accepts `active` or `disabled` and requires
  `plugins:manage`. State is persisted and pushed to the cross-process cache. Disablement is not an uninstall or a schema rollback; plugin data is
  retained. Verify backend and worker plugin versions after an update,
  especially when `requires_worker_update` is true (as in the Jira manifest).
- Plugin-specific migration failures can leave the Helm Job successful while
  the plugin remains unavailable. Check persisted database diagnostics and
  plugin health, not just Helm status. Successful hook Jobs are deleted by
  `hook-succeeded`, so capture logs before deletion or use log aggregation.

## Transport Security and Networking

In Compose, Nginx listens on `${HTTP_PORT:-8180}` and `${HTTPS_PORT:-8443}`.
Its configuration redirects HTTP to HTTPS and reads `server.crt` and `server.key`
from the repository-mounted `nginx/certs/` directory. `TLS_CERT_DIR` in
`.env.example` is not consumed by `docker-compose.yml`; changing the certificate
location requires changing the Compose mount.

The k3s chart routes every host name (`localhost`, the host IP) through Traefik to the Nginx
service.
HTTPS is passed through to Nginx, while HTTP is routed to its redirect listener.
The Nginx workload mounts the cert-manager-managed TLS Secret named by
`apps.nginx.tlsSecretName`, whose default is `indramesh-local-tls`.

## Rollback Procedure

> **The legacy Jira Core drop is one-way.** Once that cut-over has run, the legacy Core
> Jira data and schema are gone. There is **no rollback to old images** and no
> schema downgrade — see [Legacy Jira Core Cut-over](#legacy-jira-core-cut-over-one-way).
> Previous-image/Helm rollback below is only for schema-compatible releases and
> must never cross the destructive drop. Forward correction uses final-schema-compatible code.

### Docker Compose

1. Verify Core and plugin migration compatibility **before** restarting older
   code. There is no automatic downgrade; the destructive Core revision's
   `downgrade()` explicitly raises `RuntimeError`. Do not assume that rebuilding
   an old image can restore deleted data or plugin schema state.
2. Check out a confirmed schema-compatible application revision.
3. Rebuild and recreate the affected services:

   ```bash
   docker compose up -d --build backend worker frontend
   ```

4. Restore stateful data only from a backup known to be appropriate for the
   rollback. Compose persists database, Valkey, OpenSearch, Jira, and storage
   data in the named volumes listed above (`postgres18_data`, `valkey_data`,
   `opensearch_data`, `storage_data`), but no Compose backup automation is defined.

### k3s

1. Inspect the release history:

   ```bash
   KUBECONFIG=$HOME/.kube/config helm history indramesh -n indramesh
   ```

2. Roll back to a confirmed revision:

   ```bash
   KUBECONFIG=$HOME/.kube/config helm rollback indramesh <revision> -n indramesh
   ```

3. Check the release and affected workloads:

   ```bash
   KUBECONFIG=$HOME/.kube/config kubectl get pods -n indramesh
   ```

The chart does not define an automatic Alembic downgrade. Confirm migration
compatibility and the intended recovery procedure before rolling back a release.
Helm rollback restores release resources, not PostgreSQL contents or plugin files
on the retained PVC. Do not roll back across the destructive Core revision.

The k3s chart schedules PostgreSQL backups daily at `02:00` by default. Dumps are
written to the retained `postgres-backups` PVC. The repository provides
`scripts/k8s/verify_backup_restore.sh` to verify a fresh backup against a
scratch database without replacing the application database.
Retention defaults are 30 days or 14 dumps, retaining the most recent complete
dump. The CronJob uses `0 2 * * *` without an explicit `timeZone` field: verify
controller timezone before treating this as a guaranteed UTC schedule. Database
dumps are not a backup of storage blobs, plugin files, encryption keys, or the
whole deployment.
These are the pieces of state the operator runbook describes, and that document is the procedure
to follow when a restore is actually needed: [OPERATIONS.md](./OPERATIONS.md) carries the Compose
backup and restore commands, the `FERNET_KEY` requirement, both upgrade paths and the hardening
checklist.

## Monitoring

### Docker Compose

Compose health checks cover PostgreSQL, Valkey, Jira, OpenSearch, backend,
and the storage service. The backend exposes `GET /health`; Nginx waits for the
backend health check before starting. No repository configuration integrates the
Compose stack with Sentry, Datadog, New Relic, or OpenTelemetry.

`GET /health` returns a static healthy response; it is not an end-to-end
database, queue, or plugin probe. `GET /api/v1/plugins/{name}/health` requires
`plugins:read` and runs the loaded plugin's health check (`404` if not loaded).
Check worker/plugin readiness separately before admitting work.

System log access is provided by the `log-explorer` plugin (Administration →
System-Logs) under the `system-logs:read` permission. The plugin queries the
bundled Loki stack server-side; the backend no longer reads the Docker socket
or the Kubernetes API. See `docs/LOGGING-INFRASTRUCTURE.md` for the log stack
itself; this access path is not log retention.

### k3s

The backend exposes Prometheus-format metrics at `GET /metrics`. When
`monitoring.enabled` and `apps.enabled` are true, the chart creates a
`ServiceMonitor` that scrapes `backend:8000/metrics` every 30 seconds.

The installer provisions `kube-prometheus-stack` in the `monitoring` namespace.
The chart includes an IndraMesh dashboard ConfigMap. Prometheus uses ephemeral
storage with a seven-day retention configuration, so it is not a historical
metrics archive. Grafana is proxied at the configured local route
`https://localhost/grafana/`.

