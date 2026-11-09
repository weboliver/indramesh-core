import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.target import Target
from app.models import Schema
from app.plugin_system.connector_registry import (
    ConnectorTypeValidationError,
    get_connector_type_registry,
    resolve_connector_type,
)
from app.schemas.crud import TargetCreate, TargetUpdate
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
from app.services.outbound_url_guard import (
    check_resolved_target,
    read_allow_internal_flag,
)
from app.services.service_account_service import (
    ENTITY_TYPE as SERVICE_ACCOUNT_ENTITY_TYPE,
    revoke_endpoint_tokens,
)
from app.services.source_service import assert_flag_write_allowed

ENTITY_TYPE = "target"


async def create_target(
    db: AsyncSession, target_data: TargetCreate, actor_id: uuid.UUID
) -> Target:
    # CORESEAM-01: normalize + resolve the connector identity on WRITE only, and
    # let the descriptor own the per-connector rules (config, requires_url,
    # requires_schema_id, requires_export_folder). Raises ValueError (mapped to
    # 422 by the route) before any row is created.
    declaration = resolve_connector_type(
        target_data.connector_type, "target"
    ).declaration
    missing = declaration.missing_requirements(
        "target",
        url=target_data.target_url,
        schema_id=target_data.schema_id,
        export_folder=target_data.export_folder,
    )
    if missing:
        raise ValueError(
            f"{declaration.name} connector requires: " + ", ".join(missing)
        )
    config = declaration.validate_config(target_data.config)

    if target_data.schema_id is not None:
        result = await db.execute(
            select(Schema).where(Schema.id == target_data.schema_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Schema not found")

    # CSV and other non-Jira targets may not have a target_url; DB column is NOT NULL
    target_url = target_data.target_url if target_data.target_url else ""

    # SESS-07 / D-06: same guard as the Source create path, against the literal
    # host and the resolved answer set, unless this endpoint owns the waiver.
    if "target" in declaration.requires_public_url:
        await assert_flag_write_allowed(
            db, actor_id, persisted_config=None, merged_config=config
        )
        await check_resolved_target(
            target_url, allow_internal=read_allow_internal_flag(config)
        )

    db_target = Target(
        schema_id=target_data.schema_id,
        name=target_data.name,
        target_url=target_url,
        config=config,
        export_folder=target_data.export_folder,
        connector_type=declaration.name,
        is_active=target_data.is_active,
    )
    db.add(db_target)
    await db.flush()
    new_values = model_to_dict(db_target)
    await audit_log(db, "targets", db_target.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_target)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_target


async def list_targets(
    db: AsyncSession, name: str | None = None, connector_type: str | None = None, skip: int = 0, limit: int = 100
) -> tuple[List[Target], int]:
    # Read-path filter: normalized like the Sources list, with NO registry
    # resolution — unknown historical values stay filterable.
    if connector_type:
        connector_type = connector_type.strip().lower()
    cache_key = build_list_cache_key(
        ENTITY_TYPE, name=name or "", connector_type=connector_type or "", skip=skip, limit=limit,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_query = select(func.count()).select_from(Target)
    query = select(Target).order_by(Target.created_at.asc(), Target.id.asc())
    if name:
        query = query.where(Target.name.ilike(f"%{name}%"))
        count_query = count_query.where(Target.name.ilike(f"%{name}%"))
    if connector_type:
        query = query.where(Target.connector_type == connector_type)
        count_query = count_query.where(Target.connector_type == connector_type)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(
        query.offset(skip).limit(limit)
    )
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_target(
    db: AsyncSession, target_id: uuid.UUID
) -> Target | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(target_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()
    if target:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(target),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return target


async def update_target(
    db: AsyncSession, target_id: uuid.UUID, target_update: TargetUpdate, actor_id: uuid.UUID
) -> Target | None:
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        return None

    old_values = model_to_dict(target)

    # CORESEAM-01: resolve the (possibly changed) connector identity up front,
    # before any mutation, so the SESS-07 checks below decide from the merged
    # post-update state and a refused PATCH leaves the row untouched.
    persisted_type = target.connector_type
    persisted_url = target.target_url
    persisted_config = target.config
    resolved_type = persisted_type
    if target_update.connector_type is not None:
        # Changing the connector identity is always a validated write.
        resolved_type = resolve_connector_type(
            target_update.connector_type, "target"
        ).declaration.name

    # A persisted connector that is unknown (or belongs to a plugin that is not
    # active) stays read-tolerant: unrelated field updates keep working. Any
    # write that touches the connector contract itself fails closed.
    entry = get_connector_type_registry().resolve(resolved_type)
    declaration = entry.declaration if entry is not None else None
    if declaration is None and target_update.config is not None:
        raise ConnectorTypeValidationError(
            f"unknown target connector type {resolved_type!r}"
        )

    merged_config = persisted_config
    if target_update.config is not None and declaration is not None:
        merged_config = declaration.validate_config(target_update.config)
    url_changed = (
        target_update.target_url is not None
        and target_update.target_url != persisted_url
    )
    type_changed = (
        target_update.connector_type is not None
        and resolved_type != persisted_type
    )

    # D-08: the internal-target waiver is a superuser grant, and a flagged row
    # may only be repointed (URL or connector type) by a superuser.
    await assert_flag_write_allowed(
        db,
        actor_id,
        persisted_config=persisted_config,
        merged_config=merged_config,
        repoints=url_changed or type_changed,
    )
    # SESS-07: re-run the address check only when this PATCH can change the
    # decision — an unrelated rename must not resolve anything.
    if declaration is not None and "target" in declaration.requires_public_url:
        if url_changed or type_changed or target_update.config is not None:
            await check_resolved_target(
                target_update.target_url
                if target_update.target_url is not None
                else persisted_url,
                allow_internal=read_allow_internal_flag(merged_config),
            )

    if target_update.schema_id is not None:
        result = await db.execute(
            select(Schema).where(Schema.id == target_update.schema_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Schema not found")
        target.schema_id = target_update.schema_id
    if target_update.name is not None:
        target.name = target_update.name
    if target_update.connector_type is not None:
        target.connector_type = resolved_type
    if target_update.target_url is not None:
        target.target_url = target_update.target_url
    if target_update.config is not None:
        target.config = merged_config
    if target_update.export_folder is not None:
        target.export_folder = target_update.export_folder
    if target_update.is_active is not None:
        target.is_active = target_update.is_active

    # Re-validate the merged post-update state through the same descriptor — a
    # PATCH touching only connector_type or only config can leave the *other*
    # one mismatched against what is now persisted (D-03b).
    if declaration is not None:
        missing = declaration.missing_requirements(
            "target",
            url=target.target_url,
            schema_id=target.schema_id,
            export_folder=target.export_folder,
        )
        if missing:
            raise ValueError(
                f"{declaration.name} connector requires: " + ", ".join(missing)
            )
        # Normalization is idempotent: csv config keeps its defaults materialized.
        target.config = declaration.validate_config(target.config)

    new_values = model_to_dict(target)
    await audit_log(db, "targets", target_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    if url_changed:
        await revoke_endpoint_tokens(db, target_id=target_id)
    await db.commit()
    await db.refresh(target)
    await invalidate_entity_cache(ENTITY_TYPE)
    if url_changed:
        await invalidate_entity_cache(SERVICE_ACCOUNT_ENTITY_TYPE)
    return target


async def delete_target(
    db: AsyncSession, target_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(Target).where(Target.id == target_id)
    )
    target = result.scalar_one_or_none()
    if not target:
        return False
    old_values = model_to_dict(target)
    await audit_log(db, "targets", target_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(target)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
