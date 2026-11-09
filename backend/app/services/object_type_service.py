import uuid
from typing import List, Optional

from sqlalchemy import select, func, text, update
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ObjectType, Schema, ObjectTypeAttribute, AttributeTypeDataType, Object
from app.schemas.crud import ObjectTypeCreate, ObjectTypeUpdate, ObjectTypeResponse
from app.services.attribute_type_service import get_or_create_canonical_by_data_type
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.services.object_type_attribute_service import (
    collect_descendant_type_ids,
    _invalidate_descendant_resolved_caches,
)
from app.services.record_permission_service import build_record_permission_filter
from app.services.search_service import refresh_index_mapping
from app.services.translation_service import resolve_translations, delete_dependent_translations, check_schema_multilingual
from app.db.redis import get_redis, get_arq_pool
from app.services.audit_service import audit_log, model_to_dict
from app.services.value_lock_service import assert_types_deletable
from app.public_api.objecttype_schema_cache import purge_public_objecttype_schema_cache


class ObjectTypeNameConflict(ValueError):
    """A duplicate ObjectType name within one schema.

    Domain error (D-01/D-03): the REST ObjectType routes translate it back to
    the original HTTP 409 with the same detail, while the SDK adapter maps it to
    ``CoreDataConflict`` (``conflict``) — no ``HTTPException`` crosses the SDK
    write boundary.
    """


# The four auto-provisioned system attributes (246 D-01/D-03): Name + Key are
# TEXT and visible; Created + Updated are DATETIME and hidden (system-filled,
# D-03). Order defines the sort values 9995-9998. Mirrors the migration
# backfill spec in alembic/versions/246_object_access_model.py exactly.
_SYSTEM_ATTRIBUTES = [
    ("Name", AttributeTypeDataType.TEXT.value, False, 9995),
    ("Key", AttributeTypeDataType.TEXT.value, False, 9996),
    ("Created", AttributeTypeDataType.DATETIME.value, True, 9997),
    ("Updated", AttributeTypeDataType.DATETIME.value, True, 9998),
]


async def _provision_system_attributes(
    db: AsyncSession, object_type_id: uuid.UUID, actor_id: uuid.UUID | None = None
) -> None:
    """246 D-01/D-03: auto-provision the four system ObjectTypeAttributes
    (Name, Key, Created, Updated) for a native ObjectType.

    Uses the 245 D-11 get_or_create_canonical_by_data_type seam for the
    attribute_types rows — NEVER mints new catalog rows. Skipping any system
    OTA whose name already exists on that ObjectType (collision guard). Called
    after db.flush() on the parent ObjectType so object_type_id is populated,
    before db.commit().
    """
    for name, data_type, is_hidden, sort in _SYSTEM_ATTRIBUTES:
        existing = await db.execute(
            select(ObjectTypeAttribute).where(
                ObjectTypeAttribute.object_type_id == object_type_id,
                ObjectTypeAttribute.name == name,
            )
        )
        if existing.scalar_one_or_none() is not None:
            continue
        # 250-Korrektur: wenn ein vererbender Vorfahre (parent-side
        # inherits_attributes) das System-Attribut bereits bereitstellt, kein
        # dupliziertes eigenes OTA anlegen — das Kind referenziert über die
        # CTE-Vererbung auf das Parent-Attribut.
        if await ancestor_provides_attribute(db, name, object_type_id):
            continue
        canonical = await get_or_create_canonical_by_data_type(
            db, data_type, actor_id=actor_id
        )
        db.add(ObjectTypeAttribute(
            object_type_id=object_type_id,
            name=name,
            attribute_type_id=canonical.id,
            is_system=True,
            is_hidden=is_hidden,
            minimum_cardinality=1,
            maximum_cardinality=1,
            sort=sort,
        ))


async def create_object_type(
    db: AsyncSession,
    object_type_data: ObjectTypeCreate,
    actor_id: uuid.UUID,
    *,
    id: uuid.UUID | None = None,
) -> ObjectType | None:
    result = await db.execute(
        select(Schema).where(Schema.id == object_type_data.schema_id)
    )
    schema = result.scalar_one_or_none()
    if not schema:
        return None

    if object_type_data.parent_id:
        result = await db.execute(
            select(ObjectType).where(ObjectType.id == object_type_data.parent_id)
        )
        parent = result.scalar_one_or_none()
        if not parent:
            return None

    # ObjectType names are unique within a schema — reject duplicates before
    # any insert (no DB-level unique constraint covers the (schema_id, name)
    # pair today).
    existing = await db.execute(
        select(ObjectType.id).where(
            ObjectType.schema_id == object_type_data.schema_id,
            func.lower(ObjectType.name) == object_type_data.name.strip().lower(),
        )
    )
    if existing.scalar_one_or_none():
        raise ObjectTypeNameConflict(
            f"An ObjectType named '{object_type_data.name}' already exists in this schema."
        )

    if object_type_data.multilingual:
        await check_schema_multilingual(db, object_type_data.schema_id)

    db_object_type = ObjectType(
        id=id or uuid.uuid4(),
        schema_id=object_type_data.schema_id,
        parent_id=object_type_data.parent_id,
        name=object_type_data.name,
        description=object_type_data.description,
        sort=object_type_data.sort,
        label_attribute_id=object_type_data.label_attribute_id,
        icon_id=object_type_data.icon_id,
        multilingual=object_type_data.multilingual,
        inherits_attributes=object_type_data.inherits_attributes,
        is_abstract=object_type_data.is_abstract,
    )
    db.add(db_object_type)
    await db.flush()
    # 246 D-01 / DECOUPLE-04: the REST create route auto-provisions the four
    # system attributes (Name, Key, Created, Updated) for every native
    # ObjectType.
    await _provision_system_attributes(db, db_object_type.id, actor_id)
    new_values = model_to_dict(db_object_type)
    await audit_log(db, "object_types", db_object_type.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_object_type)
    # D-16: the definition change is committed — this is where the index
    # mapping moves, never on a document write.
    await refresh_index_mapping(db_object_type.id)
    await invalidate_entity_cache("object_type")
    await purge_public_objecttype_schema_cache(
        await collect_descendant_type_ids(db, db_object_type.id)
    )
    # D-20: Sync to Elasticsearch (best-effort, non-blocking)
    try:
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "index_object_type_job",
            str(db_object_type.id),
            # Unique per call, not just per object_type_id — a purely
            # entity-scoped _job_id gets silently deduped by ARQ against its
            # own completed-job result cache (keep_result=86400) for any
            # later edit within 24h of the first, not just genuinely
            # in-flight jobs. See object_service._enqueue_index_job.
            _job_id=f"index_object_type_{db_object_type.id}_{uuid.uuid4().hex}",
        )
    except Exception:
        pass  # Best-effort — ES indexing failures don't block the API response
    result = await db.execute(
        select(ObjectType).options(selectinload(ObjectType.icon))
        .where(ObjectType.id == db_object_type.id)
    )
    db_object_type = result.scalar_one()
    return db_object_type


async def list_object_types(
    db: AsyncSession, skip: int = 0, limit: int = 100, schema_id: uuid.UUID | None = None,
    user_id: str | None = None, is_superuser: bool = False,
    name: str | None = None, lang: str | None = None,
) -> tuple[List[ObjectType], int]:
    cache_key = build_list_cache_key(
        "object_type", skip=skip, limit=limit, schema_id=str(schema_id) if schema_id else None,
        user_id=user_id, name=name, lang=lang,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    query = select(ObjectType).options(selectinload(ObjectType.icon)).order_by(ObjectType.sort, ObjectType.name)
    count_query = select(func.count()).select_from(ObjectType)
    if schema_id is not None:
        query = query.where(ObjectType.schema_id == schema_id)
        count_query = count_query.where(ObjectType.schema_id == schema_id)
    if name:
        pattern = f"%{name}%"
        query = query.where(ObjectType.name.ilike(pattern))
        count_query = count_query.where(ObjectType.name.ilike(pattern))
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db, user_id, "object_type", is_superuser=is_superuser
        )
        if filter_clause is not None:
            query = query.where(filter_clause)
            count_query = count_query.where(filter_clause)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    serialized_items = [ObjectTypeResponse.model_validate(item).model_dump() for item in items]
    if lang:
        # MULTI-05/D-03: resolve translated name/description, overwriting in place.
        # D-06 strictness applies when the schema scope is known (schema-filtered
        # list); unfiltered lists resolve per-entity with the D-03 fallback.
        resolved = await resolve_translations(
            db, "object_type",
            [item.id for item in items],
            ["name", "description"],
            lang,
            schema_id=schema_id,
        )
        for item in serialized_items:
            entity_resolution = resolved.get(item["id"], {})
            serving: str | None = None
            if "name" in entity_resolution:
                value, serving = entity_resolution["name"]
                item["name"] = value
            if "description" in entity_resolution:
                value, serving_desc = entity_resolution["description"]
                item["description"] = value
                if serving_desc != serving:
                    serving = serving if serving is not None else serving_desc
            if serving is not None:
                item["resolved_language"] = serving
    await set_cached_with_tags(
        cache_key,
        {"items": serialized_items, "total": total},
        tags=[build_tag_key("object_type")],
        ttl=CACHE_TTL_LIST,
    )
    if lang:
        return serialized_items, total
    return items, total


async def get_object_type(
    db: AsyncSession, object_type_id: uuid.UUID, lang: str | None = None
) -> ObjectType | dict | None:
    cache_key = build_cache_key(
        "object_type", f"{object_type_id}:{lang}" if lang else str(object_type_id)
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(ObjectType).options(selectinload(ObjectType.icon))
        .where(ObjectType.id == object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if object_type:
        serialized = ObjectTypeResponse.model_validate(object_type).model_dump()
        if lang:
            # MULTI-05/D-03: resolve translated name/description, overwriting in
            # place; D-06 strict check via the entity's own schema scope.
            resolved = await resolve_translations(
                db, "object_type", [object_type.id], ["name", "description"],
                lang, schema_id=object_type.schema_id,
            )
            entity_resolution = resolved.get(object_type.id, {})
            if "name" in entity_resolution:
                value, serving = entity_resolution["name"]
                serialized["name"] = value
                serialized["resolved_language"] = serving
            if "description" in entity_resolution:
                value, serving_desc = entity_resolution["description"]
                serialized["description"] = value
                if serialized.get("resolved_language") is None:
                    serialized["resolved_language"] = serving_desc
        await set_cached_with_tags(
            cache_key, serialized,
            tags=[build_tag_key("object_type")], ttl=CACHE_TTL_ENTITY,
        )
    if lang:
        return serialized if object_type else None
    return object_type


async def update_object_type(
    db: AsyncSession,
    object_type_id: uuid.UUID,
    object_type_update: ObjectTypeUpdate,
    actor_id: uuid.UUID,
) -> ObjectType | None:
    result = await db.execute(
        select(ObjectType).where(ObjectType.id == object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        return None
    old_values = model_to_dict(object_type)
    # D-04: collect the pre-update descendant set BEFORE the ORM mutation —
    # re-parenting changes the ancestor chain, and the purge must cover the
    # type + all descendants whose inherited attribute shape can change.
    descendant_type_ids = await collect_descendant_type_ids(db, object_type.id)

    schema_moved = False
    if object_type_update.schema_id is not None:
        result = await db.execute(
            select(Schema).where(Schema.id == object_type_update.schema_id)
        )
        schema = result.scalar_one_or_none()
        if not schema:
            return None
        if object_type.schema_id != object_type_update.schema_id:
            object_count_result = await db.execute(
                select(func.count()).select_from(Object).where(
                    Object.object_type_id == object_type.id
                )
            )
            if object_count_result.scalar() > 0:
                raise ValueError(
                    "Cannot move a populated ObjectType to a different Schema — "
                    "remove or reassign its Objects first"
                )
            object_type.schema_id = object_type_update.schema_id
            schema_moved = True
    if "parent_id" in object_type_update.model_fields_set:
        if object_type_update.parent_id is not None:
            result = await db.execute(
                select(ObjectType).where(
                    ObjectType.id == object_type_update.parent_id
                )
            )
            parent = result.scalar_one_or_none()
            if not parent:
                return None
            cycle_check = text("""
                WITH RECURSIVE ancestors AS (
                    SELECT id, parent_id, 0 AS depth FROM object_types WHERE id = :target_id
                    UNION ALL
                    SELECT ot.id, ot.parent_id, a.depth + 1
                    FROM object_types ot INNER JOIN ancestors a ON ot.id = a.parent_id
                    WHERE a.depth < 100
                )
                SELECT 1 FROM ancestors WHERE id = :subject_id
            """)
            result = await db.execute(cycle_check, {"target_id": object_type_update.parent_id, "subject_id": object_type.id})
            if result.scalar_one_or_none() is not None:
                raise ValueError("Reparenting rejected: would create a cycle")
        if object_type.parent_id != object_type_update.parent_id:
            object_type.parent_id = object_type_update.parent_id
    if object_type_update.name is not None and object_type.name != object_type_update.name:
        dup_result = await db.execute(
            select(ObjectType.id).where(
                ObjectType.schema_id == object_type.schema_id,
                ObjectType.id != object_type.id,
                func.lower(ObjectType.name) == object_type_update.name.strip().lower(),
            )
        )
        if dup_result.scalar_one_or_none():
            raise ObjectTypeNameConflict(
                f"An ObjectType named '{object_type_update.name}' already exists in this schema."
            )
        object_type.name = object_type_update.name
    if object_type_update.description is not None and object_type.description != object_type_update.description:
        object_type.description = object_type_update.description
    if object_type_update.sort is not None and object_type.sort != object_type_update.sort:
        object_type.sort = object_type_update.sort
    if "icon_id" in object_type_update.model_fields_set and object_type.icon_id != object_type_update.icon_id:
        object_type.icon_id = object_type_update.icon_id
    if "label_attribute_id" in object_type_update.model_fields_set and object_type.label_attribute_id != object_type_update.label_attribute_id:
        object_type.label_attribute_id = object_type_update.label_attribute_id
    if "inherits_attributes" in object_type_update.model_fields_set and object_type.inherits_attributes != object_type_update.inherits_attributes:
        object_type.inherits_attributes = object_type_update.inherits_attributes
    if "is_abstract" in object_type_update.model_fields_set and object_type.is_abstract != object_type_update.is_abstract:
        if object_type_update.is_abstract is True and not object_type.is_abstract:
            # D-02: only the local false->true toggle is guarded — a populated
            # ObjectType cannot be made abstract through this native path.
            object_count_result = await db.execute(
                select(func.count()).select_from(Object).where(
                    Object.object_type_id == object_type.id
                )
            )
            if object_count_result.scalar() > 0:
                raise ValueError(
                    "Cannot make a populated ObjectType abstract — "
                    "remove or reassign its Objects first"
                )
        object_type.is_abstract = object_type_update.is_abstract
    if "multilingual" in object_type_update.model_fields_set and object_type.multilingual != object_type_update.multilingual:
        if object_type_update.multilingual is True:
            await check_schema_multilingual(db, object_type.schema_id)
        object_type.multilingual = object_type_update.multilingual
        if object_type_update.multilingual is False:
            # D-10 / MULTI-03: turning the flag OFF cascades to all child attributes'
            # flags in the same transaction. Translations become dormant (not served,
            # not editable) but are NOT deleted — re-enabling restores them.
            await db.execute(
                update(ObjectTypeAttribute)
                .where(ObjectTypeAttribute.object_type_id == object_type_id)
                .values(multilingual=False)
            )

    if schema_moved:
        # WR-01: narrow (not close) the TOCTOU window between the populated-move
        # guard above and this commit — a concurrent create_object/update_object
        # targeting this ObjectType could have committed an Object row in the
        # meantime. Re-check immediately before commit so the move is rejected
        # rather than silently producing a populated ObjectType in the wrong
        # Schema.
        object_count_result = await db.execute(
            select(func.count()).select_from(Object).where(
                Object.object_type_id == object_type.id
            )
        )
        if object_count_result.scalar() > 0:
            await db.rollback()
            raise ValueError(
                "Cannot move a populated ObjectType to a different Schema — "
                "remove or reassign its Objects first"
            )

    new_values = model_to_dict(object_type)
    await audit_log(db, "object_types", object_type_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(object_type)
    # D-16: schema change -> mapping refresh (single put_mapping seam).
    await refresh_index_mapping(object_type_id)
    await invalidate_entity_cache("object_type")
    if "multilingual" in object_type_update.model_fields_set and object_type_update.multilingual is not None:
        # Flag changes affect attribute-name resolution (D-07/D-10) — clear both tag families.
        await invalidate_entity_cache("object_type_attribute")
    if "parent_id" in object_type_update.model_fields_set:
        await _invalidate_descendant_resolved_caches(db, object_type.id)
    await purge_public_objecttype_schema_cache(descendant_type_ids)
    # D-20: Sync to Elasticsearch
    try:
        arq_pool = await get_arq_pool()
        await arq_pool.enqueue_job(
            "index_object_type_job",
            str(object_type_id),
            # See create_object_type above — unique per call to avoid ARQ's
            # 24h completed-job dedup silently dropping later edits.
            _job_id=f"index_object_type_{object_type_id}_{uuid.uuid4().hex}",
        )
    except Exception:
        pass
    cache_key = build_cache_key("object_type", str(object_type_id))
    redis = await get_redis()
    await redis.delete(cache_key)
    result = await db.execute(
        select(ObjectType).options(selectinload(ObjectType.icon))
        .where(ObjectType.id == object_type_id)
    )
    object_type = result.scalar_one()
    return object_type


async def resolve_ancestor_ota_by_name(
    db: AsyncSession, name: str, object_type_id: uuid.UUID
) -> Optional[uuid.UUID]:
    """Return the ObjectTypeAttribute id of the nearest vererbender Vorfahre
    (parent-side inherits_attributes gate) that defines an attribute with `name`.

    Parent-side Semantik (250-Korrektur, Jira): ein Parent propagiert seine
    Attribute an Kinder, wenn DER PARENT inherits_attributes=true hat. Der Walk
    nach oben stoppt an einem Vorfahren ohne Flag. Wird genutzt, um bei der
    Anlage eines Kind-Objekttyps kein dupliziertes eigenes System-OTA
    (Created/Updated) anzulegen und Timestamps stattdessen in das geerbte
    Parent-Attribut zu schreiben — das Kind referenziert auf das Parent-OTA.
    """
    query = text("""
        WITH RECURSIVE ot_ancestors AS (
            SELECT id, parent_id, inherits_attributes, 0 AS depth
            FROM object_types
            WHERE id = :object_type_id
            UNION ALL
            SELECT ot.id, ot.parent_id, ot.inherits_attributes, ot_ancestors.depth + 1
            FROM object_types ot
            INNER JOIN ot_ancestors ON ot.id = ot_ancestors.parent_id
            WHERE ot.inherits_attributes = true AND ot_ancestors.depth < 100
        )
        SELECT ota.id
        FROM object_type_attributes ota
        INNER JOIN ot_ancestors a ON ota.object_type_id = a.id
        WHERE ota.name = :name AND a.depth > 0
        ORDER BY a.depth ASC
        LIMIT 1
    """)
    result = await db.execute(
        query, {"object_type_id": str(object_type_id), "name": name}
    )
    return result.scalar_one_or_none()


async def ancestor_provides_attribute(
    db: AsyncSession, name: str, object_type_id: uuid.UUID
) -> bool:
    """True when a vererbender Vorfahre (parent-side inherits_attributes gate)
    of `object_type_id` bereits ein ObjectTypeAttribute mit `name` bereitstellt.

    Wrapper around resolve_ancestor_ota_by_name — genutzt, um duplizierte
    eigene System-OTAs (Created/Updated) bei Anlage eines Kind-Objekttyps zu
    vermeiden.
    """
    return await resolve_ancestor_ota_by_name(db, name, object_type_id) is not None


async def get_descendant_type_ids(db: AsyncSession, type_id: uuid.UUID) -> List[uuid.UUID]:
    """Return all descendant object_type_ids of `type_id` (children, grandchildren, ...).

    Cheap EXISTS pre-check avoids the recursive CTE cost for leaf types (D-03):
    most ObjectTypes in a typical hierarchy are leaves, and this is called on the
    hot /objects/ list endpoint, so skipping the CTE for them matters.

    Cycle-safe recursive CTE mirroring is_ancestor (depth < 100).
    Returns [] when the type has no descendants.
    """
    has_children = await db.execute(
        text("SELECT 1 FROM object_types WHERE parent_id = :type_id LIMIT 1"),
        {"type_id": str(type_id)},
    )
    if has_children.scalar_one_or_none() is None:
        return []

    sql = text("""
        WITH RECURSIVE descendants AS (
            SELECT id, parent_id, 0 AS depth FROM object_types WHERE id = :type_id
            UNION ALL
            SELECT ot.id, ot.parent_id, d.depth + 1
            FROM object_types ot
            INNER JOIN descendants d ON ot.parent_id = d.id
            WHERE d.depth < 100
        )
        SELECT id FROM descendants WHERE id != :type_id
    """)
    result = await db.execute(sql, {"type_id": str(type_id)})
    return [row[0] for row in result.all()]


async def delete_object_type(
    db: AsyncSession, object_type_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(ObjectType).where(ObjectType.id == object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        return False
    old_values = model_to_dict(object_type)
    # D-04: collect the descendant set BEFORE the delete commits (the CTE
    # reads the parent chain — the type is gone afterwards).
    descendant_type_ids = await collect_descendant_type_ids(db, object_type.id)
    # 290 D-15: the type itself and every type in its cascade set must be
    # deletable — a locked descendant protects the whole subtree. Fresh
    # persisted markers under FOR SHARE, before any translation purge/audit.
    await assert_types_deletable(db, descendant_type_ids)
    descendant_schema_ids = {
        str(row[0]): row[1]
        for row in (
            await db.execute(
                select(ObjectType.id, ObjectType.schema_id).where(
                    ObjectType.id.in_([uuid.UUID(tid) for tid in descendant_type_ids])
                )
            )
        ).all()
    }
    # MULTI-07 / D-15: purge the translations of everything the cascade removes
    # (no DB-level backstop exists for the polymorphic entity_id).
    await delete_dependent_translations(
        db, object_type_ids=[uuid.UUID(tid) for tid in descendant_type_ids]
    )
    await audit_log(db, "object_types", object_type_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    # Break the object_types.label_attribute_id <-> object_type_attributes.object_type_id
    # FK cycle before deleting — when an ObjectType's label points at one of its
    # own attributes, SQLAlchemy's unit-of-work can't linearize the cascade
    # delete of both sides in one flush and raises CircularDependencyError.
    if object_type.label_attribute_id is not None:
        object_type.label_attribute_id = None
        await db.flush()
    await db.delete(object_type)
    await db.commit()
    # D-16: every schema-changing path goes through the one refresh seam. A
    # deleted type has no definition left to resolve, so this resolves no row
    # and writes nothing — an Elasticsearch mapping cannot drop a field without
    # a reindex, and the per-type field names never collide with a survivor.
    await refresh_index_mapping(object_type_id)
    # D-20: Remove from Elasticsearch (best-effort) — descendant_type_ids already
    # includes object_type_id itself (see collect_descendant_type_ids docstring),
    # plus every descendant type cascade-deleted alongside it in Postgres.
    try:
        arq_pool = await get_arq_pool()
        for type_id in descendant_type_ids:
            await arq_pool.enqueue_job(
                "remove_object_type_job",
                str(type_id),
                str(descendant_schema_ids.get(type_id, object_type.schema_id)),
                # Unique per call — see create_object_type above.
                _job_id=f"remove_object_type_{type_id}_{uuid.uuid4().hex}",
            )
    except Exception:
        pass
    # D-15 cascade: the DB-level cascade above removed the type's Object rows
    # and their ObjectValue/ObjectRef rows too, so the full entity family must
    # be invalidated — otherwise a 300s entity-cache entry keeps serving a
    # deleted object/value/ref after DELETE /object-types/{id}.
    await invalidate_entity_cache("object_type")
    await invalidate_entity_cache("object_type_attribute")
    await invalidate_entity_cache("object")
    await invalidate_entity_cache("object_value")
    await invalidate_entity_cache("object_ref")
    await purge_public_objecttype_schema_cache(descendant_type_ids)
    return True
