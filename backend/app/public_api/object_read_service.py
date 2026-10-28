"""Batched read assembly for the public object read path (READ-03).

``load_object_read`` implements a fixed query plan (object → object type →
resolved attribute definitions → attribute types → reference types → values →
refs → statuses → referenced objects + their types). Every collection query
uses an IN-list, so the query count is INDEPENDENT of the attribute/value
count — no N+1 (T-201-04). The result is the plain-dict ``ObjectReadData``
contract consumed by ``build_object_shape`` (Plan 201-01) without any further
DB access.

Type-schema tier (D-01/D-02, Phase 207): steps 2-5 depend ONLY on
object_type_id — byte-identical for every object of a type, every caller,
every attribute selection — so they are served from the shared
``cache:public:objecttype-schema:{object_type_id}`` tier (no user_id, no
attrs_hash). Step 9's referenced-object type resolution joins the same tier
(D-06). On a tier hit a single-object GET runs 3 read queries without refs
(object, values, refs) and 4 with refs (+ ref-objects). A tier MISS rebuilds
from the DB and warms the tier; the cache helpers are fail-open (D-08), and a
corrupt payload is treated as a miss (T-207-01). Known accepted trade-off: on
a COLD tier step 9 resolves each distinct target type with its own
miss-path rebuild (N sequential round-trips where the old single IN-list
query ran 1) — fine in steady state (tier warm), a real cold-path regression
for high-fan-out objects; a batched miss-path helper is the documented
follow-up.

T-201-05: only explicitly listed columns are copied into the dicts — no
ORM-row serialization helpers (would leak has_deny_record_permission etc.).
"""

import copy
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from hashlib import md5
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select, text
from sqlalchemy.dialects.postgresql import aggregate_order_by
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import joinedload

from app.models import (
    AttributeType,
    ClassificationFamily,
    ClassificationLabel,
    Object,
    ObjectClassificationLabel,
    ObjectRef,
    ObjectType,
    ObjectTypeAttribute,
    ObjectValue,
    ReferenceType,
    Status,
)
from app.public_api.object_shape import (
    ObjectReadData,
    build_object_shape,
    has_reference_entries,
    strip_hidden_references,
)
from app.services.cache_service import get_cached, set_cached
from app.services.classification_service import load_classifications
from app.services.record_permission_service import build_record_permission_filter
from app.services.select_option_service import (
    build_option_map,
    resolve_display_value,
    resolve_select_values,
)
from app.services.translation_service import (
    resolve_translations,
    validate_read_language,
)


def _as_uuid(value) -> uuid.UUID | None:
    """Coerce a raw id to a UUID or None — defensive against dirty value_structs."""
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError):
        return None


def _key_value_from_values(values: list[dict]) -> str | None:
    """Stored "Key" attribute scalar from an already-loaded value batch.

    The values query joins the writing OTA's name (``attribute_name``) and
    system flag (``attribute_is_system``), so a keyless object's native key
    fallback (DECOUPLE-02) costs ZERO extra queries on the single read and the
    page read. Only the SYSTEM Key OTA counts (WR-04): a user-defined attribute
    literally named "Key" is a different attribute and cannot be the mirror the
    keygen rename keeps in sync. The first non-empty scalar in the
    pre-ordered (created_at, id) batch wins — deterministic.
    """
    for v in values:
        if not v.get("attribute_is_system"):
            continue
        if (v.get("attribute_name") or "").lower() != "key":
            continue
        value = v.get("value_simple")
        if value not in (None, ""):
            return value
    return None


def _key_value_scalar_subquery():
    """Correlated scalar: an object's stored "Key" attribute value, if any.

    Folded into the referenced-targets IN query so the native-key fallback
    for reference values costs ZERO extra round-trips (READ-03 budget).
    """
    return (
        select(ObjectValue.value_simple)
        .join(
            ObjectTypeAttribute,
            ObjectValue.object_type_attribute_id == ObjectTypeAttribute.id,
        )
        .where(ObjectValue.object_id == Object.id)
        .where(ObjectTypeAttribute.name == "Key")
        .where(ObjectTypeAttribute.is_system.is_(True))
        .order_by(ObjectValue.created_at, ObjectValue.id)
        .limit(1)
        .correlate(Object)
        .scalar_subquery()
    )


def _object_labels_scalar_subquery():
    """Correlated scalar: an object's classification label refs as one jsonb value.

    Folded into the step-1 object query so the object's own labels cost ZERO
    extra round-trips (READ-03 budget, T-317.1-19). Same element keys and
    family ``sort`` / label ``position`` / ``key`` order as the attribute-defs
    CTE, so both public surfaces render the block identically. An unclassified
    object coalesces to ``[]``.
    """
    return (
        select(
            func.coalesce(
                func.jsonb_agg(
                    aggregate_order_by(
                        func.jsonb_build_object(
                            "label_id", ClassificationLabel.id,
                            "key", ClassificationLabel.key,
                            "family", ClassificationFamily.key,
                            "name", ClassificationLabel.name,
                            "position", ClassificationLabel.position,
                        ),
                        ClassificationFamily.sort,
                        ClassificationLabel.position,
                        ClassificationLabel.key,
                    )
                ),
                text("'[]'::jsonb"),
            )
        )
        .select_from(ObjectClassificationLabel)
        .join(
            ClassificationLabel,
            ObjectClassificationLabel.label_id == ClassificationLabel.id,
        )
        .join(
            ClassificationFamily,
            ClassificationLabel.family_id == ClassificationFamily.id,
        )
        .where(ObjectClassificationLabel.object_id == Object.id)
        .correlate(Object)
        .scalar_subquery()
    )


def _decode_label_refs(raw: Any) -> list[dict]:
    """Coerce a classification label-ref payload to a list of dicts.

    The CTE emits a ``jsonb`` column; the asyncpg dialect already decodes it to
    Python objects (list of dicts), but a raw ``text()`` result may arrive as a
    JSON string — decode defensively, an unusable payload degrades to ``[]``.
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return []
    return list(raw) if isinstance(raw, list) else []


def _attr_def_dict(row: Any) -> dict:
    """Map one CTE row to the attribute_def contract dict (data_type filled later)."""
    return {
        "id": str(row.id),
        "name": row.name,
        "description": row.description,
        "sort": row.sort,
        "attribute_type_id": str(row.attribute_type_id),
        "minimum_cardinality": row.minimum_cardinality,
        "maximum_cardinality": row.maximum_cardinality,
        "is_required": row.is_required,
        "is_hidden": row.is_hidden,
        "is_unique": row.is_unique,
        "structure": row.structure,
        "referenced_object_type_id": (
            str(row.referenced_object_type_id) if row.referenced_object_type_id else None
        ),
        "reference_type_id": str(row.reference_type_id) if row.reference_type_id else None,
        "classification_labels": _decode_label_refs(row.classification_labels),
        "data_type": None,
    }


def _object_type_dict(ot: ObjectType) -> dict:
    """ObjectType contract dict (also used for referenced object types)."""
    return {
        "id": str(ot.id),
        "name": ot.name,
        "sort": ot.sort,
        "parent_id": str(ot.parent_id) if ot.parent_id else None,
        "schema_id": str(ot.schema_id),
        "created_at": ot.created_at,
        "updated_at": ot.updated_at,
        "is_abstract": ot.is_abstract,
        "icon": (
            {"id": str(ot.icon.id), "name": ot.icon.name, "url": ot.icon.url}
            if ot.icon
            else None
        ),
    }


# CTE inheritance resolution — same SQL shape as
# list_object_type_attributes (object_type_attribute_service.py), but WITHOUT
# COUNT(*) OVER()/LIMIT/OFFSET (all rows) and WITHOUT the reference-type batch
# sub-query (step 5 of the plan handles reference types in its own IN query).
#
# CLASS-02/D-07: every resolved attribute row carries its classification label
# refs as ONE correlated jsonb sub-select (no extra round-trip, no N+1).
# attributes.py::_SCHEMA_ATTR_DEFS_CTE_SQL repeats the identical sub-select
# inline (its own SQL constant), so the single-root and schema-level definition
# CTEs emit the same label-ref keys and ordering. Family ``sort`` then label
# ``position``/``key`` is the public display order (matches
# classification_service.load_classifications).
_CLASSIFICATION_LABELS_COLUMN_SQL = """
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
    ) AS classification_labels"""

_ATTR_DEFS_CTE_SQL = """
WITH RECURSIVE ot_ancestors AS (
    SELECT id, parent_id, name, 0 AS depth
    FROM object_types
    WHERE id = :object_type_id

    UNION ALL

    SELECT ot.id, ot.parent_id, ot.name, ot_ancestors.depth + 1
    FROM object_types ot
    INNER JOIN ot_ancestors ON ot.id = ot_ancestors.parent_id
    WHERE ot_ancestors.depth < 100
)
SELECT resolved.*,""" + _CLASSIFICATION_LABELS_COLUMN_SQL + """
FROM (
    SELECT DISTINCT ON (ota.name)
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
        ota.default_value AS default_value,
        ota.structure AS structure,
        ota.referenced_object_type_id AS referenced_object_type_id,
        ota.reference_type_id AS reference_type_id,
        ota.created_at AS created_at,
        ota.updated_at AS updated_at,
        ot_ancestors.id AS defining_object_type_id,
        ot_ancestors.name AS defining_object_type_name,
        (ot_ancestors.depth > 0) AS is_inherited
    FROM object_type_attributes ota
    INNER JOIN ot_ancestors ON ota.object_type_id = ot_ancestors.id
    ORDER BY ota.name, ot_ancestors.depth ASC
) resolved
ORDER BY resolved.sort ASC, resolved.name ASC
"""


# Names-only variant of the CTE above (READ-02 pre-cache validation): resolves
# the same DISTINCT ON (ota.name) attribute definitions for an object's type
# but returns only the names, in ONE query. A UNION ALL sentinel arm folds the
# existence signal into this SAME query (CR-01, zero extra round-trips): it
# emits exactly one NULL-name row IFF the object row exists, and zero rows
# when it does not. The resolved-names arm never emits NULL, so
# list_attribute_names can distinguish "object missing" (no rows at all) from
# "object exists, no attribute defs" (only the sentinel NULL row) from a
# single db.execute — see the READ-03 query-budget tests
# test_filtered_path_adds_exactly_one_query and
# test_query_count_equal_for_2_and_8_attr_objects, which this MUST NOT break.
_LIST_ATTR_NAMES_SQL = """
WITH RECURSIVE ot_ancestors AS (
    SELECT id, parent_id, name, 0 AS depth
    FROM object_types
    WHERE id = (SELECT object_type_id FROM objects WHERE id = :object_id)

    UNION ALL

    SELECT ot.id, ot.parent_id, ot.name, ot_ancestors.depth + 1
    FROM object_types ot
    INNER JOIN ot_ancestors ON ot.id = ot_ancestors.parent_id
    WHERE ot_ancestors.depth < 100
)
SELECT resolved.name
FROM (
    SELECT DISTINCT ON (ota.name)
        ota.name AS name,
        ot_ancestors.depth AS depth
    FROM object_type_attributes ota
    INNER JOIN ot_ancestors ON ota.object_type_id = ot_ancestors.id
    ORDER BY ota.name, ot_ancestors.depth ASC
) resolved

UNION ALL

SELECT NULL AS name FROM objects WHERE id = :object_id

ORDER BY name ASC NULLS LAST
"""


# Type-schema tier (D-01/D-02): steps 2-5 of the read plan (ObjectType+icon,
# attribute-defs CTE, AttributeType details, ReferenceType details) depend
# ONLY on object_type_id, so their results are cached once per type and shared
# by every object, caller and attribute selection. Long-TTL safety net only
# (D-03, 600s from the discretion range 300/600/3600) — the explicit SCAN
# purge on schema mutations lands in plan 02.
_OBJECTTYPE_SCHEMA_TIER_TTL = 600

# Public object payload shape version (DECOUPLE-02, T-290-02-01): bumped when
# the rendered objectKey/objectTypeAttributeId contract changes. The cached
# wrapper carries the marker, and a payload without the CURRENT marker is
# treated as a miss and rebuilt — a stale Jira-shaped payload can never be
# served after this change, without touching the established cache key
# ownership (D-09: still shared across callers, still no user_id).
# v3 (317.1-05): the four top-level classification keys (classificationLabels,
# confidentiality, personalData, exportControl) — a v2 payload lacks them.
_PUBLIC_OBJECT_SHAPE_VERSION = 3
_PUBLIC_OBJECT_SHAPE_VERSION_KEY = "_shapeVersion"


def _type_schema_cache_key(object_type_id: uuid.UUID) -> str:
    """Canonical type-schema tier key — NO user_id, NO attrs_hash (D-02)."""
    return f"cache:public:objecttype-schema:{str(object_type_id)}"


def _normalize_cached_object_type(raw: dict) -> dict:
    """Deep-copy a cached object_type dict, re-coercing the datetimes.

    ``set_cached`` serializes datetimes to ISO strings via
    ``json.dumps(default=str)``, but ``build_object_type_shape`` and
    ``_build_reference_value`` call ``.isoformat()`` on ``created_at`` /
    ``updated_at`` (object_shape.py:114, 284-287) — a str would raise
    AttributeError. ``_coerce_datetime`` degrades malformed cached values to
    None instead of raising (T-207-06, matches the ``_page_item_contract``
    precedent). All other fields pass through unchanged (ids are str-typed
    already).
    """
    out = copy.deepcopy(raw)
    out["created_at"] = _coerce_datetime(out.get("created_at"))
    out["updated_at"] = _coerce_datetime(out.get("updated_at"))
    return out


async def _load_type_schema(db: AsyncSession, object_type_id: uuid.UUID) -> dict:
    """Miss-path builder: the pre-tier steps 2-5 queries, exactly as they ran
    per request before the tier (ObjectType+icon joinedload; the
    attribute-defs CTE; the AttributeType batch fill; the ReferenceType batch
    fill). Returns the tier payload ``{"object_type": <_object_type_dict(ot)>,
    "attribute_defs": <filled defs list>}`` with ``data_type`` and
    ``reference_type`` already filled on every def."""
    result = await db.execute(
        select(ObjectType)
        .options(joinedload(ObjectType.icon))
        .where(ObjectType.id == object_type_id)
    )
    ot = result.scalar_one()

    result = await db.execute(
        text(_ATTR_DEFS_CTE_SQL), {"object_type_id": object_type_id}
    )
    defs = [_attr_def_dict(row) for row in result.fetchall()]

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
                at_id = uuid.UUID(d["attribute_type_id"])
                d["data_type"] = data_types.get(at_id)

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
                # referenceType.objectSchemaId/url16 pass through from the
                # contract dict (object_shape.py reads .get()). The icon URL
                # is keyed by the native ReferenceType.id (D-05 native
                # identity); objectSchemaId stays None for schema-less
                # reference types (T-201-09 shape tolerance: golden oracle
                # compares type categories only).
                "objectSchemaId": str(rt.schema_id) if rt.schema_id else None,
                "url16": (
                    f"/rest/insight/1.0/config/referencetype/{rt.id}/image.png?size=16"
                ),
            }
            for rt in result.scalars().all()
        }
        for d in defs:
            if d["reference_type_id"]:
                d["reference_type"] = ref_types.get(uuid.UUID(d["reference_type_id"]))

    return {"object_type": _object_type_dict(ot), "attribute_defs": defs}


async def _get_type_schema(db: AsyncSession, object_type_id: uuid.UUID) -> dict:
    """Tier-first type schema: cached payload on a hit, DB rebuild on a miss.

    Fail-open (D-08): ``get_cached``/``set_cached`` swallow Redis errors, so a
    Redis outage degrades to uncached reads. A corrupt (non-dict, or dict with
    a malformed object_type/attribute_defs shape) payload is treated as a miss
    and rebuilt from the DB — the rebuild then overwrites the corrupt value
    (T-207-01). Datetimes are re-coerced on every hit
    (``_normalize_cached_object_type``), so the returned ``object_type`` dict
    always carries real ``datetime`` values for ``.isoformat()`` callers.
    """
    cached = await get_cached(_type_schema_cache_key(object_type_id))
    if (
        isinstance(cached, dict)
        and isinstance(cached.get("object_type"), dict)
        and isinstance(cached.get("attribute_defs"), list)
        # CLASS-02/D-07 shape guard: a payload written before the classification
        # block existed has defs without ``classification_labels`` — treat it as
        # a miss so a stale unclassified schema can never be served.
        and all(
            isinstance(d, dict) and isinstance(d.get("classification_labels"), list)
            for d in cached["attribute_defs"]
        )
    ):
        return {
            "object_type": _normalize_cached_object_type(cached["object_type"]),
            "attribute_defs": cached["attribute_defs"],
        }
    schema = await _load_type_schema(db, object_type_id)
    await set_cached(
        _type_schema_cache_key(object_type_id), schema, ttl=_OBJECTTYPE_SCHEMA_TIER_TTL
    )
    return schema


async def load_object_read(
    db: AsyncSession, object_id: uuid.UUID, lang: str | None = None
) -> ObjectReadData | None:
    """Assemble one object's full read data via the fixed batched query plan.

    Each numbered step is exactly ONE query; collection queries use IN-lists.
    Returns None for a nonexistent object id.

    ``lang`` (Plan 223-05 D-13): resolves the statuses-dict names AND SELECT
    option display values in place before the shape mapper runs. No lang ->
    base SELECT resolution only (ids -> display values), byte-identical to
    the pre-phase output; statuses names are base by construction. Strict
    per-schema language check applies (the object's schema scope is known).
    """
    # 1. Object — the object's own classification label refs ride this query as
    #    a correlated jsonb scalar (CLASS-05, zero extra round-trips).
    result = await db.execute(
        select(Object, _object_labels_scalar_subquery()).where(Object.id == object_id)
    )
    row = result.first()
    if not row:
        return None
    obj, object_labels_raw = row

    # 2-5. Type schema from the shared tier (D-01/D-02): ObjectType+icon,
    #      attribute-defs CTE, AttributeType details, ReferenceType details —
    #      all depend only on object_type_id, so a hit serves them with ZERO
    #      queries; a miss runs the DB path and warms the tier (600s safety
    #      net, D-03). `defs` keeps the same dict shape as before (data_type /
    #      reference_type already filled), so build_attribute_values and
    #      build_object_shape need no changes.
    type_schema = await _get_type_schema(db, obj.object_type_id)
    object_type = type_schema["object_type"]
    defs = type_schema["attribute_defs"]

    # 6. Values (batch) — joined to the writing OTA's name (WR-01): a value
    # may legally be written against an ancestor type's OTA that a child
    # type's own OTA now shadows by name (object_value_service.py's
    # ancestor-check), so defn_by_id lookup in build_attribute_values can miss
    # even though the def resolves fine by name. The join is still ONE query.
    result = await db.execute(
        select(ObjectValue, ObjectTypeAttribute.name, ObjectTypeAttribute.is_system)
        .join(ObjectTypeAttribute, ObjectValue.object_type_attribute_id == ObjectTypeAttribute.id)
        .where(ObjectValue.object_id == object_id)
        .order_by(ObjectValue.created_at, ObjectValue.id)
    )
    values = [
        {
            "id": str(v.id),
            "object_type_attribute_id": str(v.object_type_attribute_id),
            "attribute_name": ota_name,
            "attribute_is_system": ota_is_system,
            "value_integer": v.value_integer,
            "value_float": v.value_float,
            "value_boolean": v.value_boolean,
            "value_simple": v.value_simple,
            "value_datetime": v.value_datetime,
            "value_struct": v.value_struct,
        }
        for v, ota_name, ota_is_system in result.all()
    ]

    # 6b. Native key fallback material (DECOUPLE-02): the stored "Key"
    #     attribute scalar for this object, resolved from the value batch
    #     above — zero extra queries. Object.key still wins in the mapper.
    key_values: dict[str, str] = {}
    obj_key_value = _key_value_from_values(values)
    if obj_key_value:
        key_values[str(obj.id)] = obj_key_value

    # 7. References (batch)
    result = await db.execute(select(ObjectRef).where(ObjectRef.from_object_id == object_id))
    refs = [
        {
            "id": str(r.id),
            "from_attribute_name": r.from_attribute_name,
            "to_object_id": str(r.to_object_id),
        }
        for r in result.scalars().all()
    ]

    # 8. Statuses (batch, only if any value's value_struct carries an "id")
    status_uuids: set[uuid.UUID] = set()
    for v in values:
        struct = v["value_struct"]
        sid = _as_uuid(struct.get("id")) if isinstance(struct, dict) else None
        if sid:
            status_uuids.add(sid)
    statuses: dict[str, dict] = {}
    if status_uuids:
        result = await db.execute(select(Status).where(Status.id.in_(status_uuids)))
        for s in result.scalars().all():
            statuses[str(s.id)] = {
                "id": str(s.id),
                "name": s.name,
                "status_category": s.status_category,
            }

    # 9. Referenced objects + their types (batch, only if refs exist)
    ref_objects: dict[str, dict] = {}
    ref_object_types: dict[str, dict] = {}
    if refs:
        ref_target_ids = {
            uuid.UUID(r["to_object_id"]) for r in refs if r["to_object_id"]
        }
        if ref_target_ids:
            # DECOUPLE-02: the Key-value scalar comes back in the SAME IN
            # query as the target rows (correlated subquery) — ref key
            # fallback adds zero round-trips. Object.key travels along on
            # the ORM row.
            result = await db.execute(
                select(Object, _key_value_scalar_subquery()).where(
                    Object.id.in_(ref_target_ids)
                )
            )
            target_type_ids: set[uuid.UUID] = set()
            for o, target_key_value in result.all():
                ref_objects[str(o.id)] = {
                    "id": str(o.id),
                    "name": o.name,
                    "key": o.key,
                    "created_at": o.created_at,
                    "updated_at": o.updated_at,
                    "object_type_id": str(o.object_type_id),
                }
                if target_key_value and str(o.id) not in key_values:
                    key_values[str(o.id)] = target_key_value
                target_type_ids.add(o.object_type_id)
            if target_type_ids:
                # 9b. Referenced-object types from the tier (D-06) — the same
                #     object_type_id → {ObjectType, icon} lookup as step 2,
                #     per distinct target type. Each target type resolves to a
                #     dict whether served from the tier or rebuilt on a miss
                #     (the miss path warms the tier; datetimes re-coerced).
                #     Known accepted trade-off: on a COLD tier this is N
                #     sequential miss-path round-trips where the old single
                #     IN-list query ran 1 (batched miss-path helper is the
                #     documented follow-up).
                for t_id in target_type_ids:
                    ref_object_types[str(t_id)] = (
                        await _get_type_schema(db, t_id)
                    )["object_type"]

    # 10. ?lang= resolution (Plan 223-05 D-13): SELECT values ALWAYS resolve
    #     id -> base display (byte-identical to pre-phase output); with lang
    #     the translation overlay replaces both SELECT displays and statuses
    #     dict names in place (222 D-02 closure). Strict per-schema language
    #     check: the object's schema scope is known (D-05/D-06).
    if lang:
        await _apply_read_lang(db, values, defs, statuses, lang, strict_schema_id=obj.schema_id)
    else:
        _resolve_select_display_in_place(values, _def_by_id(defs))

    return ObjectReadData(
        obj={
            "id": str(obj.id),
            "name": obj.name,
            "key": obj.key,
            "created_at": obj.created_at,
            "updated_at": obj.updated_at,
        },
        object_type=object_type,
        attribute_defs=defs,
        values=values,
        refs=refs,
        key_values=key_values,
        statuses=statuses,
        ref_objects=ref_objects,
        ref_object_types=ref_object_types,
        object_labels=_decode_label_refs(object_labels_raw),
    )


def _coerce_datetime(value: Any) -> datetime | None:
    """Guarded ISO-8601 coercion — a malformed cached string degrades to None
    instead of raising ValueError (IN-02, matches the defensive style of
    ``_as_uuid``)."""
    if isinstance(value, datetime):
        return value
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# ?lang= read resolution (Plan 223-05, D-13 — closes the 222 D-02 deferral).
#
# Both single-object (load_object_read) and batched page (load_objects_read)
# reads resolve IN PLACE on the assembled dicts BEFORE build_object_shape:
# the shape mapper stays a pure renderer (object_shape.py unchanged).
#
# - SELECT values: ALWAYS resolved id -> base display value (post-migration
#   value_simple stores option ids; the wire must be byte-identical to the
#   pre-phase string output). With ?lang= the lang-aware resolve_select_values
#   runs instead — its chain is translated -> default -> base -> raw (D-19).
# - Statuses dicts: base names by construction; with ?lang= a
#   resolve_translations overlay overwrites the name values in place.
#
# Strictness (D-05/D-06): unknown/inactive code -> 400; not-enabled-for-the
# object's schema -> 400 on single-object reads (schema scope known);
# unfiltered/batched reads validate existence only and resolve per-entity
# (222 D-05 per-item rule).
# ---------------------------------------------------------------------------


def _def_by_id(defs: list[dict]) -> dict[str, dict]:
    return {str(d["id"]): d for d in defs}


def _is_select_def(defn: dict | None) -> bool:
    return bool(defn and defn.get("data_type") == "select")


def _resolve_select_display_in_place(values: list[dict], def_by_id: dict[str, dict]) -> None:
    """Base SELECT resolution (no lang): option id -> display value on the
    value dicts, byte-identical for non-SELECT attributes (empty option map
    -> resolve_display_value no-op). Unresolvable values stay raw (D-15)."""
    option_maps: dict[str, dict[str, str]] = {}
    for v in values:
        defn = def_by_id.get(str(v.get("object_type_attribute_id")))
        if not _is_select_def(defn):
            continue
        attr_key = str(v.get("object_type_attribute_id"))
        if attr_key not in option_maps:
            option_maps[attr_key] = build_option_map(defn.get("structure"))
        option_map = option_maps[attr_key]
        if option_map and v.get("value_simple"):
            v["value_simple"] = resolve_display_value(v["value_simple"], option_map)


async def _overlay_status_names(db: AsyncSession, statuses: dict[str, dict], lang: str) -> None:
    """Overlay ?lang= translations on the statuses dict names in place.

    Per-entity fallback (schema_id=None — the caller has already enforced
    strict schema scope where it is known). Entities without a translation
    row keep their base name (D-19 fallback chain).
    """
    if not statuses:
        return
    resolved = await resolve_translations(
        db, "status", [uuid.UUID(sid) for sid in statuses], ["name"], lang, schema_id=None
    )
    for sid, status in statuses.items():
        entity_resolution = resolved.get(uuid.UUID(sid), {})
        if "name" in entity_resolution:
            status["name"] = entity_resolution["name"][0]


async def _overlay_select_values(
    db: AsyncSession, values: list[dict], def_by_id: dict[str, dict], lang: str
) -> None:
    """Overlay ?lang= translations on SELECT value dicts in place.

    resolve_select_values runs the full chain (translated -> default ->
    base -> raw, D-19/D-15) with the per-entity fallback shape, batched in
    ONE resolve pass. Non-SELECT values pass through untouched.
    """
    stored_by_attr: dict[uuid.UUID, list[str]] = {}
    attrs: list[Any] = []
    for v in values:
        defn = def_by_id.get(str(v.get("object_type_attribute_id")))
        if not _is_select_def(defn):
            continue
        attr_id = uuid.UUID(str(v.get("object_type_attribute_id")))
        if attr_id not in stored_by_attr:
            stored_by_attr[attr_id] = []
            attrs.append(SimpleNamespace(id=attr_id, structure=defn.get("structure")))
        stored_by_attr[attr_id].append(v.get("value_simple"))
    if not attrs:
        return
    resolved = await resolve_select_values(db, attrs, stored_by_attr, lang)
    consumed: dict[uuid.UUID, int] = {aid: 0 for aid in resolved}
    for v in values:
        defn = def_by_id.get(str(v.get("object_type_attribute_id")))
        if not _is_select_def(defn):
            continue
        attr_id = uuid.UUID(str(v.get("object_type_attribute_id")))
        displays = resolved.get(attr_id)
        if displays:
            v["value_simple"] = displays[consumed[attr_id]]
            consumed[attr_id] += 1


async def _apply_read_lang(
    db: AsyncSession,
    values: list[dict],
    defs: list[dict],
    statuses: dict[str, dict],
    lang: str,
    strict_schema_id: uuid.UUID | None = None,
) -> None:
    """?lang= pass over one assembled read (single object or page).

    ``strict_schema_id`` set (single-object read): unknown code AND
    not-enabled-for-schema both 400 (D-05/D-06, scope known). None
    (batched page spanning schemas): existence-only validation, per-entity
    resolution (222 D-05). Mutates ``values``/``statuses`` in place.
    """
    await validate_read_language(db, lang, strict_schema_id)
    await _overlay_status_names(db, statuses, lang)
    await _overlay_select_values(db, values, _def_by_id(defs), lang)


def _page_item_contract(item: Object | dict) -> dict:
    """Normalize one engine page item to the allow-listed contract dict.

    ``execute_aql`` returns ORM ``Object`` instances on a cache MISS, but the
    ``list_objects`` Redis cache round-trips ``jsonable_encoder`` output, so a
    cache HIT delivers plain dicts (str-UUID ids, ISO-8601 datetimes). Both
    shapes are coerced to ONE contract dict here so the assembly never touches
    ORM-specific attributes (T-204-01: only the allow-listed columns travel
    further; the deny/permission flags and other cached columns stay out).
    """
    if isinstance(item, dict):
        created_at = item.get("created_at")
        updated_at = item.get("updated_at")
        return {
            "id": uuid.UUID(str(item["id"])),
            "name": item["name"],
            "key": item.get("key"),
            "object_type_id": uuid.UUID(str(item["object_type_id"])),
            "created_at": _coerce_datetime(created_at),
            "updated_at": _coerce_datetime(updated_at),
        }
    return {
        "id": item.id,
        "name": item.name,
        "key": item.key,
        "object_type_id": item.object_type_id,
        "created_at": item.created_at,
        "updated_at": item.updated_at,
    }


@dataclass
class AqlPageAssembly:
    """Page-level assembly for the public AQL envelope (D-03/D-04/D-07).

    ``load_objects_read`` builds one of these per engine page: every result
    object assembled through the exact ``ObjectReadData`` path as
    ``load_object_read`` (D-04 — identical rendering across the public
    surfaces), per-type resolved attribute definitions for the top-level
    ``objectTypeAttributes`` block (D-03), and a query count independent of
    the page size (D-07).
    """

    page_objects: dict[str, ObjectReadData]  # str(object_id) -> ObjectReadData
    type_defs: dict[str, list[dict]]         # str(object_type_id) -> resolved defs (CTE order)
    ref_object_types: dict[str, dict]        # str(object_type_id) -> ObjectType contract dict


async def load_objects_read(
    db: AsyncSession, objects: list[Object | dict], lang: str | None = None
) -> AqlPageAssembly:
    """Assemble one engine page into per-object ObjectReadData (D-04) + per-type defs (D-03).

    Fixed query plan — the count is INDEPENDENT of the page size and of the
    distinct-type count k (D-07): per-page per-type resolution (steps 1-3)
    and the referenced-object type resolution (step 7b) are served from the
    shared type-schema tier (D-01/D-02, plan 03), so a WARM-tier page costs
    a fixed ≤ 5 queries: values (1) + refs (1) + object-label batch (1,
    317.1-05) + statuses (≤1) + ref-objects (1, only if refs exist). On a COLD
    tier each distinct type runs its own
    miss-path rebuild — k sequential round-trips where the pre-tier code ran
    1-3 batched IN-list queries (accepted trade-off, same as load_object_read;
    the tier is long-TTL 600s and warm almost always). Page items are
    normalized once via ``_page_item_contract``: ``execute_aql`` delivers
    ORM ``Object`` instances on a cache MISS, but the ``list_objects`` Redis
    cache round-trips ``jsonable_encoder`` output, so a cache HIT delivers
    plain dicts (str-UUID ids, ISO-8601 datetimes) — both shapes carry all
    needed columns and are treated identically (no page-object re-query). The
    only ``select(Object)`` is the step-7 referenced-targets IN query, same
    as ``load_object_read`` (T-204-01 allow-lists still apply to every dict).
    """
    if not objects:
        return AqlPageAssembly(page_objects={}, type_defs={}, ref_object_types={})

    page_items = [_page_item_contract(o) for o in objects]
    object_ids = [o["id"] for o in page_items]
    object_id_strs = {str(o["id"]) for o in page_items}

    # 1-3. Per-type schema from the shared tier (D-07, plan 03): ObjectType+
    #      icon, resolved attribute defs (CTE inheritance), and the
    #      AttributeType/ReferenceType detail fill (data_type /
    #      reference_type already populated in the payload) all come from
    #      ``_get_type_schema`` — one tier lookup per DISTINCT type, zero DB
    #      queries on a warm tier. On a cold tier each distinct type runs its
    #      own miss-path rebuild (N sequential round-trips where the old
    #      batched IN-list queries ran 1-3 — the same accepted trade-off as
    #      plan 01's step 9; steady state is tier-warm). ``type_contracts`` /
    #      ``type_defs`` keep their exact pre-tier shapes (str-UUID keys,
    #      filled defs), so the assembly below is unchanged.
    type_ids = list(dict.fromkeys(o["object_type_id"] for o in page_items))
    # ONE tier lookup per DISTINCT type (D-07) — the payload carries both the
    # object_type contract and the resolved defs; a second lookup per type
    # would double the cold-tier miss-path rebuild (and warm-tier Redis GETs).
    type_schemas = {
        str(tid): await _get_type_schema(db, tid) for tid in type_ids
    }
    type_contracts: dict[str, dict] = {
        tid: schema["object_type"] for tid, schema in type_schemas.items()
    }
    type_defs: dict[str, list[dict]] = {
        tid: schema["attribute_defs"] for tid, schema in type_schemas.items()
    }

    # 4. Values (batch) — WR-01 name join, one query
    result = await db.execute(
        select(ObjectValue, ObjectTypeAttribute.name, ObjectTypeAttribute.is_system)
        .join(ObjectTypeAttribute, ObjectValue.object_type_attribute_id == ObjectTypeAttribute.id)
        .where(ObjectValue.object_id.in_(object_ids))
        .order_by(ObjectValue.created_at, ObjectValue.id)
    )
    values_by_object: dict[str, list[dict]] = {sid: [] for sid in object_id_strs}
    for v, ota_name, ota_is_system in result.all():
        values_by_object[str(v.object_id)].append(
            {
                "id": str(v.id),
                "object_type_attribute_id": str(v.object_type_attribute_id),
                "attribute_name": ota_name,
                "attribute_is_system": ota_is_system,
                "value_integer": v.value_integer,
                "value_float": v.value_float,
                "value_boolean": v.value_boolean,
                "value_simple": v.value_simple,
                "value_datetime": v.value_datetime,
                "value_struct": v.value_struct,
            }
        )

    # 4b. Native key fallback material (DECOUPLE-02): per-object stored "Key"
    #     attribute scalar from the value batch above — zero extra queries.
    #     Object.key (carried on the normalized page item) still wins.
    key_values: dict[str, str] = {}
    for sid, vals in values_by_object.items():
        kv = _key_value_from_values(vals)
        if kv:
            key_values[sid] = kv

    # 4c. Object classification labels (CLASS-05): ONE page-level batch,
    #     independent of the page size and distinct-type count (T-317.1-19).
    #     Page items come from the AQL engine, so there is no object query to
    #     fold a scalar sub-select into; this is the page plan's one addition.
    object_labels_by_object: dict[uuid.UUID, list[dict]] = await load_classifications(
        db, "object", object_ids
    )

    # 5. References (batch)
    result = await db.execute(
        select(ObjectRef).where(ObjectRef.from_object_id.in_(object_ids))
    )
    refs_by_object: dict[str, list[dict]] = {sid: [] for sid in object_id_strs}
    all_refs: list[dict] = []
    for r in result.scalars().all():
        ref_dict = {
            "id": str(r.id),
            "from_attribute_name": r.from_attribute_name,
            "to_object_id": str(r.to_object_id),
        }
        refs_by_object[str(r.from_object_id)].append(ref_dict)
        all_refs.append(ref_dict)

    # 6. Statuses (batch, only if any value's value_struct carries an "id")
    status_uuids: set[uuid.UUID] = set()
    for values in values_by_object.values():
        for v in values:
            struct = v["value_struct"]
            sid = _as_uuid(struct.get("id")) if isinstance(struct, dict) else None
            if sid:
                status_uuids.add(sid)
    statuses: dict[str, dict] = {}
    if status_uuids:
        result = await db.execute(select(Status).where(Status.id.in_(status_uuids)))
        for s in result.scalars().all():
            statuses[str(s.id)] = {
                "id": str(s.id),
                "name": s.name,
                "status_category": s.status_category,
            }

    # 7. Referenced objects + their types (batch, only if refs exist)
    ref_objects: dict[str, dict] = {}
    ref_object_types: dict[str, dict] = {}
    if all_refs:
        ref_target_ids = {
            uuid.UUID(r["to_object_id"]) for r in all_refs if r["to_object_id"]
        }
        if ref_target_ids:
            # DECOUPLE-02: Key-value scalar folded into the SAME IN query
            # (correlated subquery) — ref key fallback adds zero round-trips.
            result = await db.execute(
                select(Object, _key_value_scalar_subquery()).where(
                    Object.id.in_(ref_target_ids)
                )
            )
            target_type_ids: set[uuid.UUID] = set()
            for o, target_key_value in result.all():
                ref_objects[str(o.id)] = {
                    "id": str(o.id),
                    "name": o.name,
                    "key": o.key,
                    "created_at": o.created_at,
                    "updated_at": o.updated_at,
                    "object_type_id": str(o.object_type_id),
                }
                if target_key_value and str(o.id) not in key_values:
                    key_values[str(o.id)] = target_key_value
                target_type_ids.add(o.object_type_id)
            if target_type_ids:
                # 7b. Referenced-object types from the tier (D-06) — the same
                #     object_type_id → {ObjectType, icon} lookup as steps 1-3,
                #     per distinct target type (matches load_object_read's
                #     step-9b handling). Each target type resolves to a dict
                #     whether served from the tier or rebuilt on a miss (the
                #     miss path warms the tier; datetimes re-coerced). Known
                #     accepted trade-off: on a COLD tier this is N sequential
                #     miss-path round-trips where the old single IN-list query
                #     ran 1 (batched miss-path helper is the documented
                #     follow-up).
                for t_id in target_type_ids:
                    ref_object_types[str(t_id)] = (
                        await _get_type_schema(db, t_id)
                    )["object_type"]

    # 7c. ?lang= resolution (Plan 223-05 D-13), page-wide and batched: the
    #     statuses dict is shared across the page (overlaid once) and every
    #     page value dict resolves in place (ONE resolve_select_values pass
    #     over all objects' values). No lang -> base SELECT resolution only.
    #     A page may span schemas, so only language EXISTENCE is validated
    #     (unknown/inactive -> 400); per-schema enabledness resolves
    #     per-entity (222 D-05 per-item rule, no page-wide 400).
    all_values = [v for values in values_by_object.values() for v in values]
    page_def_by_id = {
        str(d["id"]): d for defs in type_defs.values() for d in defs
    }
    if lang:
        await validate_read_language(db, lang)
        await _overlay_status_names(db, statuses, lang)
        await _overlay_select_values(db, all_values, page_def_by_id, lang)
    else:
        _resolve_select_display_in_place(all_values, page_def_by_id)

    # 8. One ObjectReadData per object — obj dict built ONLY from the passed
    #    items' allow-listed columns (T-204-01); defs/values/refs filtered per
    #    object; statuses/ref maps shared page-wide
    page_objects: dict[str, ObjectReadData] = {}
    for item in page_items:
        sid = str(item["id"])
        page_objects[sid] = ObjectReadData(
            obj={
                "id": sid,
                "name": item["name"],
                "key": item["key"],
                "created_at": item["created_at"],
                "updated_at": item["updated_at"],
            },
            object_type=type_contracts[str(item["object_type_id"])],
            attribute_defs=type_defs[str(item["object_type_id"])],
            values=values_by_object[sid],
            refs=refs_by_object[sid],
            key_values=key_values,
            statuses=statuses,
            ref_objects=ref_objects,
            ref_object_types=ref_object_types,
            object_labels=object_labels_by_object.get(item["id"], []),
        )

    return AqlPageAssembly(
        page_objects=page_objects,
        type_defs=type_defs,
        ref_object_types=ref_object_types,
    )


def build_public_object_cache_key(
    object_id: str, attributes: list[str] | None, lang: str | None = None
) -> str:
    """READ-04 cache key: ``cache:public:object:{id}:{attrs_hash}`` (D-09).

    The attributes hash is md5 of the sorted lowercased names — stable across
    case/order variants of the same filter; ``None`` (no filter) → ``all``,
    ``[]`` (explicit empty filter) → ``none`` — the two must never collide,
    since ``get_public_object`` stores a full shape for ``None`` and an
    empty-``attributes`` shape for ``[]`` (IN-05).

    ``lang`` (Plan 223-05, T-223-22): when set, a ``:{lang}`` suffix is
    appended — a ?lang=de payload can never be served to a ?lang=en request
    (cross-language leakage is a correctness AND authorization-adjacent
    issue). The no-lang key is byte-identical to the pre-phase format.

    The key is SHARED across all callers (D-09): the assembled shape is
    user-independent, so two service accounts reading the same object with
    the same attributes share ONE 60s cache entry instead of fragmenting
    per user. This is safe because the permission dependency
    (``require_public_object_read``) runs BEFORE the cache wrapper
    (T-201-14) — a DENY-granted client 404s without ever consulting the
    shared entry. The ``cache:public:*`` namespace is deliberately separate
    from the internal ``cache:object:*`` (T-201-12) — the two surfaces can
    never share an entry. Embedded referenced objects are record-filtered per
    caller AFTER the cache read (``record_filter_object_shape``, quick
    261012-blo); the cached shape stays caller-independent.
    """
    if attributes is None:
        attrs_hash = "all"
    elif not attributes:
        attrs_hash = "none"
    else:
        attrs_hash = md5(
            ",".join(sorted(a.lower() for a in attributes)).encode()
        ).hexdigest()[:8]
    key = f"cache:public:object:{object_id}:{attrs_hash}"
    if lang:
        key += f":{lang}"
    return key


async def cache_public_object_shape(
    key: str, shape: dict, ttl: int = 60
) -> None:
    """Store a fresh public object shape under ``key`` with the CURRENT marker.

    Every writer of a ``cache:public:object:*`` entry MUST seed through this
    helper: the read path (``get_public_object``) treats a wrapper without the
    current ``_shapeVersion`` as a miss (DECOUPLE-02), so an unversioned seed
    is dead on arrival — the next consumer GET would rebuild and overwrite it.
    Fail-open: ``set_cached`` swallows Redis errors.
    """
    await set_cached(
        key,
        {**shape, _PUBLIC_OBJECT_SHAPE_VERSION_KEY: _PUBLIC_OBJECT_SHAPE_VERSION},
        ttl=ttl,
    )


async def get_public_object(
    db: AsyncSession,
    object_id: uuid.UUID,
    attributes: list[str] | None,
    lang: str | None = None,
) -> dict | None:
    """Cache-aware public object read (READ-04).

    Cache-first under ``cache:public:object:*``; a hit returns the assembled
    Jira shape without touching the DB loader. A miss assembles via
    ``load_object_read`` → ``build_object_shape`` and stores the shape for
    60s (object tier is volatile). Fail-open: ``get_cached``/``set_cached``
    swallow Redis errors, so a Redis outage degrades to uncached reads.
    Shape versioning (DECOUPLE-02): the stored wrapper carries
    ``_shapeVersion``; a payload without the current marker (e.g. a
    pre-DECOUPLE-02 Jira-shaped entry) is a miss and is rebuilt.

    ``lang`` (Plan 223-05 D-13): threaded into the loader for in-place
    status/SELECT resolution AND into the cache key (T-223-22) — a de
    payload is never served to an en request.

    ORDERING CONTRACT (T-201-14): the endpoint's permission dependency must
    run BEFORE this wrapper — a DENY-granted client 404s without ever
    consulting the cache (the D-09 shared entry is never read by a client
    without read permission). Unknown attribute names must be validated by
    the caller BEFORE this call (``list_attribute_names``), so a cached hit
    can never serve an unknown-name request (T-201-12).
    """
    key = build_public_object_cache_key(str(object_id), attributes, lang)
    cached = await get_cached(key)
    if (
        isinstance(cached, dict)
        and cached.get(_PUBLIC_OBJECT_SHAPE_VERSION_KEY)
        == _PUBLIC_OBJECT_SHAPE_VERSION
    ):
        return {
            k: v for k, v in cached.items()
            if k != _PUBLIC_OBJECT_SHAPE_VERSION_KEY
        }
    data = await load_object_read(db, object_id, lang)
    if data is None:
        return None
    if attributes is not None:
        keep = {a.lower() for a in attributes}
        data.attribute_defs = [
            d for d in data.attribute_defs if d["name"].lower() in keep
        ]
    shape = build_object_shape(data)
    # object tier is volatile — 60s
    await cache_public_object_shape(key, shape, ttl=60)
    return shape


async def load_visible_reference_targets(
    db: AsyncSession,
    from_object_ids: list[uuid.UUID],
    user_id: str,
    *,
    is_superuser: bool,
) -> dict[str, str] | None:
    """Visible referenced-object id → object type id for the ObjectRef targets
    of ``from_object_ids`` (quick 261012-blo, B-05).

    ``None`` means no filtering applies (superuser, no ids, or a role-less
    caller — permissive default) and costs zero queries; otherwise exactly 2
    queries run (role ids inside ``build_record_permission_filter`` + one
    select over the targets with the object record filter applied).
    """
    if is_superuser or not from_object_ids:
        return None
    object_filter = await build_record_permission_filter(
        db, user_id, "object", is_superuser=False
    )
    if object_filter is None:
        return None
    result = await db.execute(
        select(Object.id, Object.object_type_id)
        .where(
            Object.id.in_(
                select(ObjectRef.to_object_id).where(
                    ObjectRef.from_object_id.in_(list(from_object_ids))
                )
            )
        )
        .where(object_filter)
    )
    return {str(oid): str(type_id) for oid, type_id in result.all()}


async def record_filter_object_shape(
    db: AsyncSession,
    shape: dict,
    object_id: uuid.UUID,
    user_id: str,
    *,
    is_superuser: bool,
) -> dict:
    """Apply the per-caller referenced-object filter to a built object shape
    (quick 261012-blo). Superusers and reference-free shapes are returned
    unchanged with zero queries; the input is never mutated."""
    if is_superuser or not has_reference_entries(shape):
        return shape
    visible = await load_visible_reference_targets(
        db, [object_id], user_id, is_superuser=False
    )
    if visible is None:
        return shape
    return strip_hidden_references(shape, visible)


async def list_attribute_names(db: AsyncSession, object_id: uuid.UUID) -> list[str] | None:
    """Resolved attribute-definition names for an object — single CTE query.

    Same inheritance resolution as ``load_object_read`` (nearest ancestor's
    definition wins). Distinguishes nonexistence from an existing object with
    no attribute defs via a UNION ALL sentinel arm folded into
    ``_LIST_ATTR_NAMES_SQL`` (CR-01): a nonexistent object yields zero rows at
    all → returns ``None``; an existing object always yields at least the
    sentinel's single NULL-name row → returns ``list[str]`` (``[]`` when it
    resolves no attribute definitions). READ-02 validation runs on this
    result BEFORE the cache wrapper, so unknown-name requests 400 without
    ever touching a cached shape (T-201-12), and the caller 404s on ``None``
    before that validation (CR-01). This remains a SINGLE ``db.execute`` —
    the query count is fixed (1) regardless of attribute count and MUST NOT
    grow to 2; see test_filtered_path_adds_exactly_one_query and
    test_query_count_equal_for_2_and_8_attr_objects (READ-03 budget).
    """
    result = await db.execute(text(_LIST_ATTR_NAMES_SQL), {"object_id": object_id})
    rows = result.fetchall()
    if not rows:
        return None
    return [row.name for row in rows if row.name is not None]
