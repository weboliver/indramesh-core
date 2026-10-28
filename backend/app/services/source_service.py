import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ObjectType, Schema, Source
from app.plugin_system.connector_registry import (
    ConnectorTypeValidationError,
    get_connector_type_registry,
    resolve_connector_type,
)
from app.schemas.crud import SourceCreate, SourceUpdate
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
from app.services.record_permission_service import build_record_permission_filter
from app.services.audit_service import audit_log, model_to_dict
from app.services.service_account_service import (
    ENTITY_TYPE as SERVICE_ACCOUNT_ENTITY_TYPE,
    revoke_endpoint_tokens,
)
from app.services.value_lock_service import assert_types_deletable


async def create_source(db: AsyncSession, source_data: SourceCreate, actor_id: uuid.UUID) -> Source:
    # CORESEAM-01: normalize + resolve the connector identity on WRITE only, and
    # let the descriptor own the per-connector rules (config, requires_url) —
    # mirroring target_service.create_target. Raises ValueError (mapped to 422 by
    # the route) for blank, malformed, unregistered, or Target-only connector
    # values, and for a write that violates the resolved descriptor — before any
    # row is created (T-289-01-01).
    declaration = resolve_connector_type(source_data.type, "source").declaration
    missing = declaration.missing_requirements("source", url=source_data.source_url)
    if missing:
        raise ValueError(
            f"{declaration.name} connector requires: " + ", ".join(missing)
        )
    config = declaration.validate_config(source_data.config)
    db_source = Source(
        name=source_data.name,
        type=declaration.name,
        source_url=source_data.source_url,
        config=config,
        import_folder=source_data.import_folder,
    )
    db.add(db_source)
    await db.flush()
    new_values = model_to_dict(db_source)
    await audit_log(db, "sources", db_source.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_source)
    await invalidate_entity_cache("source")
    return db_source


async def list_sources(
    db: AsyncSession, name: str | None = None, type: str | None = None, skip: int = 0, limit: int = 100,
    user_id: str | None = None, is_superuser: bool = False,
) -> tuple[List[Source], int]:
    cache_key = build_list_cache_key(
        "source", name=name or "", type=type or "", skip=skip, limit=limit, user_id=user_id,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_query = select(func.count()).select_from(Source)
    query = select(Source).order_by(Source.created_at.asc(), Source.id.asc())
    if name:
        query = query.where(Source.name.ilike(f"%{name}%"))
        count_query = count_query.where(Source.name.ilike(f"%{name}%"))
    if type:
        # Read-path filter: a plain normalized string comparison with NO
        # registry resolution — unknown historical values stay filterable.
        normalized_type = type.strip().lower()
        query = query.where(Source.type == normalized_type)
        count_query = count_query.where(Source.type == normalized_type)
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db, user_id, "source", is_superuser=is_superuser
        )
        if filter_clause is not None:
            count_query = count_query.where(filter_clause)
            query = query.where(filter_clause)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key("source")],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_source(db: AsyncSession, source_id: uuid.UUID) -> Source | dict | None:
    cache_key = build_cache_key("source", str(source_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if source:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(source),
            tags=[build_tag_key("source")],
            ttl=CACHE_TTL_ENTITY,
        )
    return source


async def update_source(
    db: AsyncSession, source_id: uuid.UUID, source_update: SourceUpdate, actor_id: uuid.UUID
) -> Source | None:
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        return None
    old_values = model_to_dict(source)
    if source_update.name is not None:
        source.name = source_update.name
    if source_update.type is not None:
        # CORESEAM-01: same write-time normalization + registry resolution as create.
        source.type = resolve_connector_type(
            source_update.type, "source"
        ).declaration.name

    # A persisted connector that is unknown (or belongs to a plugin that is not
    # active) stays read-tolerant: unrelated field updates keep working. Any
    # write that touches the connector contract itself fails closed — mirroring
    # target_service.update_target.
    entry = get_connector_type_registry().resolve(source.type)
    declaration = entry.declaration if entry is not None else None
    if declaration is None and source_update.config is not None:
        raise ConnectorTypeValidationError(
            f"unknown source connector type {source.type!r}"
        )

    url_changed = False
    if source_update.source_url is not None:
        url_changed = source_update.source_url != source.source_url
        source.source_url = source_update.source_url
    if source_update.config is not None:
        source.config = declaration.validate_config(source_update.config)
    if source_update.import_folder is not None:
        source.import_folder = source_update.import_folder

    # Re-validate the merged post-update state through the same descriptor — a
    # PATCH touching only type or only config can leave the *other* one
    # mismatched against what is now persisted (T-289-01-01, D-03b mirror).
    if declaration is not None:
        missing = declaration.missing_requirements("source", url=source.source_url)
        if missing:
            raise ValueError(
                f"{declaration.name} connector requires: " + ", ".join(missing)
            )
        # Normalization is idempotent, as on the Target path.
        source.config = declaration.validate_config(source.config)
    new_values = model_to_dict(source)
    await audit_log(db, "sources", source_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    if url_changed:
        await revoke_endpoint_tokens(db, source_id=source_id)
    await db.commit()
    await db.refresh(source)
    await invalidate_entity_cache("source")
    if url_changed:
        await invalidate_entity_cache(SERVICE_ACCOUNT_ENTITY_TYPE)
    return source


async def delete_source(db: AsyncSession, source_id: uuid.UUID, actor_id: uuid.UUID) -> bool:
    result = await db.execute(select(Source).where(Source.id == source_id))
    source = result.scalar_one_or_none()
    if not source:
        return False
    # 290 D-15 (WR-05): an externally managed member ObjectType protects the
    # whole source — collect every ObjectType across the source's schemas and
    # run the same fresh persisted marker check (FOR SHARE, never the cache)
    # that delete_schema uses, BEFORE any audit/delete. Otherwise the
    # source-level ORM cascade would destroy a locked descendant type (and all
    # its objects), the most destructive entry point of the same guard class.
    ot_result = await db.execute(
        select(ObjectType.id)
        .join(Schema, ObjectType.schema_id == Schema.id)
        .where(Schema.source_id == source_id)
    )
    await assert_types_deletable(db, ot_result.scalars().all())
    old_values = model_to_dict(source)
    await audit_log(db, "sources", source_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(source)
    await db.commit()
    await invalidate_entity_cache("source")
    return True
