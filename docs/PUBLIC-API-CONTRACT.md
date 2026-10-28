# IndraMesh Core Public REST API — Contract Baseline

**Version:** v11.0 (classic Jira Assets Insight/DC-11 baseline)
**Status:** Approved baseline — implementations MUST match this contract
**Compatibility oracle:** golden-file fixtures in `backend/tests/fixtures/` + `test_public_contract_oracle.py`

## Consumer Summary

The IndraMesh Core Public REST API is a Jira Assets–compatible surface for external consumers, served under the base path `/public/api/v1`. Authentication uses static service-account bearer tokens issued and managed through an internal admin API (Phase 200). The primary query language for consumers is GraphQL under the same base path (Phase 205), with AQL available for Jira compatibility (Phase 204). The existing internal `/api/v1` surface is untouched. This document is the contract that Phases 200–206 implement against and that the oracle tests assert against.

## 1. Endpoint Inventory

Classic Insight/DC-11 — the generation the live Jira 11.3.6 instance speaks — is the single baseline for path contracts, payloads, and semantics; the Cloud v2 contract is out of scope. IndraMesh serves the compatible surface under its own base path `/public/api/v1` (DEV-07) instead of Jira's `/rest/insight/1.0` and `/rest/assets/1.0` roots. In the table below, `exact` parity means the Jira wire shape is reproduced; `adapted` means a deviation applies and is referenced by its DEV-ID in Section 4.

| Method | Path (under /public/api/v1) | Purpose | Jira parity (exact/adapted) | Phase |
|---|---|---|---|---|
| (admin, internal) | `/api/v1/api-clients` | ApiClient admin CRUD; token issuance is a side effect of client creation (POST `/api/v1/api-clients/`) — no separate ApiToken resource exists | adapted — internal admin surface, not part of the Jira compatibility surface | 200 |
| `GET` | `/object/{id}` | Read a single object with the full Jira Object shape (attributes, avatar, objectType, _links) | exact (DEV-02: optional `attributes` filter is a IndraMesh addition) | 201 |
| `GET` | `/objecttype/{id}` | Read an object type definition | exact (DEV-06: no invented DC-only fields) | 202 |
| `GET` | `/objectschema/{id}` | Read an object schema | exact (DEV-06) | 202 |
| `GET` | `/objectschema/{id}/objecttypes` | List the object types of a schema | exact | 202 |
| `GET` | `/objectschema/list` | List all object schemas | exact | 202 |
| `GET` | `/objecttype/{id}/attributes` | List the attribute definitions of an object type | exact | 202 |
| `GET` | `/objectschema/{id}/attributes` | List the attribute definitions of a schema | exact | 202 |
| `GET` | `/config/statustype` | List status types | exact | 202 |
| `GET` | `/config/statustype/{id}` | Read a single status type | exact | 202 |
| `GET` | `/icon/global` | List global icons | exact | 202 |
| `GET` | `/icon/{id}` | Read a single icon | exact | 202 |
| `POST` | `/object/create` | Create an object from a Jira ObjectIn payload | exact (201 + full Object response shape) | 203 |
| `PUT` | `/object/{id}` | Replace the attribute set of an object (Jira replace semantics) | adapted — label-attribute padding on empty sets; unpassed attributes keep correct values | 203 |
| `DELETE` | `/object/{id}` | Delete an object | adapted (DEV-04: 204 No Content vs Jira 200 + body) | 203 |
| `GET` | `/aql/objects?qlQuery=&page=&resultPerPage=` | AQL search returning the ObjectListResult envelope | adapted (DEV-05: `pageSize` alias; top-level pagination metadata) | 204 |
| `POST` | GraphQL endpoint under `/public/api/v1` | GraphQL queries authenticated via service-account token | adapted — new surface without a Jira equivalent | 205 |

## 2. Example Payloads

All examples below are trimmed to the essential shape. The raw, unnormalized golden files under `backend/tests/fixtures/` (captured live from Jira 11.3.6 on 2026-08-02) are the compatibility oracle.

### a) Object — `GET /object/{id}`

From `backend/tests/fixtures/jira-object-1842.json`. Shows the scalar value wrapper (`value`/`searchValue`/`displayValue`), the status wrapper (`status`), and the reference wrapper (`referencedObject`):

```json
{
  "id": 1842,
  "label": "exchange.prod.app1",
  "objectKey": "SK-1842",
  "objectType": {
    "id": 295,
    "name": "Servers",
    "type": 0,
    "icon": {"id": 25, "name": "3D Printer", "url16": "...", "url48": "..."},
    "position": 0,
    "objectCount": 0,
    "parentObjectTypeId": 293,
    "objectSchemaId": 3,
    "inherited": false,
    "abstractObjectType": false
  },
  "created": "2026-07-28T10:47:50.309Z",
  "updated": "2026-07-28T10:49:47.226Z",
  "hasAvatar": false,
  "timestamp": 1785235787226,
  "attributes": [
    {
      "id": 10031,
      "objectTypeAttributeId": 2094,
      "objectAttributeValues": [
        {"value": "exchange.prod.app1", "searchValue": "exchange.prod.app1", "referencedType": false, "displayValue": "exchange.prod.app1"}
      ]
    },
    {
      "id": 10330,
      "objectTypeAttributeId": 2163,
      "objectAttributeValues": [
        {"status": {"id": 2, "name": "Running", "category": 1}, "searchValue": "Running", "referencedType": false, "displayValue": "Running"}
      ]
    },
    {
      "id": 10332,
      "objectTypeAttributeId": 2197,
      "objectAttributeValues": [
        {
          "referencedObject": {"id": 1841, "label": "Windows Server 2019 Standard", "objectKey": "SK-1841", "objectType": {"id": 297, "name": "Operating System"}},
          "searchValue": "SK-1841",
          "referencedType": true,
          "displayValue": "Windows Server 2019 Standard"
        }
      ]
    }
  ],
  "extendedInfo": {"openIssuesExists": false, "attachmentsExists": false},
  "_links": {"self": "http://localhost:8080/secure/ShowObject.jspa?id=1842"},
  "archived": false,
  "name": "exchange.prod.app1"
}
```

Value wrappers differ by attribute type: scalar values use `value`, status values (type 7) use the `status` object, reference values (type 1) use the `referencedObject` object. See probe record finding 7.

Objects additionally carry the read-only IndraMesh classification extension keys described in
Section 4 (DEV-14): `classificationLabels`, `confidentiality`, `personalData`, `exportControl`.
They are absent from the Jira fixture and are additive, so a shape comparison against the golden
file treats them as allowed extra keys.

### b) IQL / ObjectListResult envelope — `GET /aql/objects`

From `backend/tests/fixtures/jira-iql-envelope.json` (captured with `qlQuery=objectType="Servers"&resultPerPage=3`):

```json
{
  "objectEntries": [
    {"id": 1842, "label": "exchange.prod.app1", "objectKey": "SK-1842", "objectType": {"id": 295, "name": "Servers"}, "hasAvatar": false, "attributes": [], "archived": false, "name": "exchange.prod.app1"}
  ],
  "objectTypeAttributes": [],
  "objectTypeId": 0,
  "objectTypeIsInherited": false,
  "abstractObjectType": false,
  "totalFilterCount": 16,
  "startIndex": 1,
  "toIndex": 3,
  "pageObjectSize": 3,
  "pageNumber": 1,
  "orderWay": "ascending",
  "qlQuery": "objectType=\"Servers\"",
  "qlQuerySearchResult": true,
  "conversionPossible": false,
  "iql": "objectType=\"Servers\"",
  "iqlSearchResult": true,
  "pageSize": 6
}
```

Pagination metadata is TOP-LEVEL — there is NO nested `pagination` object (the raw response contains no `pagination` key at all). The response `pageSize` value is an echo quirk of this Jira generation (returns 6 for `resultPerPage=3`) and must not be trusted by consumers.

### c) Error shape — 404

From `backend/tests/fixtures/jira-error-404.json`:

```json
{
  "errorMessages": ["NotFoundInsightException: Could not find Object with id: 999,999"],
  "errors": {}
}
```

Jira's error envelope is `{errorMessages, errors}` — NOT `{"message": ...}`. IndraMesh does NOT reproduce this envelope; see DEV-03 and Section 3.

### d) ObjectType — `GET /objecttype/{id}`

From `backend/tests/fixtures/jira-objecttype-295.json`:

```json
{
  "id": 295,
  "name": "Servers",
  "type": 0,
  "icon": {"id": 25, "name": "3D Printer", "url16": "...", "url48": "..."},
  "position": 0,
  "created": "2026-07-28T10:46:46.723Z",
  "updated": "2026-07-28T10:46:46.723Z",
  "objectCount": 8,
  "parentObjectTypeId": 293,
  "objectSchemaId": 3,
  "inherited": false,
  "abstractObjectType": false,
  "parentObjectTypeInherited": false
}
```

### e) ObjectSchema — `GET /objectschema/{id}`

From `backend/tests/fixtures/jira-schema-1.json`:

```json
{
  "id": 1,
  "name": "SDM - ITSM",
  "objectSchemaKey": "SDMITSM",
  "status": "Ok",
  "created": "2026-07-03T23:29:44.254Z",
  "updated": "2026-07-03T23:29:44.254Z",
  "objectCount": 110,
  "archivedObjectCount": 0,
  "objectTypeCount": 21
}
```

### f) Attribute definition — `GET /objecttype/{id}/attributes`

From `backend/tests/fixtures/jira-objecttype-295-attributes.json`:

```json
{
  "id": 2093,
  "name": "Key",
  "label": false,
  "type": 0,
  "defaultType": {"id": 0, "name": "Text"},
  "editable": false,
  "system": true,
  "sortable": true,
  "summable": false,
  "indexed": true,
  "minimumCardinality": 1,
  "maximumCardinality": 1,
  "removable": false,
  "hidden": false,
  "includeChildObjectTypes": false,
  "uniqueAttribute": false,
  "options": "",
  "position": 0
}
```

Attribute definitions additionally carry the read-only IndraMesh classification extension keys
described in Section 4 (DEV-14): `classificationLabels` (a list of
`{labelId, key, family, name, position}`, ordered by family then position and `[]` when
unclassified), `confidentiality` (one entry or `null`), `personalData` (bool) and `exportControl`
(list). They are additive to the Jira shape, never replace a Jira field.

### g) Status type — `GET /config/statustype`

From `backend/tests/fixtures/jira-statustypes.json`:

```json
[
  {"id": 5, "name": "Action Needed", "category": 2},
  {"id": 2, "name": "Running", "category": 1},
  {"id": 7, "name": "Closed", "category": 0}
]
```

`category` is an integer: 0 = Inactive, 1 = Active, 2 = Pending.

### h) Icon — `GET /icon/global`

From `backend/tests/fixtures/jira-icons.json`:

```json
[
  {"id": 25, "name": "3D Printer", "url16": ".../icon/25/icon.png?size=16", "url48": ".../icon/25/icon.png?size=48"},
  {"id": 56, "name": "AV Receiver", "url16": ".../icon/56/icon.png?size=16", "url48": ".../icon/56/icon.png?size=48"}
]
```

## 3. Pagination & Error Conventions

### Pagination

- `resultPerPage` is the canonical page-size parameter: default 25, maximum 500 (CONT-03).
- `page` selects the page number (1-based).
- `pageSize` is accepted as an ALIAS for `resultPerPage` (IndraMesh adaptation — live Jira ignores `pageSize`; see DEV-05 and probe record finding 4).
- Pagination metadata in list responses is returned as TOP-LEVEL fields: `totalFilterCount`, `startIndex`, `toIndex`, `pageObjectSize`, `pageNumber`, `pageSize`, `orderWay`. There is NO nested `pagination` object (probe record finding 3).
- The IQL POST body pagination parameters (`page`, `resultsPerPage`) are ignored by this Jira generation (probe record finding 5); pagination lives on the GET `/aql/objects` query parameters.

### Error Envelope

- IndraMesh uses the FastAPI error envelope — the `{detail}` envelope, i.e. `{"detail": "..."}` (CONT-04).
- Status codes: 400 invalid query parameters (e.g. unknown `attributes` names, empty filter list), 401 authentication failure, 403 permission denied, 404 not found, 409 value-lock conflict on public writes, 422 request-body/path validation (`RequestValidationError`), 429 GraphQL rate limit.
- Jira's own envelope is `{"errorMessages": [...], "errors": {}}` — the live 404 example is in Section 2(c); the deliberate deviation is documented as DEV-03.

### GraphQL Error Contract

GraphQL-level failures are never a 4xx: the GraphQL endpoints (`/api/v1/graphql`, `/public/api/v1/graphql`)
and the saved-query execute routes return HTTP 200 with the standard `{data, errors}` envelope. Two
error codes are stable contract strings; the limit VALUES behind them are operator configuration and
explicitly NOT part of the contract (they change without a contract change).

| Code | When | `extensions` |
|---|---|---|
| `QUERY_TOO_COMPLEX` | a submitted document breaches a limit (rejected before execution) | `limit` naming the axis — one of `tokens`, `aliases`, `depth`, `cost` — plus integer `actual` and `allowed` |
| `QUERY_TIMEOUT` | the operation was cut off while running | `source`: `statement_timeout` (PostgreSQL cancelled the statement) or `wall_clock` (the execution backstop fired) |

- A limit rejection and a timeout both carry `data: null` — a cancelled or rejected operation never
  returns partial or stale data.
- A successful GraphQL response also reports the measured cost in the response headers:
  `X-GraphQL-Cost` (the same number a `cost` rejection reports as `actual`) and
  `X-GraphQL-Cost-Limit`.
- A nested list longer than `GRAPHQL_NESTED_LIST_LIMIT` is NOT an error: the response keeps its data,
  the list is cut to the cap, and `errors[]` carries one `LIST_TRUNCATED` entry with `field` (the
  truncated field) and `cap` (the applied limit).
- Requests rejected before execution keep their existing behaviour: missing/expired credentials stay
  `401`, missing permission stays `403`, the GraphQL rate limit stays `429`, and a malformed body
  stays `400`.
- The one 4xx of the saved-query write surface is the `422` for an invalid document. Its `detail`
  carries `path` and `message` and, when the document breaches a limit, the same
  `code`/`limit`/`actual`/`allowed` fields as an execution rejection — so saving and executing share
  one mental model (a document that cannot execute is not stored, while an already stored
  over-limit query keeps its `422`-free metadata edits and is still rejected at execution).
- `docs/STABILITY.md` (created in Phase 316) must list `QUERY_TOO_COMPLEX` and `QUERY_TIMEOUT` as
  stable codes and the limit VALUES as explicitly non-stable.

## 4. Deviations from Jira

Every deliberate deviation from the Jira wire contract is listed here. Implementations MUST follow the "IndraMesh behavior" column; the "Rationale" column explains why the deviation exists.

| ID | Area | Jira behavior | IndraMesh behavior | Rationale |
|---|---|---|---|---|
| DEV-01 | ID space | Opaque integer IDs (`id: 1842`) | **UUIDs-as-strings as the ONLY ID space** (CONT-02); `jira_object_id` appears ONLY as metadata on synced entities, never as a primary ID | IndraMesh's internal model is UUID-based; consumers must treat IDs as opaque strings |
| DEV-02 | `attributes` param | `?attributes=` is ignored (live-verified) | GET /object accepts an optional `attributes` param to filter returned attributes (case-insensitive, comma-separated names; omitted → all attributes) | Bandwidth and privacy control for consumers; the Jira-compatible default (all attributes) applies when omitted |
| DEV-03 | Error envelope | `{"errorMessages": [...], "errors": {}}` | FastAPI `{detail}` envelope, i.e. `{"detail": "..."}` (CONT-04) | Consistent with IndraMesh's existing /api/v1 surface; documented here as a deliberate deviation |
| DEV-04 | DELETE response | 200 + deleted object body | 204 No Content | REST convention; consumers must not expect a body |
| DEV-05 | `pageSize` alias | `pageSize` param NOT honored (live-verified; only `resultPerPage` works) | `pageSize` accepted as an alias for `resultPerPage` | Jira Cloud consumers send `pageSize`; accepting it reduces adaptation cost |
| DEV-06 | DC-only fields | Jira 11.3.6 does not emit `hasChildren`, `parentObjectId`, `createdBy`, `updatedBy` on object/objecttype/schema reads (live-verified — see Section 5) | IndraMesh does NOT invent these fields | Contract follows the live baseline; if a future Jira generation emits them, a follow-up phase adapts |
| DEV-07 | Base path | `/rest/insight/1.0` and `/rest/assets/1.0` | `/public/api/v1` | Namespacing without touching the existing `/api/v1` (REGR-02) |
| DEV-08 | AQL `pageSize` echo | Returns a doubled/echo `pageSize` (6 for `resultPerPage=3`) | Returns the honest actual page size (`pageSize = len(entries)`) | The Jira echo is an unreliable quirk; consumers get a truthful value |
| DEV-09 | Avatar URLs | Distinct signed URLs per size (`url16`…`url288`) | All five size keys reuse the single local icon URL (IndraMesh Icon has one `url` column) | No per-size image variants exist locally; the keys are present for shape compatibility |
| DEV-10 | Attribute-definition flags | Authoritative `editable`/`system` flags | Best-effort: `editable = not is_required`, `system` always `false` | IndraMesh's attribute model has no direct `system` flag; values are derived |
| DEV-11 | ObjectSchema constants | `status` and `archivedObjectCount` reflect Jira's lifecycle | `status` fixed to `"Ok"` and `archivedObjectCount` fixed to `0` | IndraMesh has no schema status/archive concept |
| DEV-12 | Status-type `objectSchemaId` | Statustype entries are `{id, name, category}` only (Section 2(g)) | IndraMesh emits an additional `objectSchemaId` field, always set to the owning schema's UUID-as-string (never `null`) | IndraMesh has no global-status concept (`Status.schema_id` is NOT NULL) |
| DEV-13 | Non-native attribute types | Jira Assets has no `json`, `jira`, or `confluence` attribute kinds | These kinds map to the `(0, 0)` Text fallback in the attribute-definition type index | No Jira-native equivalent exists; Text is the safe wire shape |
| DEV-14 | Classification extension keys | Jira Assets has no classification/label markers (its "labels" are object labels, a different concept) | Attribute definitions and objects carry four additive, read-only keys: `classificationLabels` (`[{labelId, key, family, name, position}]`, ordered by family then position, `[]` when unclassified), `confidentiality` (one entry or `null`), `personalData` (bool) and `exportControl` (list). Public writes ignore them — `POST /object/create` and `PUT /object/{id}` are unchanged | Exposes the per-instance classification marker to integrators without changing the Jira-compatible write path; 1.0.0 stores the marker only and no behaviour depends on it |

## 5. Probe Findings Appendix

Probe metadata and live-verified findings from `docs/PUBLIC-API-PROBE-RECORD.md` (captured from the live Jira 11.3.6 container, `indramesh-jira-1` at localhost:8080, captured 2026-08-02, PAT auth — basic auth disabled on the instance).

Live-verified findings:

1. **DC-only fields ABSENT** — `hasChildren`, `parentObjectId`, `createdBy`, `updatedBy` do NOT appear in object/objecttype/schema GET responses. This resolves the pre-probe MEDIUM-confidence flag: the fields are not emitted by this generation and MUST NOT be invented in the contract (DEV-06).
2. **Error envelope** is `{"errorMessages": [...], "errors": {}}`, not `{"message": ...}` (DEV-03).
3. **IQL pagination metadata is top-level** (`totalFilterCount`, `startIndex`, `toIndex`, `pageObjectSize`, `pageNumber`, `pageSize`, `orderWay`); no `pagination` key is present in the captured response — there is no nested pagination object.
4. **`pageSize` param is not honored**; `resultPerPage` and `page` work (DEV-05).
5. **POST /objecttype/{id}/objects ignores body pagination** — pagination lives on the GET `/aql/objects` query parameters.
6. **DELETE returns 200 + deleted object body**, not 204 (DEV-04).
7. **Value wrappers** — reference values use the `referencedObject` wrapper, status values use the `status` wrapper (Section 2(a)).

Full captured payloads: `docs/PUBLIC-API-PROBE-RECORD.md`; golden files: `backend/tests/fixtures/`.

*Phase 199 — Contract Baseline & P0 Decisions*
