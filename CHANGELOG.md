# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.1] - 2026-11-09

### Added

- **Languages** — Italian ships as a sixth interface language; every screen in the core application is translated, while plugin surfaces stay in English for now. Administrators can add their own languages on the Languages page, and plugins can react to a deleted language through the new `on_language_deleted` hook.

### Changed

- **GraphQL Studio** — a fresh installation of the studio is inactive until an administrator activates it, so installing the plugin no longer opens the internal `/api/v1/graphql` endpoint on a production instance by itself. Existing installations stay active; if you do not need the studio on a production instance, deactivate it once.
- **Languages** — `POST /languages/` normalizes the submitted code and rejects anything that is not a BCP-47 style tag with `422`; `DELETE /languages/{id}` refuses bundled languages with `409`; `LanguageResponse` carries `is_bundled`, so a client can tell a bundled language from a custom one.
- **Plugins** — `install_plugin_docker.sh` and `install_plugin_k8s.sh` update an installed plugin when the source carries a higher version; the same version does nothing and a lower one is refused.
- **Search** — releases from 1.0.1 on lay out a schema's search index across every object type; a database created under 1.0.0 can hold indices that cover only part of its object types, and objects of the missing types stay out of the search results. Repair them once after the upgrade as a documented operator step: `python -m app.maintenance repair-search-indexes` reports where an index diverges and rebuilds only that part, so a second run is a no-op.
- **Jira connector** — a Source or Target created under 1.0.0 whose URL points at an address inside the deployment network is refused after the upgrade until a superuser sets `allow_internal_target` to `true` in that row's `config`. The address check runs on pull, push, export and the connection test, so an unchanged internal URL stops syncing rather than silently leaving the deployment.

### Fixed

- **RBAC and permissions** — object lists, counts, AQL queries and GraphQL reads for a user with record-level deny rules are considerably faster than in 1.0.0 and return exactly the same records as before.
- **REST API** — a repeated object list request is served from the cache without any database query.
- **Data model and hierarchy** — deleting a source also removes the key sequences of its schemas from the database, as deleting a single schema already did.
- **Data model and hierarchy** — deleting a source, schema, object type, attribute or object also removes the translations of everything deleted with it, and an import that replaces a value removes the old value's translations. On the next start, translations and search indices left behind by earlier deletes are removed.
- **Languages** — the start-up seed removes UI strings that left the code together with their translations, so the translation coverage counts current keys only, and deleting a language also clears its profile preferences and cached dictionary.

### Security

- **Database identity** — the backend and worker now run their normal queries as a read/write application user (`DB_APP_USER`) without DDL or superuser rights, while the installer, the migrations and the deploy step keep the privileged identity. An existing 1.0.0 installation reaches this model by re-running the installer, which appends the missing `DB_APP_USER`/`DB_APP_PASSWORD` keys and generates the password; `DB_USER`/`DB_PASSWORD` stay unchanged, so the administrator keeps database access. The change closes the SQL-injection path out of the application process, not a shell inside those containers: on Compose and on k3s the backend keeps the privileged identity for the maintenance paths. Plugin code running in the backend or worker can no longer run DDL through `PluginContext.db()`; schema changes belong in plugin migrations.

### Known limitations (1.0.1)

The release notes carry the limitations of this release in one place, at category level. Every count below is the number of remaining deferred items in that category after the 1.0.1 re-triage; the total is the sum of the category rows and includes the four risks accepted for this release.

| Category | Count | Limitation and workaround |
|----------|-------|--------------------------|
| Plugin trust boundary and plugin API surface | 15 | Bundled plugin frontends run in the application origin and plugin code runs in-process; the plugin API is wider and less versioned than the public statement suggests, and some promised capabilities have no Core caller. Workaround: install only first-party plugins and keep the plugin set fixed for the deployment. |
| Accepted by-design trust boundaries | 3 | Three behaviours are accepted rather than fixed: a short cache of record-permission decisions can delay a newly added DENY, bundled plugins intentionally carry application trust, and the duplicated permission factories enforce identical rules. Workaround: wait out the cache window after changing a DENY, and do not install third-party plugins. |
| Migration history and downgrade fidelity | 12 | 1.0 ships a fresh database baseline; the historical migration chain contains destructive upgrade steps and downgrades that cannot restore what they removed, and the roundtrip is not tested. Workaround: back up a pre-1.0 installation before upgrading and treat the upgrade as one-way. |
| Write-path atomicity, task and queue consistency | 20 | Some multi-step writes still commit in separate transactions, and a failed queue enqueue can leave a task or export row pending forever; the worker topology assumes a single replica. Workaround: check the reported result of a long-running import or export before retrying, and keep one worker replica. |
| Data-model integrity and catalog consistency | 6 | Name uniqueness and several catalog invariants are enforced in application code rather than by the database, so concurrent writers can create duplicates or inconsistent references. Workaround: create catalog objects one at a time and avoid concurrent edits of the same schema. |
| Import and export contracts and payload handling | 4 | Import definitions accept attribute bindings that belong to another object type, export filenames collide within the same second, and the accepted CSV upload body is buffered in memory before it reaches the temporary file. Workaround: validate a definition with a small dry run and do not start two exports of the same definition in the same second. |
| Frontend failure reporting and browser-surface hygiene | 25 | Many frontend reads and writes render a failure as an empty state or report nothing at all, and search text or filter values persist in the URL and browser storage. Workaround: reload after a failed action and verify the result explicitly; clear browser storage on a shared machine. |
| Deployment, infrastructure and observability | 17 | The shipped deployment is single-node-oriented: rate limits, backup coverage, storage-service authentication, chart portability and log-store authentication remain hardening items, and the optional observability and Kubernetes-dashboard front doors are off by default and are an explicit operator decision when switched on. Workaround: keep the stack on a trusted network, back up the database and the encryption key together, and do not expose the log store. |
| Dependency currency, secrets and review-material hygiene | 9 | Low-severity dependency advisories, file-permission and temporary-root findings, and credential-shaped strings in private review artifacts remain. No live production credential is involved, and the dependency upgrades that mattered for the release shipped. Workaround: re-run the dependency scans before the next public release and keep the review artifacts private. |
| Backend robustness, dead code and remaining contracts | 13 | Several error paths echo raw exception text, the search index can resurrect a deleted document after a race, translation upserts and startup seeds can collide, and some dead routes and symbols remain; reference and value lists can stay stale for up to a minute after a public-API object write, permission checks on wide public-API writes cost one lookup per attribute type, and a GraphQL document with a very long fragment chain is answered with a generic error instead of the limit error. Workaround: treat a 500 as an unknown result rather than a failed one, re-read the object before retrying, and run the documented index repair after the upgrade. |
| Review-tooling and process gaps | 20 | The private review tooling carries unresolved hygiene warnings and Info findings (cleanup coverage, fixture credentials, exit-code contracts, scan-rule coverage), plus four skipped Info findings from an earlier phase. No operator action is required; this is maintainer tooling. |
| Unread remainders and triaged-only todos | 22 | Some deep plugin engine paths, cluster-only deployment checks, a dynamic scan and the personal-data classification were not completed before the release; the Jira connection cannot be rebound after a URL change, the first binding of a Source created without a Jira connection takes the URL valid at that moment, and the disposable-test sandbox remains a todo. Workaround: keep Jira Source and Target URLs stable (a changed URL needs the binding to be deleted and rebuilt), create Jira Sources together with their connection, test write paths only on disposable data, and verify cluster-only behaviour on the target installation. |
| Accepted risks 1.0.1 | 4 | Four behaviours are accepted for this release rather than changed: a rotated-out refresh cookie stays valid until its own expiry (at most eight hours) because rotation keeps no server-side store and therefore detects no reuse; a holder of the role-management permission can still add any permission to a role they hold; Jira sources and targets created under 1.0.0 that point at an address inside the deployment network are refused after the upgrade until a superuser allows it per row; and the content-security policy keeps its outbound image allowance because connector icons are still rendered directly. Workaround: end a session by logging out or changing the password when a token may have leaked, grant the role-management permission only to trusted operators, set the Jira internal-address allowance deliberately during the upgrade, and read connector icons as a trusted-source feature. |
| **Total** | **170** | All remaining deferred rows of the private ledger after the 1.0.1 re-triage, including the four risks accepted for this release. |

Two upgrade consequences sit behind those rows and are worth planning for: the search index of an existing installation can cover only part of its object types until the documented repair has run once, and an existing Jira Source or Target at an internal address stops syncing until a superuser allows that row's internal target. Both are one-time operator steps described under `Changed` above.

## [1.0.0] - 2026-11-01

The initial public release of IndraMesh Core.

### Added

- **Data model and hierarchy** — a hierarchical data model from Source through Schema and ObjectType down to Object, attribute values and object references, with forward migrations that carry stored data along on an upgrade.
- **Data model and hierarchy** — attribute types that carry a privacy and security classification, so a value is visible only where the record and its attribute type permit it.
- **REST API** — CRUD for every level of the model, a permission-scoped public read API under `/public/api/v1`, and documented import, export and query endpoints.
- **GraphQL API** — a typed GraphQL endpoint for integrated tooling and the bundled studio, scoped by the same permissions as the REST API.
- **RBAC and permissions** — roles, permission grants and per-record policies, plus a per-object audit log that records who changed what.
- **RBAC and permissions** — secure by default: imports and group writes are Administrator-only, exports and schedules are bound to their creator, and the password policy is configurable by an administrator.
- **Search** — full-text search over object values and catalog entities, with hidden fields stripped before a result reaches a caller.
- **Tasks and schedules** — a background task queue with retries, progress and history, and cron-style schedules for recurring imports, exports and connector runs.
- **Plugins** — an installable plugin system with its own API surface and frontend extension points, shipping the first-party Jira, GraphQL Studio, Log Explorer, AI Schema Wizard and AI Translation plugins.
- **Logging and observability** — structured application logs, a Prometheus metrics endpoint, and a bundled log viewer with an optional Loki, Alloy and Grafana stack.
- **Deployment** — a Docker Compose quickstart and a Helm chart for a single-node k3s installation, both from the same public source tree, with a `/health` endpoint that reports the running release.
