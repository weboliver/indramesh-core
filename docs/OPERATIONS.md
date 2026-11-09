# Operations

This is the operator runbook for an IndraMesh Core installation: how to **back up and restore** a
stack, how to **upgrade** it to a new release, and how to **harden** a self-hosted deployment.

It applies to the two installation models IndraMesh Core 1.0 supports — the Docker Compose stack
and a single-node k3s deployment — and states the commands for each model separately. For what
each model is, how it is installed, and how its services are laid out, see
[DEPLOYMENT.md](./DEPLOYMENT.md).

- [Backup and Restore](#backup-and-restore)
- [Upgrade](#upgrade)
- [Production Hardening](#production-hardening)

## Backup and Restore

A complete backup of an IndraMesh Core installation is **three pieces of state, taken together**:

| Piece | What it holds | Where it is taken from |
|---|---|---|
| A database archive | Every schema, object type, object and value — including the plugin tables (`plugin_*`), because the plugins store their tables in the same application database | `pg_dump -Fc` |
| An archive of the uploaded-file volume | Uploaded import/export files and object attachments | the `storage_data` volume (Compose), the storage claim (k3s) |
| The environment file | Every credential and key the stack needs, including the `FERNET_KEY` | `.env`, kept outside the stack |

The dump and the file archive belong together: a database restored without its files yields rows
that point at file storage which is no longer there.

The search index and the cache are deliberately **not** part of a backup. They are derived state —
every document and every cached value can be rebuilt from the database — and a restore therefore
ends by rebuilding them (see [After a restore](#after-a-restore)).

### Docker Compose

There is no backup script to install: the commands below *are* the procedure (a wrapper script
would add its own flags to the operator interface, and the flags would then be a contract). Run
them from the repository root, against the stack the `.env` file in that checkout describes.

#### Back up

```bash
TS="$(date -u +%Y%m%dT%H%M%SZ)"

# The archives belong OUTSIDE the checkout. A `backups/` directory inside one is
# not gitignored, and the environment-file copy in step 4 carries every
# credential and key the stack has — one `git add -A` away from the repository's
# history. Set the variable to where the archives belong and keep them there.
BACKUP_DIR="${BACKUP_DIR:-$HOME/indramesh-backups}"
mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# 1. Dump the application database in PostgreSQL's custom format, streaming the
#    archive out of the running database container onto the host.
docker compose exec -T postgres \
  sh -c 'pg_dump -Fc -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  > "$BACKUP_DIR/indramesh-${TS}.dump"

# 2. Read the archive's table of contents back before trusting it.
docker compose exec -T postgres pg_restore --list \
  < "$BACKUP_DIR/indramesh-${TS}.dump" > /dev/null

# 3. Archive the uploaded-file volume, from the running storage container.
docker compose exec -T storage tar czf - -C /data storage \
  > "$BACKUP_DIR/storage-${TS}.tar.gz"

# 4. Copy the environment file to the same secure location as the two archives.
#    It is the third piece of state and cannot be reconstructed.
install -m 600 .env "$BACKUP_DIR/env-${TS}"
```

**Step 2 is what makes step 1 a backup.** A write can fail halfway or be interrupted, and the
resulting file has the right name while restoring nothing. `pg_dump`'s own exit status and a
successful listing of the archive's table of contents are two independent signals, and only both
together justify deleting an older archive. The scheduled Kubernetes backup follows the same
order for the same reason.

**The environment file is the piece with no recovery path.** `.env` carries the database, cache
and search credentials, the JWT signing key and the `FERNET_KEY`. `FERNET_KEY` is the symmetric
key that encrypts stored ServiceAccount bearer tokens and plugin configuration secrets on their
way into the database. If `FERNET_KEY` is lost, that ciphertext is **unrecoverable** — it is
present in the restored database and nothing can decrypt it again. A database-only restore is
therefore an incomplete restore, not a degraded one. Store the file with the archives, in the
same place and under the same access rules, and never commit it.

Changing `FERNET_KEY` is a rotation, not an edit:
[Fernet Key Rotation](./DEPLOYMENT.md#fernet-key-rotation) in DEPLOYMENT.md is the only safe way to
do it, and it removes the old key only after `python -m app.maintenance rotate-fernet-key` exits `0`.

#### Restore

Run the steps in this order. The order matters because the backend migrates and seeds an empty
database when it starts (`alembic upgrade head`, the plugin bootstrap and the initial
administrator account): starting the whole stack first leaves a database that the restore then
collides with.

```bash
# 0. Stop every service. Nothing may write to the database or the file volume
#    during the restore. Stop — never `docker compose down`, which removes the
#    containers and can take the named volumes with it.
docker compose stop

# 1. Start ONLY the database service, and wait until it is healthy.
#    A bare `docker compose up -d postgres` returns as soon as the container is
#    created — on a fresh volume that is before PostgreSQL has finished
#    initializing, and the next step then fails on a missing
#    /var/run/postgresql/.s.PGSQL.5432 socket. `--wait` (placed after the
#    service name so the single-service form stays readable) blocks until the
#    postgres service's own healthcheck, `pg_isready`, passes.
docker compose up -d postgres --wait

# 2. Recreate the application database empty, so the archive goes into a database
#    the application has not touched.
docker compose exec -T postgres \
  sh -c 'dropdb -U "$POSTGRES_USER" --if-exists "$POSTGRES_DB" && createdb -U "$POSTGRES_USER" "$POSTGRES_DB"'

# 3. Restore the archive into it.
docker compose exec -T postgres \
  sh -c 'pg_restore -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < "$BACKUP_DIR/indramesh-<timestamp>.dump"

# 4. Restore the uploaded files while the storage service is NOT running — the
#    one-off container below reads the same volume the service mounts.
docker compose run --rm --no-deps -T --entrypoint sh storage \
  -c 'tar xzf - -C /data' < "$BACKUP_DIR/storage-<timestamp>.tar.gz"

# 5. Start the rest of the stack. The start-up migration is a no-op against the
#    restored schema.
docker compose up -d
```

Step 2 is destructive: it deletes the current application database. It is the point of a restore,
but it is also the step to skip if what you actually need is the current data.

`pg_restore`'s exit status is not on its own the proof that a restore succeeded — and not on its own
the proof that one failed. Its default mode exits non-zero on warnings alone, and an archive taken
from a stack whose plugins had run carries statements that fail against any database the
application has not started on yet: `ALTER TABLE ... OWNER TO <plugin role>` for the `plugin_*`
tables, because the backend creates that role when it starts (which is *after* this restore) and it
does not exist while the archive is being loaded. Read its output and expect those ownership
errors to be the only ones; then verify the result — a successful login with an account that exists
only in the archive, a known object being present, and a previously uploaded file downloading
again.

#### After a restore

The search index and the cache come up empty, because they are derived state and are rebuilt
rather than restored. Rebuild the search index once the stack is healthy by triggering the
reindex job — `reindex_all_job` in the worker — through either of the documented entry points:

- `POST /api/v1/search/reindex/all`, with an authenticated session that holds the
  `search:trigger` permission, or
- the reindex task in the task UI.

Until it has run, search answers from an index that does not match the restored data. The cache
needs no equivalent step: it refills from normal use.

### k3s

The single-node k3s deployment keeps the same three pieces of state, and the chart already
automates the database one.

- **The `indramesh-backup` CronJob** dumps the application database with `pg_dump -Fc` daily at
  `02:00` by default (the `backup.schedule` Helm value), writing a UTC-timestamped custom-format
  archive named `indramesh-<YYYYMMDDTHHMMSSZ>.dump` to the dedicated `postgres-backups` PVC.
  Each run writes the archive to a temporary name, verifies it with `pg_restore --list`, and only
  then renames it to its final name, so an interrupted run never leaves a corrupt file where a
  restore would find it. Retention removes archives older than 30 days or beyond the newest 14,
  whichever bound is exceeded first, and never removes the newest complete archive, and it runs
  only after a successful dump in the same job. `concurrencyPolicy: Forbid` with a bounded
  `startingDeadlineSeconds` keeps runs from overlapping or piling up after downtime.
- **`scripts/k8s/verify_backup_restore.sh`** is the repeatable, non-destructive proof that an
  archive actually restores. Run it from the repository root:

  ```bash
  bash scripts/k8s/verify_backup_restore.sh
  ```

  It creates a disposable marker row, triggers a fresh dump through the same CronJob so the
  marker is captured, and then restores the newest archive into a **brand-new
  `indramesh_restore_check` database on the same running PostgreSQL instance**. The live
  application database is never the restore target, and the application pods are never restarted.
  It verifies the restore by comparing the marker row, the public-schema table count and the
  presence of the core tables (`sources`, `schemas`, `object_types`, `objects`) between the source
  and the restored database — never by a bare `pg_restore` exit code. On success it drops
  `indramesh_restore_check` and prints `RESTORE VERIFICATION: PASS`; on a mismatch it keeps that
  database for diagnosis. A rerun is refused while an `indramesh_restore_check` database from an
  earlier failed run still exists: inspect and drop it deliberately first.

The CronJob archives the database only. Archive the storage claim that holds the uploaded files
(`data-storage-0`) and store `.env` — including `FERNET_KEY` — alongside it, or a Kubernetes
recovery is incomplete for exactly the reason the Compose one would be.

A Kubernetes recovery follows the same order rule as the Compose one: the archive goes into a
fresh, empty database **before** the application tier starts, because the application migrates and
seeds an empty database at start-up. Scale the application tier to zero, keep it there for the
whole restore, and rebuild the search index afterwards as described in
[After a restore](#after-a-restore).

Once the archive is restored and the application tier is up, run the application-role step once on
the backend Deployment:

```bash
KUBECONFIG=$HOME/.kube/config kubectl -n indramesh exec deploy/indramesh-backend -- python -m app.db.roles
```

An archive taken on 1.0.0 carries neither the application role's grants nor the keygen functions,
and scaling the application tier up runs no Job, so nothing else applies them. Compose needs no
extra step: `backend/start.sh` runs the same step on every start.

## Upgrade

An upgrade from one 1.x release to a later one is a **source update followed by a rebuild**: the
images are built locally from the checked-out release, so there is nothing to pull. Read the
release notes first — they name anything a particular release adds to the procedure.

The full step-by-step procedure for both installation models lives in
[DEPLOYMENT.md § Upgrading to a New Release](./DEPLOYMENT.md#upgrading-to-a-new-release). This
section is the short version; where the two differ, that section and its installer are the
authority.

### Upgrading from 1.0.0

1.0.1 hardens the session, the signing key, the connector's outbound target and the runtime
database identity, so five changes are visible to an operator after the rebuild. Each one is
deliberate; none needs a manual migration step.

- **Every user signs in once more.** 1.0.0 issued 30-day access tokens; a token whose `exp - iat`
  exceeds the new 15-minute lifetime is refused, so the first request after the upgrade lands on the
  login screen. The interface also removes the old `localStorage` token entry once, on the first
  load.
- **A script that logs in through `/auth/login` must re-login or refresh within 15 minutes.** The
  access token now lives 15 minutes; `POST /api/v1/auth/refresh` returns a fresh one without a new
  password. `imsh_` API-client bearer tokens — the service-account clients of the public API — are
  unchanged and need no adjustment.
- **An existing Jira Source or Target at an internal address stops syncing.** Core now refuses a URL
  that resolves to a non-public address, both when it is saved and at every pull/push. A superuser
  re-enables such an endpoint by setting `allow_internal_target` to the literal JSON boolean `true`
  in that Source's or Target's `config`; see
  [CONFIGURATION.md § Internal connector targets](./CONFIGURATION.md#internal-connector-targets).
  There is no global switch.
- **Avatar images are no longer shown.** The interface renders initials instead, so a viewer's
  browser never contacts the host named in a profile's `avatar_url`. Stored values are untouched.
- **The runtime database identity is split from the administrator's.** Backend and worker run their
  normal queries as a read/write application role without DDL or superuser rights. The installer
  rerun adds `DB_APP_USER` and a generated `DB_APP_PASSWORD` to `.env` (Compose) or to the
  `indramesh-database` Secret (k3s), and the deploy chain creates the application role with its
  grants and the two keygen functions. `DB_USER`/`DB_PASSWORD` are untouched: the administrator
  keeps `docker compose exec postgres psql` and `kubectl exec` access.

**Known limitation of the identity split.** On Compose the backend and worker still read
`DB_USER`/`DB_PASSWORD`, because `backend/start.sh` runs the migrations. On k3s the backend pod keeps
them because the plugin install runs `kubectl exec … app.plugin_system.migrate` there. The change
closes the SQL-injection path out of the application process, not a shell inside those containers,
and row deletion through the application role remains possible — that is what backups are for. A
plugin that needs a schema change declares it in a migration; plugin code running in the backend or
worker can no longer run DDL through `PluginContext.db()`.

**First start after the upgrade.** The deploy step removes translations whose entity no longer
exists and logs the number removed. The worker removes search indices that belong to no schema when
it starts. Neither needs any action, and a failure in either is a logged warning that does not stop
the start. Select option translations are skipped in a run where attribute definitions cannot be
read completely. These rows are removed without an audit entry.

### Docker Compose

1. **Back up first.** Take the database archive, the file archive and the environment file as
   described in [Backup and Restore](#backup-and-restore). A release can migrate the schema, and a
   schema migration has no automatic downgrade.
2. **Check out the new release** in the existing clone (`git fetch --tags && git checkout <tag>`).
   Untracked files — `.env`, and any plugin installed under `plugins/` — are left alone.
3. **Rebuild and recreate the changed services** by re-running the installer,
   `./install_indramesh_docker.sh`. It overwrites nothing in `.env` and fills only empty or
   `CHANGE_ME` values; a variable the release **renamed or added** is absent rather than empty, so
   the installer stops and names it. Set that key in `.env` by hand — never delete `.env` to force a
   fresh one, because that regenerates `JWT_SECRET_KEY` and `FERNET_KEY` and makes the values
   already encrypted in the database unreadable — and run it again. Rotating `JWT_SECRET_KEY` also
   signs everyone out: every access token and every stored refresh cookie becomes invalid, so all
   browser sessions end at once. It rebuilds every locally built
   image (including the database and search images) and recreates the changed services. For a
   narrower change, `./rebuild_indramesh_docker.sh` or `docker compose up -d --build` rebuilds and
   recreates the selected services only.
   Nginx re-resolves `backend` and `frontend` at runtime, so no manual restart or reload is needed
   after a rebuild: a recreated container is picked up within about ten seconds, and a 502 during
   that window clears by itself.
4. **The migrations run themselves.** The backend applies the whole chain when it starts —
   `alembic upgrade head` first, then the plugin migrations. Compose has no separate migration
   step and no migration Job; nothing else has to be run for the schema to be current.
5. **Verify** by logging in and checking a known object. If the release notes say the search index
   changed, rebuild it as described in [After a restore](#after-a-restore).

**A rollback across a migration is a restore, not a downgrade.** No release ships an automatic
schema downgrade, so going back to a pre-upgrade release means restoring the archive taken in
step 1 — see [Backup and Restore](#backup-and-restore) — not pointing the stack at the older image
again.

### k3s

1. **Back up first** — a fresh dump from the `indramesh-backup` CronJob, or the nightly one, plus
   the environment file, as described in [Backup and Restore](#backup-and-restore).
2. **Check out the new release** in the existing clone.
3. **Reconcile and redeploy** with `./install_indramesh_k8s.sh && ./rebuild_indramesh_k8s.sh`. The
   installer validates `.env` against the new release and stops with a named key when the release
   added one, then reconciles the Secrets and the stateful services; the rebuild performs one Helm
   upgrade carrying the new image tags.
4. **The chart runs the migrations as Jobs**, which is the opposite of the Compose path: a
   pre-upgrade Job runs the Core migrations and a post-upgrade Job runs the plugin migrations. A
   plain pod restart runs neither Job, so a restart is not an upgrade.
5. **A release that changed the durable-state wiring is a reinstall, not an upgrade.** Where a
   release replaced or retired a stateful service's storage, the installation is rebuilt rather
   than upgraded in place, and the Kubernetes path is a reinstall rather than an in-place upgrade
   — the same boundary [DEPLOYMENT.md § Environment Setup](./DEPLOYMENT.md#environment-setup)
   states for a pre-existing installation.
6. **Check the rollout** with `KUBECONFIG=$HOME/.kube/config kubectl get pods -n indramesh` and log
   in.

Installed plugins are versioned separately from Core. Update one by re-running its install command —
`install_plugin_docker.sh` or `install_plugin_k8s.sh` — with the new release when its release notes
say it needs the new Core release; [DEPLOYMENT.md § Updating a Plugin](./DEPLOYMENT.md#updating-a-plugin)
describes the version checks the installer applies.

## Production Hardening

A self-hosted installation owns its own perimeter. The items below are the ones that matter before
the stack is reachable by anyone outside the host; none of them replaces keeping the host itself
patched, and none of them is specific to one installation model unless it says so.

- **Keep the data services on the internal network.** In Compose every stateful service publishes
  its port on `127.0.0.1` only, and nginx is the single externally reachable entry point — leave
  those mappings alone and never publish a data port on `0.0.0.0`. In k3s the stateful tier is
  admitted only by its own network policies, so treat those policies as part of the deployment
  rather than as an optional hardening step.
- **Terminate TLS at the perimeter.** The bundled nginx redirects HTTP to HTTPS and serves the
  certificate from the repository-mounted `nginx/certs/` directory in Compose, or from the
  cert-manager-managed Secret named by the chart on k3s. Replace the self-signed certificate with a
  trusted one before the stack is exposed beyond the host.
- **Restrict the administrative surface.** Administration permissions are the ones that change
  configuration and credentials; grant them to the accounts that need them and no others, and keep
  the initial administrator account's password out of the repository.
- **Run the stack as its non-root service users.** The application containers run as an
  unprivileged user, and the stateful services keep their own fixed identities. Do not add
  `user: root` or a privileged flag to a service that does not already carry one.
- **Manage secrets through the environment file, never a committed file.** The credentials live in
  `.env`, which is gitignored and stays that way; a secret that reaches a tracked file has left the
  trust boundary even if it is removed in a later commit. Rotating a credential means changing it
  in `.env` and recreating the service that reads it — rerunning the installer is not a rotation.

The generated API documentation is exposed by the application itself, and whether that is
acceptable is the operator's call. `/docs`, `/redoc` and `/openapi.json` are served by the backend
at its framework defaults, and the bundled nginx does **not** route any of them: it forwards only
the login, upload, `/api/` and `/public/` paths to the backend and serves the frontend on `/`, so
none of the three is reachable through the front door. What is exposed is the backend port. The
Compose stack publishes it on loopback (`127.0.0.1:${BACKEND_PORT:-8000}`), which makes the three
paths reachable without authentication from the host, and on a single-node k3s installation they
are reachable in-cluster. The measure is therefore operational: keep `BACKEND_PORT` bound to
`127.0.0.1` and never to `0.0.0.0`, restrict who can log in on the host, and if you place your own
reverse proxy or load balancer in front of the backend and forward *all* paths to it, block these
three paths there. There is **no built-in toggle** to disable the documentation surface in this
release — the control is the network reachability of the backend port and whatever sits in front
of it, not a configuration value.

## Known Performance Characteristics

Properties an operator should know about before sizing an installation. They are correctness-neutral:
the visible result set is identical in every case below.

- **The record-level filter costs roughly one pass over a principal's DENY set per statement.** A
  restricted object list, its total count, an AQL query and its count each decide every **cascade**
  permission level once, against an uncorrelated set of denied record ids. That is the corrected
  behaviour (before 1.0.1 the set was re-read once per candidate row), but it means the cost follows the
  **number of DENY rows the principal's roles add up to**, not the number of rows returned. Measured on
  1.0.1, 50 000 objects, database time only: 500 object DENYs → 19.55 ms page / 12.21 ms count / 26.34 ms
  AQL page / 14.80 ms AQL count, i.e. 1.4×–1.9× a superuser's statement. With 350 500 decided ids
  (50 000 object DENYs plus 300 000 orphan rows) the same statements take 472–585 ms, 30.8×–68.0× a
  superuser's — still about 4–5 % of what 1.0.0 took.
- **Orphan DENY rows still count.** `record_permissions.record_id` carries no foreign key, so a DENY
  row whose target object no longer exists stays in the decided set and inflates every restricted
  statement. Pruning dangling DENY rows is the cheapest improvement available to an operator; the
  object-list path additionally serves from the list cache, while AQL does not.
- **The GraphQL visibility path is the most sensitive surface to a large DENY set.** It measured 4.4× a
  superuser's statement on the 500-DENY shape (1.32 ms vs 0.30 ms) and **1499.1× (544.18 ms vs 0.36 ms)**
  at 350 500 decided ids. It is recorded, not bounded, in this release.

The technique for bounding very large DENY sets is an open product decision (BACKLOG `PERF-DENY-01`),
not a configuration option: there is no cap, no sampling and no cross-request cache of decisions.
