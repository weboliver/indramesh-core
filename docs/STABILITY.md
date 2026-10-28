# Stability and Versioning

IndraMesh Core is versioned with [Semantic Versioning](https://semver.org/spec/v2.0.0.html). This
document declares the surface those version numbers describe: what the project promises to keep
compatible within a major release, what is deliberately outside that promise, and how a change to a
promised surface is announced.

It is written for two readers — the operator deciding whether an upgrade is routine, and the
integration author deciding what can be relied on — and it is the reference for the question "is
this a breaking change?". Read it together with [`docs/API.md`](API.md), which documents the API
itself.

## The Stable Surface

The stable surface is **what is documented**, in exactly two documents:

- [`docs/API.md`](API.md) — the API reference.
- [`docs/PUBLIC-API-CONTRACT.md`](PUBLIC-API-CONTRACT.md) — the public REST API contract baseline.

Within those two documents, the stable surface is:

- **The `/api/v1` REST endpoints** the two documents describe, with the request and response
  formats, status codes and error envelopes they document.
- **The public GraphQL endpoint**, `POST /public/api/v1/graphql`, authenticated with an API-client
  bearer token.
- **Saved-query execution** — `POST /api/v1/saved-queries/{id}/execute` and its public-realm
  counterpart `POST /public/api/v1/saved-queries/{id}/execute`.

**An endpoint becomes stable when it is documented in one of those two documents — not when it appears in the generated schema.** The OpenAPI document served by the backend's documentation
routes lists every registered route, including internal, experimental and plugin routes that are
not part of this promise: appearing there means a route exists, not that it is versioned.
Documentation is the act that makes a route stable, and removing or changing a documented route is
a breaking change like any other.

## Excluded Surfaces

The surfaces below are deliberately **outside** the versioning promise. They may change in any
release, including a patch release, and a change to one of them is not a breaking change:

| Excluded surface | Why it is excluded |
|---|---|
| **The plugin interface and its SDK (`indramesh.sdk`)** — everything a plugin imports, implements, declares or registers against | The plugin API is **experimental**: it evolves together with the first-party plugins that exercise it, and it is not a stability interface. Breaking changes to it are announced in the changelog rather than forbidden, and a plugin states the Core release line it requires in its own README. |
| **The internal GraphQL endpoint** — `/api/v1/graphql`, the read-oriented schema over the same data | It lives under `/api/v1`, but it is a view for the administration interface and for developer tooling rather than a documented integration surface, and it is registered only outside a production environment. It is named here explicitly so that it is not read as covered by the promise above. |
| **The database schema and the core tables** | The schema is an implementation detail. Reading or writing the tables directly — through raw SQL, a reporting query or a replication consumer — is unsupported, and a table, column, constraint or index may change in a minor or patch release. Integrations use the documented API instead. |
| **The granularity and payload of internal hook events** | Hook events are an internal extension seam for the first-party plugins. New events may be added at any time, and the fields or the delivery granularity of an existing event may change, without that being a breaking change to the API. |
| **The numeric limit values** — query cost and query depth, statement and wall-clock timeouts, request rate limits, request size limits | The numbers are operational tuning: they exist to protect an installation and may be lowered or raised in a patch release. The distinction is deliberate — the **machine-readable error codes** that these limits produce are part of the promise (see *Operator Interfaces Under the Promise*), while the numbers behind them are not. |
| **The Helm chart's values and template surface** | The chart's keys and templates are a deployment reference for a local single-node cluster, not a versioning interface. See *Installation Scope and Version Boundary*. |

## Installation Scope and Version Boundary

- **Version line.** Only the latest 1.x release line is supported. A fix for an older 1.x release is
  delivered in the current line rather than as a patch to the older one, so an upgrade within 1.x is
  the supported way to receive fixes.
- **Supported installation models.** Docker Compose and a local single-node k3s installation, each
  following its documented procedure ([`docs/DEPLOYMENT.md`](DEPLOYMENT.md) and
  [`docs/OPERATIONS.md`](OPERATIONS.md)). Those two models are what 1.0 covers.
- **The Kubernetes installer and the chart are supported for a local single-node k3s install.** The
  bundled chart and the Kubernetes installer are supported for that target, but the chart's values
  and templates are explicitly **not** a versioning interface: a value key may be renamed and a
  template may be replaced in a minor or patch release, and a deployment that overrides chart
  internals follows those changes itself. Treat a pinned copy of the chart as a reference to
  re-check on upgrade, not as a contract.
- **Multi-node clusters are not covered.** The chart and its supporting scripts are built and
  exercised for a single-node cluster; a multi-node deployment is unverified work rather than a
  supported configuration.
- **OpenShift/OKD is a known limitation, not a supported target.** The chart is built to be
  OpenShift-*compatible* — its manifests are meant to be admissible under a restricted SCC — but it
  has **never been verified** on OpenShift or OKD: no release has been installed, exercised or
  upgraded on either. IndraMesh Core 1.0 supports Docker Compose and single-node k3s only. Treat an
  OpenShift/OKD deployment as unverified work rather than as a supported configuration.

## Operator Interfaces Under the Promise

Three interfaces between an installation and the project are part of the promise, even though each
one excludes its values:

- **The documented environment-variable names** — the setting names documented in
  [`docs/CONFIGURATION.md`](CONFIGURATION.md) and present in the versioned `.env.example`. A rename
  goes through a deprecation like any other documented change. The *values* in those variables are
  yours, and default values and validation limits are not part of the promise.
- **The machine-readable error codes of the stable API** — the stable `code` values a client can
  branch on, for example the query-limit codes `QUERY_TOO_COMPLEX` and `QUERY_TIMEOUT`. The message
  texts that accompany them are not part of the promise: they may be reworded at any time.
- **The invocation and flags of the public scripts** — `install_indramesh_docker.sh`,
  `install_indramesh_k8s.sh`, `install_plugin_docker.sh`, `install_plugin_k8s.sh`, `start`, `stop`,
  `rebuild_indramesh_docker.sh` and `rebuild_indramesh_k8s.sh`. Their interface is how each one is
  invoked and which flags it accepts. Their output — the wording, layout and progress lines an
  operator reads — is not part of the promise and may be reworded in any release.

## Deprecation

**Anything deprecated in a 1.x release keeps working until the next major release.** It is not
removed within the 1.x line, so an upgrade within 1.x never removes a documented surface you depend
on; removal happens at the next major version, and the change is a breaking change by definition.

A deprecation is announced in the changelog and in the API documentation. The announcement names
the surface, the replacement, and the release that will remove it; the changelog is published with
each release and is the record to look for when you plan an upgrade. Because the announcement
precedes the removal by at least a major version, a deprecation is a warning you can act on, not a
surprise in a patch release.