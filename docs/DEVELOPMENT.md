<!-- generated-by: gsd-doc-writer -->
# Development

This guide covers the local development workflow, build commands, style checks, branches, and pull requests. For first-time setup, see [Getting Started](GETTING-STARTED.md); for environment variables, see [Configuration](CONFIGURATION.md).

## Local Setup

The Docker Compose stack is the supported development environment, defined by the public base file `docker-compose.yml` (PostgreSQL, Valkey as the `redis` service, OpenSearch, the backend, worker, storage service, frontend, and Nginx). The base file is production-shaped — the frontend image is built with `ENVIRONMENT=production` and serves the compiled SPA behind nginx's strict CSP. The develop checkout layers the mirrored `docker-compose.dev.yml` (Vite dev server with HMR, the development CSP, and the source bind mounts for hot reload) plus the two development-only, non-mirrored files — `docker-compose.jira.yml` (the Jira container) and `docker-compose.e2e.yml` (the `e2e-tests` Playwright runner behind the `e2e` profile, normally started by the E2E runner) — on top through the private `.env`'s `COMPOSE_FILE`, and sets `COMPOSE_PROFILES=logging` to keep the bundled log pipeline running for the `log-explorer` plugin. The one-shot `cutover` service uses the separate `maintenance` profile and is not part of normal development startup.

Run all Compose operations and the rebuild/E2E helpers from the main checkout, never from a Git worktree. Compose uses the shared project name `indramesh`: starting it from a worktree can replace the running services with mounts into that worktree. Worktree development must use the existing stack unchanged.

1. Clone the repository and enter it:
   ```bash
   git clone https://github.com/weboliver/indramesh-core.git
   cd indramesh-core
   ```
2. Create the local environment file and select the developer file set:
   ```bash
   cp .env.example .env
   ```
   The template is a valid env file with `CHANGE_ME` placeholders; run `./install_indramesh_docker.sh`
   to generate the secrets, or fill them by hand. Append the developer file set and profile to the
   private `.env`:
   ```
   COMPOSE_FILE=docker-compose.yml:docker-compose.dev.yml:docker-compose.jira.yml:docker-compose.e2e.yml
   COMPOSE_PROFILES=logging
   ```
   Do not source `.env` as a shell script or commit it.
3. Set `ENVIRONMENT=development` in the private `.env` for backend hot reload and console logging (the template ships `production`). The frontend shape is selected by `docker-compose.dev.yml`, not by this variable — the overlay pins the Vite dev server and the development CSP. Set `JWT_SECRET_KEY`, `FERNET_KEY`, and `CORS_ALLOWED_ORIGINS`; retain `NGINX_TRUSTED_IP`. The backend refuses to start without these security settings. Compose supplies `INDRAMESH_RUNTIME_MODE=compose`. Keep the plugin migration-role credentials configured for the shipped Jira plugin, which requires a restricted role. See [Configuration](CONFIGURATION.md) for the full reference.
4. Build and start the development stack:
   ```bash
   docker compose up -d --build
   ```
   `backend/start.sh` applies Core Alembic migrations, bootstraps the plugin database role, and runs plugin migrations before launching Uvicorn. Core migration and role-bootstrap failures abort startup; individual plugin migration failures leave that plugin unavailable rather than aborting the whole backend.
5. Follow a service log when diagnosing local changes:
   ```bash
   docker compose logs -f backend
   ```

The Compose images install their own dependencies. To use frontend scripts directly from the host for linting or builds, first install the frontend dependencies:

```bash
cd frontend
npm install
```

For local Python commands, activate the repository virtual environment first:

```bash
source .venv/bin/activate
```

### Reloading and rebuilding

- **Backend:** `backend/` is bind-mounted and Uvicorn runs with `--reload` unless `ENVIRONMENT=production`; ordinary Python source changes reload automatically.
- **Frontend:** `frontend/` is bind-mounted and Vite provides hot-module replacement in the default development mode.
- **Worker:** the worker source is bind-mounted, but the ARQ process does not watch files. Restart it after a worker-side Python change:
  ```bash
  docker compose restart worker
  ```
  In particular, plugin classes and task declarations are loaded into the worker's own registry at startup (`backend/app/worker.py`); a bind mount does not refresh that registry between jobs.
- **Dependencies, Dockerfiles, Compose configuration, or `ENVIRONMENT`:** rebuild and recreate the affected service. The project helper also restarts Nginx after rebuilding the backend or frontend so it resolves the recreated service:
  ```bash
  ./rebuild_indramesh_docker.sh backend
  ./rebuild_indramesh_docker.sh frontend
  ```
  The helper also accepts `worker` and an optional second argument `--no-cache`. Restart both processes when changing shared plugin code; for ordinary Core backend Python changes, Uvicorn reload is sufficient. Plugin files are mounted outside `/app`, so do not rely on the default Uvicorn watcher to notice changes under `plugins/`.
  `backend`, `worker`, `storage` and `cutover` each build the backend sources into their own image (`indramesh-backend`, `indramesh-worker`, ...) from a shared layer cache, so the helper's `backend` and `worker` selectors both rebuild and recreate `backend`, `worker` and `storage` together.

## Build Commands

Run frontend package scripts from `frontend/`. The table lists every script in `frontend/package.json`, grouped by development/build, checks, and translation tooling. There is no backend package-script runner; Compose builds the backend image and `backend/start.sh` runs database migrations at startup.

| Command | Description |
|---|---|
| `npm run dev` | Starts the Vite development server. In the Compose frontend container it runs with `--host`; Vite proxies `/api/v1/` to `http://backend:8000`. |
| `npm run build` | Creates a production frontend bundle with `vite build`. |
| `npm run build:plugin-example` | Builds `plugins/example/frontend/index.jsx` using its Vite library configuration; writes `plugins/example/static/example.js`. |
| `npm run build:plugin-jira` | Builds `plugins/jira/frontend/index.jsx` using its Vite library configuration; writes `plugins/jira/static/jira.js`. |
| `npm run preview` | Serves a built Vite bundle locally. |
| `npm run lint` | Runs ESLint as `eslint . --max-warnings 0`. |
| `npm run test:plugin-runtime` | Runs the loader, registry, and contribution-utils Node tests, then ESLint over `src/shared/plugin-runtime` with no warnings allowed. |
| `npm run i18n:extract` | Extracts translation keys and synchronizes the primary catalog with `i18next-cli`. |
| `npm run i18n:lint` | Validates translation keys with `i18next-cli`. |

The development API proxy in `frontend/vite.config.js` targets the Compose hostname `backend`; a host-side `npm run dev` does not automatically make that hostname resolvable. Use the Compose frontend for the supported full-stack workflow. Host-side lint, builds, and Node tests do not require that proxy.

## Plugin Build, Reload, and Version Lifecycle

Plugins are separate deployable artifacts, not part of `npm run build` for the host SPA. Compose mounts `plugins/` read-only into both backend and worker at `/opt/indramesh/plugins`; edit and build them on the host.

1. Edit the plugin's Python implementation and/or frontend source. Its `plugin.toml` declares the module, capabilities, frontend bundle path, extension-point families, and optional database migrations. Import the public Python API through `indramesh.sdk`; browser bundles use `@indramesh/plugin-api` and the host import map. Plugin code must import no `app.*` module (an AST gate in the backend's automated test suite enforces this).
2. For frontend changes, build the affected bundle from `frontend/`:
   ```bash
   npm run build:plugin-jira
   # Or, when changing the example plugin:
   npm run build:plugin-example
   ```
   The Vite configurations under `plugins/*/frontend/` produce ES-module library bundles in each plugin's `static/` directory, with fixed filenames. They externalize React, the router, TanStack Query, and the plugin API so the plugin shares the host runtime. Each build empties that plugin's output directory. These scripts are one-shot builds, not plugin HMR/watch commands.
3. Bump `version` in the affected `plugins/<name>/plugin.toml` when publishing changed plugin code or bundle content. Do not change `api_version` merely to refresh a bundle: it is the Core SDK compatibility target. `backend/app/plugin_system/versioning.py` requires the same API major and a plugin API minor no newer than Core's; patch is ignored by the compatibility check.
4. Reload the processes that hold the plugin. From the main checkout, restart backend and worker after a plugin implementation or manifest change:
   ```bash
   docker compose restart backend worker
   ```
   Backend startup runs the migration pipeline and rediscovers plugins; the worker independently discovers and loads its registry at startup. For database changes, follow the plugin migration-role path in [Configuration](CONFIGURATION.md#plugin-migration-role-requirements), not a state-toggle operation. A plugin state toggle activates/disables the loaded instance; it is not a Python hot-reload endpoint.
5. Refresh the browser/runtime list after the backend has loaded the new manifest. `PluginProvider.jsx` tracks `name@version`: when refreshed discovery returns a different version, it requests and registers the bundle again. Rebuilding bytes without changing the manifest version does not reload an already-started bundle in an open session; reload the page for that local-only case. The loader fetches the versioned asset with the authenticated API client and imports it through a fresh blob URL; asset responses use `Cache-Control: private, no-store`.

Both shipped manifests set `requires_worker_update=true`. Check the Plugins management view's worker diagnostics: `worker_loaded_version` must match the active plugin `version`. The task catalog marks plugin task types unavailable when they differ. Updating the backend alone does not update the worker's loaded implementation. The example plugin is inactive by default; enable it through plugin management if needed for development.

## Code Style

- **ESLint:** `frontend/eslint.config.js` is an ESLint flat configuration. Run `npm run lint` from `frontend/`; warnings fail the command. It checks application JavaScript/JSX plus the Vite and Playwright configuration files.
- **Formatting:** no Prettier, Biome, or `.editorconfig` configuration is committed. Match the surrounding formatting instead of applying an unconfigured formatter.
- **Backend:** no Python linter or formatter is configured. `backend/pyproject.toml` configures Vulture as an optional dead-code check; when needed, run it in the backend container with `docker compose exec backend vulture app`.
- **Conventions:** use `snake_case.py` files and 4-space Python indentation; use PascalCase for SQLAlchemy models and Pydantic schemas. Backend imports use the `app.` package path rather than relative imports. API resources use kebab-case plural paths, and route handlers raise explicit FastAPI `HTTPException` values for errors.
- **Tests:** backend tests use `test_<area>.py`, `Test<PascalCase>`, and `test_<action>_<entity>` naming. Frontend feature code lives under `frontend/src/features/`; shared UI and hooks live under `frontend/src/shared/`.
- **Cache serialization:** for ORM API responses with computed properties, use the relevant Pydantic response model's `model_validate(obj).model_dump()` rather than `jsonable_encoder(obj)`, which omits Python properties.

No repository CI workflow is configured to enforce these checks, so run the relevant commands before opening a pull request.

## Coordinated breaking SDK contract

The v20 Jira-connector cut-over changes two public plugin SDK input contracts in
one coordinated release. There is no compatibility alias and no separate deploy
step.

- `ConnectorCommentCreate` and `ConnectorFileCreate` take `external_id`
  (required string, length 1..100) instead of a connector-named identity input.
  The same remote id still feeds the deterministic
  `{source}:{object}:{external_id}` id, so media replay identity is unchanged.
- `TargetCreate` and `TargetUpdate` no longer accept the cloud flag or the
  default conflict strategy.

Both DTOs are strict (`extra="forbid"`): retired or renamed fields are rejected.
Every tracked consumer changes in the same release and no Core data is preserved
or migrated. DTO class and operation export names are unchanged. On the REST
side the retired request keys are rejected selectively by presence, so the REST
and SDK contracts stay deliberately different.

## Plugin SDK contracts (Phase 307 reference)

The `ai-translation` plugin exercises the neutral extension contracts added across Phases 302/303.
They are the reference for any plugin that reads/writes Core data, declares observable Task types or
contributes UI into an existing host surface.

### Neutral Core-data authorizer and projections

`backend/app/plugin_system/core_data.py` exposes `read_translations`, `write_translations`,
`read_ui_translation_catalog` and `write_ui_translations` on the public `indramesh.sdk` surface.
Every batch runs the Core `translation_access.batch_authorizer(actor)` — the plugin never supplies
the actor, never grants itself a Core write right, and never sees an ORM entity: the functions return
**neutral projections** (plain DTOs) only. Writes take a `mode` (`fill_missing` or `replace`); a
target filled in the meantime is reported as an expected **skip**, which is not a database error and
must never be masked as one.

### `admission_permissions` declaration field

`TaskTypeDeclaration` (`backend/app/plugin_system/capabilities.py`) carries an
`admission_permissions: tuple[str, ...] = ()` field alongside `required_permission`. Each entry names
a Core permission **or** a plugin-declared permission and is enforced at admission time by
`backend/app/services/task_service.py` with the same `403` shape; it is independent of the route
gate and of the `required_permission` check. This is how a plugin task requires the Core data it will
touch (for example `translations:read`) in addition to its own permission.

### Host draft/review session and `isPresent` contracts

- `frontend/src/shared/plugin-runtime/translation-draft-session.js` —
  `createTranslationDraftSession({ sessionId, scopeRevision })` returns
  `registerDraftHandlers(owner, { commitDraft, discardDraft? })`, `acceptDraft`, `getDraft`,
  `hasDraft`, `getDrafts`, `setDraftValue`, `clearDraft`, `commit`, `discard`, `unregisterOwner` and
  `subscribe`. `commitDraft` is the only required handler; a session is fenced by
  `(sessionId, scopeRevision)`, so a late answer for a changed scope is dropped.
- `frontend/src/shared/plugin-runtime/translation-review-session.js` — the neutral
  `createTranslationReviewSession` projection that narrows the existing Core catalog table to a
  contributor's rows behind a host-generated owner/session/scope fence.
- `isPresent` predicate on a contribution entry (`frontend/src/shared/plugin-runtime/contribution-utils.js`) —
  an entry with no `isPresent` is always visible; a gated entry is visible only when the predicate
  resolves true. It must be a function (validated) and must fail closed.

### Bundle build script

`npm --prefix frontend run build:plugin-ai-translation` builds `plugins/ai-translation/static/ai-translation.js`
through the plugin's Vite library config and then runs its self-testing `verify-bundle.mjs` — the same
one-shot pattern as `build:plugin-ai-schema-wizard`. Commit the rebuilt bundle; never force-add
ignored output.

## Branch Conventions

- The default branch is `master`. It is protected and merged only via pull requests; never commit or push directly to it, including documentation-only changes.
- Use `feature/milestone-v<version>-<slug>` for milestone-scoped work, for example `feature/milestone-v18.1-file-upload-comments`.
- Use `feature/<slug>` for smaller changes outside a milestone.

## PR Process

- Keep each pull request focused on one coherent change and explain what changed and why.
- Run the affected tests and, for frontend changes, `npm run lint` before opening the pull request; include commands and results in the description. See [Contributing](../CONTRIBUTING.md).
- Use the applicable branch naming pattern above and short, descriptive commit titles; no repository-defined commit-message format is documented. Never commit `.env`, secrets, or ignored files; update `.env.example` for new settings.
- Reference related issues in the pull request description when applicable.
- No pull-request template or repository-defined automated review gate is present; coordinate any additional review requirements with the maintainers.

## Testing During Development

The automated suites are part of the full development checkout, not of a published tree: the
backend `pytest` suite and the Playwright end-to-end suite are maintained there, together with the
scoped-testing discipline that decides what a change runs. Start the Compose stack before a backend
test run.

Run only the tests that cover what you changed — the affected backend test module, or one targeted
Playwright specification when a UI change calls for it — never a full suite. A frontend-only change
needs no backend run; prefer the affected unit, lint, or build checks for it. Full suites and their
guard overrides are reserved for explicitly authorized release regression testing, so do not set an
override merely to bypass scoped testing.

The test configuration loads the repository-root `.env` automatically; existing shell variables
take precedence. Keep host-facing database and cache addresses there (including the configured
PostgreSQL host port) — Compose supplies container-facing overrides to backend and worker.

Follow the same discipline for a contribution: name the checks you ran and what you observed, and
scope them to the change. See [Contributing](../CONTRIBUTING.md) for the terms a contribution is
accepted under.

When testing a write path, create disposable Sources, Schemas, Targets, and Objects for that test. Do not write against manually created data.
