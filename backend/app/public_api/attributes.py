"""Public attribute-definition read endpoints (HIER-03, Plan 202-04).

Ships the standalone definition surface for consumers that render/edit
objects from resolved attribute definitions:

- GET /objecttype/{id}/attributes — the CTE-inherited attribute definitions
  of ONE object type, in Jira shape (build_attribute_definition)
- GET /objectschema/{id}/attributes — the definitions of ALL object types in
  a schema, each entry carrying its nested objectType (the DEFINING type),
  assembled from ONE batched partitioned CTE (T-202-07: query count
  INDEPENDENT of the type count — no N+1)

The CTE inheritance resolution reuses the Phase 201 machinery: the module
-level _ATTR_DEFS_CTE_SQL constant and the _attr_def_dict row converter from
object_read_service.py are imported, never reinvented. The schema-level
variant carries its own _SCHEMA_ATTR_DEFS_CTE_SQL because the single-root CTE
CANNOT simply be widened to a multi-root anchor: the recursion would lose
track of which leaf type each ancestor row belongs to, and a schema-wide
DISTINCT ON (ota.name) would silently conflate same-named attributes across
unrelated types. The schema CTE therefore carries the originating leaf type
id through the recursion as an explicit origin_id column and partitions by
it — a batched query that returns wrong per-type results would be worse than
a correct N+1 one (T-202-07's DoS-mitigation claim only holds if the batched
query is also correct).

Label flags resolve against ObjectType.label_attribute_id: the objecttype
level compares against the REQUESTED type's id; the schema level against the
DEFINING type's id (via the CTE's defining_object_type_id column) — so two
schema types with a same-named "Name" attribute can carry different label
flags without conflating.

T-202-01: the wire shape is allow-list constructed via build_attribute_definition
+ build_object_type_shape — internal columns (attribute_type_id, raw structure,
...) are never referenced as output keys.

T-202-02: require_public_hierarchy_read("object-types"/"schemas") runs
BEFORE the route body — type-level RBAC gates the surface; the cache is
never consulted for a denied client. The by-id routes add a record-level
read step (404 on DENY), and the schema-level listing drops every entry whose
own or defining object type is DENY'd (quick 261011-tv1). A DENY'd referenced
object type is emitted as referenceObjectType: null (quick 261011-wfr).

T-202-03: reads are cached under cache:public:objecttype:{id}:attrs:{user} /
cache:public:schema:{id}:attrs:{user} — namespace separate from the internal
cache surfaces, keyed with client identity; TTL 60s, fail-open on Redis
errors.
"""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.db import get_db
from app.models import AttributeType, ObjectType, ReferenceType, Schema
from app.public_api.object_read_service import (
    _ATTR_DEFS_CTE_SQL,
    _attr_def_dict,
    _object_type_dict,
)
from app.public_api.object_shape import build_attribute_definition, build_object_type_shape
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_hierarchy_read
from app.services.cache_service import get_cached, set_cached
from app.services.record_permission_service import build_record_permission_filter

# Two routers: the routes live on different prefixes
# (/objecttype/{id}/attributes vs /objectschema/{id}/attributes) — one
# router per prefix keeps the paths free of route-order traps.
objecttype_attributes_router = APIRouter(prefix="/objecttype", tags=["public-objecttype-attributes"])
schema_attributes_router = APIRouter(prefix="/objectschema", tags=["public-schema-attributes"])

# Schema-level CTE inheritance resolution. Unlike the single-root
# _ATTR_DEFS_CTE_SQL, the anchor starts from ALL of the schema's object types
# and carries the originating leaf type id through the recursion as
# origin_id; the DISTINCT ON is then partitioned by (origin_id, ota.name), so
# two unrelated schema types that both define a same-named attribute keep
# their own resolved row (no silent conflation). defining_object_type_id is
# the type the winning OTA actually lives on — used for the nested objectType
# and the per-defining-type label flag.
_SCHEMA_ATTR_DEFS_CTE_SQL = """
WITH RECURSIVE ot_ancestors AS (
    SELECT id, parent_id, name, id AS origin_id, 0 AS depth
    FROM object_types
    WHERE schema_id = :schema_id

    UNION ALL

    SELECT ot.id, ot.parent_id, ot.name, ot_ancestors.origin_id, ot_ancestors.depth + 1
    FROM object_types ot
    INNER JOIN ot_ancestors ON ot.id = ot_ancestors.parent_id
    WHERE ot_ancestors.depth < 100
)
SELECT resolved.*,
    -- CLASS-02/D-07: the resolved row's classification label refs, in family
    -- sort then label position/key order — ONE correlated sub-select, so the
    -- batched schema query stays ONE statement (no N+1, T-202-07). Mirrors
    -- object_read_service._ATTR_DEFS_CTE_SQL's column exactly.
    COALESCE(
        (
            SELECT jsonb_agg(
                jsonb_build_object(
                    'label_id', cl.id,
                    'key', cl.key,
                    'family', cf.key,
                    'name', cl.name,
                    'position', cl.position
                )
                ORDER BY cf.sort, cl.position, cl.key
            )
            FROM object_type_attribute_classification_labels acl
            INNER JOIN classification_labels cl ON cl.id = acl.label_id
            INNER JOIN classification_families cf ON cf.id = cl.family_id
            WHERE acl.object_type_attribute_id = resolved.id
        ),
        '[]'::jsonb
    ) AS classification_labels
FROM (
    SELECT DISTINCT ON (ot_ancestors.origin_id, ota.name)
        ota.id AS id,
        ota.object_type_id AS object_type_id,
        ota.name AS name,
        ota.description AS description,
        ota.sort AS sort,
        ota.attribute_type_id AS attribute_type_id,
        ota.is_required AS is_required,
        ota.minimum_cardinality AS minimum_cardinality,
        ota.maximum_cardinality AS maximum_cardinality,
        ota.is_unique AS is_unique,
        ota.is_hidden AS is_hidden,
        ota.structure AS structure,
        ota.referenced_object_type_id AS referenced_object_type_id,
        ota.reference_type_id AS reference_type_id,
        ot_ancestors.origin_id AS defining_object_type_id_for_origin,
        ot_ancestors.id AS defining_object_type_id,
        (ot_ancestors.depth > 0) AS is_inherited
    FROM object_type_attributes ota
    INNER JOIN ot_ancestors ON ota.object_type_id = ot_ancestors.id
    ORDER BY ot_ancestors.origin_id, ota.name, ot_ancestors.depth ASC
) resolved
ORDER BY resolved.defining_object_type_id_for_origin, resolved.sort ASC, resolved.name ASC
"""


async def _enrich_defs(db: AsyncSession, defs: list[dict]) -> None:
    """Steps 4-5 of load_object_read (object_read_service.py): attribute
    types IN-batch → data_type; reference types IN-batch → reference_type.
    Copied verbatim from the Phase 201 plan so both endpoints share the exact
    same enrichment contract. Both batches are skipped when there is nothing
    to resolve, keeping the query count fixed per shape of the defs."""
    if defs:
        attr_type_ids = {
            uuid.UUID(d["attribute_type_id"]) for d in defs if d["attribute_type_id"]
        }
        if attr_type_ids:
            result = await db.execute(
                select(AttributeType).where(AttributeType.id.in_(attr_type_ids))
            )
            data_types = {at.id: at.data_type for at in result.scalars().all()}
            for d in defs:
                d["data_type"] = data_types.get(uuid.UUID(d["attribute_type_id"]))

    ref_type_ids = {
        uuid.UUID(d["reference_type_id"]) for d in defs if d["reference_type_id"]
    }
    if ref_type_ids:
        result = await db.execute(
            select(ReferenceType).where(ReferenceType.id.in_(ref_type_ids))
        )
        ref_types = {
            rt.id: {
                "id": str(rt.id),
                "reference": rt.reference,
                "color": rt.color,
                "objectSchemaId": str(rt.schema_id) if rt.schema_id else None,
                # D-05: the icon URL is keyed by the native ReferenceType.id.
                "url16": (
                    f"/rest/insight/1.0/config/referencetype/{rt.id}/image.png?size=16"
                ),
            }
            for rt in result.scalars().all()
        }
        for d in defs:
            if d["reference_type_id"]:
                d["reference_type"] = ref_types.get(uuid.UUID(d["reference_type_id"]))


async def _load_ref_object_types(db: AsyncSession, defs: list[dict], type_filter=None) -> dict[str, dict]:
    """Referenced object types for reference attrs — ONE IN query (batched).

    A type excluded by type_filter is missing from the map, so
    build_attribute_definition emits referenceObjectType: null for it."""
    ref_ids = {
        uuid.UUID(d["referenced_object_type_id"])
        for d in defs if d["referenced_object_type_id"]
    }
    out: dict[str, dict] = {}
    if ref_ids:
        stmt = (
            select(ObjectType)
            .options(joinedload(ObjectType.icon))
            .where(ObjectType.id.in_(ref_ids))
        )
        if type_filter is not None:
            stmt = stmt.where(type_filter)
        result = await db.execute(stmt)
        for t in result.scalars().all():
            out[str(t.id)] = _object_type_dict(t)
    return out


async def _public_objecttype_attributes(
    db: AsyncSession, object_type_id: uuid.UUID, type_filter=None
) -> list[dict] | None:
    # existence probe
    result = await db.execute(select(ObjectType).where(ObjectType.id == object_type_id))
    ot = result.scalar_one_or_none()
    if ot is None:
        return None
    # resolved defs via the Phase 201 single-root CTE (inheritance + name
    # override), all rows
    result = await db.execute(text(_ATTR_DEFS_CTE_SQL), {"object_type_id": object_type_id})
    defs = [_attr_def_dict(row) for row in result.fetchall()]
    await _enrich_defs(db, defs)
    label_id = ot.label_attribute_id
    ref_object_types = await _load_ref_object_types(db, defs, type_filter)
    for d in defs:
        d["is_label_attribute"] = (d["id"] == str(label_id)) if label_id else False
    return [build_attribute_definition(d, ref_object_types) for d in defs]


async def _public_schema_attributes(
    db: AsyncSession, schema_id: uuid.UUID, type_filter=None
) -> list[dict] | None:
    # 1. types (existence probe = empty list) — note the icon relationship is
    # selectin-loaded in ONE query on first access via _object_type_dict
    stmt = select(ObjectType).where(ObjectType.schema_id == schema_id)
    if type_filter is not None:
        stmt = stmt.where(type_filter)
    result = await db.execute(stmt)
    types = result.scalars().all()
    if not types:
        probe = await db.execute(select(Schema.id).where(Schema.id == schema_id))
        if probe.scalar_one_or_none() is None:
            return None
        return []
    type_dicts = {str(t.id): _object_type_dict(t) for t in types}
    label_ids = {str(t.id): t.label_attribute_id for t in types}

    # 2. resolved defs for ALL types in ONE partitioned CTE
    result = await db.execute(text(_SCHEMA_ATTR_DEFS_CTE_SQL), {"schema_id": schema_id})
    rows = result.fetchall()
    if type_filter is not None:
        rows = [
            row for row in rows
            if str(row.defining_object_type_id_for_origin) in type_dicts
            and str(row.defining_object_type_id) in type_dicts
        ]
    defs = [_attr_def_dict(row) for row in rows]
    defining_ids = [str(row.defining_object_type_id) for row in rows]

    # 3-4. batched enrichment (attribute types + reference types)
    await _enrich_defs(db, defs)
    ref_object_types = await _load_ref_object_types(db, defs, type_filter)

    out = []
    for d, defining_id in zip(defs, defining_ids):
        label_id = label_ids.get(defining_id)
        d["is_label_attribute"] = (d["id"] == str(label_id)) if label_id else False
        shape = build_attribute_definition(d, ref_object_types)
        # schema-level entries carry their nested objectType — the DEFINING
        # type (for an inherited attr that is the ancestor that owns the OTA)
        shape["objectType"] = build_object_type_shape(type_dicts[defining_id])
        out.append(shape)
    return out


def _is_current_definition_payload(cached: Any) -> bool:
    """Cache-hit shape guard (DECOUPLE-02, T-290-02-01; CLASS-02/D-07).

    Definition payloads rendered before the native-identifier change carry
    int/None ``objectTypeAttributeId`` values (the foreign Jira integer), and
    payloads rendered before attribute classification lack the
    ``classificationLabels`` list. A cached payload that does not satisfy the
    current contract is treated as a miss and rebuilt, so a stale Jira-shaped
    or unclassified list can never be served. The cache KEY and its ownership
    stay unchanged (T-202-03).
    """
    return isinstance(cached, list) and all(
        isinstance(entry, dict)
        and isinstance(entry.get("objectTypeAttributeId"), str)
        and isinstance(entry.get("classificationLabels"), list)
        for entry in cached
    )


@objecttype_attributes_router.get("/{object_type_id}/attributes")
async def public_get_objecttype_attributes(
    object_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("object-types", "object_type_id")),
):
    key = f"cache:public:objecttype:{object_type_id}:attrs:{current_user.id}"
    cached = await get_cached(key)
    if _is_current_definition_payload(cached):
        return cached
    type_filter = await build_record_permission_filter(
        db, str(current_user.id), "object_type", is_superuser=current_user.is_superuser)
    shape = await _public_objecttype_attributes(db, object_type_id, type_filter)
    if shape is None:
        raise HTTPException(status_code=404, detail="Object type not found")
    await set_cached(key, shape, ttl=60)
    return shape


@schema_attributes_router.get("/{schema_id}/attributes")
async def public_get_schema_attributes(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("schemas", "schema_id")),
):
    key = f"cache:public:schema:{schema_id}:attrs:{current_user.id}"
    cached = await get_cached(key)
    if _is_current_definition_payload(cached):
        return cached
    type_filter = await build_record_permission_filter(
        db, str(current_user.id), "object_type", is_superuser=current_user.is_superuser)
    shape = await _public_schema_attributes(db, schema_id, type_filter)
    if shape is None:
        raise HTTPException(status_code=404, detail="Schema not found")
    await set_cached(key, shape, ttl=60)
    return shape
