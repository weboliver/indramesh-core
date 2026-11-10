# Third-Party Notices

IndraMesh Core is licensed under the [GNU Affero General Public License v3.0](LICENSE). It is built
on top of the open-source packages listed below; their full license texts ship with the packages
themselves (the backend virtualenv's `site-packages/*/LICENSE*`, `frontend/node_modules/*/LICENSE*`)
and with the container images, which are pulled from their own registries at deploy time.

This list covers the direct application dependencies declared in `backend/requirements.txt` and in
the `dependencies` section of `frontend/package.json`, plus the third-party container images that
`docker-compose.yml` references. It does not inventory transitive dependencies or operating-system
packages inside container images.

Third-party code also travels inside committed build outputs:

- `plugins/graphql-studio/static/graphql-studio-heavy.js` - the graphql-studio plugin's heavy editor
  payload: a self-contained browser bundle of Monaco, GraphiQL and graphql-js under MIT. The notice
  blocks it carries are reproduced in `plugins/graphql-studio/static/THIRD-PARTY-LICENSES`, which
  ships next to it in the plugin's repository.
- the per-plugin registration bundles under `plugins/*/static/*.js` - these embed no third-party
  license or copyright marker, because the host application supplies React and the other shared
  runtime modules; beyond each plugin's own `LICENSE` they need no notices file. The rule is the
  marker, not the plugin name: a bundle that starts carrying notices fails the generator's check.

The application dependencies are licensed under OSI-approved permissive licenses (MIT, BSD,
Apache-2.0, ISC) or LGPL, and are compatible with AGPL-3.0 for the way they are used here
(dynamically loaded or imported at runtime, not statically linked or modified).

This file is generated - do not edit it by hand. Regenerate it with
`scripts/generate-third-party-notices.sh` and verify it with
`scripts/generate-third-party-notices.sh --check`, which fails as soon as a declared dependency set
and this list disagree.

## Backend (Python)

The direct runtime dependencies declared in `backend/requirements.txt`, with the version resolved
in this checkout and the license each package declares in its own distribution metadata. A version
followed by `(declared: ...)` is declared as a range rather than an exact pin, so the resolved
version can move without a change to the declared set.

| Package | Version | License |
|---|---|---|
| alembic | 1.19.0 | MIT |
| APScheduler | 3.11.3 | MIT |
| arq | 0.28.0 | MIT |
| asyncpg | 0.31.0 | Apache-2.0 |
| bcrypt | 4.3.0 | Apache-2.0 |
| bleach | 6.4.0 | Apache-2.0 |
| cron_descriptor | 2.1.0 | MIT |
| cryptography | 50.0.2 | Apache-2.0 OR BSD-3-Clause |
| fastapi | 0.141.1 | MIT |
| graphql-core | 3.3.1 | MIT |
| httpx | 0.28.1 | BSD-3-Clause |
| instructor | 1.15.4 | MIT |
| jsonschema | 4.26.0 | MIT |
| lark | 1.3.1 (declared: >=1.2.0) | MIT |
| openai | 2.54.0 | Apache-2.0 |
| opensearch-py | 3.2.0 | Apache-2.0 |
| prometheus-fastapi-instrumentator | 8.1.0 | ISC |
| psycopg2-binary | 2.9.12 | LGPL with exceptions |
| pydantic | 2.13.4 | MIT |
| pydantic-settings | 2.14.2 | MIT |
| PyJWT | 2.15.1 | MIT |
| python-dotenv | 1.2.2 | BSD-3-Clause |
| python-multipart | 0.0.32 | Apache-2.0 |
| pytz | 2026.3.post1 | MIT |
| redis | 5.3.1 | MIT |
| secure | 2.0.1 | MIT |
| slowapi | 0.1.10 | MIT |
| SQLAlchemy | 2.0.51 | MIT |
| starlette | 1.4.0 | BSD-3-Clause |
| strawberry-graphql | 0.335.0 | MIT |
| structlog | 26.1.0 | MIT OR Apache-2.0 |
| uvicorn | 0.52.1 | BSD-3-Clause |

## Frontend (JavaScript/npm)

The direct production dependencies declared in the `dependencies` section of
`frontend/package.json`, resolved in this checkout; the same `(declared: ...)` marker as above.

| Package | Version | License |
|---|---|---|
| @dagrejs/dagre | 3.1.0 | MIT |
| @tanstack/react-query | 5.101.4 | MIT |
| @xyflow/react | 12.11.2 | MIT |
| axios | 1.20.0 | MIT |
| clsx | 2.1.1 | MIT |
| dompurify | 3.4.17 | MPL-2.0 OR Apache-2.0 |
| fuse.js | 7.4.2 | Apache-2.0 |
| graphql | 16.14.2 (declared: ^16.9.0) | MIT |
| i18next | 26.3.6 | MIT |
| lucide-react | 1.28.0 | ISC |
| markdown-it | 15.0.3 | MIT |
| react | 19.2.8 | MIT |
| react-colorful | 5.7.0 | MIT |
| react-day-picker | 10.0.1 | MIT |
| react-dom | 19.2.8 | MIT |
| react-i18next | 17.0.11 | MIT |
| react-router-dom | 7.18.2 | MIT |
| tailwind-merge | 3.6.0 | MIT |
| web-vitals | 5.3.0 | Apache-2.0 |

## Infrastructure (Docker images, not distributed with this repository)

These run as separate containers via `docker-compose.yml` and are pulled from their own registries
at deploy time; no code from them is redistributed from this repository, so no license obligation
of theirs attaches to it from their use. The table is read from the `image:` references of
`docker-compose.yml`, and the generator refuses an image it has no recorded license for - a new
Compose image cannot reach the published tree with a guessed license.

| Image | License |
|---|---|
| nginx:1.30.6-alpine | BSD-2-Clause |
| grafana/loki:3.7.9 | AGPL-3.0 |
| grafana/alloy:v1.21.3 | Apache-2.0 |
| ghcr.io/tecnativa/docker-socket-proxy:v0.5.0 | Apache-2.0 |
| grafana/grafana:13.2.5 | AGPL-3.0 |

## Own stateful images from this repository

`docker-compose.yml` and the Kubernetes installer build these images from the repository's own
sources (`deploy/k8s-images/stateful/`), so they are first-party and do not appear in the table
above. Each is a thin wrapper around an upstream image; the upstream components it carries are
named here, because once a service builds its wrapper the upstream `image:` reference no longer
reaches the table the generator reads.

`indramesh-stateful-postgres` — the upstream components compiled into it inside the image build,
from pinned upstream tags (they are not redistributed as separate packages):

| Component | Version (pinned) | License |
|---|---|---|
| PostgreSQL server | 18.4 (`postgres:18.4-alpine` base image) | PostgreSQL License (permissive, similar to MIT/BSD) |
| Apache AGE | 1.8.0 (official Apache source release for PostgreSQL 18; git tag `PG18/v1.8.0-rc0`) | Apache-2.0 |
| pgvector | 0.8.7 (git tag `v0.8.7`) | PostgreSQL License |

`indramesh-stateful-opensearch` — the wrapper only makes the security plugin's tooling
group-readable for an arbitrary-UID Kubernetes pod; the search engine itself is the upstream
image:

| Component | Version (pinned) | License |
|---|---|---|
| OpenSearch | 3.9.0 (`opensearchproject/opensearch:3.9.0` base image) | Apache-2.0 |

`indramesh-stateful-valkey` — the wrapper only applies the Alpine package updates and makes
`/data` group-writable for an arbitrary-UID pod; the server itself is the upstream image:

| Component | Version (pinned) | License |
|---|---|---|
| Valkey | 9.1.1 (`valkey/valkey:9.1.1-alpine` base image) | BSD-3-Clause |

## Kubernetes Deployment Infrastructure

The Kubernetes installer deploys or configures the following external components. They remain
separate services and are not incorporated into the IndraMesh Core application code.

| Component | License | Use in IndraMesh deployment |
|---|---|---|
| k3s | Apache-2.0 | Kubernetes distribution used by the installer |
| Helm | Apache-2.0 | Chart installation and release management |
| cert-manager | Apache-2.0 | TLS certificate management |
| kube-prometheus-stack | Apache-2.0 | Monitoring chart providing Prometheus-stack components |
| Prometheus, Prometheus Operator, kube-state-metrics, node-exporter | Apache-2.0 | Metrics collection and Kubernetes workload monitoring |
| Grafana | AGPL-3.0 | Monitoring dashboards; deployed as a separate service through kube-prometheus-stack |
| Traefik | MIT | k3s ingress controller used by the IngressRoute resources |
| Headlamp | Apache-2.0 | Optional Kubernetes management UI |

Grafana and Loki are AGPL-3.0: a deployment that modifies them and offers them to users over a
network must comply with that license. Their license does not change the license of the separate
IndraMesh Core application.

## Trademark Notice

"Jira" and "Jira Assets" are trademarks of Atlassian Pty Ltd. IndraMesh Core is an independent
project that integrates with Jira Assets via its public API; it is **not affiliated with, endorsed
by, or sponsored by** Atlassian.
