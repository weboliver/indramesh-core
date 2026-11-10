import uuid
from typing import List

from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func, case
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AttributeType, ObjectTypeAttribute
from app.schemas.crud import AttributeTypeCreate, AttributeTypeUpdate
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
from app.services.record_permission_service import build_record_permission_filter, delete_dependent_record_permissions
from app.services.audit_service import audit_log, model_to_dict

ENTITY_TYPE = "attribute_type"

# Approved canonical naming map (245-01 Task 1 checkpoint, option-a). Mirrors
# CANONICAL_ATTRIBUTE_TYPE_NAMES in
# alembic/versions/f6931c094d0e_add_textarea_attribute_type.py exactly —
# test_attribute_type_system.py asserts the two stay equal so the live
# catalog and this module's naming contract can never drift apart (D-11/D-14).
CANONICAL_ATTRIBUTE_TYPE_NAMES: dict[str, str] = {
    "double": "Double",
    "integer": "Integer",
    "boolean": "Boolean",
    "text": "Text",
    "datetime": "DateTime",
    "json": "JSON",
    "status": "Status",
    "version": "Version",
    "group": "Group",
    "user": "User",
    "jira": "Jira",
    "confluence": "Confluence",
    "reference": "Reference",
    "select": "Select",
    "url": "URL",
    "email": "Email",
    "textarea": "Textarea",
}


def canonical_attribute_type_pairs() -> list[tuple[str, str]]:
    """The canonical (name, data_type) pairs, name-first.

    Single runtime-derived allow-list for the pytest_sessionfinish
    attribute_types reconciliation (245-06): every caller derives its
    allow-list from this function so the conftest cleanup statements and
    the canonical map can never drift apart (D-11/D-14 — one immutable
    row per data_type)."""
    return [(name, data_type) for data_type, name in CANONICAL_ATTRIBUTE_TYPE_NAMES.items()]


async def get_or_create_canonical_by_data_type(
    db: AsyncSession, data_type: str, actor_id: uuid.UUID | None = None
) -> AttributeType:
    """One immutable AttributeType row per data_type (D-11). Returns the
    existing canonical row for `data_type` if one exists; otherwise inserts
    it under its canonical display name and returns the new row. Never
    inserts a second row for a data_type that already has one — callers
    (discover_attributes, the AttributeType create API) rely on this to
    recycle the shared row instead of forking per-name duplicates.

    Defensive against pre-existing duplicate rows for the same data_type
    (e.g. debris from tests/older code that inserted directly, predating
    this helper's adoption): picks deterministically -- exact canonical-name
    match first, then earliest created_at -- mirroring the 245-01 migration's
    own precedence, rather than crashing or minting yet another duplicate.
    This helper does not repoint/delete the other duplicates itself; that
    one-time cleanup is the migration's job, not every call site's."""
    canonical_name = CANONICAL_ATTRIBUTE_TYPE_NAMES.get(data_type, data_type)
    result = await db.execute(
        select(AttributeType)
        .where(AttributeType.data_type == data_type)
        .order_by(
            case((AttributeType.name == canonical_name, 0), else_=1),
            AttributeType.created_at.asc(),
        )
        .limit(1)
    )
    attribute_type = result.scalar_one_or_none()
    if attribute_type:
        return attribute_type

    attribute_type = AttributeType(name=canonical_name, data_type=data_type)
    db.add(attribute_type)
    await db.flush()
    await audit_log(db, "attribute_types", attribute_type.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(attribute_type))
    return attribute_type


async def create_attribute_type(
    db: AsyncSession, attribute_type_data: AttributeTypeCreate, actor_id: uuid.UUID
) -> AttributeType:
    # D-11/D-13: one immutable row per data_type -- delegate to the same
    # canonical get-or-create helper discover_attributes uses, so the API
    # can never mint a second row for a data_type that already has one.
    # The submitted `name` is intentionally ignored on creation; only the
    # canonical display name from CANONICAL_ATTRIBUTE_TYPE_NAMES is ever
    # used, and no audit entry is written when an existing row is reused.
    db_attribute_type = await get_or_create_canonical_by_data_type(
        db, attribute_type_data.data_type, actor_id=actor_id
    )
    await db.commit()
    await db.refresh(db_attribute_type)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_attribute_type


async def list_attribute_types(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    user_id: str | None = None, is_superuser: bool = False,
) -> tuple[List[AttributeType], int]:
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit, user_id=user_id)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_query = select(func.count()).select_from(AttributeType)
    query = select(AttributeType).order_by(AttributeType.name)
    if user_id is not None:
        filter_clause = await build_record_permission_filter(
            db, user_id, ENTITY_TYPE, is_superuser=is_superuser
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
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_attribute_type(
    db: AsyncSession, attribute_type_id: uuid.UUID
) -> AttributeType | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(attribute_type_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    if attribute_type:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(attribute_type),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return attribute_type


async def update_attribute_type(
    db: AsyncSession,
    attribute_type_id: uuid.UUID,
    attribute_type_update: AttributeTypeUpdate,
    actor_id: uuid.UUID,
) -> AttributeType | None:
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    if not attribute_type:
        return None

    # D-11: data_type is the row's identity -- relabeling a canonical row
    # would mint a second row for the OLD type on the next get-or-create
    # call, reintroducing exactly the fork the migration removed. The
    # field_validator already guarantees the new value is a valid enum
    # member; this only blocks changing it on an EXISTING row.
    if (attribute_type_update.data_type is not None
            and attribute_type_update.data_type != attribute_type.data_type):
        raise ValueError("data_type is immutable and cannot be changed on an existing attribute type")

    old_values = model_to_dict(attribute_type)
    # D-11: the canonical display name is as immutable as data_type on
    # canonical rows -- renaming "Text" -> "Foo" would silently break the
    # canonical-catalog invariant (CANONICAL_ATTRIBUTE_TYPE_NAMES and the
    # conftest reconciliation allow-list would no longer match the DB), and
    # renaming onto another canonical name would collide with
    # AttributeType.name unique=True. Non-canonical rows (data_type not in
    # the canonical map) may still be renamed.
    if (attribute_type_update.name is not None
            and attribute_type_update.name != attribute_type.name):
        canonical = CANONICAL_ATTRIBUTE_TYPE_NAMES.get(attribute_type.data_type)
        if canonical is not None:
            raise ValueError(
                f"name is immutable on canonical attribute types "
                f"(must stay {canonical!r}); delete and recreate to rename"
            )

    if attribute_type_update.name is not None:
        attribute_type.name = attribute_type_update.name

    new_values = model_to_dict(attribute_type)
    new_name = attribute_type.name
    await audit_log(db, "attribute_types", attribute_type_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    try:
        await db.commit()
    except IntegrityError:
        # Defense-in-depth: non-canonical rows can still be renamed onto a
        # name that collides with another row (e.g. AttributeType.name
        # unique=True) -- surface that as 409, not an unhandled 500
        # (mirrors group_service/language_service patterns).
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Attribute type name '{new_name}' already exists.",
        )
    await db.refresh(attribute_type)
    await invalidate_entity_cache(ENTITY_TYPE)
    return attribute_type


async def delete_attribute_type(
    db: AsyncSession, attribute_type_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(AttributeType).where(AttributeType.id == attribute_type_id)
    )
    attribute_type = result.scalar_one_or_none()
    if not attribute_type:
        return False

    # Deviation (Rule 2, 245-02 Task 3): attribute_types.id cascades via
    # ON DELETE CASCADE to object_type_attributes -- deleting a row still
    # referenced by any ObjectTypeAttribute would silently destroy those
    # rows (and, transitively, their values). Under D-11's get-or-create
    # semantics every create/discovery now recycles one of the 16 canonical
    # rows, so this guard prevents an unsuspecting delete (e.g. a legacy
    # test written against the old fresh-row-per-POST semantics) from
    # cascading into shared, potentially real, synced data.
    usage_result = await db.execute(
        select(func.count()).select_from(ObjectTypeAttribute).where(
            ObjectTypeAttribute.attribute_type_id == attribute_type_id
        )
    )
    if usage_result.scalar() > 0:
        raise ValueError("Cannot delete attribute type: still in use by one or more object type attributes")

    old_values = model_to_dict(attribute_type)
    await audit_log(db, "attribute_types", attribute_type_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await delete_dependent_record_permissions(db, attribute_type_ids=[attribute_type_id])
    await db.delete(attribute_type)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True
