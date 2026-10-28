import uuid
from typing import List

from sqlalchemy import delete, func, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models import (
    AttributeType,
    AttributeTypeDataType,
    ObjectRef,
    ObjectType,
    ObjectTypeAttribute,
    ReferenceType,
)
from app.schemas.crud import ObjectTypeAttributeCreate, ObjectTypeAttributeUpdate, ObjectTypeAttributeResponse, ReferenceTypeNested
from app.schemas.classification import CLASSIFICATION_INPUT_FIELDS
from app.services.classification_service import (
    attach_classifications,
    classification_audit_keys,
    classification_kwargs,
    load_classifications,
    set_labels,
)
from app.services.translation_service import resolve_translations, delete_translations_for
from app.services.select_option_service import ensure_option_ids, removed_option_ids, option_ids_from_structure
from app.services.record_permission_service import (
    _batch_check_has_deny_record_permission,
    build_record_permission_filter,
)
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    invalidate_by_tags,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.public_api.objecttype_schema_cache import (
    purge_public_definition_caches,
    purge_public_objecttype_schema_cache,
)
from app.services.audit_service import audit_log, model_to_dict
from app.services.search_service import refresh_index_mapping


# OTA writes are audited against the owning ObjectType — the existing auditable
# entity type whose toggle governs them — so the ObjectType's audit history
# shows its attribute-shape changes. No new audit entity-type config row is
# introduced (D-04/D-11); the actor is the route user or the resolved SDK actor.
_AUDIT_ENTITY_TYPE = "object_types"


def _build_resolved_tag(object_type_id: uuid.UUID) -> str:
    return f"tag:object_type_attribute:resolved:{str(object_type_id)}"


def _has_current_lock_shape(payload: object) -> bool:
    """Whether a cached attribute payload carries the current shape.

    WR-07: pre-290 entries stored no boolean ``is_locked``. The fail-closed
    frontend reads a missing marker as "locked", so such an entry is treated as
    a miss and rebuilt instead of served as a false read-only lock. The public
    definition cache received the same guard in DECOUPLE-02.

    317.1: a classification write precedes this shape, so an entry without a
    ``classification_labels`` list was written before labels were attached and
    would report a classified attribute as unclassified — rebuild it instead
    (RESEARCH Pitfall 1).
    """
    return (
        isinstance(payload, dict)
        and isinstance(payload.get("is_locked"), bool)
        and isinstance(payload.get("classification_labels"), list)
    )


def _is_current_list_payload(cached: object) -> bool:
    """Shape guard for the cached object-type-attribute LIST payload (WR-07).

    Every item must carry a boolean ``is_locked`` and a
    ``classification_labels`` list; a malformed, pre-290 or
    pre-classification ``{"items", "total"}`` payload is a miss and is rebuilt
    by the caller.
    """
    if not isinstance(cached, dict):
        return False
    items = cached.get("items")
    if not isinstance(items, list):
        return False
    return all(_has_current_lock_shape(item) for item in items)


async def collect_descendant_type_ids(db: AsyncSession, object_type_id: uuid.UUID) -> list[str]:
    """Collect [object_type_id] + all descendant type ids (D-05).

    Shared utility: consumed by object_type_service and
    reference_type_service (type-schema tier purge) as well as by the
    internal resolved-tag invalidation paths below. Keep the return type
    stable — it is a cross-module contract.

    Uses the same recursive CTE over the parent_id chain as
    ``_invalidate_descendant_resolved_caches`` — the shared descendant
    collection for both the resolved-tag invalidation and the type-schema
    tier purge (an ancestor attribute mutation must never leave a
    descendant's stale shape in the tier).
    """
    descendant_sql = """
    WITH RECURSIVE ot_descendants AS (
        SELECT id, 0 AS depth FROM object_types WHERE id = :object_type_id

        UNION ALL

        SELECT ot.id, ot_descendants.depth + 1 FROM object_types ot
        INNER JOIN ot_descendants ON ot.parent_id = ot_descendants.id
        WHERE ot_descendants.depth < 100
    )
    SELECT id FROM ot_descendants
    """
    result = await db.execute(text(descendant_sql), {"object_type_id": object_type_id})
    rows = result.fetchall()
    return [str(object_type_id)] + [
        str(row.id) for row in rows if str(row.id) != str(object_type_id)
    ]


async def _schema_ids_of_object_types(
    db: AsyncSession, object_type_ids: list[str]
) -> list[str]:
    """Schema ids owning the given object types (scope of the public schema payload).

    The per-client schema definition payload is not type-scoped, so the purge
    needs the schemas of the affected types rather than the type ids.
    """
    if not object_type_ids:
        return []
    result = await db.execute(
        select(ObjectType.schema_id)
        .where(ObjectType.id.in_([uuid.UUID(oid) for oid in object_type_ids]))
        .distinct()
    )
    return [str(schema_id) for schema_id in result.scalars().all()]


async def _invalidate_descendant_resolved_caches(db: AsyncSession, object_type_id: uuid.UUID) -> None:
    """Invalidate resolved-attribute caches for object_type_id and all its descendants.

    Uses a recursive CTE to walk down the parent_id chain and collects all
    descendant IDs, then invalidates each one's resolved tag.
    """
    type_ids = await collect_descendant_type_ids(db, object_type_id)
    tag_list = [_build_resolved_tag(uuid.UUID(type_id)) for type_id in type_ids]
    if tag_list:
        await invalidate_by_tags(tag_list)


async def _attribute_data_type(db: AsyncSession, attribute_type_id: uuid.UUID) -> str | None:
    """The AttributeType's data_type for an OTA (``None`` when it vanished)."""
    return (
        await db.execute(
            select(AttributeType.data_type).where(
                AttributeType.id == attribute_type_id
            )
        )
    ).scalar_one_or_none()


async def _ref_maintenance_type_ids(
    db: AsyncSession,
    declaring_type_id: uuid.UUID,
    attribute_id: uuid.UUID,
    attribute_name: str,
) -> list[uuid.UUID]:
    """Declaring type + descendants that actually resolve to this attribute.

    A reference row stores the **from object's** type, so an inherited
    reference attribute leaves refs under a descendant type that a single-type
    predicate never reaches (EXT-09-D1). The subtree walk closes that gap.

    A descendant may declare its **own** attribute with the same name, which
    shadows the inherited one — its refs belong to the descendant's attribute
    and must not be renamed or deleted. Those subtrees are excluded, not just
    the declaring type id (a grandchild of the shadowing type resolves to the
    descendant's attribute too). This ambiguity is inherent to name-keyed refs;
    the after-1.0 ``object_refs.from_attribute_id`` column removes it.
    """
    type_ids = await collect_descendant_type_ids(db, declaring_type_id)
    type_uuids = [uuid.UUID(type_id) for type_id in type_ids]
    shadowing = (
        await db.execute(
            select(ObjectTypeAttribute.object_type_id).where(
                ObjectTypeAttribute.object_type_id.in_(type_uuids),
                ObjectTypeAttribute.name == attribute_name,
                ObjectTypeAttribute.id != attribute_id,
            )
        )
    ).scalars().all()
    excluded: set[str] = set()
    for shadowing_root in {str(type_id) for type_id in shadowing}:
        excluded.update(
            await collect_descendant_type_ids(db, uuid.UUID(shadowing_root))
        )
    return [
        type_uuid
        for type_id, type_uuid in zip(type_ids, type_uuids)
        if type_id not in excluded
    ]


async def create_object_type_attribute(
    db: AsyncSession,
    attribute_data: ObjectTypeAttributeCreate,
    *,
    actor_id: uuid.UUID | None = None,
    id: uuid.UUID | None = None,
) -> ObjectTypeAttribute | None:
    """Create an ObjectTypeAttribute.

    ``actor_id`` is the acting user (an authenticated REST user or the SDK's
    resolved actor). When it is ``None`` — an internal caller such as system
    provisioning or sync — no audit entry is written, exactly as before.
    """
    result = await db.execute(
        select(ObjectType).where(
            ObjectType.id == attribute_data.object_type_id
        )
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        return None
    # D-08 / MULTI-02: an attribute can only be marked multilingual when the
    # parent ObjectType opted in first — server-side enforcement, 400 at the route.
    if attribute_data.multilingual and not object_type.multilingual:
        raise ValueError("Object type is not multilingual: cannot set multilingual=True on its attribute")

    result = await db.execute(
        select(AttributeType).where(
            AttributeType.id == attribute_data.attribute_type_id
        )
    )
    attribute_type = result.scalar_one_or_none()
    if not attribute_type:
        return None

    if attribute_data.referenced_object_type_id:
        result = await db.execute(
            select(ObjectType).where(
                ObjectType.id == attribute_data.referenced_object_type_id
            )
        )
        ref_type = result.scalar_one_or_none()
        if not ref_type:
            return None

    # D-02: normalize the incoming structure BEFORE assignment — a dict with
    # an "options" key gets the canonical {"id", "value", "position"} shape.
    # Copy the dict before mutating (never touch the caller's object).
    structure = attribute_data.structure
    if isinstance(structure, dict) and "options" in structure:
        structure = dict(structure)
        structure["options"] = ensure_option_ids(structure["options"])

    db_attribute = ObjectTypeAttribute(
        id=id or uuid.uuid4(),
        object_type_id=attribute_data.object_type_id,
        name=attribute_data.name,
        description=attribute_data.description,
        sort=attribute_data.sort,
        attribute_type_id=attribute_data.attribute_type_id,
        is_required=attribute_data.is_required,
        default_value=attribute_data.default_value,
        structure=structure,
        referenced_object_type_id=attribute_data.referenced_object_type_id,
        minimum_cardinality=attribute_data.minimum_cardinality,
        maximum_cardinality=attribute_data.maximum_cardinality,
        is_unique=attribute_data.is_unique,
        is_hidden=attribute_data.is_hidden,
        reference_type_id=attribute_data.reference_type_id,
        multilingual=attribute_data.multilingual,
    )
    db.add(db_attribute)
    # Flush first so the generated id is available to the audit entry; the audit
    # is written in the SAME transaction as the insert, before the single commit.
    await db.flush()
    # D-07: the labels are written in the same transaction as the row. A
    # ValueError (unknown / wrong-family id) reaches the route as 400 and the
    # flushed row is never committed.
    _, created_labels = await set_labels(
        db,
        "attribute",
        db_attribute.id,
        **classification_kwargs(attribute_data, partial=False),
    )
    if actor_id is not None:
        created_values = model_to_dict(db_attribute)
        created_values["classification_labels"] = classification_audit_keys(
            created_labels
        )
        await audit_log(
            db,
            _AUDIT_ENTITY_TYPE,
            attribute_data.object_type_id,
            "CREATE",
            actor_id,
            old_values=None,
            new_values=created_values,
        )
    await db.commit()
    # D-16: the new attribute reaches the index mapping here — a document write
    # never touches the mapping.
    await refresh_index_mapping(attribute_data.object_type_id)
    await invalidate_entity_cache("object_type_attribute")
    await _invalidate_descendant_resolved_caches(db, attribute_data.object_type_id)
    await purge_public_objecttype_schema_cache(
        await collect_descendant_type_ids(db, attribute_data.object_type_id)
    )
    result = await db.execute(
        select(ObjectTypeAttribute).options(selectinload(ObjectTypeAttribute.reference_type))
        .where(ObjectTypeAttribute.id == db_attribute.id)
    )
    stored_attribute = result.scalar_one()
    await attach_classifications(db, "attribute", [stored_attribute])
    return stored_attribute


async def list_visible_attribute_name_data_types(
    db: AsyncSession,
    user_id: str | None = None,
    is_superuser: bool = False,
):
    query = (
        select(ObjectTypeAttribute.name, AttributeType.data_type)
        .distinct()
        .outerjoin(
            AttributeType,
            AttributeType.id == ObjectTypeAttribute.attribute_type_id,
        )
    )
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db,
            user_id,
            "object_type",
            is_superuser=is_superuser,
            alias="object_types",
        )
        if filter_clause is not None:
            query = query.join(
                ObjectType,
                ObjectType.id == ObjectTypeAttribute.object_type_id,
            ).where(filter_clause)

    result = await db.execute(query)
    return result.all()


async def list_object_type_attributes(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    object_type_id: uuid.UUID | None = None,
    user_id: str | None = None, is_superuser: bool = False,
    lang: str | None = None,
) -> tuple[List[ObjectTypeAttribute], int]:
    cache_key = build_list_cache_key(
        "object_type_attribute", skip=skip, limit=limit, object_type_id=object_type_id,
        user_id=user_id, lang=lang,
    )
    cached = await get_cached(cache_key)
    if cached is not None and _is_current_list_payload(cached):
        return cached["items"], cached["total"]

    if object_type_id:
        # CTE-based inheritance resolution — walk parent_id chain and merge
        # attributes from all ancestors, with DISTINCT ON for name-based override.
        # D-06 (parent-side semantics): a parent ObjectType propagates its
        # attributes to its children when THAT PARENT has inherits_attributes=true.
        # The recursive step therefore gates on the joined parent's flag
        # (`ot.inherits_attributes`), not the child's.
        cte_sql = """
        WITH RECURSIVE ot_ancestors AS (
            SELECT id, parent_id, name, inherits_attributes, 0 AS depth
            FROM object_types
            WHERE id = :object_type_id

            UNION ALL

            SELECT ot.id, ot.parent_id, ot.name, ot.inherits_attributes, ot_ancestors.depth + 1
            FROM object_types ot
            INNER JOIN ot_ancestors ON ot.id = ot_ancestors.parent_id
            WHERE ot.inherits_attributes = true AND ot_ancestors.depth < 100
        )
        SELECT resolved.*, COUNT(*) OVER() AS total_count
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
                ota.is_system AS is_system,
                ota.default_value AS default_value,
                ota.structure AS structure,
                ota.referenced_object_type_id AS referenced_object_type_id,
                ota.reference_type_id AS reference_type_id,
                ota.multilingual AS multilingual,
                ota.managed_by AS managed_by,
                ota.externally_readonly AS externally_readonly,
                ota.created_at AS created_at,
                ota.updated_at AS updated_at,
                ot_ancestors.id AS defining_object_type_id,
                ot_ancestors.name AS defining_object_type_name,
                (ot_ancestors.depth > 0) AS is_inherited
            FROM object_type_attributes ota
            INNER JOIN ot_ancestors ON ota.object_type_id = ot_ancestors.id
            ORDER BY ota.name, ot_ancestors.depth ASC
        ) resolved
        ORDER BY resolved.sort ASC
        LIMIT :limit OFFSET :skip
        """

        result = await db.execute(text(cte_sql), {
            "object_type_id": object_type_id,
            "skip": skip,
            "limit": limit,
        })
        rows = result.fetchall()
        total = rows[0].total_count if rows else 0

        # D-02/D-14: the effective lock is contextual to the REQUESTED type —
        # its own marker OR the resolved attribute's marker, never the defining
        # ancestor's marker alone.
        requested_type_result = await db.execute(
            select(ObjectType.externally_readonly).where(ObjectType.id == object_type_id)
        )
        requested_type_readonly = bool(requested_type_result.scalar_one_or_none())

        # Batch-load reference types for all rows in one query
        ref_type_ids = {row.reference_type_id for row in rows if row.reference_type_id}
        ref_type_map = {}
        if ref_type_ids:
            ref_result = await db.execute(
                select(ReferenceType).where(ReferenceType.id.in_(ref_type_ids))
            )
            for rt in ref_result.scalars().all():
                ref_type_map[rt.id] = ReferenceTypeNested(
                    id=rt.id,
                    reference=rt.reference,
                    color=rt.color,
                )

        # Build serializable dicts from CTE result rows
        items = []
        for row in rows:
            rt = ref_type_map.get(row.reference_type_id) if row.reference_type_id else None
            item = {
                "id": row.id,
                "object_type_id": row.object_type_id,
                "name": row.name,
                "description": row.description,
                "sort": row.sort,
                "attribute_type_id": row.attribute_type_id,
                "is_required": row.is_required,
                "minimum_cardinality": row.minimum_cardinality,
                "maximum_cardinality": row.maximum_cardinality,
                "is_unique": row.is_unique,
                "is_hidden": row.is_hidden,
                "is_system": row.is_system,
                "default_value": row.default_value,
                "structure": row.structure,
                "referenced_object_type_id": row.referenced_object_type_id,
                "reference_type_id": row.reference_type_id,
                "multilingual": row.multilingual,
                "managed_by": row.managed_by,
                "externally_readonly": row.externally_readonly,
                "is_external": row.managed_by is not None,
                "is_locked": requested_type_readonly or bool(row.externally_readonly),
                "created_at": row.created_at,
                "updated_at": row.updated_at,
                "defining_object_type_id": row.defining_object_type_id,
                "defining_object_type_name": row.defining_object_type_name,
                "is_inherited": bool(row.is_inherited),
                "reference_type": rt.model_dump() if rt else None,
            }
            items.append(item)

        if user_id is not None and items:
            filter_clause = await build_record_permission_filter(
                db, user_id, "object_type", is_superuser=is_superuser
            )
            if filter_clause is not None:
                distinct_ids = {item["defining_object_type_id"] for item in items if item.get("defining_object_type_id")}
                visible_result = await db.execute(
                    select(ObjectType.id).where(ObjectType.id.in_(distinct_ids)).where(filter_clause)
                )
                visible_ids = {row[0] for row in visible_result.all()}
                items = [item for item in items if item["defining_object_type_id"] in visible_ids]
                total = len(items)

        deny_ids: set[str] = set()
        if items:
            attr_type_ids = [item["attribute_type_id"] for item in items if item.get("attribute_type_id")]
            if attr_type_ids:
                deny_ids = await _batch_check_has_deny_record_permission(
                    db, attr_type_ids, "attribute_type",
                )
        for item in items:
            item["has_deny_record_permission"] = str(item["attribute_type_id"]) in deny_ids if item.get("attribute_type_id") else False

        # D-07: attach the classification after visibility/deny filtering (so no
        # hidden row's labels are loaded) and before the cache write. The dict
        # payload then carries both the generic list and the derived fields.
        await attach_classifications(db, "attribute", items)

        if lang and items:
            # D-06 strictness: the schema scope is known (the filtered ObjectType's schema)
            attr_schema_result = await db.execute(
                select(ObjectType.schema_id).where(ObjectType.id == object_type_id)
            )
            attr_schema_id = attr_schema_result.scalar_one_or_none()
            resolved = await resolve_translations(
                db, "object_type_attribute",
                [item["id"] for item in items],
                ["name", "description"],
                lang,
                schema_id=attr_schema_id,   # None → per-entity fallback (unfiltered flat branch)
            )
            for item in items:
                entity_resolution = resolved.get(item["id"], {})
                if "name" in entity_resolution:
                    value, serving = entity_resolution["name"]
                    item["name"] = value
                    item["resolved_language"] = serving
                if "description" in entity_resolution:
                    value, serving_desc = entity_resolution["description"]
                    item["description"] = value
                    if item.get("resolved_language") is None:
                        item["resolved_language"] = serving_desc

            # D-01: resolve embedded status_options[].name via the status translation branch.
            # Per-entity fallback (schema_id=None) per D-05 per-item rule — the attribute-level
            # D-06 check already gated the request against the filtered ObjectType's schema.
            status_ids: list = []
            for item in items:
                structure = item.get("structure") or {}
                for opt in structure.get("status_options") or []:
                    if opt.get("id"):
                        status_ids.append(opt["id"])
            if status_ids:
                status_resolved = await resolve_translations(
                    db, "status", list(set(status_ids)), ["name"], lang, schema_id=None,
                )
                for item in items:
                    structure = dict(item.get("structure") or {})
                    opts = structure.get("status_options")
                    if not opts:
                        continue
                    new_opts = []
                    for opt in opts:
                        opt_dict = dict(opt)
                        # resolve_translations returns uuid.UUID keys — convert string id.
                        opt_id = opt_dict.get("id")
                        res = status_resolved.get(uuid.UUID(opt_id) if opt_id else None, {})
                        if "name" in res:
                            value, serving = res["name"]
                            opt_dict["name"] = value
                            if item.get("resolved_language") is None:
                                item["resolved_language"] = serving
                        new_opts.append(opt_dict)
                    structure["status_options"] = new_opts
                    item["structure"] = structure

        await set_cached_with_tags(
            cache_key,
            {"items": items, "total": total},
            tags=[build_tag_key("object_type_attribute"), _build_resolved_tag(object_type_id)],
            ttl=CACHE_TTL_LIST,
        )

        return items, total

    # No object_type_id filter — use original flat ORM query (no inheritance)
    query = select(ObjectTypeAttribute).options(selectinload(ObjectTypeAttribute.reference_type))
    count_query = select(func.count()).select_from(ObjectTypeAttribute)
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db, user_id, "object_type", is_superuser=is_superuser, alias="object_types"
        )
        if filter_clause is not None:
            query = query.join(ObjectType, ObjectType.id == ObjectTypeAttribute.object_type_id).where(filter_clause)
            count_query = count_query.join(ObjectType, ObjectType.id == ObjectTypeAttribute.object_type_id).where(filter_clause)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()

    deny_ids: set[str] = set()
    if items:
        attr_type_ids = [item.attribute_type_id for item in items if item.attribute_type_id]
        if attr_type_ids:
            deny_ids = await _batch_check_has_deny_record_permission(
                db, attr_type_ids, "attribute_type",
            )
    for item in items:
        item.has_deny_record_permission = str(item.attribute_type_id) in deny_ids if item.attribute_type_id else False

    # D-02/D-14: no requested type context in the flat list — the owning type
    # of each attribute is the effective requested type (marker OR own flag).
    owner_ids = {item.object_type_id for item in items}
    owner_readonly_map: dict[uuid.UUID, bool] = {}
    if owner_ids:
        owner_lock_result = await db.execute(
            select(ObjectType.id, ObjectType.externally_readonly).where(
                ObjectType.id.in_(owner_ids)
            )
        )
        owner_readonly_map = {
            row.id: bool(row.externally_readonly) for row in owner_lock_result.all()
        }
    for item in items:
        item.is_locked = (
            owner_readonly_map.get(item.object_type_id, False)
            or bool(item.externally_readonly)
        )

    # D-07: labels on the ORM rows before serialization, so the dumped response
    # carries the derived convenience fields as well as the generic list.
    await attach_classifications(db, "attribute", items)

    serialized_items = [ObjectTypeAttributeResponse.model_validate(item).model_dump() for item in items]
    if lang:
        resolved = await resolve_translations(
            db, "object_type_attribute",
            [item.id for item in items],
            ["name", "description"],
            lang,
            schema_id=None,
        )
        for item in serialized_items:
            entity_resolution = resolved.get(item["id"], {})
            if "name" in entity_resolution:
                value, serving = entity_resolution["name"]
                item["name"] = value
                item["resolved_language"] = serving
            if "description" in entity_resolution:
                value, serving_desc = entity_resolution["description"]
                item["description"] = value
                if item.get("resolved_language") is None:
                    item["resolved_language"] = serving_desc

        # D-01: same status_options resolution for the serialized flat items.
        status_ids: list = []
        for item in serialized_items:
            structure = item.get("structure") or {}
            for opt in structure.get("status_options") or []:
                if opt.get("id"):
                    status_ids.append(opt["id"])
        if status_ids:
            status_resolved = await resolve_translations(
                db, "status", list(set(status_ids)), ["name"], lang, schema_id=None,
            )
            for item in serialized_items:
                structure = dict(item.get("structure") or {})
                opts = structure.get("status_options")
                if not opts:
                    continue
                new_opts = []
                for opt in opts:
                    opt_dict = dict(opt)
                    # resolve_translations returns uuid.UUID keys — convert string id.
                    opt_id = opt_dict.get("id")
                    res = status_resolved.get(uuid.UUID(opt_id) if opt_id else None, {})
                    if "name" in res:
                        value, serving = res["name"]
                        opt_dict["name"] = value
                        if item.get("resolved_language") is None:
                            item["resolved_language"] = serving
                    new_opts.append(opt_dict)
                structure["status_options"] = new_opts
                item["structure"] = structure

    await set_cached_with_tags(
        cache_key,
        {"items": serialized_items, "total": total},
        tags=[build_tag_key("object_type_attribute")],
        ttl=CACHE_TTL_LIST,
    )
    if lang:
        return serialized_items, total
    return items, total


async def list_object_type_attributes_multi(
    db: AsyncSession,
    root_ids: list[uuid.UUID],
    user_id: str | None = None,
    is_superuser: bool = False,
) -> list[dict]:
    """Cache-free multi-root sibling of ``list_object_type_attributes`` (D-05/D-08).

    Resolves the parent-chain inheritance CTE for ALL requested roots in one
    query (never one CTE per root), applies the record-permission visibility
    filter and the AttributeType deny check ONCE over the union of distinct
    ids (D-07), and caps each root independently at 1000 rows — parity with
    the GraphQL resolver's pre-existing single-root ``limit=1000`` call
    (D-05 / RESEARCH.md Open Question 1), via a per-root
    ``ROW_NUMBER() OVER (PARTITION BY root_id ...)`` window in an outer
    layer, replacing the single-root branch's ``LIMIT``/``OFFSET`` (which
    this sibling does not use — no pagination args, no ``total_count``: a
    single global ``COUNT(*) OVER()`` would be wrong per root).

    NEVER calls ``build_list_cache_key``/``get_cached``/``set_cached_with_tags``
    — D-08: the GraphQL batch path deliberately diverges from the REST list
    cache; ``list_object_type_attributes`` above keeps its cache/pagination
    contract byte-identical.

    Returns a flat list of row dicts (same per-item shape as the single-root
    CTE branch above) each additionally carrying a ``"root_id"`` key so
    callers (the grouped DataLoader) can group by requested root without a
    second query.
    """
    if not root_ids:
        return []

    # D-05: base case seeds one row per requested root; the recursive step
    # carries root_id unchanged through the join. DISTINCT ON must lead with
    # root_id (Postgres requires DISTINCT ON columns to be an ORDER BY
    # prefix) so two roots sharing an attribute name never collapse to one
    # row (Pitfall 3) — nearest-wins by ascending depth within each root.
    # Parent-side gate (D-06/Jira semantics): the joined parent must carry
    # inherits_attributes=true for its attributes to propagate to the child.
    cte_sql = """
    WITH RECURSIVE ot_ancestors AS (
        SELECT id AS root_id, id, parent_id, name, inherits_attributes, 0 AS depth
        FROM object_types
        WHERE id = ANY(:root_ids)

        UNION ALL

        SELECT ot_ancestors.root_id, ot.id, ot.parent_id, ot.name, ot.inherits_attributes, ot_ancestors.depth + 1
        FROM object_types ot
        INNER JOIN ot_ancestors ON ot.id = ot_ancestors.parent_id
        WHERE ot.inherits_attributes = true AND ot_ancestors.depth < 100
    )
    SELECT capped.*
    FROM (
        SELECT resolved.*,
            ROW_NUMBER() OVER (
                PARTITION BY resolved.root_id ORDER BY resolved.sort ASC
            ) AS rn
        FROM (
            SELECT DISTINCT ON (ot_ancestors.root_id, ota.name)
                ot_ancestors.root_id AS root_id,
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
                ota.is_system AS is_system,
                ota.default_value AS default_value,
                ota.structure AS structure,
                ota.referenced_object_type_id AS referenced_object_type_id,
                ota.reference_type_id AS reference_type_id,
                ota.multilingual AS multilingual,
                ota.managed_by AS managed_by,
                ota.externally_readonly AS externally_readonly,
                ota.created_at AS created_at,
                ota.updated_at AS updated_at,
                ot_ancestors.id AS defining_object_type_id,
                ot_ancestors.name AS defining_object_type_name,
                (ot_ancestors.depth > 0) AS is_inherited
            FROM object_type_attributes ota
            INNER JOIN ot_ancestors ON ota.object_type_id = ot_ancestors.id
            ORDER BY ot_ancestors.root_id, ota.name, ot_ancestors.depth ASC
        ) resolved
    ) capped
    WHERE capped.rn <= :per_root_limit
    ORDER BY capped.root_id, capped.sort ASC
    """

    result = await db.execute(
        text(cte_sql),
        {"root_ids": list(root_ids), "per_root_limit": 1000},
    )
    rows = result.fetchall()

    # D-02/D-14: per-root requested-type markers for the contextual lock.
    root_lock_result = await db.execute(
        select(ObjectType.id, ObjectType.externally_readonly).where(
            ObjectType.id.in_(root_ids)
        )
    )
    root_readonly_map = {
        row.id: bool(row.externally_readonly) for row in root_lock_result.all()
    }

    # Batch-load reference types for all rows in one query (same pattern as
    # the single-root branch above).
    ref_type_ids = {row.reference_type_id for row in rows if row.reference_type_id}
    ref_type_map = {}
    if ref_type_ids:
        ref_result = await db.execute(
            select(ReferenceType).where(ReferenceType.id.in_(ref_type_ids))
        )
        for rt in ref_result.scalars().all():
            ref_type_map[rt.id] = ReferenceTypeNested(
                id=rt.id,
                reference=rt.reference,
                color=rt.color,
            )

    items = []
    for row in rows:
        rt = ref_type_map.get(row.reference_type_id) if row.reference_type_id else None
        items.append({
            "root_id": row.root_id,
            "id": row.id,
            "object_type_id": row.object_type_id,
            "name": row.name,
            "description": row.description,
            "sort": row.sort,
            "attribute_type_id": row.attribute_type_id,
            "is_required": row.is_required,
            "minimum_cardinality": row.minimum_cardinality,
            "maximum_cardinality": row.maximum_cardinality,
            "is_unique": row.is_unique,
            "is_hidden": row.is_hidden,
            "is_system": row.is_system,
            "default_value": row.default_value,
            "structure": row.structure,
            "referenced_object_type_id": row.referenced_object_type_id,
            "reference_type_id": row.reference_type_id,
            "multilingual": row.multilingual,
            "managed_by": row.managed_by,
            "externally_readonly": row.externally_readonly,
            "is_external": row.managed_by is not None,
            "is_locked": root_readonly_map.get(row.root_id, False)
            or bool(row.externally_readonly),
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "defining_object_type_id": row.defining_object_type_id,
            "defining_object_type_name": row.defining_object_type_name,
            "is_inherited": bool(row.is_inherited),
            "reference_type": rt.model_dump() if rt else None,
        })

    # D-07: visibility + deny filtering ONCE over the union of distinct ids
    # (not per root) — same permissive-default semantics as the single-root
    # branch above.
    if user_id is not None and items:
        filter_clause = await build_record_permission_filter(
            db, user_id, "object_type", is_superuser=is_superuser
        )
        if filter_clause is not None:
            distinct_ids = {
                item["defining_object_type_id"]
                for item in items if item.get("defining_object_type_id")
            }
            visible_result = await db.execute(
                select(ObjectType.id).where(ObjectType.id.in_(distinct_ids)).where(filter_clause)
            )
            visible_ids = {row[0] for row in visible_result.all()}
            items = [item for item in items if item["defining_object_type_id"] in visible_ids]

    deny_ids: set[str] = set()
    if items:
        attr_type_ids = [item["attribute_type_id"] for item in items if item.get("attribute_type_id")]
        if attr_type_ids:
            deny_ids = await _batch_check_has_deny_record_permission(
                db, attr_type_ids, "attribute_type",
            )
    for item in items:
        item["has_deny_record_permission"] = (
            str(item["attribute_type_id"]) in deny_ids if item.get("attribute_type_id") else False
        )

    return items


async def get_object_type_attribute(
    db: AsyncSession, attribute_id: uuid.UUID, lang: str | None = None
) -> ObjectTypeAttribute | dict | None:
    cache_key = build_cache_key(
        "object_type_attribute", f"{attribute_id}:{lang}" if lang else str(attribute_id)
    )
    cached = await get_cached(cache_key)
    if _has_current_lock_shape(cached):
        return cached
    result = await db.execute(
        select(ObjectTypeAttribute).options(selectinload(ObjectTypeAttribute.reference_type)).where(
            ObjectTypeAttribute.id == attribute_id
        )
    )
    attribute = result.scalar_one_or_none()
    if attribute:
        # D-07: attach before model_validate so the dumped payload carries both
        # the generic list and the computed convenience fields.
        await attach_classifications(db, "attribute", [attribute])
        # Load the ObjectType for origin metadata
        ot_result = await db.execute(
            select(ObjectType).where(ObjectType.id == attribute.object_type_id)
        )
        ot = ot_result.scalar_one_or_none()
        response_data = ObjectTypeAttributeResponse.model_validate(attribute).model_dump()
        response_data["defining_object_type_id"] = attribute.object_type_id
        response_data["defining_object_type_name"] = ot.name if ot else ""
        response_data["is_inherited"] = False
        # D-02/D-14: owning type is the effective requested type here.
        response_data["is_locked"] = bool(attribute.externally_readonly) or bool(
            ot.externally_readonly if ot else False
        )
        if lang:
            # D-06 strictness: schema scope known via the already-loaded parent OT (ot.schema_id)
            resolved = await resolve_translations(
                db, "object_type_attribute", [attribute_id], ["name", "description"],
                lang, schema_id=ot.schema_id if ot else None,
            )
            entity_resolution = resolved.get(attribute_id, {})
            if "name" in entity_resolution:
                value, serving = entity_resolution["name"]
                response_data["name"] = value
                response_data["resolved_language"] = serving
            if "description" in entity_resolution:
                value, serving_desc = entity_resolution["description"]
                response_data["description"] = value
                if response_data.get("resolved_language") is None:
                    response_data["resolved_language"] = serving_desc

            # D-01: resolve embedded status_options[].name for the single attribute.
            structure = dict(response_data.get("structure") or {})
            opts = structure.get("status_options")
            if opts:
                status_ids = [opt.get("id") for opt in opts if opt.get("id")]
                if status_ids:
                    status_resolved = await resolve_translations(
                        db, "status", list(set(status_ids)), ["name"], lang, schema_id=None,
                    )
                    new_opts = []
                    for opt in opts:
                        opt_dict = dict(opt)
                        # resolve_translations returns uuid.UUID keys — convert string id.
                        opt_id = opt_dict.get("id")
                        res = status_resolved.get(uuid.UUID(opt_id) if opt_id else None, {})
                        if "name" in res:
                            value, serving = res["name"]
                            opt_dict["name"] = value
                            if response_data.get("resolved_language") is None:
                                response_data["resolved_language"] = serving
                        new_opts.append(opt_dict)
                    structure["status_options"] = new_opts
                    response_data["structure"] = structure
        await set_cached_with_tags(
            cache_key,
            response_data,
            tags=[build_tag_key("object_type_attribute")],
            ttl=CACHE_TTL_ENTITY,
        )
        return response_data
    return attribute


async def update_object_type_attribute(
    db: AsyncSession,
    attribute_id: uuid.UUID,
    attribute_update: ObjectTypeAttributeUpdate,
    *,
    actor_id: uuid.UUID | None = None,
) -> ObjectTypeAttribute | None:
    result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.id == attribute_id
        )
    )
    attribute = result.scalar_one_or_none()
    if not attribute:
        return None

    # Capture the pre-update snapshot before any mutation for the audit diff.
    old_values = model_to_dict(attribute)
    # Capture original object_type_id before potential update to it
    original_object_type_id = attribute.object_type_id

    # 246 D-03: system attributes are non-renamable (explicit is_system flag).
    if (
        attribute.is_system
        and attribute_update.name is not None
        and attribute_update.name != attribute.name
    ):
        raise ValueError("System attributes cannot be renamed.")

    if attribute_update.object_type_id is not None:
        result = await db.execute(
            select(ObjectType).where(
                ObjectType.id == attribute_update.object_type_id
            )
        )
        if not result.scalar_one_or_none():
            return None
        attribute.object_type_id = attribute_update.object_type_id
    if attribute_update.name is not None:
        attribute.name = attribute_update.name
    if attribute_update.description is not None:
        attribute.description = attribute_update.description
    if attribute_update.sort is not None:
        attribute.sort = attribute_update.sort
    if attribute_update.attribute_type_id is not None:
        result = await db.execute(
            select(AttributeType).where(
                AttributeType.id == attribute_update.attribute_type_id
            )
        )
        if not result.scalar_one_or_none():
            return None
        attribute.attribute_type_id = attribute_update.attribute_type_id
    if attribute_update.is_required is not None:
        attribute.is_required = attribute_update.is_required
    if attribute_update.default_value is not None:
        attribute.default_value = attribute_update.default_value
    if attribute_update.structure is not None:
        # D-02: normalize the incoming structure BEFORE assignment — copy the
        # dict, never mutate the caller's object.
        new_structure = attribute_update.structure
        if isinstance(new_structure, dict) and "options" in new_structure:
            new_structure = dict(new_structure)
            new_structure["options"] = ensure_option_ids(new_structure["options"])
        # D-11: diff-based purge — delete select_option translation rows of
        # options removed by this structure rewrite, in the same transaction
        # (before commit, mirroring the 221 D-15 delete placement).
        old_structure = attribute.structure
        old_options = (
            old_structure.get("options") or []
            if isinstance(old_structure, dict) else []
        )
        new_options = (
            new_structure.get("options") or []
            if isinstance(new_structure, dict) else []
        )
        removed = removed_option_ids(old_options, new_options)
        if removed:
            await delete_translations_for(db, "select_option", [uuid.UUID(i) for i in removed])
        attribute.structure = new_structure
    if attribute_update.referenced_object_type_id is not None:
        attribute.referenced_object_type_id = (
            attribute_update.referenced_object_type_id
        )
    if attribute_update.minimum_cardinality is not None:
        attribute.minimum_cardinality = attribute_update.minimum_cardinality
    if attribute_update.maximum_cardinality is not None:
        attribute.maximum_cardinality = attribute_update.maximum_cardinality
    if attribute_update.is_unique is not None:
        attribute.is_unique = attribute_update.is_unique
    if attribute_update.is_hidden is not None:
        attribute.is_hidden = attribute_update.is_hidden
    if attribute_update.reference_type_id is not None:
        attribute.reference_type_id = attribute_update.reference_type_id
    if attribute_update.multilingual is True:
        # D-08 / MULTI-02: checked against the attribute's CURRENT parent —
        # unless object_type_id is being changed, in which case the NEW parent
        # (already assigned above) is the effective one.
        effective_parent_id = attribute.object_type_id
        parent_result = await db.execute(
            select(ObjectType).where(ObjectType.id == effective_parent_id)
        )
        parent = parent_result.scalar_one_or_none()
        if not parent or not parent.multilingual:
            raise ValueError("Object type is not multilingual: cannot set multilingual=True on its attribute")
    if "multilingual" in attribute_update.model_fields_set and attribute_update.multilingual is not None:
        attribute.multilingual = attribute_update.multilingual

    # EXT-09-D1 / D-12: object_refs are keyed on the attribute *name*, so a
    # rename must move them in the same transaction or every existing reference
    # silently detaches. Only a reference-typed attribute has refs, and only
    # the subtree that actually resolves to it (inherited refs included,
    # shadowed subtrees excluded). A move to another type keeps the refs tied
    # to the type subtree they were created under — the original one.
    refs_renamed = 0
    old_name = old_values.get("name")
    if attribute_update.name is not None and attribute_update.name != old_name:
        if (
            await _attribute_data_type(db, attribute.attribute_type_id)
        ) == AttributeTypeDataType.REFERENCE:
            target_type_ids = await _ref_maintenance_type_ids(
                db, original_object_type_id, attribute_id, old_name
            )
            if target_type_ids:
                refs_renamed = (
                    await db.execute(
                        update(ObjectRef)
                        .where(
                            ObjectRef.object_type_id.in_(target_type_ids),
                            ObjectRef.from_attribute_name == old_name,
                        )
                        .values(from_attribute_name=attribute.name)
                    )
                ).rowcount

    # D-07: a classification field in the request replaces exactly those
    # families; an omitted field leaves its family untouched (A4). Any label
    # error raises before the commit and the route answers 400.
    classification_changed = bool(
        CLASSIFICATION_INPUT_FIELDS & set(attribute_update.model_fields_set)
    )
    if classification_changed:
        old_label_refs, new_label_refs = await set_labels(
            db,
            "attribute",
            attribute_id,
            **classification_kwargs(attribute_update, partial=True),
        )
    else:
        current_label_refs = (
            await load_classifications(db, "attribute", [attribute_id])
        )[attribute_id]
        old_label_refs = new_label_refs = current_label_refs
    old_values["classification_labels"] = classification_audit_keys(old_label_refs)
    new_values = model_to_dict(attribute)
    new_values["classification_labels"] = classification_audit_keys(new_label_refs)

    # Audit the change in the same transaction, before the single commit.
    if actor_id is not None:
        await audit_log(
            db,
            _AUDIT_ENTITY_TYPE,
            original_object_type_id,
            "UPDATE",
            actor_id,
            old_values=old_values,
            new_values=new_values,
        )

    await db.commit()
    # D-16: a rename/type change is a definition change — refresh the mapping of
    # the type the attribute belonged to and of the type it moved to.
    await refresh_index_mapping(original_object_type_id)
    if (
        attribute_update.object_type_id is not None
        and attribute_update.object_type_id != original_object_type_id
    ):
        await refresh_index_mapping(attribute_update.object_type_id)
    await invalidate_entity_cache("object_type_attribute")
    if refs_renamed:
        await invalidate_entity_cache("object_ref")
    if "multilingual" in attribute_update.model_fields_set and attribute_update.multilingual is not None:
        await invalidate_entity_cache("object_type")
    await _invalidate_descendant_resolved_caches(db, original_object_type_id)
    affected_type_ids = await collect_descendant_type_ids(db, original_object_type_id)
    await purge_public_objecttype_schema_cache(affected_type_ids)
    moved_type_ids: list[str] = []
    if attribute_update.object_type_id is not None and attribute_update.object_type_id != original_object_type_id:
        await _invalidate_descendant_resolved_caches(db, attribute_update.object_type_id)
        moved_type_ids = await collect_descendant_type_ids(db, attribute_update.object_type_id)
        await purge_public_objecttype_schema_cache(moved_type_ids)
    if classification_changed:
        # The per-client definition payloads carry classificationLabels and are
        # keyed per client, so the shared tier purge above never reaches them.
        await purge_public_definition_caches(
            affected_type_ids + moved_type_ids,
            await _schema_ids_of_object_types(db, affected_type_ids + moved_type_ids),
        )
    result = await db.execute(
        select(ObjectTypeAttribute).options(selectinload(ObjectTypeAttribute.reference_type))
        .where(ObjectTypeAttribute.id == attribute_id)
    )
    updated_attribute = result.scalar_one()
    await attach_classifications(db, "attribute", [updated_attribute])
    return updated_attribute


async def delete_object_type_attribute(
    db: AsyncSession,
    attribute_id: uuid.UUID,
    *,
    actor_id: uuid.UUID | None = None,
) -> bool:
    result = await db.execute(
        select(ObjectTypeAttribute).where(
            ObjectTypeAttribute.id == attribute_id
        )
    )
    attribute = result.scalar_one_or_none()
    if not attribute:
        return False
    # 246 D-03: system attributes are non-deletable (explicit is_system flag).
    if attribute.is_system:
        raise ValueError("System attributes cannot be deleted.")
    attribute_object_type_id = attribute.object_type_id
    old_values = model_to_dict(attribute)
    # D-15 / MULTI-07: purge the polymorphic translation rows in the SAME
    # transaction as the entity delete — no orphans.
    await delete_translations_for(db, "object_type_attribute", [attribute_id])
    # WR-01: the attribute's own SELECT option ids also own select_option
    # translation rows — purge those too (entity_id is not a real FK, so
    # nothing at the DB level cleans them up).
    option_ids = option_ids_from_structure(attribute.structure)
    if option_ids:
        await delete_translations_for(db, "select_option", option_ids)
    # EXT-09-D1 / D-12: a deleted reference attribute takes its refs with it —
    # across the subtree that resolved to it, with a shadowing descendant's own
    # same-named refs untouched. Maintenance deletion (no per-row hook), in the
    # same transaction as the attribute delete.
    refs_deleted = 0
    if (
        await _attribute_data_type(db, attribute.attribute_type_id)
    ) == AttributeTypeDataType.REFERENCE:
        target_type_ids = await _ref_maintenance_type_ids(
            db, attribute_object_type_id, attribute_id, attribute.name
        )
        if target_type_ids:
            refs_deleted = (
                await db.execute(
                    delete(ObjectRef).where(
                        ObjectRef.object_type_id.in_(target_type_ids),
                        ObjectRef.from_attribute_name == attribute.name,
                    )
                )
            ).rowcount
    # D-03: audit the delete before the row is removed, same transaction.
    if actor_id is not None:
        await audit_log(
            db,
            _AUDIT_ENTITY_TYPE,
            attribute_object_type_id,
            "DELETE",
            actor_id,
            old_values=old_values,
            new_values=None,
        )
    await db.delete(attribute)
    await db.commit()
    # D-16: the attribute is gone from the definition — refresh the type's
    # mapping through the one put_mapping seam.
    await refresh_index_mapping(attribute_object_type_id)
    await invalidate_entity_cache("object_type_attribute")
    if refs_deleted:
        await invalidate_entity_cache("object_ref")
    await _invalidate_descendant_resolved_caches(db, attribute_object_type_id)
    await purge_public_objecttype_schema_cache(
        await collect_descendant_type_ids(db, attribute_object_type_id)
    )
    return True
