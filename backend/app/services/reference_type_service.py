import uuid
from typing import List

from sqlalchemy import select, func, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ObjectTypeAttribute, ReferenceType
from app.schemas.crud import ReferenceTypeCreate, ReferenceTypeUpdate, ReferenceTypeResponse
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
from app.services.audit_service import audit_log, model_to_dict
from app.services.translation_service import resolve_translations, delete_translations_for
from app.services.object_type_attribute_service import collect_descendant_type_ids
from app.public_api.objecttype_schema_cache import purge_public_objecttype_schema_cache

ENTITY_TYPE = "reference_type"


async def _purge_affected_type_schemas(db: AsyncSession, reference_type_id: uuid.UUID) -> None:
    """Purge the type-schema tier for every object type whose attributes
    reference this reference type (+ their descendants — inherited attribute
    definitions, D-05). Best-effort: the purge helper is fail-open by
    construction (D-03/D-04)."""
    result = await db.execute(
        select(ObjectTypeAttribute.object_type_id)
        .where(ObjectTypeAttribute.reference_type_id == reference_type_id)
        .distinct()
    )
    ids_to_purge: list[str] = []
    for row in result.all():
        ids_to_purge.extend(await collect_descendant_type_ids(db, row[0]))
    if ids_to_purge:
        await purge_public_objecttype_schema_cache(list(dict.fromkeys(ids_to_purge)))


async def create_reference_type(
    db: AsyncSession,
    reference_type_data: ReferenceTypeCreate,
    actor_id: uuid.UUID,
    *,
    id: uuid.UUID | None = None,
) -> ReferenceType:
    db_reference_type = ReferenceType(
        id=id or uuid.uuid4(),
        reference=reference_type_data.reference,
        description=reference_type_data.description,
        color=reference_type_data.color,
        source_id=reference_type_data.source_id,
        schema_id=reference_type_data.schema_id,
    )
    db.add(db_reference_type)
    await db.flush()
    new_values = model_to_dict(db_reference_type)
    await audit_log(db, "reference_types", db_reference_type.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_reference_type)
    await invalidate_entity_cache(ENTITY_TYPE)
    await _purge_affected_type_schemas(db, db_reference_type.id)
    return db_reference_type


async def list_reference_types(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    schema_id: uuid.UUID | None = None,
    lang: str | None = None,
) -> tuple[List[ReferenceType], int]:
    cache_key = build_list_cache_key(
        ENTITY_TYPE, skip=skip, limit=limit,
        schema_id=str(schema_id) if schema_id else None,
        lang=lang,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    base = select(ReferenceType)
    count_base = select(func.count()).select_from(ReferenceType)
    if schema_id is not None:
        cond = or_(ReferenceType.schema_id == schema_id, ReferenceType.schema_id.is_(None))
        base = base.where(cond)
        count_base = count_base.where(cond)
    count_result = await db.execute(count_base)
    total = count_result.scalar()
    result = await db.execute(
        base.offset(skip).limit(limit)
    )
    items = result.scalars().all()
    serialized_items = [ReferenceTypeResponse.model_validate(item).model_dump() for item in items]
    if lang:
        # LOOKUP-02: resolve translated reference/description, overwriting in place.
        # D-06 strictness applies when the schema scope is known (schema-filtered
        # list); unfiltered lists resolve per-entity with the D-03 fallback.
        # Schema-less rows (schema_id NULL) automatically yield no entries from
        # resolve_translations (D-03) — their base values stay, no resolved_language.
        resolved = await resolve_translations(
            db, ENTITY_TYPE,
            [item.id for item in items],
            ["reference", "description"],
            lang,
            schema_id=schema_id,
        )
        for item in serialized_items:
            entity_resolution = resolved.get(item["id"], {})
            serving: str | None = None
            if "reference" in entity_resolution:
                value, serving = entity_resolution["reference"]
                item["reference"] = value
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
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    if lang:
        return serialized_items, total
    return items, total


async def get_reference_type(
    db: AsyncSession, reference_type_id: uuid.UUID,
    lang: str | None = None,
) -> ReferenceType | dict | None:
    cache_key = build_cache_key(
        ENTITY_TYPE, f"{reference_type_id}:{lang}" if lang else str(reference_type_id)
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(ReferenceType).where(ReferenceType.id == reference_type_id)
    )
    reference_type = result.scalar_one_or_none()
    if reference_type:
        serialized = ReferenceTypeResponse.model_validate(reference_type).model_dump()
        if lang:
            # LOOKUP-02: resolve translated reference/description, overwriting in
            # place; D-06 strict check via the entity's own schema scope.
            # For schema-less entities (schema_id=None) D-03 skip applies:
            # resolve_translations returns {} for them, but the language validation
            # still fires (D-09 — unknown language still 400s).
            resolved = await resolve_translations(
                db, ENTITY_TYPE, [reference_type.id], ["reference", "description"],
                lang, schema_id=reference_type.schema_id,
            )
            entity_resolution = resolved.get(reference_type.id, {})
            if "reference" in entity_resolution:
                value, serving = entity_resolution["reference"]
                serialized["reference"] = value
                serialized["resolved_language"] = serving
            if "description" in entity_resolution:
                value, serving_desc = entity_resolution["description"]
                serialized["description"] = value
                if serialized.get("resolved_language") is None:
                    serialized["resolved_language"] = serving_desc
        await set_cached_with_tags(
            cache_key, serialized,
            tags=[build_tag_key(ENTITY_TYPE)], ttl=CACHE_TTL_ENTITY,
        )
    if lang:
        return serialized if reference_type else None
    return reference_type


async def update_reference_type(
    db: AsyncSession,
    reference_type_id: uuid.UUID,
    reference_type_update: ReferenceTypeUpdate,
    actor_id: uuid.UUID,
) -> ReferenceType | None:
    result = await db.execute(
        select(ReferenceType).where(ReferenceType.id == reference_type_id)
    )
    reference_type = result.scalar_one_or_none()
    if not reference_type:
        return None

    old_values = model_to_dict(reference_type)
    if reference_type_update.reference is not None:
        reference_type.reference = reference_type_update.reference
    if reference_type_update.description is not None:
        reference_type.description = reference_type_update.description
    if reference_type_update.color is not None:
        reference_type.color = reference_type_update.color
    if reference_type_update.source_id is not None:
        reference_type.source_id = reference_type_update.source_id
    if reference_type_update.schema_id is not None:
        reference_type.schema_id = reference_type_update.schema_id

    new_values = model_to_dict(reference_type)
    await audit_log(db, "reference_types", reference_type_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(reference_type)
    await invalidate_entity_cache(ENTITY_TYPE)
    await _purge_affected_type_schemas(db, reference_type_id)
    return reference_type


async def delete_reference_type(
    db: AsyncSession, reference_type_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(ReferenceType).where(ReferenceType.id == reference_type_id)
    )
    reference_type = result.scalar_one_or_none()
    if not reference_type:
        return False
    old_values = model_to_dict(reference_type)
    await audit_log(db, "reference_types", reference_type_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    # D-15 / MULTI-07: purge the polymorphic translation rows in the SAME
    # transaction as the entity delete — no orphans.
    await delete_translations_for(db, "reference_type", [reference_type_id])
    await db.delete(reference_type)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    await _purge_affected_type_schemas(db, reference_type_id)
    return True
