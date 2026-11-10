<!-- generated-by: gsd-doc-writer -->
# Getting Started

This walkthrough takes you from a fresh clone to a running IndraMesh Core stack with a successful
authenticated API call.

## Two Supported Paths

IndraMesh Core has two distinct operator paths — they do not overlap:

- **Public Compose quickstart (this guide):** the production-shaped stack defined by the public
  `docker-compose.yml`, built and started with the single installer command
  `./install_indramesh_docker.sh`. The default service set is PostgreSQL, Valkey, OpenSearch,
  backend, worker, the storage service, the frontend and Nginx. This is the
  supported way to run the product from a fresh clone.
- **k3s/WSL2 bootstrap:** a single-node Kubernetes environment installed with
  `./install_indramesh_k8s.sh` and controlled with `./start`/`./stop`. It is a separate fresh-data foundation —
  see [`docs/DEPLOYMENT.md`](./DEPLOYMENT.md#kubernetes-host-prerequisites) for the host
  prerequisites. The k3s release does not include Jira and is available at
  `https://localhost` when installed.

Jira is a **development-only** Compose integration: its service, its volume and the `JIRA_URL`
wiring live in the non-mirrored `docker-compose.jira.yml`, which never ships in the public tree and
never joins the Kubernetes path.

The Compose path is driven by Compose itself, but the two root lifecycle wrappers serve it too: on
a host **without** a `k3s` systemd unit, `./start` and `./stop` start and stop the existing Compose
containers (`docker compose start`/`stop`, never `up`/`down`). On a host that has both
installations, `./start --compose` and `./stop --compose` force the Compose shape while `--k3s`
forces k3s; a bare `./start` there stays k3s. The k3s-only flags (`--enable`, `--disable`, `--full`)
are rejected in Compose mode rather than silently ignored.

### Contributing to Core (the development variant)

A contributor who wants hot reload, the Jira container and the Playwright E2E runner layers the
**mirrored** dev overlay and the two dev-only files on top of the base file by putting these two
lines in the private `.env`:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml:docker-compose.jira.yml:docker-compose.e2e.yml
COMPOSE_PROFILES=logging
```

`COMPOSE_FILE` selects the four files and `COMPOSE_PROFILES=logging` keeps the bundled log pipeline
feeding the `log-explorer` plugin. That private `.env` is a maintainer arrangement — the public tree
ships none of it.

Follow the Compose steps below for the public quickstart; use `docs/DEPLOYMENT.md` when you want the
bootstrap/k3s environment.

## Prerequisites

- **Git**, **curl**, **Docker**, the **Docker Compose plugin** (Compose `2.20` or newer — the stack
  relies on `depends_on.required`) and **`openssl`**. The entire stack runs via `docker-compose.yml`;
  there is no supported bare-metal setup path.
- **No host Python or Node install is required to run the stack.** The installer generates every
  secret through the project's own backend image, and the backend and frontend images install their
  own dependencies. The repository virtual environment (`.venv`) is a **developer-tooling**
  prerequisite for running the backend tests locally, not a quickstart prerequisite.
- About **4 GB of memory** available to the Docker daemon — the search service alone reserves a 2 GB
  Java heap.
- Ports: Nginx `8180`/`8443` (host-mapped defaults for `HTTP_PORT`/`HTTPS_PORT` — the container listens
  internally on `80`/`443`), the loopback data ports `5434` (Postgres, host-mapped), `6379`
  (`VALKEY_PORT`), `9200` (`OPENSEARCH_PORT`), plus whatever you set for `BACKEND_PORT`/`FRONTEND_PORT`,
  must be free on the host.

### Installing Docker + Docker Compose (if not already present)

Check first — Docker Desktop or a prior native install may already provide both:

```bash
docker --version && docker compose version
```

If either command is missing, install Docker Engine and the Compose plugin natively via Docker's
official `apt` repository (works on native Ubuntu and Ubuntu-under-WSL2 alike):

```bash
# 1. Prerequisites + Docker's GPG key
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc

# 2. Add the repository (matches your Ubuntu release automatically)
. /etc/os-release
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update

# 3. Install Docker Engine + Compose plugin
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin

# 4. Start the daemon and run without sudo
sudo systemctl enable --now docker
sudo usermod -aG docker "$USER"
```

After step 4, **start a new shell** (or `newgrp docker`) for the group membership to take effect,
then re-run the check command above to confirm both `docker` and `docker compose` work without
`sudo`.

**On WSL2 specifically:** do not install Docker Desktop for this repo's Compose stack unless you
have a specific reason to — a native Docker Engine install in the distro (above) avoids Docker
Desktop's WSL integration entirely, which has a known conflict with the k3s bootstrap path (see
`docs/DEPLOYMENT.md`'s "Kubernetes Host Prerequisites" section) if you ever run both. `/etc/wsl.conf` needs
`[boot]\nsystemd=true` for `systemctl enable --now docker` to work — see `docs/DEPLOYMENT.md` if that
file doesn't already exist on your distro.

## Installation Steps

1. Clone the public repository and move into it:
   ```bash
   git clone git@github.com:weboliver/indramesh-core.git
   cd indramesh-core
   ```
2. Run the installer:
   ```bash
   ./install_indramesh_docker.sh
   ```
   It creates `.env` from `.env.example`, generates every secret (database, Valkey,
   OpenSearch, the plugin role, `JWT_SECRET_KEY`, `FERNET_KEY` and `ADMIN_PASSWORD`), builds and
   starts the stack, waits until the backend is healthy and nginx answers, and prints the login URL
   and the admin password. Re-running it is safe: an existing `.env` is never overwritten and only
   empty or placeholder values are filled.

   Prefer to drive the steps by hand? Copy the template and let Compose build and start the stack:
   ```bash
   cp .env.example .env
   docker compose up -d --build
   ```
   Every required secret is guarded by Compose (`${VAR:?…}`), so a copy of `.env.example` renders the
   whole project; leaving the `CHANGE_ME` placeholders in place is never a working configuration.
   Compose exits with a variable error naming the key if a required value is missing or blanked.

   Upgrading an existing `.env`? Rename the four `POSTGRES_*` entries in its database block to
   `DB_USER`, `DB_PASSWORD`, `DB_NAME`, and `DB_PORT`, keeping the same values. An installation
   created before the current durable-state layout must be rebuilt rather than upgraded in place — see
   [OPERATIONS.md § Upgrade](OPERATIONS.md#upgrade). A stale old block
   left in place is harmless — the `postgres` service's explicit environment mapping wins over the
   file's forwarded keys — but if `DB_USER`, `DB_PASSWORD`, or `DB_NAME` is missing, `docker compose`
   aborts and names the key instead of starting Postgres with empty credentials. One silent caveat:
   a shell-level override such as exporting the old password variable in front of `docker compose`
   is no longer read at all; use `DB_PASSWORD` for such overrides.
3. Notes for a stack you are wiring by hand (the installer already handles all of these):
   - `NGINX_TRUSTED_IP` must equal the value the backend reads. It is the trusted-proxy address for
     Uvicorn's `ProxyHeadersMiddleware` **and** the static `ipv4_address` assigned to the `nginx`
     service in `docker-compose.yml`; both come from this one variable, so they cannot drift.
   - `COMPOSE_SUBNET` is the Compose subnet. An address-pool overlap
     (`Pool overlaps with other one on this address space`) is fixed by changing that one value —
     for example `COMPOSE_SUBNET=172.29.0.0/16` — keeping `NGINX_TRUSTED_IP` inside the range.
   - `PLUGIN_DB_USER` stays `indramesh_plugin` and `PLUGIN_DB_PASSWORD` must be non-empty. The
     shipped Jira plugin declares `require_restricted_role = true` in
     `plugins/jira/plugin.toml` and requires the restricted migration role to load successfully.
   - `CORS_ALLOWED_ORIGINS` is a comma-separated list of trusted origins, e.g.
     `http://localhost:5173,https://localhost:8443` for the default direct frontend and Nginx URLs.
     Wildcards (`*`) are rejected at startup.
   - `BACKEND_PORT` and `FRONTEND_PORT` are already set in `.env.example` (`8000` and `5173`) and are
     used by `docker-compose.yml` with no explicit fallback — keep them set to make the host URLs
     below predictable.

## First Run

1. The installer builds and starts the full stack with `docker compose up -d --build`.
   Run Compose service operations only from the main checkout, never from a Git worktree.
   The image builds install backend dependencies from `backend/requirements.txt` and
   `backend/plugin-requirements.txt`, and frontend dependencies with `npm install`.
   Backend startup runs Core migrations, bootstraps the plugin database role, and runs plugin
   migrations before starting Uvicorn; no separate fresh-install migration command is needed.
   `backend` and `worker` wait on `postgres`, `valkey`, and `opensearch` health checks before
   starting; `nginx` waits on `backend` and `frontend`.
2. Confirm the backend is healthy:
   ```bash
   curl http://localhost:8000/health
   # {"status": "healthy"}
   ```
3. Log in as `admin` with the password the installer printed. `ADMIN_PASSWORD` is used **only** when
   the admin user is first created on a fresh database; changing it in `.env` afterwards has **no
   effect**, because the password is changed in the UI. There is no plaintext first-run password file
   to retrieve — the installer reports the value once, on the run that generated it.
4. Log in and call an authenticated endpoint. `/api/v1/auth/login` expects an OAuth2
   password-grant form body (`application/x-www-form-urlencoded`), not JSON:
   ```bash
   curl -X POST http://localhost:8000/api/v1/auth/login \
      --data-urlencode "username=admin" \
      --data-urlencode "password=<printed-password>"
   # {"access_token": "<jwt>", "token_type": "bearer"}

   curl http://localhost:8000/api/v1/sources \
     -H "Authorization: Bearer <access_token>"
   ```
5. Open the whole stack through Nginx at `https://localhost:8443`
   (`http://localhost:8180` redirects to HTTPS; configured by `HTTP_PORT`/`HTTPS_PORT`, self-signed
   cert in `nginx/certs/`), or the direct frontend at `http://localhost:5173` (or the configured
   `FRONTEND_PORT`), and log in with the same `admin` credentials. The non-standard default ports are
   deliberate — they keep this stack from colliding with both the k3s stack on
   80/443 and Jira's `8080` (`docs/DEPLOYMENT.md`) if everything is running on the same host.

## Optional: Jira Plugin Connection and Credentials

Core startup and the authenticated API call above do not require a configured Jira connection.
Jira synchronization belongs to the shipped `jira` plugin; setting the legacy `JIRA_URL`
environment variable does **not** configure it. The Jira container itself is a development-only
service supplied by `docker-compose.jira.yml` (see the development variant above).

1. Make sure your Jira instance is ready and that you have a Jira personal access token (PAT)
   whose account can access Assets schemas. Jira installation, licensing, and token issuance
   are managed in Jira, not by the IndraMesh bootstrap admin account.
2. Create and save a Source with connector type `jira` for pulling, or a Target with connector
   type `jira` for pushing. Set its URL to the address reachable from backend and worker.
   For the Compose Jira service this is `http://jira:8080`, **not** `http://localhost:8080`
   (the latter is the host browser URL).
3. Open **Service accounts** at `/configuration/service-accounts`. Create an account with a
   name, select that Source and/or Target, and enter the Jira PAT in its corresponding
   source/target bearer-token field. These are write-only credentials; a blank token field
   when editing does not mean the stored token is missing. Do not put the PAT in a URL,
   connector configuration, or an IndraMesh login request.
4. Reopen the saved Source/Target's Jira connector settings, select the bound service account,
   and run the connection test. The plugin probes Jira identity and Assets schema access;
   success reports “Connected. Jira accepted the service account.” Save the connector settings
   to retain the selected default account and conflict-handling preference.

The connection editor and credential fields are implemented in
`plugins/jira/frontend/JiraConnectionEditor.jsx` and
`frontend/src/features/settings/ServiceAccountModal.jsx`. The plugin binds a snapshot of the
endpoint URL; changing a bound URL can make the binding stale rather than silently relinking
it. Use newly created disposable data for any synchronization trial, never existing user assets.

## Common Setup Issues

- **Backend container exits immediately with a `RuntimeError` on startup** — one of
  `JWT_SECRET_KEY`, `FERNET_KEY`, `CORS_ALLOWED_ORIGINS`, or `NGINX_TRUSTED_IP` is missing or
  invalid in `.env`. Check `docker compose logs backend` for the specific variable named in the
  error, fix `.env`, then `docker compose up -d` again (no rebuild needed for env-only changes).
- **Compose refuses to start and names a required variable** — a value guarded by `${VAR:?…}` is
  missing or blanked. Copy `.env.example` again and run the installer, which fills the placeholders,
  rather than deleting the line.
- **OpenSearch container fails its health check / backend can't reach it** — the search service
  installs its demo security configuration on first start and requires a **compliant**
  `OPENSEARCH_PASSWORD` (8–100 characters with an uppercase, a lowercase, a digit and a special
  character); the container exits at start when the value is missing or does not match, and the
  demo configuration turns HTTP TLS off only inside the container network. Make sure
  `OPENSEARCH_PASSWORD` in `.env` is non-empty and compliant — the installer generates a compliant
  value. The password is read only on the first start of an empty `opensearch_data` volume, so a
  later change does not rotate it; remove the volume to start over.
- **Jira plugin missing or unavailable although `/health` succeeds** — inspect
  `docker compose logs backend` for plugin migration/load errors. Ensure
  `PLUGIN_DB_PASSWORD` is set and the plugin role bootstrap succeeds; Core health alone does
  not prove that a plugin loaded. A rejected connection test requires checking the PAT and
  Assets permissions in Jira, or the container-reachable URL for network failures.
- **Login returns 422 Unprocessable Entity** — `/api/v1/auth/login` uses
  `OAuth2PasswordRequestForm`, which requires form-urlencoded `username`/`password` fields, not a
  JSON body. Use `curl -d "username=...&password=..."` (or `-F`), not `-H "Content-Type:
  application/json"` with a JSON payload.
- **Login returns 429 Too Many Requests** — the login-route limiter (`RATE_LIMIT_LOGIN`, default
  `5/minute`) is always active regardless of `RATE_LIMIT_ENABLED`. Wait out the window, or check
  `RATE_LIMIT_LOGIN` in `.env` if you're scripting repeated logins.
- **Port already in use** — `DB_PORT` (default `5434`), `BACKEND_PORT`,
  `FRONTEND_PORT`, `VALKEY_PORT` (default `6379`), `OPENSEARCH_PORT` (default `9200`),
  `HTTP_PORT` (default `8180`), and `HTTPS_PORT` (default `8443`) are all
   configurable in `.env` if the default conflicts with something already running on the host.
  If you override the Nginx port pair, also update the static redirect maps in
  `nginx/nginx.conf`; changing `.env` alone does not change those maps. Update CORS origins too.
- **`Pool overlaps with other one on this address space`** — the Compose subnet clashes with an
  existing network. The installer checks the subnet against every Docker network and host route
  before it starts anything: while `COMPOSE_SUBNET` and `NGINX_TRUSTED_IP` are at their defaults it
  picks a free range and writes both into `.env`; a value you set yourself is never changed, and an
  overlap aborts naming the conflicting network. To choose by hand, set `COMPOSE_SUBNET` to a free
  range (for example `172.29.0.0/16`) and `NGINX_TRUSTED_IP` to an address inside it, then re-run
  the installer.

## Next Steps

- [`docs/DEPLOYMENT.md`](DEPLOYMENT.md) — the separate k3s/WSL2 bootstrap path (`./install_indramesh_k8s.sh`, `./start`,
  `./stop`); not part of the Compose development workflow above.
- [`docs/CONFIGURATION.md`](CONFIGURATION.md) — full environment variable reference, defaults, and per-environment
  overrides.
- [`docs/ARCHITECTURE.md`](ARCHITECTURE.md) — system overview, component diagram, and key abstractions.
- [`docs/DEVELOPMENT.md`](DEVELOPMENT.md) — local development workflow, build/lint commands, and coding conventions.
- [`docs/API.md`](API.md) — full REST (`/api/v1`), GraphQL (`/api/v1/graphql`), and Public API
  (`/public/api/v1`) reference.
- [`README.md`](../README.md) — stack summary, the one-command quickstart, and links to the
  development workflow.
