# Example plugin — frontend reference build

This directory is the reference setup for a plugin frontend bundle (D-04). It is
also the end-to-end proof for the runtime loading contract (PLUGFE-02): the
committed artifact `plugins/example/static/example.js` is built from here and
loaded by the host SPA at runtime, with no frontend rebuild.

## Build

```bash
cd frontend
npm run build:plugin-example
```

This delegates to the plugin-local project in this directory: it runs `npm ci`
against the committed `package-lock.json`, then the plugin-local `build` script,
then `verify-bundle.mjs`. The plugin-local `build` runs one Vite invocation from
this directory:

| Invocation | Config | Output | Role |
|---|---|---|---|
| registration | `vite.config.mjs` | `plugins/example/static/example.js` | lightweight descriptor loaded at login |

The output is committed — the core never builds a plugin bundle at deploy time.
Every dependency resolves from this directory's own lockfile, never through a
parent host `node_modules`.

`verify-bundle.mjs` inspects the built registration bundle and fails the build
on a non-host runtime import, a blob-relative sibling resolution, a separate CSS
asset, an external worker/font URL, or a leaked heavy marker (`monaco`,
`graphiql`, `codicon`); the detector is self-tested at startup.

`vite.config.mjs` is a plain object export on purpose: Vite resolves a config
file's bare imports from the config file's own directory upwards, and there is no
`node_modules` under `plugins/` (only `frontend/node_modules`), so
`import { defineConfig } from 'vite'` would fail to resolve. The entry is
`index.jsx` because Vite applies the JSX transform only to `.jsx`/`.tsx` files;
the automatic runtime emits `import { jsx } from 'react/jsx-runtime'`, which is
one of the externalized specifiers.

## Host specifiers

A bundle may only import the eight bare specifiers the host maps (import map in
`frontend/index.html`):

| Specifier | Resolves to |
|---|---|
| `react` | host React |
| `react/jsx-runtime` | host JSX runtime |
| `react-dom` | host ReactDOM |
| `react-dom/client` | host ReactDOM client |
| `react-router` | host `react-router` |
| `react-router-dom` | host `react-router-dom` |
| `@tanstack/react-query` | host TanStack Query |
| `@indramesh/plugin-api` | host UI kit and shared runtime: `Button`, `Table`, `PageHeader`, `DropdownMenu`/`DropdownMenuItem`/`DropdownMenuSeparator`, `showToast`, `cn`, `LucideIcon`, `useTranslation`; the shared axios client `api` and `serializeRepeatedParams`; `confirmDialog`/`alertDialog`; RBAC `Can`/`useCan`; helpers `extractErrorMessage`, `interpolateDefault`, `pluginNamespace`, `usePluginTranslation`; form controls `FormField`/`TextInput`/`NumberInput`/`SelectInput`/`CheckboxField`; `TableSkeleton`/`PaginationBar`/`EmptyState`; and the shared read hooks `useSources`/`useSource`, `useSchemas`/`useSchema`, `useObjectTypes`/`useObjectType`, `useTargets`/`useTarget`, `useObjects`/`useObject`, `useObjectTypeAttributes`, `useTasks`/`useTask`, `useServiceAccounts`, `useTaskTypes`, `useFormatDate` |

These must be listed in `build.rollupOptions.external` and MUST NOT be bundled —
a second React copy is a second module instance, so the host's hooks (and the
router's context) would read from a different React than the one that rendered
the tree. That is a contract violation and fails the plugin into the `ui_fault`
runtime state; it must never work by accident in one mode. Any other bare import
(or a node_modules package) does not resolve in the browser; bundle it or drop
it.

Import shape: the `react` and `react-dom` shims expose their namespace AND a
`default` export, so both `import { useState } from 'react'` and
`import React from 'react'` work. The other six specifiers are named-export only.

The manifest's `bundle` value is plugin-relative (here `static/example.js`); the
host imports it from
`/api/v1/plugins/<name>/assets/<bundle>?v=<version>`. The `?v=` is the
manifest's `version` and exists purely to defeat a stale cache — bump `version`
in `plugin.toml` whenever you publish a rebuilt artifact. When the UI retries a
failed load it appends an `attempt` parameter, because a browser caches a failed
dynamic import for the exact URL.

Bump `plugin.toml`'s `version` on **every** bundle rebuild and recreate the
backend and worker from the executing checkout
(`docker compose up -d --force-recreate backend worker`). The worker's
`worker_loaded_version` must equal the manifest version or the task type is not
dispatchable (D-12); the live plugin row's version is only refreshed by that
recreation, not by a plain restart.

## Descriptor contract

The bundle's default export is a plain object with optional arrays (D-05):

| Family | Purpose | Required fields | Conflict key |
|---|---|---|---|
| `pages` | Pages under `/plugins/<name>/<path>` | `id`, `path`, `component` | `path` |
| `navItems` | Navigation entries | `id`, `label`, `path` | `id` |
| `attributeEditors` | Attribute editors by `data_type` | `id`, `data_type`, `component` | `data_type` |
| `actionButtons` | ObjectType toolbar buttons | `id`, `objectType`, `component` | `id` |
| `settingsSections` | Appended settings sections | `id`, `label`, `component` | `id` |
| `taskForms` | Form for one plugin task type in Create Task | `id`, `tasktype`, `component` | `tasktype` |
| `connectorConfigEditors` | Connector-specific config controls in Source/Target | `id`, `connector_type`, `label`, `component` (optional `roles`, `labelKey`) | `connector_type` |
| `objectDetailSections` | Inline section on the object detail panel | `id`, `label`, `component` (optional `labelKey`, `permission`) | `id` |
| `schemaSettingsSections` | Inline section on Schema settings | `id`, `label`, `component` (optional `labelKey`, `permission`) | `id` |
| `objectTypeSettingsSections` | Inline section on Edit Object Type | `id`, `label`, `component` (optional `labelKey`, `permission`) | `id` |
| `itemBadges` | Batch-resolved badges next to item names | `id`, `kinds`, `resolve` (optional `permission`) | `id` |

An entry in one of the six families introduced in Phase 291 that is malformed is
dropped individually with a non-transient `entry_invalid` fault; the rest of the
bundle still loads. A malformed `navItems` entry is dropped the same way.

An optional `navItems` entry may additionally declare `labelKey` (resolved
through the plugin's own i18n namespace, `label` as the fallback), `permission`
(`resource:action`; the item is hidden — not disabled — for a user without it)
and `badge` (a hook-style function returning the count; `0`/negative/non-integer
renders no pill, `1`–`99` exact, `>=100` as `99+`; an optional `badgeLabel(count)`
supplies the accessible name). The example's Tools item is
`permission: 'sync:trigger'` with `badge: useExampleTaskCount`.

Every entry carries a stable `id`. Only the families the host applies are
relevant; the first registration per conflict key wins (D-07), and later
registrations are rejected with a recorded warning — never silent, never
last-wins. `frontend.extension_points` in `plugin.toml` must list exactly the
families the bundle registers (D-06): a mismatch in either direction is a UI
fault and none of the bundle's contributions are applied.

A `navItems` entry's `group` matches a static dropdown by its untranslated
canonical name (`Tools`, `Configuration`, `Integration`, `Administration`) and is
appended after that dropdown's static items; an unmatched group renders as its
own root-level dropdown after the static ones. The `icon` value is a
`LucideIcon` name.

Component props: a page and a settings section receive no props (they own their
data), an attribute editor receives `{ attribute, value, onChange, error }`, and
an action button receives `{ objectType }`.

### Component props per family (Phase 291)

| Family | Props | Contract |
|---|---|---|
| `objectDetailSections` | `{ object }` | Render inside the host frame. No own border/card; return plain elements. |
| `schemaSettingsSections` | `{ schema }` | Same frame rules; the host draws the heading and attribution. |
| `objectTypeSettingsSections` | `{ objectType, schemaId }` | `schemaId` is the persisted schema id (never the in-progress form value). |
| `taskForms` | `{ taskType, value, onChange, disabled }` | Controlled. `value` is `{ scope, plugin_parameters }`; call `onChange({ scope, plugin_parameters, valid })` on every change (and once on mount). Only an explicit `valid: false` keeps Trigger disabled. The host owns the type select, permanent/name fields, banners, footer and the POST; the plugin owns scope + parameters and the ownership rule (a form only applies to its own plugin's catalog type). |
| `connectorConfigEditors` | `{ role, connectorType, value, onChange, record }` | `value` and `onChange` carry **only `config`**. The host owns Name, Type, Source URL, and (Target) Target URL/Schema (`target_url`/`schema_id`) and the CSV export folder — never put those in `config`. Optional `targetFields: { requiresUrl?, requiresSchemaId? }` independently require the host-drawn URL/Schema inputs for that type; both inputs stay visible but optional without the flags. Align the flags with your backend `ConnectorTypeDeclaration` `requires_url`/`requires_schema_id`; the server declaration is authoritative when metadata is absent or wrong. |
| `itemBadges` | `resolve({ kind, ids, context })` | Called once per rendered list with the sorted, de-duplicated visible ids (never once per row). Return a `Map`/object of `id -> [{ label, icon?, title? }]` (data only, never markup/colours). Items show at most two, then a `+N` pill. Invalidate with `['plugin-item-badges', '<plugin>']`. |

### Translations (FEEXT-04, D9)

A descriptor may declare `translations: { <lng>: { <key>: string } }`. The host
installs it under the reserved namespace `plugin-<name>` (never core `systemui`).
Keys must be non-empty strings without `:` (the host does not override i18next's
namespace separator). Translate with `usePluginTranslation('<name>')` (or
`useTranslation` from `@indramesh/plugin-api`); fill `{{vars}}` with
`interpolateDefault` (never `react-i18next` directly — it is not an import-map
specifier and would be bundled). Switching the UI language re-renders plugin
labels, section titles, nav labels, badge labels and task-form copy **without a
reload**; the fallback order is active language → plugin English → the
`t(key, default)` default. A missing/invalid translation file is a console
warning, never a UI fault.

### Query-key invalidation vocabulary

A plugin may invalidate these core query-key prefixes after a cache-changing
write: `['objects']`, `['object-types']`, `['schemas']`, `['sources']`,
`['targets']`, `['tasks']`, `['task-types']`. Item badges use
`['plugin-item-badges', '<plugin>']`.

An `attributeEditors` entry REPLACES the core editor for that `data_type` in
EVERY object form of the instance, and D-07 first-wins makes the first plugin
that registers a type its permanent owner. Ship a functional superset of the
core editor (a real input bound to the `value`/`onChange` props) and avoid
ubiquitous types such as `text` unless taking over that type is the plugin's
purpose — the example's own entry targets `url` for exactly this reason.

## Reserved path segments

Plugin routes are mounted by the core under `/api/v1/plugins/<name>/`; the
segments `health`, `config`, `state` and `assets` are owned by the core routes
and can never be claimed by a plugin-declared path.

## Known limitations

- Bundle serving requires a plugin DIRECTORY under `INDRAMESH_PLUGINS_DIR`
  (`./plugins` in development). An entry-point-installed plugin
  (`importlib.metadata`, no directory) has no assets route in this phase —
  packaged-plugin bundle serving is deferred.
- An `actionButtons` entry matching by ObjectType NAME is not unique because
  ObjectType names are schema-scoped. Authors should use the ObjectType `id` or
  the `'*'` wildcard.
- The admin config form can replace a stored secret but never CLEAR one: an
  empty password field is treated as "unchanged". Clearing goes through
  `PUT /api/v1/plugins/<name>/config` directly.