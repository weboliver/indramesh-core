# example plugin

Opt-in, registration-only **reference plugin** — the plugin you read to write your own. It is
deliberately not default-active and demonstrates the extension-point and capability surface in a
lightweight shape.

## Purpose

Since Phase 308 the example carries **no heavy editor**: the Monaco/GraphiQL chain, the two-stage
Vite build and the generated `example-reference` artifact are gone, and `plugin.toml` declares no
`[frontend.deferred_assets]` entry at all. What remains is the lightweight reference for the plugin
runtime — capability declarations, permission declarations, connector/task registration, and a
frontend bundle that exercises the extension-point families at registration size. The real
deferred/heavy contract is demonstrated by `graphql-studio`.

- Manifest: `plugin.toml` v0.7.0 (`capabilities = ["api_routes", "task_type", "connector"]`,
  `requires_worker_update = true`, `active_by_default = false`).
- Backend: `module.py` + `database.py` + `migrations/` (private `alembic_version_example` chain).
- Frontend build contract and host-specifier rules: [`frontend/README.md`](frontend/README.md).
- Committed artifact: `static/example.js` (registration bundle only).

## Install

**Requires IndraMesh Core 1.0.0 or later** (plugin API `1.0.0`). Unlike the other first-party
plugins, this one ships **inside the Core tree** and has no repository of its own: it is already
present in the Core checkout at `plugins/example`, and a Compose stack already discovers it — switch
it on in Administration → Plugins, because its manifest declares `active_by_default = false`. There
is deliberately no Compose install command here: installing the plugin from the checkout it already
sits in would be refused, since the source and the destination are the same directory.

On the local k3s stack the plugins PVC starts empty, so the plugin is installed from the checkout:

```bash
./install_plugin_k8s.sh plugins/example
```

The script copies the plugin from the checkout onto the plugins PVC and restarts `backend` and
`worker` so the registry re-scans — plugin discovery runs exactly once at process startup, so the
restart is what activates it. It refuses an existing destination and offers no way around that
refusal: replacing an installed plugin means removing its directory on the claim deliberately first
and installing again. See **Installing and Updating Plugins** in the Core README for the removal
commands, including the writable helper pod for the k3s case.

**Manual equivalent:** copy the directory to `plugins/example` in a Compose checkout yourself, or
onto the plugins PVC on Kubernetes, and restart `backend` **and** `worker`.

## Permissions

| Permission | Default roles | Purpose |
|---|---|---|
| `example-tools:read` | Administrator, Editor, Viewer | Read access declared by the reference plugin. |
| `example-tools:manage` | Administrator | Manage access; required by the hidden maintenance task type. |

Declared in `plugin.toml` (`[[permissions]]`); applied insert-only on first discovery.

## Configuration

`[config_schema]` declares two properties:

| Property | Type | Notes |
|---|---|---|
| `greeting` | string | Default `hello` — a plain, non-secret reference property. |
| `api_token` | string (`format = password`) | Stored encrypted; the admin form can replace but never clear a stored secret. |

Values are per-plugin config rows managed in Administration → Plugins (or
`PUT /api/v1/plugins/example/config`). No environment variable, no LLM and no key requirement.

## Task types

| Task type | Required permission | Notes |
|---|---|---|
| `example.cleanup-assets` | `sync:trigger` | Schema-scoped; schedulable; the visible reference type. |
| `example.hidden-maintenance` | `example-tools:manage` | `catalog_visible = false` — hidden from the generic Create Task picker only; still startable, retryable and present in the task list. |

## Connectors

One connector type, `example` (label `Example Connector`), source role only, `requires_url` — the
reference for a connector's `config_model` and config editor.

## Frontend

Bundle `static/example.js` — registration only, no heavy or deferred asset. Extension points
(15): `pages`, `navItems`, `attributeEditors`, `actionButtons`, `settingsSections`, `taskForms`,
`connectorConfigEditors`, `objectDetailSections`, `schemaSettingsSections`,
`objectTypeSettingsSections`, `itemBadges`, `schemaWizardEntries`, `translationPanelActions`,
`translationAdminActions`, `taskDetailActions`. Since it is not default-active, activate it in
Administration → Plugins (or `PUT /api/v1/plugins/example/state`) to load it.

## License

AGPL-3.0-or-later — see [`LICENSE`](LICENSE); it is the same license text as IndraMesh Core's.
