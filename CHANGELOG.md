# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
