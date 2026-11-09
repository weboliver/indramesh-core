<!-- generated-by: gsd-doc-writer -->
# API Reference

IndraMesh Core exposes a single FastAPI application (`backend/app/main.py`) across **three** API
surfaces, all served by the same `backend` process:

- **Internal REST API** — `/api/v1`, JWT-authenticated, covers the full 17-entity hierarchy plus
  RBAC, tasks, search and plugin management. This is the bulk of this document.
- **Internal GraphQL API** — `/api/v1/graphql`, a read-only Strawberry schema over the same data,
  authenticated with the same JWT session; registered only outside production. See [GraphQL API](#graphql-api-apiv2graphql).
- **Public API** — `/public/api/v1`, a separate REST (+ its own GraphQL mount) realm authenticated
  exclusively by `ApiClient` bearer tokens (never the internal JWT), intended for external
  integrations. See [Public API](#public-api-publicapiv1).

Additionally, `/api/v1/aql` (and a public-realm equivalent under `/public/api/v1/aql`) exposes
**AQL**, a custom Jira-Assets-style query language that compiles to a hybrid
OpenSearch + PostgreSQL execution plan. See [AQL — Custom Query Language](#aql--custom-query-language).

Core internal REST routers are registered in `main.py` with `/api/v1`. Plugin-declared
routers are added at startup under `/api/v1/plugins/{name}`. Unless a table explicitly
shows `/api/...` or `/public/...`, its data paths below are relative to `/api/v1`;
operational paths in the first table are absolute.
Use the backend's `/docs`, `/redoc` and `/openapi.json` for the deployed request schemas;
installed plugin routes depend on startup discovery. Operational endpoints include:

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/health` | None | Liveness probe used by Docker Compose's healthcheck and the SPA's runtime version read (proxied same-origin by nginx's `location = /health`). Returns `{"status": "healthy", "version": "<app version>"}` — exactly those two keys, where `version` is `backend/app/version.py`'s `__version__` (e.g. `1.0.0`) and never a build sha or environment name. Exempted from rate limiting (`@limiter.exempt`). |
| `GET` | `/api/v1/` | None | Root welcome message: `{"message": "Welcome to API"}`. Exempted from rate limiting. |
| `GET` | `/metrics` | None at application level | Prometheus scrape endpoint; excluded from the OpenAPI schema. Restrict access at the deployment perimeter. |

Data routes require JWT authentication or public API-client authentication as described below.
Login and FastAPI's documentation/OpenAPI endpoints are not authenticated data routes.

## Authentication & Authorization

IndraMesh Core uses **JWT bearer tokens** (`HS256`, `PyJWT`), issued via `POST /api/v1/auth/login`
and validated by `app/services/auth_service.py::get_current_user` on every protected route.
The browser session is the same credential pair seen from the other side: a short-lived access
token sent as a bearer header, plus a refresh credential held in an `HttpOnly` cookie that the
browser exchanges for a new access token without a login.
Authorization is enforced by a role-based access control (RBAC) system layered on top of that
token — see [Users, Roles & Permissions](#users-roles--permissions) below for the resource model.

| Method | Path | Auth | Description |
|---|---|---|---|
| `POST` | `/auth/login` | None (rate-limited) | OAuth2 password grant (`OAuth2PasswordRequestForm`: `username` + `password` form fields). Returns `{"access_token": "...", "token_type": "bearer"}` and sets the `indramesh_refresh` cookie. Rate-limited to `RATE_LIMIT_LOGIN` (default `5/minute`, env-configurable) via a dedicated `login_limiter` that is **always active** regardless of `RATE_LIMIT_ENABLED`. |
| `POST` | `/auth/refresh` | Refresh cookie | Mints a fresh access token from the `indramesh_refresh` cookie and rotates that cookie, keeping the same session id and the same absolute deadline. Reads only the cookie — no `Authorization` header and no request body — and returns the same `Token` body as login. `401` when the cookie is absent, malformed, past either lifetime bound, revoked or minted before a password change; `503` when the revocation store is unreachable. |
| `POST` | `/auth/logout` | Refresh cookie (optional) | Ends the browser session. The cookie is expired on the response first, then the session id is blocklisted (and the presented bearer token's `jti`, when an `Authorization` header is sent), so the same cookie can no longer refresh. Answers `200` with `{"message": "Signed out"}`; if the revocation store is unreachable the cookie is still cleared and the answer is `503` with `Message: Signed out locally; revocation store unavailable`, never a `500` that leaves the session alive. |
| `GET` | `/auth/me` | Bearer | Returns the current user as `UserResponse`. |
| `GET` | `/auth/me/permissions` | Bearer | Returns the caller's effective permission names as a JSON string array. |
| `POST` | `/auth/revoke` | Bearer | Self-service logout. Decodes the caller's own JWT, extracts its `jti` claim, and adds it to a Valkey-backed blocklist for the token's remaining TTL — any authenticated user may revoke their own token, this is not admin-only. |

**Sending credentials:**

```bash
curl -X POST http://localhost:8000/api/v1/auth/login \
  -d "username=admin&password=$ADMIN_PASSWORD"

curl http://localhost:8000/api/v1/objects/ \
  -H "Authorization: Bearer <access_token>"
```

**Login hardening:**
- The two failure answers are identical and the credential check costs the same either way: an
  unknown username is verified against a fixed dummy bcrypt digest, so the median difference at the
  credential-check level stays within `LOGIN_TIMING_TOLERANCE_SECONDS` (**50 ms**).
- A per-account throttle **delays** the next attempt instead of locking the account: from the third
  recorded failure onwards each further attempt is delayed 0.5 s, 1 s, 2 s, 4 s and then capped at
  **8 s**, and the counter decays after 15 minutes. Nothing is ever locked out — a hard lockout
  would itself be a denial-of-service vector against a known account. The per-IP `login_limiter`
  above stays in place beside it, the submitted username is only stored as a SHA-256 digest, and an
  unreachable Valkey means zero delay (fail-open), never a failed login.

**Token internals** (`app/services/auth_service.py`, `app/services/session_service.py`):
- Signing key: `JWT_SECRET_KEY` env var, at least **32 characters**. Startup (`app/main.py`) and the
  lazy loader (`get_secret_key`) share one validation rule and refuse a shorter value with
  `JWT_SECRET_KEY must be at least 32 characters`; HS256 is symmetric, so a short key is
  brute-forceable offline from a single known token.
- **Lifetimes.** An access token lives **15 minutes** (`exp - iat == 900`). The refresh credential
  behind the `indramesh_refresh` cookie is valid for **30 days sliding** — every rotation restarts
  the 8-hour idle window — with a hard **30-day ceiling measured from login**, after which a new
  login is required. Access tokens issued with a longer lifetime are refused (`exp - iat` above
  15 minutes plus the clock-skew tolerance), which is what makes the 1.0.0 → 1.0.1 upgrade force
  one re-login per user.
- Access and refresh credentials share the HS256 key but not the claim (`typ`): a refresh credential
  presented as a bearer token is refused. The cookie itself is scoped to `Path=/api/v1/auth` and
  carries `HttpOnly`, `SameSite=Strict`, and `Secure` when the request arrived over HTTPS.
- `imsh_` API-client bearer tokens — the service-account clients of the public API — are unchanged:
  they authenticate with `Authorization: Bearer imsh_…` under `/public/api/v1/*`, receive no
  cookie, and are not subject to the access-token lifetime above.
- Every token carries a unique `jti` claim, enabling the revoke/blocklist flow above.
- `CLOCK_SKEW_TOLERANCE_SECONDS` (default `10`) tolerates clock drift on the `nbf` claim.
- `MAX_TOKEN_AGE_SECONDS` is an optional additional absolute-age ceiling on top of the token's own
  `exp` claim; unset by default (only `exp` is enforced).
- A revoked token's `jti` and a logged-out session's `sid` are checked against the Valkey blocklist
  on every request via `blocklist_state` (one `MGET` for both namespaces). The check is
  **fail-closed**: when the revocation store is unreachable, a protected route and `/auth/refresh`
  answer `503` with `Retry-After` instead of treating the credential as valid.

**Authorization dependencies** — three FastAPI dependencies gate every route, defined in
`app/services/auth_service.py`:
- `get_current_user` — any authenticated, non-revoked user. No further permission check.
- `require_permission(resource, action)` — a dependency **factory**. Requires the caller to hold
  the `"{resource}:{action}"` permission (e.g. `"sources:create"`, `"tasks:delete"`) across any of
  their assigned roles. Superusers bypass this check with zero DB/cache queries. Non-superusers are
  checked against `resolve_user_permissions()`, which resolves the union of all role-granted
  permissions minus any explicitly denied (`is_deny=True`) permission — **deny wins globally**,
  regardless of which role granted it. Raises `403` with `Not authorized. Missing permission:
  {resource}:{action}.` on failure. Results are cached in Valkey for 60 seconds per user
  (`resolve_user_permissions`).
- `require_record_permission(resource, action, entity_type, id_param, require_type_level=True)` — a
  stricter factory used on single-record `GET`/`PATCH`/`DELETE` routes for `Source`, `ObjectType`,
  `Schema`, `AttributeType`, and `Object`. Composes the type-level check above with a **record-level**
  override resolved via `RecordPermission` rows (see below): type-level `DENY` is final; otherwise an
  explicit record-level `DENY` returns `403`; an explicit record-level `ALLOW` or no record entry at
  all falls through to the type-level result. `require_type_level=False` (used only on `GET
  /sources/{id}`) skips the type-level check entirely: type-level grants are not needed
  to read a source; an explicit record DENY still blocks access.
- `require_superuser` is defined in `auth_service.py` but is **not used by any router** in the
  current codebase — legacy superuser-only gating has been fully migrated to the `require_permission`
  RBAC system described above.

`UserResponse` shape (`app/schemas/auth.py`):
```json
{
  "id": "00000000-0000-4000-8000-000000000001",
  "username": "admin",
  "email": "admin@example.com",
  "is_active": true,
  "is_superuser": true
}
```

Non-superusers additionally have hidden-attribute values (`ObjectTypeAttribute.is_hidden == true`)
filtered out at the SQL level on `GET /object-values/` and `GET /object-values/{id}` — a
superuser sees every value, others never see hidden-attribute values even via direct ID lookup.

## Users, Roles & Permissions

Three routers implement the RBAC model consumed by `require_permission`/`require_record_permission`
above. All endpoints in this section require `get_current_user` plus the specific `require_permission`
noted per route (superusers bypass every check).

### Users — `/users` (`app/api/users.py`)

| Method | Path | Permission | Description |
|---|---|---|---|
| `GET` | `/users/me/profile` | any authenticated user | Returns the caller's own `UserProfileResponse`; auto-creates the profile row if none exists. |
| `PATCH` | `/users/me/profile` | any authenticated user | Updates the caller's own profile fields. |
| `POST` | `/users/me/change-password` | any authenticated user (rate-limited: `RATE_LIMIT_PASSWORD_CHANGE`, default `5/minute`) | Body: current + new password. Bumps the user's `token_version`, so **every** live access token and refresh cookie of that user — including the calling one — stops working; a suspected compromise is ended by the password change itself. No blocklist entry is needed: the epoch is the mechanism (see `/auth/refresh` above). |
| `POST` | `/users/` | `users:create` | Create a user. Returns `UserAdminResponse`, `201`. |
| `GET` | `/users/` | `users:read` | List users. Filters: `skip`, `limit` (default `REFERENCE_DEFAULT_LIMIT`), `q` (username/email substring match). `X-Total-Count` header set. |
| `GET` | `/users/picker` | `objects:read` | Restricted user-picker projection (`UserPickerResponse`), not the user-administration response. |
| `GET` | `/users/{user_id}` | `users:read` | Fetch one user. `404` if missing. |
| `PATCH` | `/users/{user_id}` | `users:update` | Partial update. Self-protection: a user cannot deactivate (`is_active=false`) or remove their own superuser flag via this route — `400`. |
| `POST` | `/users/{user_id}/reset-password` | `users:update` + superuser | Body: `PasswordReset` (`new_password`, 8–255 characters). Sets another account's password without the old one and ends every live session of that user (token version bump). `403` for a non-superuser, `400` for your own account (use `/users/me/change-password`), `404` for an unknown user or a service account. |
| `DELETE` | `/users/{user_id}` | `users:delete` | `400` if attempting to delete your own account. |
| `POST` | `/users/{user_id}/roles` | `users:manage` | Body: `UserRoleAssign` (`role_id`). Assigns a role to a user. `409` on duplicate assignment. |
| `DELETE` | `/users/{user_id}/roles/{role_id}` | `users:manage` | Removes a role assignment. Self-lockout guard: a non-superuser removing their own **last** role gets `403`; superusers and removals of other users' roles are unrestricted. `404` if the assignment doesn't exist. |
| `GET` | `/users/{user_id}/groups` | `users:read` | List the user's group memberships. |
| `POST` | `/users/{user_id}/groups/{group_id}` | `groups:manage` | Add group membership. |
| `DELETE` | `/users/{user_id}/groups/{group_id}` | `groups:manage` | Remove group membership with self-lockout protection. |

**Profile avatar (deprecated in the interface):** `UserProfileResponse.avatar_url` is still stored
and returned by `GET`/`PATCH /users/me/profile`, but the administration interface no longer renders
it — the avatar surfaces show initials instead, so a viewer's browser never contacts a third-party
host on the profile's behalf. A stored value remains valid and is left untouched; a later
server-side proxy can start rendering it again without a data migration.

### Roles — `/roles` (`app/api/roles.py`)

| Method | Path | Permission | Description |
|---|---|---|---|
| `POST` | `/roles/` | `roles:create` | Create a role. |
| `GET` | `/roles/` | `roles:read` | List roles (`RoleListResponse`). Filters: `skip`, `limit`, `q` (name substring). |
| `GET` | `/roles/{role_id}` | `roles:read` | Fetch one role. |
| `PATCH` | `/roles/{role_id}` | `roles:update` | Partial update. |
| `DELETE` | `/roles/{role_id}` | `roles:delete` | `403` if the role is a system role (`Role.is_system == true`) — system roles cannot be deleted. |
| `POST` | `/roles/{role_id}/permissions` | `roles:manage` | Body: `RolePermissionAssign` (`permission_id`, `is_deny`). Assigns (or denies) a permission on the role. |
| `DELETE` | `/roles/{role_id}/permissions/{permission_id}` | `roles:manage` | Removes a role-permission assignment. `404` if not found. |
| `GET` | `/roles/{role_id}/groups` | `roles:read` | List groups assigned to the role. |

### Groups — `/groups` (`app/api/groups.py`)

A `Group` bundles users so they can share role assignments in one place (`groups_roles` join) instead
of assigning the same role to every member individually — a user's effective permissions are the
union of their own direct roles plus every role granted through group membership
(`resolve_effective_role_ids`).

| Method | Path | Permission | Description |
|---|---|---|---|
| `POST` | `/groups/` | `groups:create` | Create a group. `409` on duplicate `name`. |
| `GET` | `/groups/` | `groups:read` | List groups. Filters: `skip`, `limit` (default `REFERENCE_DEFAULT_LIMIT`=200, max `1000`), `ids` (repeated — returns exactly those groups, ignoring `skip`/`limit`), `q` (name substring, case-insensitive), `role_id` + `assigned` (roles-editor filter: `assigned=true` restricts to groups holding that role, `assigned=false` to groups that don't). `X-Total-Count` header set. |
| `GET` | `/groups/{group_id}` | `groups:read` | Fetch one group. `404` if missing. |
| `PATCH` | `/groups/{group_id}` | `groups:update` | Rename a group. `409` on duplicate name. |
| `DELETE` | `/groups/{group_id}` | `groups:delete` | Deletes the group and its `groups_roles`/`users_groups` rows. Self-lockout guard: simulates the removal for the calling user rather than assuming it costs at most one role — a non-superuser member who would end up with zero effective roles after losing every role this group grants gets `403`. |
| `GET` | `/groups/{group_id}/members` | `groups:read` | Flat member list (single join on `users_groups`→`users`, no N+1). `skip`/`limit` pagination (default `REFERENCE_DEFAULT_LIMIT`). Returns `List[UserResponse]`. |
| `POST` | `/groups/{group_id}/members/{user_id}` | `groups:manage` | Add a user to the group. `409` on duplicate membership. |
| `DELETE` | `/groups/{group_id}/members/{user_id}` | `groups:manage` | Remove a user from the group. Self-lockout guard: a non-superuser removing their own membership gets `403` if it would leave them with zero effective roles. |
| `GET` | `/groups/{group_id}/roles` | `roles:read` | List the roles assigned to a group (`List[RoleListResponse]`). `skip`/`limit` pagination. |
| `POST` | `/groups/{group_id}/roles/{role_id}` | `roles:manage` | Assign a role to a group (`groups_roles`). `409` on duplicate assignment. Invalidates every member's cached permission set. |
| `DELETE` | `/groups/{group_id}/roles/{role_id}` | `roles:manage` | Remove a role from a group. Invalidates every member's cached permission set. Self-lockout guard: simulates losing just this one role (not the group's entire contribution) — a non-superuser member gets `403` only if this specific role removal would leave them with zero effective roles. |

Note the asymmetric gating: membership changes (`POST`/`DELETE /groups/{id}/members/{user_id}`) require
`groups:manage`, while changing what roles a group grants (`POST`/`DELETE /groups/{id}/roles/{role_id}`)
requires `roles:manage` — group-role assignment is treated as role-editing, not group-editing.

### Permissions — `/permissions` (`app/api/permissions.py`)

| Method | Path | Permission | Description |
|---|---|---|---|
| `GET` | `/permissions/` | `roles:read` | Lists every `Permission` row (the fixed catalog of `{resource}:{action}` strings), ordered by resource then action. Read-only — permissions themselves are seeded, not created via the API. |

### Record Permissions — `/record-permissions` (`app/api/record_permissions.py`)

Per-record RBAC overrides on top of the type-level `Permission` catalog above, consumed by
`require_record_permission`. All endpoints require `roles:manage`.

| Method | Path | Description |
|---|---|---|
| `POST` | `/record-permissions/` | Body: `RecordPermissionCreate` (`role_id`, `record_entity_type`, `record_id`, `action`, `is_deny`). Creates a single record-level grant/deny entry. One entry per request — no bulk creation. |
| `GET` | `/record-permissions/` | List entries, filterable by `role_id` and/or by `record_entity_type` + `record_id` (must be supplied together — `422` if only one is set). `skip`/`limit` pagination, `X-Total-Count` header. |
| `DELETE` | `/record-permissions/{permission_id}` | Delete by primary key. `404` if not found. |
| `GET` | `/record-permissions/effective?record_entity_type=...&record_id=...` | Returns the resolved (inheritance-aware) effective permissions for one record across **all** roles (not scoped to the caller's own roles), distinguishing direct vs. inherited grants. `record_entity_type` must be one of `source`, `schema`, `object_type`, `attribute_type`, `object`. `400` on an unrecognized entity type. |

## Principal Resource Families

The API exposes these 17 principal resource families (see [Architecture](ARCHITECTURE.md)
for relationships). This inventory is not a single parent-child chain:

```
Source, Schema, ObjectType, Object, ObjectTypeAttribute, ObjectValue, ObjectRef,
AttributeType, ReferenceType, Status, Import, Export, ImportStatus, ExportStatus,
Task, Target, ServiceAccount
```

Every one of these 17 entities has its own router in `backend/app/api/` and follows the same
CRUD convention (documented once in [Standard CRUD Conventions](#standard-crud-conventions)
rather than repeated per resource). A number of other routers (`auth`, `search`,
`schema-templates`, `templates`, `config`, `users`, `roles`, `groups`, `permissions`,
`record-permissions`, `export-definitions`, `import-definitions`,
`saved-queries`, `settings`, `audit-logs`, `audit-log-config`, `languages`,
`translation-types`, `translations`, `ui-translations`) expose additional non-CRUD action
endpoints or sit outside the 17-entity hierarchy entirely, documented in their own sections.

## Standard CRUD Conventions

All 17 principal resources plus `Icon` and `Schedule` provide the following CRUD
methods, implemented one router file per resource under `backend/app/api/`.
Specialized validation, permissions and side effects still differ by resource:

| Method | Path | Status | Description |
|---|---|---|---|
| `POST` | `/{resource}/` | `201` | Create. Body is the resource's `XxxCreate` schema. |
| `GET` | `/{resource}/` | `200` | List, paginated. Returns `List[XxxResponse]`, total count in `X-Total-Count` response header. |
| `GET` | `/{resource}/{id}` | `200` / `404` | Fetch one by UUID. |
| `PATCH` | `/{resource}/{id}` | `200` / `404` | Partial update. Body is the resource's `XxxUpdate` schema (all fields optional). |
| `DELETE` | `/{resource}/{id}` | `204` / `404` | Delete. Cascades along the hierarchy per the `ON DELETE CASCADE` rules in [Architecture — Asset Data Model](ARCHITECTURE.md#asset-data-model). |

**Common list-endpoint query parameters** (present on every `GET /{resource}/`, unless noted):
- `skip` (int, default `0`, `>= 0`) — pagination offset.
- `limit` (int, `1`–`1000`) — page size, default varies by resource group: `200` for lightweight reference resources (sources, schemas, attribute-types, reference-types, statuses, object-types, object-values, object-refs, users, roles, record-permissions); `100` for the rest (tasks, imports, exports, import-statuses, export-statuses, targets, service-accounts, icons, schedules, object-type-attributes); `50` for `objects` and `schema-templates`.
- Resource-specific filter params (documented per resource below), e.g. `source_id`, `schema_id`,
  `object_type_id`.

**Pagination header:** standard CRUD lists set `X-Total-Count`. Specialized collections
(for example plugin discovery and object comments/files) do not necessarily use this header.

**Resource-to-router map:**

| Resource | Path prefix | Router file | Extra list filters | Auth |
|---|---|---|---|---|
| Source | `/sources` | `app/api/sources.py` | — | Create: `require_permission("sources", "create")`; list: `get_current_user` (unscoped by design — no type-level read grant for non-superusers); get/update/delete: `require_record_permission("sources", ...)` (get skips the type-level check) |
| Schema | `/schemas` | `app/api/schemas.py` | `source_id` | Create: `require_permission("schemas", "create")`; list: `require_permission("schemas", "read")`; get/update/delete: `require_record_permission("schemas", ...)` |
| ObjectType | `/object-types` | `app/api/object_types.py` | `schema_id` | Create: `require_permission("object-types", "create")`; list: `require_permission("object-types", "read")`; get/update/delete: `require_record_permission("object-types", ...)`. `POST /object-types/` resolves the record write decision on two containers — the owning `Schema` and, when one is supplied, the parent `ObjectType`: an invisible container answers the route's existing missing-container answer (`404`), a readable but non-writable one answers `403`. |
| Object | `/objects` | `app/api/objects.py` | `object_type_id`, `q`, `sort_by`, `sort_order`, `filter_tree`, `attr_filters` (see below) | Create/list: `require_permission("objects", ...)`; get/update/delete: `require_record_permission("objects", ...)`. `POST /objects/` additionally resolves the record **write** decision on the target `ObjectType` container: an invisible container answers the route's existing missing-container answer (`404`), a readable but non-writable one answers `403` — both before the first write, so no partial row survives a refusal. |
| ObjectTypeAttribute | `/object-type-attributes` | `app/api/object_type_attributes.py` | `object_type_id` | `require_permission("object-type-attributes", <action>)` plus the record write decision on the `ObjectType` container: an invisible container answers the route's existing missing-container answer (`404`), a readable but non-writable one answers `403`; `DELETE` resolves the separate `delete` action, never the `write` umbrella, so a `write` DENY does not block a delete and a `delete` DENY does. |
| ObjectValue | `/object-values` | `app/api/object_values.py` | `object_id`, `object_ids` (batch); `GET /object-values/distinct` sub-route | `require_permission("object-values", <action>)` |
| ObjectRef | `/object-refs` | `app/api/object_refs.py` | `from_object_id`, `to_object_id`, `object_ids` (batch) | `require_permission("object-refs", <action>)`; record-level read `DENY` (quick 261012-cpp): for non-superusers a ref whose from or to object (or that object's type, schema or source) is DENY'd is omitted from lists and `X-Total-Count`, and `GET`/`PATCH`/`DELETE /{id}` answer `404 Object reference not found` as for an unknown id; a DENY'd `from_object_id`/`to_object_id` on create/update answers the same `404` as a nonexistent one; filtered callers bypass the shared list cache. Record-level **write** decision (D-02 as revised 2026-11-01): creating, changing or moving a reference requires `write` on its source (`from`) object — a readable source with a `write` DENY answers `403`; the target (`to`) only has to be readable, so a write-protected catalog object (a location, a vendor) stays linkable; an invisible end on either side keeps the existing `404`. |
| AttributeType | `/attribute-types` | `app/api/attribute_types.py` | — | Create/list: `require_permission("attribute-types", ...)`; get/update/delete: `require_record_permission("attribute-types", ...)` |
| ReferenceType | `/reference-types` | `app/api/reference_types.py` | `schema_id` | `require_permission("reference-types", <action>)` |
| Status | `/statuses` | `app/api/statuses.py` | — | `require_permission("statuses", <action>)` |
| Import | `/imports` | `app/api/imports.py` | — | `require_permission("imports", <action>)`; `POST` also creates a `Task` and enqueues `process_import` |
| Export | `/exports` | `app/api/exports.py` | — | `require_permission("exports", <action>)`; `POST` also creates a `Task` and enqueues `process_export` |
| ImportStatus | `/import-statuses` | `app/api/import_statuses.py` | — | `require_permission("imports", <action>)` |
| ExportStatus | `/export-statuses` | `app/api/export_statuses.py` | — | `require_permission("exports", <action>)`; the list and by-id routes additionally stay inside the caller's own exports (`visible_export_ids_select`), and a superuser is unrestricted |
| Task | `/tasks` | `app/api/tasks.py` | `task_type`, `current_status`, `schedule_id_null`, `is_permanent` | `require_permission("tasks", <action>)`; plus `/types`, `/retry` and `/cancel` (below) |
| Target | `/targets` | `app/api/targets.py` | — | `require_permission("targets", <action>)` |
| ServiceAccount | `/service-accounts` | `app/api/service_accounts.py` | `source_id` | `require_permission("service-accounts", <action>)` |
| Icon | `/icons` | `app/api/icons.py` | — | `require_permission("icons", <action>)` |
| Schedule | `/schedules` | `app/api/schedules.py` | — | `require_permission("schedules", <action>)`; plus `POST /schedules/preview` (gated on `schedules:read`) and `POST /schedules/{id}/run` (gated on `schedules:update`), both below |
| Classification label | `/classification-families` (read), `/classification-labels` (maintain) | `app/api/classification.py` | `lang` (families only) | Families: `require_permission("object-type-attributes", "read")`; label create/update/delete: `require_permission("settings", "update")` — see [Classification Labels](#classification-labels) |

`<action>` above is one of `create`/`read`/`update`/`delete`, matched to the HTTP verb (e.g. `GET
/tasks/` requires `tasks:read`). Superusers bypass every `require_permission`/
`require_record_permission` check with zero additional DB/cache queries.

**Standard error responses:** route handlers raise `HTTPException` directly (no custom exception
classes) — a `404` returns `{"detail": "<Resource> not found"}`; validation errors on the request
body return FastAPI's standard `422 { "detail": [...] }` envelope. Any *unhandled* exception is
caught by `ExceptionHandlerMiddleware` (registered 3rd in `main.py`; `RequestTimingMiddleware`,
registered last, is the actual outermost middleware) and normalized to a `500` with body
`{"detail": "Internal server error"}`. This catch-all applies to unhandled errors, not to
explicit `HTTPException` details supplied by a handler (for example atomic schema creation).

**Example — create then fetch a connector-neutral `Source`:**
```bash
curl -X POST http://localhost:8000/api/v1/sources/ \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"name": "CSV intake", "type": "CSV"}'
# -> 201 SourceResponse with id, name, type, source_url, config,
#         import_folder, created_at and updated_at

curl http://localhost:8000/api/v1/sources/<id> -H "Authorization: Bearer $TOKEN"
```

## Object Endpoints — Advanced Filtering

`GET /objects/` supports three additional, mutually-composable filtering mechanisms beyond the
standard `object_type_id`/`skip`/`limit`, implemented in `app/api/objects.py` +
`app/services/filter_utils.py`:

- `q` (string) — case-insensitive substring match on `Object.name`.
- `sort_by` / `sort_order` — `sort_by` currently only accepts `name`; `sort_order` accepts `asc`
  or `desc`. Any other value returns `422`.
- `filter_tree` (JSON string, URL-encoded) — a recursive AND/OR filter tree:
  ```json
  {
    "op": "AND",
    "conditions": [
      {"attribute_id": "<uuid>", "op": "contains", "value": "server"}
    ],
    "groups": [
      {"op": "OR", "conditions": []}
    ]
  }
  ```
  Valid `op` values per condition: `eq`, `gt`, `lt`, `gte`, `lte`, `between`, `contains`,
  `starts_with`, `before`, `after`. An unknown top-level `op` (must be `AND`/`OR`) or an unknown
  condition `op` returns `422` before any DB query runs. Type/operator mismatches caught later
  inside `build_filter_clause` (e.g. `gt` on a text attribute) also surface as `422`.
- `attr_filters` (JSON string) — a flatter, non-nested alternative: `{"<attribute_id>": {"op":
  "eq", "value": "..."}}`. Ignored when `filter_tree` is also supplied.

`GET /objects/counts` — batch object-count lookup: `object_type_ids` (repeated query param,
required) returns `{"<object_type_id>": <count>, ...}` in one call, avoiding N separate
`limit=1` list requests.

## Object Values — Distinct Values

`GET /object-values/distinct?object_type_id=<uuid>&attribute_id=<uuid>` returns the distinct values
recorded for one `(object_type, attribute)` pair, with counts, sorted by frequency descending —
used to power faceted-filter UI. Response: `List[{"value": <any>, "count": <int>}]`. The underlying
value column queried (`value_simple`, `value_float`, `value_integer`, `value_boolean`,
`value_datetime`, `value_struct`) is selected based on the attribute's `AttributeType.data_type`.

## Object Keys, Graphs, Files and Comments

These additional registered routes are not part of the generic CRUD pattern.
Record permissions on object routes compose with the named type-level permission.

| Method | Path | Description | Auth Required |
|---|---|---|---|
| `GET` | `/objects/by-key/{key}` | Native-key lookup; optional `schema_id` and `object_type_id`; record DENY gives `403` | `objects:read` + record check |
| `GET` | `/objects/{object_id}/graph` | Object relationship graph (`ObjectGraphResponse`) | `objects:read` + record check |
| `GET` | `/objects/{object_id}/released-attributes` | Attribute ids whose external write protection a per-object release lifts (e.g. a detached Jira object); empty when the ObjectType itself is read-only | `objects:read` + record check |
| `GET` | `/object-types/{object_type_id}/graph` | Type relationship graph (`GraphResponse`) | `object-types:read` + record check |
| `POST` | `/objects/{object_id}/files/upload` | Multipart `file`; returns `ObjectFileResponse`, `201` | `objects:update` + record check |
| `GET` | `/objects/{object_id}/files` | File metadata array | `objects:read` + record check |
| `GET` | `/objects/{object_id}/files/{file_id}/download` | Attachment byte stream | `objects:read` + record check |
| `DELETE` | `/objects/{object_id}/files/{file_id}` | Remove file, `204` | `objects:update` + record check |
| `POST` | `/objects/{object_id}/comments` | Create comment, `201` | `objects:update` + record check |
| `GET` | `/objects/{object_id}/comments` | Comment array; `order=asc` (default) or `desc` | `objects:read` + record check |
| `DELETE` | `/objects/{object_id}/comments/{comment_id}` | Remove comment, `204` | `objects:update` + record check |

Comment request (`ObjectCommentCreate`):

```json
{"body": "Installed in rack A.\nInspection complete."}
```

The body is trimmed, must remain nonempty, is limited to 64,000 characters after trimming,
and rejects NUL/unencodable characters with `422`. The authenticated caller supplies authorship,
not a request field. Response fields are `id`, `body`, `author_display_name`, `created_at`,
`managed_by`, and computed `is_external`.

File responses contain `id`, `filename`, `content_type`, `size_bytes`,
`uploader_display_name`, `created_at`, `managed_by`, and computed `is_external`, never the
storage key or raw uploader ID. File/comment IDs are scoped to the owning object; cross-object
lookups use `404`. The file service streams uploads with its configurable per-file size bound;
see [Request Size Limits](#request-size-limits).

## Tasks — Retry and Cancel

Beyond standard CRUD, `Task` (`app/api/tasks.py`) exposes discovery and action endpoints:

| Method | Path | Status | Description |
|---|---|---|---|
| `GET` | `/tasks/types` | `200` | Requires `tasks:read`. Returns `List[TaskTypeCatalogEntry]`; plugin entries are filtered by their declared required permission. A stale worker can leave an entry visible with `dispatchable=false`. |
| `POST` | `/tasks/{task_id}/retry` | `201` / `404` / `409` | Creates a **new** `Task` row referencing the same import/export/sync and re-enqueues the corresponding ARQ job — the original task row is preserved for audit history, never mutated. Returns `TaskActionResponse` (`{"task": <TaskResponse>, "message": "..."}`). `409` if the task's current status doesn't allow retry. |
| `POST` | `/tasks/{task_id}/cancel` | `200` / `404` / `409` | Sets a cancellation flag the worker checks between processing steps (cooperative cancellation, not immediate kill). `409` if the task is not in a cancellable state. |

Retry and cancel require `tasks:create`. Cancel returns `TaskResponse`, while retry returns
`TaskActionResponse`. Plugin creation and retry additionally authorize the **current caller**
against the task declaration: missing permission gives `403`, invalid type/parameters `422`,
and unavailable runtime or admission rejection `409`. Creating a plugin task also enqueues it;
enqueue failure is not silently swallowed.

Catalog fields are `name`, `label`, `source` (`core`/`plugin`), `plugin_name`, `schedulable`,
`scope_requirements`, `parameter_schema`, `dispatchable`, and `unavailable_reason`.
Use a returned `name` unchanged as `tasktype`; do not invent task-type identifiers.
`TaskCreate` requires `tasktype` and defaults `current_step="SCHEMA"`,
`current_status="PENDING"`, `sort=0`, `is_permanent=false`. Plugin starts accept
`plugin_parameters` validated by the declaration. Unknown create/update fields are forbidden;
progress counters are response-only, and admitted plugin parameters cannot be patched.

The native task types are `IMPORT`, `EXPORT`, and `REINDEX`; Jira runs use qualified plugin
types, not retired Core sync types. A definition-driven CSV export request is:

```json
{
  "tasktype": "EXPORT",
  "export_definition_id": "00000000-0000-4000-8000-000000000002"
}
```

Replace the example UUID with an existing definition. The response is a plain `TaskResponse`
with task identity, timestamps, status/step, nullable progress counters and admitted parameters.

## Import / Export — Job Creation Side Effects

`POST /imports/` and `POST /exports/` don't just insert a row — each also:
1. Creates a `Task` row (`tasktype=IMPORT`/`EXPORT`, `current_step=SCHEMA`, `current_status=PENDING`).
2. Enqueues the corresponding ARQ job (`process_import` / `process_export`) with `_job_id=
   "import_{task_id}"` / `"export_{task_id}"` for idempotent re-enqueue protection.
3. If the ARQ enqueue itself fails (e.g. Valkey unavailable), the error is logged but the `Import`/
   `Export` row is still returned as `201` — the task is left `PENDING` without a worker consumer
   until manually retried via `POST /tasks/{id}/retry`.

Per-row outcomes are tracked separately via `ImportStatus`/`ExportStatus` (`/import-statuses`,
`/export-statuses`), so a partially-failed import/export shows exactly which source rows succeeded
or failed.

**Additional job-creation entry points**, all layered on top of the same `Import`/`Export` + `Task` +
ARQ-enqueue pattern above:

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `POST` | `/imports/csv/upload` | `202` | `imports:create` | Multipart upload (`file`: the CSV, `import_definition_id`: form field). Validates the `.csv` extension, streams the body in 1 MB chunks and rejects with `413 {"detail": "File exceeds maximum size of <n> bytes"}` as soon as it exceeds `IMPORT_MAX_FILE_SIZE` (default 50 MB) rather than buffering the whole upload first, writes it to a temp file under `IMPORT_UPLOAD_DIR` (a volume shared with the `worker` container), then creates `Import` + `Task` and enqueues `process_import` with the temp file path. If `Task` creation fails after the `Import` row already committed, the orphaned `Import` is deleted rather than left stuck at `status=pending` with no `Task`. Returns `TaskResponse`. Exempt from the generic 1 MiB body ceiling at both the middleware and the gateway — see [Request Size Limits](#request-size-limits). |
| `GET` | `/exports/csv/{export_id}/download` | `200` / `404` | `exports:read` | Streams the completed export's CSV file (`Export.object_key`) back as `text/csv` with a `Content-Disposition: attachment` header. Resolves the path with `os.path.realpath` and rejects anything outside `EXPORT_ROOT` (directory traversal / symlink escape) with `404` before touching the filesystem, and `404`s if the export has no `object_key` yet or the file is missing on disk. |
| `POST` | `/import-definitions/{definition_id}/folder-files` | `201` | `imports:create` | Multipart upload (`files`: one or more CSVs) into `IMPORT_ROOT/<Source.import_folder>/` — the directory `import-from-folder` scans. Every name and resolved destination is validated before the first byte is written: a non-`.csv` file is `422`, a name whose resolved destination leaves the import root is `400`. Bodies stream in 1 MiB chunks under the same `IMPORT_MAX_FILE_SIZE` ceiling (default 50 MB) and land via a `.part` rename, so a concurrent scan never reads a half file. `422` if the source has no `import_folder`. Returns one `{"filename", "size_bytes"}` entry per stored file. Exempt from the generic 1 MiB body ceiling like the CSV upload. |
| `POST` | `/import-definitions/{definition_id}/import-from-folder` | `202` | `imports:create` | Scans the `ImportDefinition`'s `Source.import_folder` (under the shared `IMPORT_ROOT`, path-traversal-checked the same way) for `*.csv` files and enqueues one `Import`+`Task`+`process_folder_import` job per file found. `422` if the source has no `import_folder` configured or it resolves outside `IMPORT_ROOT`. `404` if no CSV files are found. On success the worker moves each processed file into `<folder>/processed/` so a later scan never reprocesses it; a failed file is left in place for the next scan/retry. Returns `List[TaskResponse]`. |
| `POST` | `/tasks/` | `201` | `tasks:create` | Two special-cased request shapes beyond a plain `Task` create: body `{"tasktype": "EXPORT", "export_definition_id": "..."}` resolves the `ExportDefinition`'s `object_type`→`schema_id`, creates an `Export` (`object_key="pending_csv"`) + `Task`, and enqueues `process_export`; body `{"tasktype": "IMPORT", "import_definition_id": "..."}` does the equivalent for `ImportDefinition`, creating an `Import` + `Task` and enqueuing `process_import` (no `temp_file_path` — this path is for definition-driven runs such as schedules, not file uploads). Both branches return the created `TaskResponse`. The `EXPORT` branch returns `422` if the `ExportDefinition` has no `target_id` (e.g. its `Target` was deleted after the definition was created). |

## Export & Import Definitions

`ExportDefinition` (`/export-definitions`, `app/api/export_definitions.py`) and `ImportDefinition`
(`/import-definitions`, `app/api/import_definitions.py`) are reusable, named CSV field-mapping
templates — a schema-aware "recipe" (which `ObjectType`, which `Target`/`Source`, which CSV
columns map to which attributes) that `POST /tasks/`, `POST /imports/csv/upload`, and
`POST /import-definitions/{id}/import-from-folder` execute against, rather than a one-off
import/export payload. Both follow standard CRUD (`export-definitions:<action>` /
`import-definitions:<action>`) plus the differences below.

**Shared `field_mappings` shape** (`FieldMappingItem`): `{"attr_id": "<uuid>", "csv_column":
"<string>", "column_order": <int >= 0>}`. Both `Create`/`Update` schemas reject duplicate
`csv_column` or `attr_id` values within one definition's `field_mappings` list with a `422`
validation error (a duplicate CSV header would otherwise silently drop data on read).

| Method | Path | Status | Description |
|---|---|---|---|
| `POST` | `/export-definitions/` | `201` / `404` / `422` | Body: `name`, `object_type_id`, `target_id` (required at creation), `field_mappings`, `is_permanent`. `404` if the `ObjectType`/`Target` doesn't exist; `422` if the `Target`'s `connector_type` isn't `csv`. |
| `GET` | `/export-definitions/` | `200` | `skip`/`limit` (default `100`, max `1000`) pagination, ordered by `name`. `X-Total-Count` header. |
| `GET` / `PATCH` | `/export-definitions/{id}` | `200` / `404` / `422` | `target_id` is optional on update (the FK is `ON DELETE SET NULL`, so a definition can drift to no target if its `Target` is later deleted) but re-validated against `connector_type=csv` whenever supplied. |
| `DELETE` | `/export-definitions/{id}` | `204` / `404` / `409` | `409` if `is_permanent=true` and the definition is still referenced by one or more `Schedule` rows (`Schedule.template_type="export"`) — the schedule(s) must be unlinked first. |
| `POST` | `/import-definitions/` | `201` / `404` | Body: `name`, `object_type_id`, `source_id`, `identity_attr_id` (optional — the attribute used to match existing objects for update-vs-create), `value_separator` (default `;`), `date_format`, `encoding` (default `utf-8`), `field_mappings`, `is_permanent`. `404` if `ObjectType`/`Source`/`identity_attr_id`'s `ObjectTypeAttribute` doesn't exist. |
| `GET` / `PATCH` | `/import-definitions/{id}` | `200` / `404` | Same pagination/validation shape as export definitions above (no `connector_type` check — imports aren't tied to a CSV-only `Target`). |
| `DELETE` | `/import-definitions/{id}` | `204` / `404` / `409` | Same permanent+`Schedule`-reference guard as `DELETE /export-definitions/{id}` (`Schedule.template_type="import"`). |

Both entities are also referenced by `Schedule.template_type`/`template_id` (see
[Schedules — Preview](#schedules--preview)) for cron/interval-driven recurring runs — a
`Schedule` row is what turns a stored definition into a periodic `POST /tasks/`-style invocation.

## Plugin Discovery, Administration and Runtime

`app/api/plugins.py` is the Core management router. Runtime access is deliberately separate
from administration: seeded Editor/Viewer roles can use `plugin-runtime:use` without receiving
`plugins:read` or `plugins:manage`.

| Method | Path | Description | Auth Required |
|---|---|---|---|
| `GET` | `/plugins/` | Installed-plugin administration list (`List[PluginResponse]`) | JWT + `plugins:read` |
| `GET` | `/plugins/runtime` | Active-plugin discovery (`List[PluginRuntimeResponse]`) | JWT + `plugin-runtime:use` |
| `GET` | `/plugins/{name}` | Plugin administration details | JWT + `plugins:read` |
| `GET` | `/plugins/{name}/health` | Dispatch loaded plugin health check; result `{"healthy": true}` or unhealthy result | JWT + `plugins:read` |
| `GET` | `/plugins/{name}/config` | Defaults-applied, secret-masked config | JWT + `plugins:manage` |
| `PUT` | `/plugins/{name}/config` | Validate and encrypt config; returns masked view | JWT + `plugins:manage` |
| `PUT` | `/plugins/{name}/state` | Disable/reactivate plugin; returns `PluginResponse` | JWT + `plugins:manage` |
| `GET` | `/plugins/{name}/assets/{path:path}` | Serve allowlisted bundle assets, `200` or authorized `304` | JWT + `plugin-runtime:use` |

The runtime projection contains only `name`, `version`, and nullable
`frontend: {bundle, extension_points}`. An installed but not loaded plugin can report
`frontend=null`. Admin details additionally contain state, capabilities, config schema,
worker-loaded version/time and bounded database diagnostics; runtime discovery omits them.

State request:

```json
{"state": "disabled"}
```

Only `active` and `disabled` are accepted; failed lifecycle transitions can return `409`,
and an uninitialized registry returns `503`. Config updates use `{"config": {...}}`,
validated against the loaded plugin's JSON Schema (`422` on failure). GET/PUT never return
decrypted secrets; omitted/null secret fields or the mask sentinel preserve stored ciphertext.
Config/health operations on a plugin not loaded in the serving process return `404`.
Assets are containment-checked and MIME-allowlisted (`404`/`415` on rejection), with
`ETag`, `Cache-Control: private, no-store`, and `Vary: Authorization`; validators do not bypass auth.

Plugin routes mount once at startup; disabling does not remove them. Newly installed route
bundles require startup discovery rather than hot route addition.

## AI Schema Wizard Plugin

`plugins/ai-schema-wizard` is a first-party, `api_routes`-capability plugin that owns AI-assisted
schema generation. It declared `active_by_default = true`, but that default is applied on **first
discovery only** — an existing disable or a revoked grant is never re-applied. It exposes its own
HTTP surface under **`/api/v1/plugins/ai-schema-wizard`**; the paths below are relative to that
prefix and only exist while the plugin router is mounted (see [Plugin Discovery,
Administration and Runtime](#plugin-discovery-administration-and-runtime) — mounted routes stay
after a disable but a disabled mounted plugin responds `503`).

**Authorization (independent of the Core schema/template rights):**

- The Core mount chain forces authentication, `plugin-runtime:use` and the active-state check onto
  the whole router.
- Every route additionally carries `require_permission("ai-schema-wizard", "generate")` — a
  plugin-owned permission, independent of `schema-templates:create` and `schemas:create`. Its
  default grants are **insert-only** (Administrator and Editor, never Viewer); a later
  administrative change is never reconciled. Generating does **not** imply any Core write right,
  and the generation right alone does not grant access to the admin-only
  `Configuration → Schemas` launcher.

| Method | Path | Status | Description |
|---|---|---|---|
| `GET` | `/status` | `200` | Plugin availability: `{"available": <bool>}` derived only from `ctx.llm.is_configured()`. Never a provider request, model name, base URL or key; an invalid configuration is simply `available: false`. |
| `POST` | `/generate` | `200` | Body: `{"description": "<10–2000 chars>", "category": "<optional, ≤100 chars>"}` (any provider/config/Source/extra field is rejected `422`). Returns an **inert, validated, source-free draft**: `{"draft": {…schema-tree…}, "warnings": [<≤20 strings ≤500 chars>], "suggestedName": <str\|null, ≤255 chars>}`. Nothing is persisted and no Source/actor/origin/name authority is carried. |

The draft is bound to strict collection/string caps (≤32 object types, ≤64 attributes per type,
≤512 attributes total, ≤64 statuses, ≤64 options per attribute) and a total serialized handoff
budget of 128 KiB. It is validated by the **same pure Core preflight** final creation uses, so a
draft accepted here is accepted unchanged by the final save.

**Fixed failure codes** (`POST /generate`, never a raw provider body, prompt or key):

| Status | `code` | Meaning |
|---|---|---|
| `422` | `input_rejected` | Request body failed validation (bounded message; the caller's input is never echoed). |
| `503` | `unavailable` | LLM not configured / invalid config / auth failure / model not found. |
| `429` | `rate_limited` | Provider rate limit. |
| `504` | `timeout` | Provider did not answer inside the budget. |
| `502` | `unreachable` | Provider could not be reached. |
| `422` | `invalid_result` | Model output could not be turned into a valid strict draft (or exceeded the handoff budget). |
| `500` | `unexpected` | Any other failure. |

**Budgets and retries.** Generation is one explicit user-triggered request. The server applies one
cooperative **50-second** deadline covering the `ctx.llm` call plus adaptation/preflight, with at
most `max_retries=1` internal structured retry; the browser client uses a slightly larger
**55-second** timeout (`skipRateLimitRetry`), so a `429` is surfaced as a safe error and is **not**
replayed by the host client. These are cooperative budgets — there is **no guarantee** the remote
provider stops after a client abort. Retry after a visible failure is an explicit user action only.

**Final save (Core-owned, plugin-independent).** An accepted draft is reviewed and saved through
the strict Core boundary **`POST /schemas/from-tree`** (`schemas:create`), which delegates to the
same whole-tree materializer as the plugin SDK `create_schema_from_tree`. The whole tree is
validated before any write — an invalid tree is `422` with an offending `{path, reason}`, a
name/key conflict is `409` and a missing Source is `404`; nothing is written in any error case and
generation never writes. Because the save is Core-owned, an already-accepted draft still saves while
the supplying plugin is disabled.

## Jira Bridging (Jira Plugin)

Jira Assets integration is no longer a Core API surface. The former Core `sync` and
`push_conflicts` routers are not registered; do not call legacy Core pull/push/status URLs.
Start declared operations through `POST /tasks/` using `GET /tasks/types` for discovery.
Jira connection, link, direction, status and conflict APIs belong to `plugins/jira/routes.py`.
Their paths below are relative to **`/api/v1/plugins/jira`**, and are present only when the
plugin router is mounted. Every route additionally requires JWT + `plugin-runtime:use`
and an active plugin; a disabled mounted plugin responds `503`.

| Method | Path | Description | Auth Required |
|---|---|---|---|
| `GET` | `/links/{entity}` | Read plugin link state | `sync:read` |
| `GET` | `/schemas/source/{source_id}` | List source Jira schemas | `sync:read` |
| `POST` | `/schemas/{schema_id}/link` | Link a native schema | `sync:trigger` |
| `GET` | `/schemas/source/{source_id}/adoptable` | Discover adoptable schemas | `sync:read` |
| `GET` | `/schemas/target/{target_id}` | List target Jira schemas | `sync:read` |
| `PUT` | `/directions/{entity}/{entity_id}` | Update link direction | `sync:trigger` |
| `PUT` | `/objects/{object_id}/detach` | Detach an object link | `sync:trigger` |
| `GET` | `/connections/source/{source_id}` | Read source connection | `sync:read` |
| `PUT` | `/connections/source/{source_id}` | Update source connection | `sync:trigger` |
| `POST` | `/connections/source/{source_id}/test` | Test source connection | `sync:read` |
| `GET` | `/connections/target/{target_id}` | Read target connection | `sync:read` |
| `PUT` | `/connections/target/{target_id}` | Update target connection | `sync:trigger` |
| `POST` | `/connections/target/{target_id}/test` | Test target connection | `sync:read` |
| `GET` | `/schemas/{schema_id}/sync-status` | Read plugin sync status | `sync:read` |
| `GET` | `/schemas/target/{target_id}/{jira_schema_id}/object-type-count` | Count remote object types | `sync:read` |
| `GET` | `/conflicts` | List conflicts | `conflicts:read` |
| `GET` | `/conflicts/count` | Count conflicts | `conflicts:read` |
| `GET` | `/conflicts/object-names` | Resolve conflict object names | `conflicts:read` + `objects:read` |
| `POST` | `/conflicts/{conflict_id}/resolve` | Resolve a conflict | `conflicts:manage` |

**Trigger a Jira Assets pull:**

First enable the Jira plugin, configure a Jira connection for the Source under **Jira connections**, and select an active service account bound to that Source with a Jira bearer token. The backend and worker must have a compatible active Jira plugin; the caller needs `tasks:create` and `sync:trigger` permissions. Replace the UUID placeholders with your configured Source and service account IDs.

```bash
curl -X POST http://localhost:8000/api/v1/tasks/ \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"tasktype":"jira.sync-pull","source_id":"<source-uuid>","service_account_id":"<service-account-uuid>","current_step":null,"plugin_parameters":{}}'
```

Expected result: HTTP `201` with a Task object, including its `id` and `tasktype: "jira.sync-pull"`; execution runs asynchronously. Read its status at `GET /api/v1/tasks/<task-id>`. To select a single Jira schema, set `plugin_parameters.jira_schema_id` to its Jira schema ID.

Jira synchronization now uses plugin task types `jira.sync-pull`, `jira.sync-push`, and `jira.sync-export` through the Tasks API. The former core `/api/v1/sync/*` endpoints are removed; `JIRA_URL` is not the plugin's connection configuration.

Core `Task` rows created by plugin runs remain visible through `GET /tasks/`
(see [Tasks — Retry and Cancel](#tasks--retry-and-cancel)) with their plugin task-type identifiers.

## Search

`app/api/search.py`, prefix `/search`, OpenSearch-backed. Search and cross-entity search
require `get_current_user`; reindex routes require `search:trigger`, failed-outbox listing
requires `search:read`, and outbox retry requires `search:manage`.

| Method | Path | Status | Description |
|---|---|---|---|
| `GET` | `/search` | `200` | Query params: `q` (required, min length 1), `object_type_id`/`schema_id`/`source_id` (optional filters), `page` (default `1`), `page_size` (default `20`, max `100`). Returns `SearchResponse` (`{"query", "total", "page", "page_size", "results": [SearchHit...]}`). |
| `GET` | `/search/cross-entity` | `200` | Query params: `q` (required), `page` (default `1`), `page_size` (default `5`, max `50` — this is *per entity type*, not total). Searches across objects, object types, and schemas simultaneously. Returns `CrossEntitySearchResponse` grouped by entity type. |
| `POST` | `/search/reindex` | `202` | Query param `object_type_id` (required). Verifies the `ObjectType` exists, creates a `Task` (`tasktype=REINDEX`), enqueues `bulk_reindex_job`. Returns `503` if the ARQ enqueue itself fails (job is marked `FAILED` on the `Task` row in that case, not silently dropped). |
| `POST` | `/search/reindex/schema` | `202` | Query param `schema_id` (required). Reindexes every `ObjectType` under one schema. Creates a `Task` (`tasktype=REINDEX`, scoped via `schema_id`), enqueues `schema_reindex_job`. |
| `POST` | `/search/reindex/all` | `202` | No params. Full reindex of all schemas. Creates a `Task` (`tasktype=REINDEX`), enqueues `reindex_all_job`. |
| `GET` | `/search/outbox/failed` | `200` | Dead-letter view: lists `IndexOutbox` rows the background sweeper gave up on (`status=FAILED`, attempts exhausted) — these need manual replay. Returns `{"items": [...], "total": <int>}` with `id`, `object_id`, `object_type_id`, `operation`, `attempts`, `last_error`, `created_at` per entry. |
| `POST` | `/search/outbox/{outbox_id}/retry` | `200` / `404` | Resets a dead-lettered outbox entry to `PENDING` with `attempts=0` so the next sweeper pass re-enqueues it. |

## Schema Templates

`app/api/schema_templates.py`, prefix `/schema-templates`, standard CRUD (`skip`/`limit`, `limit`
default `50`, plus `category` filter, all gated on `require_permission("schema-templates",
<action>)`) on top of this extra action:

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `POST` | `/schema-templates/{template_id}/instantiate` | `201` / `404` | `schema-templates:create` | Body: `{"source_id": "<uuid>", "name": "<optional override>"}`. Materializes a stored `SchemaTemplate.template_data` (schema name/description + object type definitions) into a real `Schema` via the same `atomic_create_schema` used by `POST /schemas/atomic-create`, then increments the template's `usage_count`. Returns the created `SchemaResponse`. |

The former Core AI-generation surface (`app/services/llm_service.py` and the
`POST /schema-templates/generate` route) is **removed**. AI-assisted schema drafting now lives
entirely in the `ai-schema-wizard` plugin (see [AI Schema Wizard Plugin](#ai-schema-wizard-plugin)),
which hands an inert draft to the same strict Core review/final-save path documented below.

`app/api/schemas.py` additionally exposes `POST /schemas/atomic-create` (`schemas:create`) — creates
a `Schema` together with a full tree of `ObjectType`/`ObjectTypeAttribute` rows in a single
transaction (`AtomicCreateRequest` body), rolling back entirely on failure rather than leaving a
partial schema. Returns `AtomicCreateResponse`, not `SchemaResponse`.

## Schema Template Export/Import (Color Themes)

Distinct from the `SchemaTemplate` library above, `app/api/schemas.py` and
`app/api/templates.py` also expose a **color-theme template** mechanism for a `Schema`'s 10
`*_color` fields:

| Method | Path | Status | Auth | Description |
|---|---|---|---|---|
| `GET` | `/schemas/{schema_id}/template` | `200` / `403` / `404` | `schemas:read` (record-permission-checked) | Downloads one schema's color palette as a JSON file attachment (`Content-Disposition: attachment`). A record `read` DENY on the schema answers `403` — the same answer the sibling `GET /schemas/{schema_id}` gives; a nonexistent id keeps `404 Schema not found`. |
| `GET` | `/schemas/{schema_id}/template-data` | `200` / `403` / `404` | `schemas:read` (record-permission-checked) | Read the schema's structural template data (distinct from its color palette). A record `read` DENY on the schema answers `403`, matching the sibling `GET /schemas/{schema_id}`; a nonexistent schema keeps its existing `404 Schema not found`, so the denied and the missing cases stay distinguishable. |
| `POST` | `/schemas/{schema_id}/template` | `200` / `403` / `404` | `schemas:create` + record `create` decision | Body: `TemplateColorsUpdate` (light/dark color groups). Applies the colors to the schema; returns the updated `SchemaResponse`. A stored record `write` DENY on the schema blocks the call with `403`; a nonexistent id keeps `404 Schema not found`. |
| `GET` | `/templates/export` | `200` | `require_permission("schemas", "read")` + record `read` decision | Downloads **every** schema's color palette as one JSON file (`{"<schema_id>": {...colors...}}`), attachment filename `schemas-templates-export.json`. A schema the caller is record-DENIED on is omitted from the payload — its id never appears as a key. |
| `POST` | `/templates/import` | `200` | `require_permission("schemas", "update")` + record `read`/`update` decision | Body: `{"<schema_id>": {...colors...}, ...}` — bulk-applies color palettes across multiple schemas in one call. Returns `TemplateBulkImportResponse`: `{"updated": [...], "skipped": [{"id", "reason"}], "errors": [{"id", "reason"}]}` — per-entry failures don't abort the whole batch. An entry the caller is record-DENIED on is returned under `skipped` with one neutral reason that does not distinguish "cannot read" from "cannot write", and nothing is written for it. |

## Schedules — Preview and Run Now

Beyond standard CRUD, `Schedule` (`app/api/schedules.py`) exposes two action endpoints:

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `POST` | `/schedules/preview` | `200` / `422` | `schedules:read` | Body: `SchedulePreviewRequest` (cron/interval definition, without persisting a `Schedule` row). Returns `SchedulePreviewResponse` (`{"human_readable": "<cron description + timezone>"}`) — a human-readable description of the cron expression, used by the UI to validate it before saving. `422` on an invalid schedule definition. |
| `POST` | `/schedules/{schedule_id}/run` | `202` / `404` | `schedules:update` | Executes the schedule's task immediately, outside its normal cron timing. Fires `_execute_scheduled_job(schedule_id)` (`app/services/scheduler_manager.py`) via `asyncio.ensure_future` without awaiting it — the route returns `202 {"status": "enqueued", "schedule_id": "..."}` right away; the Task itself is created and tracked inside `_execute_scheduled_job`'s own DB session, not returned here. `404` if the schedule doesn't exist. |

## Settings

`app/api/settings.py`, prefix `/settings` (tag `system-settings`) — a flat, generic key/value store
in `system_settings`, distinct from the LLM-specific [Configuration Endpoint](#configuration-endpoint)
below.

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `GET` | `/settings/` | `200` | `settings:read` | Returns every setting row as a plain object (`get_all_settings`). |
| `PUT` | `/settings/{key}` | `200` / `422` | `settings:update` | Body: `{"value": <any>}`; `422` if `value` is omitted. The key `history_enabled` is special-cased: the value is coerced to a bool (`str(value).lower() == "true"`) and routed through `set_history_enabled`, which force-toggles the `objects`/`object_values`/`object_refs` audit-log-config entries together (see [Audit Log & Audit Log Configuration](#audit-log--audit-log-configuration)) instead of writing a plain setting row. Every other key is stored as a plain string via `set_setting`. |

The settings key `object_file_max_size_mb` is also special-cased: it accepts whole MB values
from `1` through `2048`, rejecting booleans and fractional values with `422`. It is stored
as a string but returned as a numeric value. The file service falls back to `50` MB for
missing/invalid stored limits. `PUT /settings/{key}` rejects `value=null` as well as omission.

## Saved Queries

`app/api/saved_queries.py`, prefix `/saved-queries` — named, reusable GraphQL query documents
(query operations only), standard CRUD gated by `saved-queries:<action>`.

| Method | Path | Status | Description |
|---|---|---|---|
| `POST` | `/saved-queries/` | `201` | Body: `name`, `query_text` (GraphQL query document, validated at save — mutation/subscription/syntax errors → `422`), optional `variables` (stored defaults). `created_by_id` is set to the caller. |
| `GET` | `/saved-queries/` | `200` | `skip`/`limit` (default `100`, max `1000`), `name` (substring filter). `X-Total-Count` header. |
| `GET` | `/saved-queries/{id}` | `200` / `404` | |
| `PATCH` | `/saved-queries/{id}` | `200` / `404` | |
| `DELETE` | `/saved-queries/{id}` | `204` / `404` | |
| `POST` | `/saved-queries/{id}/execute` | `200` / `403` / `404` / `422` | Body: `SavedQueryExecuteRequest` (`variables` flat-merged over the stored defaults, request wins; `operation_name` required when the document has several operations). Runs in-process against the internal GraphQL schema in the caller's context. Permission `saved-queries:execute` (Administrator-only by default). GraphQL-level errors stay `200` with `{data, errors}`. |

Service accounts run the same queries with an `ApiClient` token via
`POST /public/api/v1/saved-queries/{id}/execute`, see [Public API](#public-api-publicapiv1).

## Configuration Endpoint

| Method | Path | Auth | Description |
|---|---|---|---|
| `GET` | `/config/llm` | `require_permission("config", "read")` | Reports LLM integration status without leaking the key itself: `{"configured": <bool>, "model": "<LLM_MODEL>", "base_url": "<LLM_BASE_URL>", "api_key_set": <bool>}`. `configured`/`api_key_set` are both derived from whether `LLM_API_KEY` is non-empty. |

## Audit Log & Audit Log Configuration

Two related routers over the same underlying audit trail written by `audit_log()` calls throughout
the service layer (`audit_log()` in `app/services/audit_service.py`).

### Audit Logs — `/audit-logs` (`app/api/audit_logs.py`)

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `GET` | `/audit-logs` | `200` | `audit-log:read` (singular resource name) | Paginated audit trail, newest first (`created_at DESC`). Filters: `entity_type`, `object_id` (owning `Object` — scopes to that object's `objects`/`object_values`/`object_refs` history, excluding plumbing `CREATE`/`DELETE` rows on `object_values` that are really a type-change delete+recreate, keeping only `UPDATE`), `actor_q` (substring match on actor first/last name or email), `start_date`/`end_date` (ISO datetime, inclusive), `page` (default `1`), `page_size` (default `50`, max `200`). Returns `AuditLogPaginatedResponse` (`items`, `total`, `page`, `page_size`, `total_pages`). Each item carries resolved `actor_display_name`/`actor_initials` (joined from `User`/`UserProfile`) and, for reference-attribute changes, resolved `reference_field_name`/`reference_old_target_name`/`reference_new_target_name`. |

For a non-superuser the page is filtered by the caller's record decision before anything is
serialized: a row is omitted when the attribute behind the change carries a record DENY, when
that attribute is `is_hidden`, or when the row's own object carries an object-level `DENY` —
attribute name, old value and new value all disappear with it, in the `object_id`-scoped history
and in the global log alike. `total` and `total_pages` shrink with the filter, because the count
is computed over the filtered statement and never over the unfiltered one. A reference target the
caller may not read is not named: `reference_old_target_name`/`reference_new_target_name` stay
`null`. An object-level `DENY` on the `object_id` parameter yields an empty page with `HTTP 200`,
not an error — lists filter silently and never fail per row. Superusers are not filtered.

### Audit Log Configuration — `/audit-log-config` (`app/api/audit_log_config.py`)

Per-entity-type on/off toggle for whether changes to that entity are written to the audit log at all.

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `GET` | `/audit-log-config` | `200` | `audit-log-config:read` | Returns a flat `{"<entity_type>": <bool>}` map (not a list) — one entry per `AuditEntityType` value (`sources`, `schemas`, `object_types`, `objects`, `object_values`, `object_refs`, `attribute_types`, `reference_types`, `statuses`, `icons`, `targets`, `service_accounts`, `api_clients`, `schema_templates`, `import_definitions`, `export_definitions`, `tasks`, `saved_queries`, `ui_translation_exports`, `audit_log_config`, `settings`, `permissions`, `users`, `roles`, `plugins`). |
| `PATCH` | `/audit-log-config/{entity_type}` | `200` / `404` / `409` / `422` | `audit-log-config:update` | Body: `{"audit_log_enabled": <bool>}`. `422` if `entity_type` isn't a recognized `AuditEntityType` value. `409` if attempting to disable an always-on entity type (`audit_log_config`, `settings`, `permissions`, `users`, `roles`, `api_clients`, `ui_translation_exports`, `plugins` — security/config-critical, can never be toggled off via this endpoint). `409` if attempting to disable `objects`/`object_values`/`object_refs` individually while the derived "History" switch (`settings.history_enabled`, see [Settings](#settings) above) is on — History must be turned off first, since it force-enables and locks all three together. |

## `?fields=` Sparse Fieldsets

`app/api/fields.py` provides a shared `FieldsFilter` FastAPI dependency, opted into by
high-volume list endpoints (`objects`, `object-values`, `object-refs`): a `?fields=id,name` query
parameter restricts each returned item to only the requested top-level fields, validated against
the resource's own `XxxResponse` Pydantic field names. An unknown field name returns `400` with the
list of valid fields for that resource. Omitting `fields` returns the full response as normal.
`objects` also accepts the classification fields (`classification_labels`, `confidentiality`,
`personal_data`, `export_control`) — including a sparse fieldset that asks for no classification at
all, which then omits the block entirely.

## Languages & Translations

Multi-language support spans four routers: `/languages` (the catalog of enabled languages),
`/translation-types` (read-only catalog of translatable field groups), `/translations` (per-entity
field translations, e.g. a `Schema`/`ObjectType` name or description in another language), and
`/ui-translations` (translation of the frontend's own UI strings — a separate "systemui" store,
keyed by string key rather than entity id).

AI-assisted translation is **not** part of these Core routers any more. The `ai-translation`
plugin owns it end to end — its own `/api/v1/plugins/ai-translation` routes, its own private
suggestion tables and the two hidden bulk Task types — see
[AI Translation plugin](#ai-translation-plugin--pluginsai-translation-pluginsai-translationroutespy)
below. The manual Core translation, dictionary, catalog and export semantics on `/translations`
and `/ui-translations` are unchanged.

### Languages — `/languages` (`app/api/languages.py`)

Standard CRUD, gated by `languages:<action>`.

| Method | Path | Status | Description |
|---|---|---|---|
| `POST` | `/languages/` | `201` | Body: `code`, `name` (English display name), `is_active`. `code` is trimmed and lowercased, then validated as a BCP-47-shaped tag (`^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|[0-9]{3}))?(-([a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*$`) — a non-conforming code is `422`. A duplicate after normalization is `409`. |
| `GET` | `/languages/` | `200` | `skip`/`limit` (default `REFERENCE_DEFAULT_LIMIT`=200, max `1000`). `X-Total-Count` header. Each entry carries the computed `is_bundled` flag (the code ships a committed catalog with the application). |
| `GET` / `PATCH` / `DELETE` | `/languages/{id}` | `200`/`204` / `404` | Standard fetch/update/delete. `DELETE` answers `409` for a bundled language (derived from the backend's `BUNDLED_LANGUAGE_CODES`, the row stays) and for a language still referenced by a schema. Deleting an operator-added language resets every `user_profiles.ui_language` holding that code to `NULL` and invalidates the cached `/ui-translations/dictionary` data. |

The format rule is enforced on **create only** — `GET` responses stay permissive so pre-existing rows with legacy codes still serialize.

A `Schema`'s own enabled-language set is managed separately, on the `schemas` router:
`GET /schemas/{schema_id}/languages` (`schemas:read`, record-permission-gated) returns the
`Schema`'s configured `List[LanguageResponse]`; `PUT /schemas/{schema_id}/languages`
(`schemas:update`) replaces that set via `SchemaLanguagesUpdate` (`{"language_codes": [...]}`).
Both `404` if the schema doesn't exist.

### Translation Types — `/translation-types` (`app/api/translation_types.py`)

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `GET` | `/translation-types/` | `200` | `translations:read` | Read-only catalog of seeded translation type codes (`skip`/`limit`, default `100`/max `1000`, `X-Total-Count` header) — the fixed set of translatable field groups `POST /translations/` entries can target. |

### Translations — `/translations` (`app/api/translations.py`)

Per-entity field translations. All endpoints gated by `translations:<action>`. The AI-assisted
suggestion/translate-job lifecycle was retired in the Phase 307 cut-over and now lives in the
`ai-translation` plugin.

| Method | Path | Status | Description |
|---|---|---|---|
| `PUT` | `/translations/` | `200` | Body: `TranslationUpsert` — upserts one `(entity, field, language)` translation. |
| `GET` | `/translations/` | `200` | Filters: `translation_type`, `entity_id`, `field` (`name`/`description`), `language_code`; `skip`/`limit` (default `100`, max `1000`). `X-Total-Count` header. |

The list is record-filtered: a row is omitted when the entity that owns it carries a record DENY
for the caller — for every record-owned translation type (`object_type`, `object_type_attribute`,
`select_option`, `object_value`, `status`, `reference_type`). `X-Total-Count` counts only the rows
the caller may see, so the header and the page agree; it never counts a denied row. The two
schema-less types — `systemui` and `classification_label` — are never filtered, because they have
no record owner.

### UI Translations — `/ui-translations` (`app/api/ui_translations.py`)

Read/write access to the frontend's own UI string dictionary ("systemui" translations — distinct
from the entity-field translations above). Reuses the `translations:<action>` permission resource
(no separate permission catalog entry). The scope-keyed bulk AI-translation lifecycle was retired
in the Phase 307 cut-over and now lives in the `ai-translation` plugin.

| Method | Path | Status | Description |
|---|---|---|---|
| `GET` | `/ui-translations/dictionary?lang=<code>` | `200` | Resolved key→string dictionary for one language (`UiDictionaryResponse`), used by the frontend at load time. |
| `GET` | `/ui-translations/keys` | `200` | Registry of UI string keys (`List[UiStringResponse]`). Filters: `namespace`; `skip`/`limit` (default `100`, max `1000`). `X-Total-Count` header. |
| `GET` | `/ui-translations/catalog` | `200` | Admin editor view (`UiTranslationCatalogResponse`): query params `editor_language` (required), `namespace` (exact match), `namespace_is_null` (select only ungrouped keys), `q` (substring on key/effective value), `skip`/`limit` (default `100`, max `200`), `ui_string_id` (single-row refresh after a mutation), `only_missing` (restrict to keys with no non-blank value in the selected language). Response includes a backend-derived `export_available` flag (`true` only when `settings.environment == "development"`). |
| `POST` | `/ui-translations/export` | `200` / `403` / `409` | Development-only filesystem snapshot of every active language's resolved UI dictionary to disk. `403` outside the `development` environment (checked server-side, independent of the advisory `export_available` catalog flag). Serialized by a single fixed Valkey `SET NX` lock (`ui_translation_export_lock`, 60s TTL) — a concurrent call gets `409`; the lock is always released in a `finally` block. Returns `UiTranslationExportResponse` (`paths` written). |
| `DELETE` | `/ui-translations/keys/{ui_string_id}/translations/{language_code}` | `204` | Deletes one key's translation in one language. |

### AI Translation plugin — `/plugins/ai-translation` (`plugins/ai-translation/routes.py`)

The `ai-translation` plugin owns every AI-assisted translation surface. Its routes are relative to
**`/api/v1/plugins/ai-translation`** and only exist while the plugin router is mounted; a disabled
plugin responds `503` (the manual Core routes above are never affected). The plugin imports only the
public `indramesh.sdk` surface and persists to its own private `plugin_ai_translation_*` tables —
never a Core table. Its permission resource is `ai-translation` with the actions `data`
(per-entity/schema field translation) and `ui` (UI-string translation).

**Starting a run — the two `POST /tasks/` shapes.** Both Task types are hidden from the generic
Create Task dialog (`catalog_visible=false`) but start through the same real Core `POST /tasks/`
gate: the current caller is authorized against the declaration's `required_permission` and
`admission_permissions` (`403` on a missing right), the parameters are schema-validated (`422`), and
an admission conflict is `409`. `GET /tasks/types` lists the type `name` unchanged as `tasktype`.

Schema bulk (per-entity field translation across every enabled target language) carries its scope in
the real Core `schema_id` column. Its only parameter is the optional `translation_types` selection
(1–6 unique values of `object_type`, `object_type_attribute`, `select_option`, `object_value`,
`status`, `reference_type`); absent means all six kinds:

```json
{ "tasktype": "ai-translation.schema-bulk", "schema_id": "<schema-uuid>", "plugin_parameters": {"translation_types": ["status", "reference_type"]} }
```

`"plugin_parameters": {}` is the "all kinds" form. An empty list, an unknown kind or a duplicate is
rejected `422` before any Task row is inserted.

UI bulk (UI-string translation) is filter-scoped and takes a validated `plugin_parameters` object:

```json
{
  "tasktype": "ai-translation.ui-bulk",
  "plugin_parameters": {
    "target_language": "de",
    "namespace": "billing",
    "namespace_is_null": false,
    "q": "invoice"
  }
}
```

`target_language` is required (2–10 chars); `namespace` and `namespace_is_null` may not both be
supplied. A second same-schema (`schema-bulk`) or same-scope (`ui-bulk`) start while one is active is
refused `409` before any Task row is inserted.

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `GET` | `/data/availability` | `200` | `ai-translation:data` | Scope-free `{"available": <bool>}` from the configured LLM — never a provider probe, model, URL or key. The schema section hides its generation controls fail-closed when `false`. |
| `GET` | `/data/status` | `200` | `ai-translation:data` | Query: `translation_type`, `entity_id`. `{"available": <bool>, "open_count": <int>}` for one entity (record-permission-checked). |
| `GET` | `/data/open-count` | `200` / `403` | `ai-translation:data` (record-permission-checked) | Query: `schema_id`. `{"open_count": <int>}` — the schema-wide open data-suggestion count. A record `read` DENY on the schema answers `403 not_authorized` and returns no count at all — the count query never runs, because a `0` would still disclose that the schema exists. |
| `GET` | `/data/schema-candidates` | `200` | `ai-translation:data` + `translations:read` + `schemas:read` | Query: `schema_id`. Six rows in canonical kind order, each `{translation_type, missing, waiting}` — missing target pairs and how many already have an open suggestion, counted only over entities the caller may read. |
| `POST` | `/data/generate` | `200` / `409` / `422` / `502` / `503` / `504` | `ai-translation:data` | Body: `{translation_type, entity_id}`. Synchronously generates and persists proposals for the entity, returning `list[SuggestionView]`. `409` when a conflicting field/schema run holds the admission lock; fixed typed error codes (`timeout`/`unreachable`/`not_configured`/`input_rejected`/`invalid_result`) carry a `{code, message}` detail, never raw provider text. |
| `GET` | `/data/suggestions` | `200` | `ai-translation:data` | Query: `translation_type`, `entity_id`. Lists that entity's open suggestions (`list[SuggestionView]`) for review. |
| `POST` | `/data/apply` | `200` / `403` / `422` | `ai-translation:data` | Query: `translation_type`, `entity_id`. Body: `{items: [...]}`. Commits the reviewed drafts through the audited Core `upsert_translation()` path (fresh `update` authority required, never the read right alone); returns `ApplyResult`. |
| `DELETE` | `/data/suggestions/{suggestion_id}` | `200` / `404` | `ai-translation:data` | Query: `translation_type`, `entity_id`. Discards one owned open suggestion (`update` authority); `404` when it is not found in scope. |
| `GET` | `/data/runs` | `200` / `403` / `404` | `ai-translation:data` + `translations:read` + `schemas:read` (record-permission-checked) | Query: `schema_id`. The latest schema run's safe `RunStatus` (`task_id`, `kind`, `outcome`, `attempted`/`generated`/`skipped`/`failed`, timestamps) — never a log, prompt or provider text; `404` when the schema has no run. A record `read` DENY on the schema answers `403 not_authorized` **before** the run lookup, with or without a run row, so a denied caller never receives the run-specific `404` and the lookup cannot become an existence oracle. `outcome` is Core's terminal truth projected onto the private row. |
| `GET` | `/ui/status` | `200` | `ai-translation:ui` | `{"available": <bool>, "open_count": <int>}` for the UI-string store. |
| `GET` | `/ui/candidates` | `200` | `ai-translation:ui` | Query: `target_language` (required), `namespace`, `namespace_is_null`, `q`, `only_missing`. Read-only preview of the missing-key block (`UiCandidatesResult`: `items`, `eligible_total`, `missing_total`, `has_more`) — never enqueues, never writes. |
| `GET` | `/ui/runs` | `200` / `404` | `ai-translation:ui` + `translations:read` | Query: `target_language`. The latest UI run's safe `RunStatus`; `404` when none. |
| `GET` | `/ui/suggestions` | `200` | `ai-translation:ui` | Query: `target_language`, `namespace`, `namespace_is_null`, `q`. Scope-keyed durable review listing (`UiSuggestionsResult`) — reloadable without a remembered `job_id`. |
| `POST` | `/ui/apply` | `200` / `409` / `422` / `503` | `ai-translation:ui` + `translations:update` | Body: `{target_language, namespace, namespace_is_null, q, items}`. Commits exactly the displayed block through the audited `upsert_translation()` path; `409` (`transaction_failed`) when the block could not be committed and nothing was written, `503` (`outcome_unknown`) when the commit outcome is unknown (refetch suggestions and the catalogue before another apply). |
| `DELETE` | `/ui/suggestions/{suggestion_id}` | `200` / `404` | `ai-translation:ui` + `translations:update` | Query: the scope fields. Writes a durable key/language tombstone for one owned proposal so a later pass never re-proposes it. |

Nothing on these routes writes a Core table, and no browser response is an authorization, runner or
persistence proof. The coordinated cut-over removed the retired Core `translation_suggestions` table
in the separate Core revision `307_drop_translation_suggestions`.

## Classification Labels

`app/api/classification.py`, prefixes `/classification-families` and `/classification-labels` —
one label catalogue **per instance** (global, not scoped to a schema). 1.0.0 ships three system
families:

| Family key | Cardinality | Ranked | Operator labels | Meaning |
|---|---|---|---|---|
| `confidentiality` | single-valued (at most one per target) | yes, by `position` | yes | Confidentiality level — seeded Public · Internal · Confidential · Strictly confidential |
| `personal_data` | single-valued | no | no — one fixed system label | Personal-data marker; assigned = yes, absent = no |
| `export_control` | multi-valued | yes, by `position` | yes | Export-control lists — seeded Dual-Use (EU) · National list (AL) · EAR · ITAR |

"Not classified" is the **absence** of an assignment; there is no pseudo-label. Families are
read-only by design (they come as data — seed or plugin — never as a create/delete surface), and a
new family needs no Core migration.

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `GET` | `/classification-families/` | `200` / `400` | `object-type-attributes:read` | Every family with its labels, both in display order; `X-Total-Count` carries the family count. Optional `lang` resolves names through `/translations` (type `classification_label`) and falls back to the base name; an unknown or inactive language is `400`. |
| `POST` | `/classification-labels/` | `201` / `400` / `409` / `422` | `settings:update` | Body: `{family, key, name, position?}`. `key` is the stable identifier templates reference (lowercase snake case, never derived from the name). Appends to the end of the family when `position` is omitted. `400` unknown family, `409` duplicate key or the fixed `personal_data` family, `422` pattern/length violations. |
| `PATCH` | `/classification-labels/{id}` | `200` / `404` / `409` / `422` | `settings:update` | Rename and/or reorder (`name`, `position`); `key` and `family` are immutable (`422`). `409` for a system label, `404` for an unknown id. |
| `DELETE` | `/classification-labels/{id}` | `204` / `404` / `409` | `settings:update` | Delete an unassigned operator label together with its translations. `409` (RESTRICT) while the label is still assigned to any attribute or object, and for every system label — a classification never disappears silently. |

Assignments are written through the attribute and object endpoints, not through the catalogue.
Any catalogue change purges the caches that render label names, so a rename shows on the very next
read.

**Attribute and object fields.** `ObjectTypeAttribute` and `Object` carry the same block:

- Response: `classification_labels` — `{label_id, key, family, name, position}` per label, ordered
  by family `sort` then label `position`, `[]` when unclassified — plus the convenience fields
  `confidentiality` (one label or `null`), `personal_data` (bool) and `export_control` (list).
- Request: `confidentiality` (label id or `null`), `personal_data` (bool) and `export_control`
  (list of label ids, at most 50). A label of another family or an unknown id is `400`; more than
  50 export-control ids is `422`. On `PATCH`, omitted means unchanged, `confidentiality: null` and
  `export_control: null`/`[]` clear their family, and `personal_data: null` leaves it unchanged.
- An Object `PATCH` that changes **only** labels fires no outbox entry or reindex, no
  `on_object_updated` hook and no `updated_at` bump — classification is metadata, not object
  content. The change is still audited (`classification_labels` old/new on the update row).

**Label translations** reuse the existing `/translations` router with translation type
`classification_label` and field `name`; base names are English and the German defaults are seeded.

**Schema templates** carry an optional `classification` block per attribute, keyed by stable label
keys (never instance UUIDs): `{"confidentiality": "internal", "personal_data": true,
"export_control": ["ear"]}`. Export writes it only for classified attributes. Import fails
closed: a key the instance does not know rejects the whole request — `400` on the template
endpoints, `422` carrying the offending path (`object_types[i].attributes[j].classification`) on
the atomic schema-from-tree route — and nothing is written.

**Inheritance rules are documented, not computed in 1.0.0.** The intended effective classification
of a value runs along Schema → ObjectType → attribute → Object: the strictest confidentiality by
rank wins, personal data and export control are OR-ed / unioned along the path, and lower levels
may only raise, never lower (lowering would need a separate permission later). 1.0.0 neither
computes that effective classification nor validates "only raise" on write. Personal data on an
ObjectType means "represents natural persons" — every Object of the type is a data subject and its
attributes are personal — but the Schema/ObjectType assignment tables have no endpoint in 1.0.0
and stay empty.

**1.0.0 stores the marker only.** No redaction, masking, access, export, sync, federation or
logging behaviour depends on a classification label. A consumer that needs such behaviour must
implement it itself until a later version ships it.

## API Clients — Public API Credentials

`app/api/api_clients.py`, prefix `/api-clients` (internal `/api/v1` realm — managed by JWT-session
admins, consumed by the Public API below). An `ApiClient` is a static service credential backing
one `/public/api/v1` consumer; each row resolves to a synthetic service `User` under the hood so
the same RBAC (`require_permission`) stack governs what that credential can do.

| Method | Path | Status | Permission | Description |
|---|---|---|---|---|
| `POST` | `/api-clients/` | `201` | `api-clients:create` | Body: `ApiClientCreate` (`name`, optional `expires_at`). Returns `ApiClientCreateResponse` — the **only** response that ever includes the raw `token` (`imsh_`-prefixed); it is generated once and not retrievable again. |
| `GET` | `/api-clients/` | `200` | `api-clients:read` | List clients (`ApiClientResponse` — `token_prefix` only, never the full token). `skip`/`limit` pagination, `X-Total-Count` header. |
| `GET` | `/api-clients/{client_id}` | `200` / `404` | `api-clients:read` | Fetch one client. |
| `PATCH` | `/api-clients/{client_id}` | `200` / `404` | `api-clients:update` | Update `name`/`expires_at`. |
| `POST` | `/api-clients/{client_id}/revoke` | `200` / `404` | `api-clients:revoke` | Sets `revoked_at` and purges the token-lookup and service-user permission caches; cache purge is best-effort if Valkey is unavailable. |
| `DELETE` | `/api-clients/{client_id}` | `204` / `404` / `400` | `api-clients:delete` | `400` if the client has not been revoked first — a client must be revoked before it can be deleted. |

**Token mechanics** (`app/services/auth_service.py::get_public_api_consumer`,
`app/models/api_client.py`): a raw token is `imsh_`-prefixed; only its SHA-256 hash
(`token_hash`) is stored at rest. Authenticating a public request hashes the presented token and
looks it up first in a Valkey cache (`cache:public:token:{hash}`, 300s TTL, fail-open to Postgres
on a cache miss or Valkey error), then against `api_clients.token_hash`, resolving to the client's
`service_user_id` → `User` row. A revoked or expired client is rejected with `401`.

## GraphQL API (`/api/v1/graphql`)

A read-only [Strawberry](https://strawberry.rocks/) GraphQL schema (`app/graphql/`) mounted at
`/api/v1/graphql`, registered as a plain `APIRouter` (not `app.mount()`) so FastAPI resolves its
`Depends()` context normally. The route is always registered; on production the router's
`graphql_endpoint_gate` serves it only while a plugin declaring `enables_http_graphql` is active.
This gate does not touch the separately registered public GraphQL mount.

```bash
curl -X POST http://localhost:8000/api/v1/graphql \
  -H "Authorization: Bearer <access_token>" \
  -H "Content-Type: application/json" \
  -d '{"query":"{ health { status version } }"}'
```

Expected result: a GraphQL `data.health` object with `status: "ok"` and the runtime package version (or `"unknown"` if package metadata is unavailable).

On a production instance `/api/v1/graphql` answers only once the GraphQL Studio plugin is installed **and** activated by an administrator — installing it alone leaves a fresh installation inactive. `/public/api/v1/graphql` is the integration surface for public clients and does not depend on the plugin.

- **Auth**: `context_getter=get_context` (`app/graphql/context.py`) resolves
  `Depends(get_current_user)` before any resolver runs — every request reaching the schema is
  already authenticated with the same JWT session used by the REST API. There is no
  GraphQL-specific type-level permission model. List root fields (`objects`, `objectTypes`,
  `schemas`, `sources`) and `search` filter out records the caller has a record-level `DENY` on.
  The by-id root fields (`object(id)`, `objectType(id)`, `schema(id)`, `source(id)`) and every
  nested relationship field go through one per-request, batched visibility check
  (`app/graphql/record_visibility.py`) with the same semantics as the list filter
  (`build_record_permission_filter`): a `read` `DENY` on the record, or on its object type, schema
  or source, hides it; with no record entry the permissive default applies; superusers bypass.
  By-id fields and single nested fields (`ObjectRef.fromObject`/`toObject`/`objectType`/`source`,
  `ObjectType.parent`) return `null`, indistinguishable from an unknown id (internal REST by-id
  `DENY` returns `403` instead). Nested lists omit DENY'd records: `Object.referencesFrom`/
  `referencesTo` drop a reference whose other endpoint is DENY'd, and `ObjectType.children` and
  `Schema.objectTypes` drop DENY'd object types. Upward fields (`Object.schema`/`objectType`,
  `ObjectType.schema`, `Schema.source`, search-hit `objectType`/`schema`) need no check because
  deny wins along the chain. A `DENY` takes effect on the GraphQL path immediately; the 60s
  record-permission cache only applies to REST. `inheritedAttributes` omits attributes whose
  defining object type is DENY'd. `ObjectValue.attribute.definingObjectTypeId`/
  `definingObjectTypeName` are nullable and return `null` when the defining object type is DENY'd
  (quick 261011-wfr). The rest of a value's attribute definition (name, data type) stays readable
  so the visible object's own value can be interpreted. Icons have no record model. (Quick
  261011-tgu/261011-tv1/261011-wfr, closing the earlier accepted risk T-182-04.)
- **Schema root fields** (`app/graphql/resolvers/query.py`): `health`, `object(id)`,
  `objects(objectTypeId, schemaId, limit, offset, filters, filter, ...)`, `objectType(id)`,
  `objectTypes(...)`, `schema(id)`, `schemas(...)`, `source(id)`, `sources(...)`, and
  `search(...)`. Field names are Strawberry's auto-camelCased Python arg names (e.g.
  `object_type_id` → `objectTypeId`).
- **Query depth limit**: `QueryDepthLimiter(max_depth=settings.graphql_max_depth)` — env-configurable
  via `GRAPHQL_MAX_DEPTH` (default `15`, `app/config.py`).
- **Introspection**: not disabled — gated by the same auth dependency as every other field, since
  every request that reaches the schema is already authenticated.
- **GraphiQL IDE**: not served by Core. The Core IDE was removed in the Phase 304 extraction
  (`graphql_ide=None` always, in every environment); the editor lives in the `graphql-studio`
  plugin. In production the whole endpoint is hidden unless a plugin declaring
  `enables_http_graphql` is active.
- **Rate limiting**: Strawberry's shared HTTP handlers
  (`strawberry.fastapi.router.handle_http_get`/`handle_http_post`) are added to SlowAPI's
  `limiter._exempt_routes` (`app/graphql/router.py`), so this internal mount is **not** rate-limited
  by the application-level limiter — only Nginx's perimeter rate limit applies. Contrast with the
  public GraphQL mount below, which layers its own enforcement back on top.
- **Batched loading**: `app/graphql/loaders/` (`id_loaders.py`, `relationship_loaders.py`) provide
  DataLoader-style batched lookups to avoid N+1 queries across nested fields.
- **Classification fields**: `ObjectTypeAttribute` and `Object` expose
  `classificationLabels { labelId key family name position }` plus the derived
  `confidentiality { … }`, `personalData` and `exportControl { … }` fields. Labels are read from one
  query per page (no per-attribute lookup), and `inheritedAttributes` reports the **defining** row's
  classification. The label type is `ClassificationLabel`. Classification is read-only in GraphQL.

**Example query:**
```graphql
query {
  objects(objectTypeId: "...", limit: 10) {
    id
    name
    values { attributeName value }
  }
}
```
```bash
curl -X POST http://localhost:8000/api/v1/graphql \
  -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  -d '{"query": "{ health { status version } }"}'
```

## AQL — Custom Query Language

AQL is a Jira-Assets-IQL-inspired custom query language (grammar: `app/aql/grammar.lark`, parsed
with [Lark](https://github.com/lark-parser/lark)) for filtering/searching Objects, independent of
the `filter_tree`/`attr_filters` mechanism on `GET /objects/`. It supports attribute comparisons
(`attr_condition`), `objectType = "..."` / `objectType IN (...)` / `objectTypeAndChildren("...")`
(hierarchy expansion) conditions, boolean `AND`/`OR`/`NOT` combinators with parentheses, reference
traversal (`object HAVING inR(...)`, optionally scoped by `refType IN (...)`), and an optional
`ORDER BY <attribute> [ASC|DESC]` clause. Keywords are case-insensitive; queries are capped at
2048 characters, nesting depth 10, and 50 total conditions (`app/aql/parser.py`) — violations raise
a `422` (internal REST) / `400` (public REST) validation error before any DB query runs.

**Pipeline** (`app/aql/`): `parser.py` (Lark grammar → `Tree`) → `transformer.py` (tree →
unresolved `CompileOutput`) → `resolver.py` (`AQLResolver`, resolves names to concrete
`ObjectType`/attribute UUIDs) → `compiler.py::execute_aql()` (orchestrates execution — text
conditions run against OpenSearch first when hybrid mode is enabled, with a
selectivity-cutoff fallback to pure Postgres for low-selectivity queries, then
`object_service.list_objects()` runs the remaining structural filter and paginates).
`suggest.py` backs autocomplete.

**Internal REST endpoints** — `app/api/aql.py`, prefix `/aql`, `get_current_user`:

| Method | Path | Status | Description |
|---|---|---|---|
| `POST` | `/aql/objects` | `200` / `422` | Body: `AQLQuery` (`qlQuery`, `page` default `1`, `pageSize` default `100`, max `500`). Returns `AQLResponse` (`items: [ObjectResponse]`, `total`, `page`, `page_size`). Parse errors return `422` with `{"error": {"code": "PARSE_ERROR", "message", "line", "column"}}`; validation errors (limits exceeded, type mismatches) return `422` with `{"error": {"code": "VALIDATION_ERROR", "message"}}`. |
| `POST` | `/aql/suggest` | `200` | Body: `AQLSuggestRequest` (`qlQuery`, `cursorPos`). Returns classified next-token autocomplete suggestions (`AQLSuggestResponse`). Read-only and advisory-only — a structurally invalid prefix returns an empty `suggestions` list, never a `422`. No dedicated rate-limit override; sits behind the standard global limiter. |

**Public API endpoint** — `GET /public/api/v1/aql/objects` (`app/public_api/aql.py`),
`require_public_hierarchy_read("objects")` — a Jira-IQL-compatible envelope, not the internal
`AQLResponse` shape: query params `qlQuery` (required), `page` (default `1`), `resultPerPage`
(default `25`, max `500`; `pageSize` is an accepted alias), `includeAttributes` (default `true`),
`includeAttributesDeep`, `attributes` (comma-separated allow-list), `lang`. Returns a Jira
`ObjectListResult`-shaped JSON object (`objectEntries`, `objectTypeAttributes`, `totalFilterCount`,
`pageNumber`, `orderWay`, `qlQuery`, etc.) rather than the internal REST response envelope, for
wire-format compatibility with Jira IQL search integrations, using native IndraMesh IDs.
Parse/validation errors return
`400` (not `422`, unlike the internal endpoint) with a plain `{"detail": "..."}` body.

## Public API (`/public/api/v1`)

**Native identities, compatibility-shaped JSON:** the public API retains Jira-style field names,
but object/type/schema/attribute identities are IndraMesh UUIDs, not remote Jira integer IDs.
Reads and writes use the same native `objectTypeAttributeId`. `objectKey` resolves `Object.key`,
then a stored Key attribute value, then the object's UUID (never a Jira ID). Reference
`searchValue` uses native key material with a target-name fallback. Compatibility-shaped output
does not imply a live Jira connection or interchangeable remote IDs.

A separate REST realm — plus its own GraphQL mount — for external consumers, mounted via
`public_router` (`app/public_api/router.py`) with no additional prefix beyond
`/public/api/v1` (i.e. it is **not** nested under `/api/v1`). It is authenticated exclusively by
`ApiClient` bearer tokens (see [API Clients](#api-clients--public-api-credentials) above) —
presenting an internal-realm JWT here is rejected with `401`, and vice versa.

Representative public write request (`ObjectIn` for create/replace):

```json
{
  "objectTypeId": "00000000-0000-4000-8000-000000000003",
  "attributes": [
    {
      "objectTypeAttributeId": "00000000-0000-4000-8000-000000000004",
      "objectAttributeValues": [{"value": "Server A"}]
    }
  ]
}
```

Replace example UUIDs with native IDs discovered from the hierarchy endpoints. Values remain
native JSON scalars/structures for validation, not universally string-coerced. Public object
responses use fields such as `id`, `objectKey`, `objectType`, and `attributes`, rather than
the internal snake-case `ObjectResponse`. Public AQL returns the separate list envelope below.

**Read-only classification extension.** Every attribute definition (the `objecttype` and
`objectschema` `/attributes` routes) and every object read additionally carries four IndraMesh
extension keys, in camelCase like the rest of this realm: `classificationLabels` — a list of
`{labelId, key, family, name, position}` ordered by family then position, `[]` when unclassified —
plus `confidentiality` (one entry or `null`), `personalData` (bool) and `exportControl` (list).
They are **read-only** on this realm: `POST /object/create` and `PUT /object/{id}` ignore them, and
labels are written only through the internal API. 1.0.0 stores the marker only — no redaction,
masking, access, export or sync behaviour depends on it.

`GET /public/api/v1/` (`public_root`) is the realm's own welcome/liveness route, returning
`{"message": "Welcome to Public API", "authenticated_as": "<username>"}`.

**REST sub-routers** (all gated by `require_public_hierarchy_read`/`require_public_object_read`/
`require_public_object_create`/`require_public_object_write`/`require_public_permission`, each requiring the caller's
`ApiClient` service-user to hold the matching `{resource}:{action}` permission — same RBAC model
as the internal API):

| Method | Path | Status | Description |
|---|---|---|---|
| `GET` | `/public/api/v1/object/{id}` | `200` / `400` / `404` | Fetch one object. `attributes` query param (comma-separated, case-insensitive, exact-name match) restricts the response to named attributes — unknown names return `400` listing them. `lang` resolves translated status/SELECT-option values. Response cached 60s (`cache:public:object:{id}:{attrs_hash}`), keyed independent of caller identity since the permission check runs before the cache lookup. Referenced objects the caller may not read are removed per caller after the cache read (quick 261012-blo); the cached entry itself stays shared. |
| `POST` | `/public/api/v1/object/create` | `201` | Body: `ObjectIn` (Jira-object-shaped payload). Validates every attribute against the target `ObjectType`'s attribute defs (data type, status options, reference targets, cardinality, label presence, read-only attributes) all-or-nothing before writing. A reference target the caller may not read (record-level read `DENY` on the object or its object type, schema or source) is rejected exactly like a nonexistent one, with the same `400` `Reference target {id} not found` at the same position in `errors` and never the type-mismatch message; superusers are not filtered (quick 261012-ca3). Writes go through the same shared `create_object`/`create_object_value`/`create_object_ref` service functions the internal REST API uses (audit row, `IndexOutbox` entry, cache invalidation) — the acting user recorded is the `ApiClient`'s synthetic service user, not an admin. |
| `PUT` | `/public/api/v1/object/{id}` | `200` / `400` / `404` | Jira **replace** semantics: passed attributes are fully replaced (delete-then-recreate), omitted attributes keep their existing values. Changing `objectTypeId` is rejected with `400`. Passed reference values are validated exactly as on create, including the record-level target check (quick 261012-ca3). For a non-superuser a passed reference attribute replaces only the references whose target the caller may read; references to objects it may not read are preserved, and the unchanged-request check (no `updated` change, no lock `409`) compares only the visible set (quick 261012-cpp). `maximumCardinality: 1` is checked against the passed values only, so a preserved hidden reference plus a new visible one can leave two stored references — counting hidden references would reveal that they exist. |
| `DELETE` | `/public/api/v1/object/{id}` | `204` / `404` | Delete one object through the shared delete pipeline. |
| `GET` | `/public/api/v1/objecttype/{id}` | `200` / `404` | Fetch one object type (Jira-shaped response), cached 60s per-caller. |
| `GET` | `/public/api/v1/objecttype/{id}/attributes` | `200` / `404` | List the object type's attribute definitions. |
| `GET` | `/public/api/v1/objectschema/list` | `200` | List all schemas (Jira calls these "object schemas"), cached 60s per-caller. |
| `GET` | `/public/api/v1/objectschema/{id}` | `200` / `404` | Fetch one schema. |
| `GET` | `/public/api/v1/objectschema/{id}/objecttypes` | `200` / `404` | List a schema's object types. |
| `GET` | `/public/api/v1/objectschema/{id}/attributes` | `200` / `404` | List every attribute definition across a schema's object types. |
| `GET` | `/public/api/v1/config/statustype` | `200` | List statuses, optional `objectSchemaId` filter, optional `lang`. |
| `GET` | `/public/api/v1/config/statustype/{id}` | `200` / `404` | Fetch one status. |
| `GET` | `/public/api/v1/icon/global` | `200` | List global (non-schema-scoped) icons. |
| `GET` | `/public/api/v1/icon/{id}` | `200` / `404` | Fetch one icon. |
| `GET` | `/public/api/v1/aql/objects` | `200` / `400` | See [AQL](#aql--custom-query-language) above. |
| `POST` | `/public/api/v1/saved-queries/{id}/execute` | `200` / `401` / `403` / `404` / `422` | Runs a saved query — see "Saved-query execution" below. |

Hierarchy-read endpoints are gated by the type-level `{resource}:read` grant
(`require_public_hierarchy_read`); `objecttype` and `objectschema` endpoints additionally honour
record-level read `DENY` (quick 261011-tv1). A `DENY` on a schema or object type, or on an
ancestor, makes every by-id route — including the `/attributes` and `/objecttypes` sub-routes —
return the same `404` as an unknown id. `/objectschema/list`, `/objectschema/{id}/objecttypes`
and `/objectschema/{id}/attributes` omit DENY'd schemas/object types; an attribute entry is
dropped when its own or its defining object type is DENY'd. Aggregates count only what the caller
can see: `objectCount`/`objectTypeCount` on `/objectschema/{id}`, `/objectschema/list` and
`/objectschema/{id}/objecttypes`, and `objectCount` on `/objecttype/{id}`, exclude DENY'd object
types and objects (quick 261011-wfr). `referenceObjectType` inside attribute definitions is `null`
for a DENY'd referenced type. `config/statustype` omits statuses of a DENY'd schema;
`?objectSchemaId=` of a DENY'd schema answers exactly like an unknown id (`[]`, or with `lang` the
same `400`), and `/config/statustype/{id}` of such a status returns `404`. `icon` has no per-record
model. Referenced objects embedded in `GET /object/{id}`, AQL entries and object create/update
responses honour record-level read `DENY` (quick 261012-blo): a reference value whose target
object, or the target's object type, schema or source, is DENY'd is omitted; a reference attribute
left without any visible value is omitted, as if the reference did not exist; and
`referenceObjectType`, in entry attribute definitions and in the AQL top-level
`objectTypeAttributes`, is `null` unless a visible referenced object of that type remains. The 60s
object cache entry stays shared; the filter runs per caller after the cache read, and superusers
skip it. Object create/update validation treats a reference target the caller may not read exactly
like a nonexistent one (quick 261012-ca3): it is neither an existence nor a type oracle, and no
reference to it is written. A `PUT` replaces only the reference values the caller may read and
keeps the others (quick 261012-cpp). Remaining residual: bare UUID pointers (`parentObjectTypeId`,
`referenceObjectTypeId`) can name a DENY'd type.
Object read/write endpoints additionally enforce record-level
`RecordPermission` overrides, matching the internal REST API's `require_record_permission`
semantics — a record-level `DENY` returns the same `404` as a nonexistent object, so existence is
never leaked to an unauthorized caller.

`POST /public/api/v1/object/create` refuses a container that carries a record `DENY` for the
caller, decided in the handler because the container id arrives in the body: a read `DENY` on the
target `ObjectType` answers the same `400 Unknown objectTypeId` as a nonexistent type (so it is no
existence oracle), and a `create` `DENY` on a readable container answers `403` with the public
write envelope `{"errors": ["Not authorized for this object type."]}`. Both decisions run before
payload validation, so a refusal never names an attribute the caller must not know about. A
reference target only has to be **readable**: an unreadable target keeps the existing `400
Reference target {id} not found`, while a write-protected but readable target stays linkable.

**Saved-query execution** — `POST /public/api/v1/saved-queries/{id}/execute`
(`app/public_api/saved_queries.py`), gated by `require_public_permission("saved-queries", "execute")`:
a service user without that permission gets `403` (the default Viewer role lacks it — grant it
through a role). Body: `SavedQueryExecuteRequest`. The response is identical to the internal
[Saved Queries](#saved-queries) execute route: `200` `{data, errors}` (also for GraphQL-level
errors), `404` for an unknown id, `422` for an invalid/non-query document or a bad
`operation_name`; a user JWT is rejected with `401`. The query runs against the **internal**
GraphQL schema (not the six-field `public_schema`), so every existing saved query works, including
`health`, `source`/`sources` (with `sourceUrl`, `config`, `importFolder`), `search` and the JSON
filter scalar — a deliberate, saved-query-only deviation from Phase 205 D-05, because admins curate
which documents exist. It executes in the service user's context, and every root and nested field
honours record-level `DENY` as described in the GraphQL **Auth** bullet (quick
261011-tgu/261011-tv1): neither a request variable that overrides a stored id nor a nested
relationship path can reach a DENY'd record. Request variables still override stored defaults.
Rate-limited by the global per-token bucket (`PUBLIC_API_RATE_LIMIT`), no route-level override. Like the internal execute route it is not hidden by the production gate of
`/api/v1/graphql`.

**Public GraphQL mount** — `GET`/`POST /public/api/v1/graphql` (`app/public_api/graphql.py`,
`app/public_api/graphql_schema.py`): a second, independent Strawberry schema
(`public_schema`/`PublicQuery`) exposing exactly six fields — `object`, `objects`, `objectType`,
`objectTypes`, `schema`, `schemas` — with `source`, `health`, and `search` deliberately absent.
`objects` accepts typed filter arguments (`FilterOperator`/`LogicalOperator`/`AttributeFilterInput`/
`FilterGroupInput`) rather than the internal schema's opaque JSON filter scalar. Authenticated via
`get_public_context` → `get_public_api_consumer` (the same `ApiClient` bearer mechanism as the REST
public endpoints) instead of `get_current_user`; GraphiQL is always disabled here
(`graphql_ide=None`) regardless of environment. The public schema shares the internal object types and
resolvers, so its by-id fields, list fields and every nested relationship field honour record-level
`DENY` exactly as in the internal schema (quick 261011-tgu/261011-tv1).

**Rate limiting**: because SlowAPI's `_exempt_routes` exemption is identity-scoped to Strawberry's
shared handler functions, it is inherited by *every* `GraphQLRouter` instance, including the public
one. To avoid the public GraphQL mount being accidentally unlimited, `public_router` attaches
`enforce_public_graphql_rate_limit` as an explicit FastAPI dependency at the include site — a
pre-Strawberry check that enforces the same per-token bucket (`PUBLIC_API_RATE_LIMIT`, see
[Rate Limits](#rate-limits) below) the public REST endpoints use. The internal `/api/v1/graphql`
mount has no equivalent dependency and remains unlimited beyond Nginx's perimeter rate limit.

## Rate Limits

Enforced by `slowapi` (`app/services/rate_limit.py`), keyed **per authenticated user** (via JWT
`sub` claim) when a valid internal bearer token is present, **per API-client token** (SHA-256 hash
of the full `imsh_...` token, so two `ApiClient`s behind one egress IP are limited independently)
for public-realm requests, otherwise **per IP**:

| Scope | Default limit | Env override | Notes |
|---|---|---|---|
| Global (all routes except `/health`, `/api/v1/`, and login) | `200/minute` | `RATE_LIMIT_GLOBAL` | Disabled entirely if `RATE_LIMIT_ENABLED=false`. Its default is pinned to `PUBLIC_API_RATE_LIMIT` (see below) so the internal and public default buckets can never silently diverge — an explicit `RATE_LIMIT_GLOBAL` env value still overrides it. |
| Public API (`/public/api/v1/*` REST + GraphQL) | `200/minute`, per-token | `PUBLIC_API_RATE_LIMIT` | REST inherits the global default (`RATE_LIMIT_GLOBAL` can override it); public GraphQL explicitly uses `PUBLIC_API_RATE_LIMIT`. Both respect `RATE_LIMIT_ENABLED`, keyed on the SHA-256 hash of the presented `imsh_` token. |
| `POST /auth/login` | `5/minute`, per-IP | `RATE_LIMIT_LOGIN` | Enforced via a separate `login_limiter` that is **always active**, independent of `RATE_LIMIT_ENABLED` — closes a gap where rate limiting could otherwise be bypassed entirely. |
| `POST /users/me/change-password` | `5/minute` | `RATE_LIMIT_PASSWORD_CHANGE` | Applied via `@limiter.limit(...)` directly on the route. |
| In-memory fallback | `50/minute` | `RATE_LIMIT_FALLBACK` | Used if the Valkey-backed limiter storage is unreachable. |

These are source defaults, not guaranteed deployment quotas: `200/minute` means 200 requests
per one-minute fixed window, login's `5/minute` means 5 per minute, and fallback `50/minute`
means 50 per minute.

The internal GraphQL mount (`/api/v1/graphql`) is exempted from all of the above (see
[GraphQL API](#graphql-api-apiv2graphql)) — only Nginx's perimeter limit applies to it.

A `429` response includes `X-RateLimit-*` and `Retry-After` headers and body
`{"error": "Rate limit exceeded: <detail>"}`.

## Request Size Limits

`MaximumContentLengthMiddleware` (`app/main.py`) rejects any request with `Content-Length` greater
than 1 MiB (1,048,576 bytes) with `413 {"detail": "Request body too large"}`, before routing,
**except** the exact `POST` paths in `_BODY_LIMIT_EXEMPT_POST_PATHS`:

- `POST /api/v1/objects/{object_id}/files/upload` — a streamed per-file counter with the
  `object_file_max_size_mb` setting (default `50`, maximum `2048` MB) is the authoritative limit.
- `POST /api/v1/imports/csv/upload` and `POST /api/v1/import-definitions/{definition_id}/folder-files`
  — both stream into the handler, whose own `IMPORT_MAX_FILE_SIZE` check (default 50 MiB) is the
  authoritative limit and answers its own `413 {"detail": "File exceeds maximum size of <n> bytes"}`.

The exemption is per anchored path and `POST`-only — never a prefix and never method-agnostic, so
`GET`/`PATCH`/`DELETE` on `/api/v1/imports/*` and every other route keep the 1 MiB ceiling.
A body between 1 MiB and 50 MiB posted to either import upload therefore reaches the route instead
of being refused at the gate.

Requests without a `Content-Length` header (e.g. chunked transfer) are not caught here — Nginx's
`client_max_body_size` is the backstop for that case in front of the `backend` service. For the two
import uploads Nginx sets a bounded `64 MiB` ceiling (identical blocks in `nginx/nginx.conf` and
`deploy/helm/indramesh/nginx.conf`, which the chart embeds verbatim), deliberately above the 50 MiB
handler default and deliberately not unbounded; raising `IMPORT_MAX_FILE_SIZE` past 64 MiB means
raising that ceiling in both files. The object-file upload keeps its own unbounded gateway
exception because its service relays into storage without ever holding the file in memory.

## Request/Response Formats and Error Codes

Internal CRUD uses JSON `XxxCreate`/`XxxUpdate` bodies and plain `XxxResponse` objects;
standard list responses are arrays, not a universal `{data: ...}` wrapper. Exceptions include
`RecordPermissionListResponse` (`items`, `total`), paginated audit/search/AQL envelopes,
retry's `{task, message}`, file byte streams and GraphQL's `{data, errors}` contract.
Login is form-encoded; file/CSV uploads are multipart.

Retired top-level Jira fields are rejected by presence (including `null`) on guarded Core
create/update schemas, with `422`. For example `jira_object_id`/`is_synched` on Object,
`jira_schema_id`/`is_synched` on Schema, and `relink_rewire`/`include_comments_files` on Task
are not Core request options. The closed matrix is in `app/schemas/retired_inputs.py`;
plugin-specific inputs belong in their declared plugin API or admitted `plugin_parameters`.

Typical `HTTPException` response:

```json
{"detail": "Object not found"}
```

Request validation response (representative item):

```json
{"detail": [{"type": "missing", "loc": ["body", "tasktype"], "msg": "Field required", "input": {}}]}
```

| Status | Meaning |
|---|---|
| `400` | Invalid action/query, public AQL parsing error or public payload validation |
| `401` | Missing/invalid credentials, expired or revoked token, wrong auth realm |
| `403` | Missing permission or internal record-level DENY |
| `404` | Missing resource; public object record DENY also conceals existence |
| `409` | Conflicting assignment/state, task admission unavailability or guarded deletion |
| `413` | Request/upload size limit exceeded |
| `415` | Unsupported plugin asset MIME type |
| `422` | Request schema/parameter validation, retired Core field, invalid plugin parameters |
| `429` | Rate limit exceeded; `{error: ...}` rather than `{detail: ...}` |
| `500` | Unhandled error normalized by exception middleware; explicit handler errors can differ |
| `502` / `503` / `504` | Upstream/queue/service failure, disabled plugin or timeout, depending on route |

Internal AQL uses its own `{error: {code, message, ...}}` envelope as documented above.
Do not assume every error is a string `detail` or that every explicit `500` is sanitized
by the catch-all middleware.

## CORS

Allowed origins come from `CORS_ALLOWED_ORIGINS` (comma-separated, required, wildcard forbidden —
see `docs/CONFIGURATION.md`). `allow_credentials=True`, `allow_methods=["*"]`,
`allow_headers=["*"]`.
