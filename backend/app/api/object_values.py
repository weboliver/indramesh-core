import uuid
from typing import Any, List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import func, select as sa_select
from sqlalchemy.orm import selectinload
from app.db import get_db
from app.models import (
    AttributeType,
    Object,
    ObjectValue,
    ObjectTypeAttribute,
    AttributeTypeDataType,
)
from app.schemas.crud import (
    ObjectValueCreate, ObjectValueUpdate, ObjectValueResponse, DistinctValueItem
)
from app.schemas.auth import UserResponse
from app.api.fields import object_values_fields, filter_fields
from app.services.auth_service import get_current_user, require_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.object_value_service import (
    create_object_value, list_object_values, get_object_value,
    update_object_value, delete_object_value, synthesize_system_bindings,
    _resolve_select_display_in_place,
    _resolve_display_in_place_lang,
)
from app.services.value_lock_service import ValueLockConflict
from app.services.value_visibility import (
    assert_record_action_allowed,
    principal_signature,
    resolve_principal_roles,
    visible_object_id_set,
    visible_value_conditions,
)
from app.services.translation_service import validate_read_language, _get_entity_schema_id
from app.services.typed_value_mapping import typed_value_column


def _get_value_column(dtype: Any) -> Any:
    # D-08: delegates to the canonical seam. `dtype` may legitimately be None
    # here (attr.attribute_type is None) — the seam's fallback-to-value_simple
    # for unrecognized/empty strings covers that the same way the old inline
    # mapping's `mapping.get(dtype, ObjectValue.value_simple)` did.
    return typed_value_column(dtype)


def _visible_value_joins() -> tuple:
    """The three joins every value query must carry for the visibility clauses."""
    return (
        (Object, Object.id == ObjectValue.object_id),
        (
            ObjectTypeAttribute,
            ObjectTypeAttribute.id == ObjectValue.object_type_attribute_id,
        ),
        (
            AttributeType,
            AttributeType.id == ObjectTypeAttribute.attribute_type_id,
        ),
    )


def _with_visibility(stmt, conditions: list):
    """Attach the `object` / `object_type_attributes` / `attribute_types` joins
    and the visibility clauses to a select over ObjectValue."""
    for target, onclause in _visible_value_joins():
        stmt = stmt.join(target, onclause)
    return stmt.where(*conditions)


async def _attribute_type_id_for_value(
    db: AsyncSession, *, value_id: uuid.UUID | None = None,
    ota_id: uuid.UUID | None = None,
) -> uuid.UUID | None:
    """The AttributeType id behind one value (for the write-path DENY gate)."""
    stmt = sa_select(ObjectTypeAttribute.attribute_type_id).select_from(ObjectTypeAttribute)
    if value_id is not None:
        stmt = stmt.join(
            ObjectValue, ObjectValue.object_type_attribute_id == ObjectTypeAttribute.id
        ).where(ObjectValue.id == value_id)
    else:
        stmt = stmt.where(ObjectTypeAttribute.id == ota_id)
    return (await db.execute(stmt)).scalar_one_or_none()


async def _object_id_for_value(
    db: AsyncSession, value_id: uuid.UUID
) -> uuid.UUID | None:
    return (
        await db.execute(
            sa_select(ObjectValue.object_id).where(ObjectValue.id == value_id)
        )
    ).scalar_one_or_none()


router = APIRouter(prefix="/object-values", tags=["object-values"])


@router.post("/", response_model=ObjectValueResponse, status_code=status.HTTP_201_CREATED)
async def create_object_value_route(
    value: ObjectValueCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-values", "create"))
):
    try:
        if not current_user.is_superuser:
            await assert_record_action_allowed(
                str(current_user.id),
                False,
                object_id=value.object_id,
                attribute_type_id=await _attribute_type_id_for_value(
                    db, ota_id=value.object_type_attribute_id
                ),
                # D-08: `create` expands to {create, write} once the alias table
                # lands, so a stored `write` DENY blocks this path too.
                action="create",
            )
        return await create_object_value(db, value, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        msg = str(e)
        if msg in ("Object not found", "Object type attribute not found"):
            raise HTTPException(status_code=404, detail=msg)
        raise HTTPException(status_code=400, detail=msg)


@router.get("/", response_model=List[ObjectValueResponse])
async def list_object_values_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-values", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    object_id: uuid.UUID = Query(None, description="Filter by object ID"),
    object_ids: List[uuid.UUID] = Query(None, description="Batch-filter by multiple object IDs (returns all matches, ignores skip/limit)"),
    fields: list[str] | None = Depends(object_values_fields),
    lang: str | None = Query(None, description="Resolve translated value content for this language code (BCP-47, e.g. 'de')"),
    response: Response = None,
):
    # V-15 / 222 D-05 strictness: batch + unfiltered lists validate the code
    # exists (unknown/inactive -> 400) and resolve per item — never a
    # list-wide 400 for individual items. The single-object scope below adds
    # the schema-envelope check (its schema is knowable).
    if lang and not object_id:
        await validate_read_language(db, lang, None)
    # 317-03 A.3: resolve the principal's roles ONCE per request and feed the
    # same list to every fragment (the two fragments render the same :role_ids
    # bind name — diverging values would silently collapse).
    role_ids = await resolve_principal_roles(
        db, str(current_user.id), current_user.is_superuser
    )
    if object_ids is not None:
        # An explicitly empty batch filter returns nothing — never the
        # unfiltered branch (a filter that is skipped is a leak).
        conditions = await visible_value_conditions(
            db, str(current_user.id), current_user.is_superuser, role_ids=role_ids
        )
        base_filters = (
            [ObjectValue.object_id.in_(object_ids)] if object_ids else []
        )
        if not object_ids:
            items, total = [], 0
        else:
            query = _with_visibility(sa_select(ObjectValue), conditions).where(*base_filters)
            count_query = _with_visibility(
                sa_select(func.count()).select_from(ObjectValue), conditions
            ).where(*base_filters)
            total = (await db.execute(count_query)).scalar()
            result = await db.execute(
                query.options(selectinload(ObjectValue.object_type_attribute))
            )
            items = result.scalars().all()
            if lang:
                items = await _resolve_display_in_place_lang(db, list(items), lang)
            else:
                items = _resolve_select_display_in_place(list(items))
        # 246 D-04/D-05: synthesize valueless system Name/Key values for the
        # *visible* objects only — a denied id in the batch must not resurrect
        # its label (P-01).
        surviving_ids = await visible_object_id_set(
            db, str(current_user.id), current_user.is_superuser, set(object_ids),
            role_ids=role_ids,
        )
        items = await synthesize_system_bindings(db, surviving_ids, list(items))
    elif object_id:
        if lang:
            # V-15: single-object scope is strict (222 D-05) — the object's
            # schema envelope must contain the requested language.
            obj_schema_id = (
                await db.execute(sa_select(Object.schema_id).where(Object.id == object_id))
            ).scalar_one_or_none()
            if obj_schema_id is not None:
                await validate_read_language(db, lang, obj_schema_id)
        # D-05 (317-03): one predicate, every value exit. The object-level
        # record DENY, the attribute-type-level record DENY and is_hidden are
        # composed into WHERE clauses — never a post-hoc Python filter, which
        # would be bypassed by (or leak through) the list cache.
        conditions = await visible_value_conditions(
            db, str(current_user.id), current_user.is_superuser, role_ids=role_ids
        )
        base_filters = [ObjectValue.object_id == object_id]
        query = _with_visibility(sa_select(ObjectValue), conditions).where(*base_filters)
        count_query = _with_visibility(
            sa_select(func.count()).select_from(ObjectValue), conditions
        ).where(*base_filters)
        total = (await db.execute(count_query)).scalar()
        result = await db.execute(
            query.options(selectinload(ObjectValue.object_type_attribute))
            .offset(skip).limit(limit)
        )
        items = result.scalars().all()
        if lang:
            items = await _resolve_display_in_place_lang(db, list(items), lang)
        else:
            items = _resolve_select_display_in_place(list(items))
        # 246 D-04/D-05: synthesize valueless system Name/Key values for the
        # requested object (no ObjectValue row stored for them).
        # P-01: the synthesis takes the SURVIVING visible ids, not the
        # requested one — otherwise a denied object still yields its Name/Key.
        surviving_ids = await visible_object_id_set(
            db, str(current_user.id), current_user.is_superuser, {object_id},
            role_ids=role_ids,
        )
        items = await synthesize_system_bindings(db, surviving_ids, list(items))
    else:
        user_role = "admin" if current_user.is_superuser else "user"
        items, total = await list_object_values(db, skip=skip, limit=limit,
            exclude_hidden=not current_user.is_superuser,
            user_role=user_role,  # NEW per D-10
            lang=lang,
            user_id=str(current_user.id),
            is_superuser=current_user.is_superuser,
            role_ids=role_ids,
            # P-02: the cached branch is principal-scoped, never shared by
            # two non-superusers with different DENY sets.
            principal=principal_signature(
                str(current_user.id), role_ids,
                is_superuser=current_user.is_superuser,
            ),
        )
    response.headers["X-Total-Count"] = str(total)
    if fields:
        filtered = [
            filter_fields(ObjectValueResponse.model_validate(item).model_dump(mode="json"), fields)
            for item in items
        ]
        return JSONResponse(content=filtered, headers=dict(response.headers))
    return items


@router.get("/distinct", response_model=List[DistinctValueItem])
async def distinct_object_values_route(
    object_type_id: uuid.UUID = Query(..., description="Filter by object type ID (required)"),
    attribute_id: uuid.UUID = Query(..., description="Filter by object type attribute ID (required)"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-values", "read")),
):
    attr_result = await db.execute(
        sa_select(ObjectTypeAttribute)
        .options(selectinload(ObjectTypeAttribute.attribute_type))
        .where(
            ObjectTypeAttribute.id == attribute_id,
            ObjectTypeAttribute.object_type_id == object_type_id,
        )
    )
    attr = attr_result.scalar_one_or_none()
    if not attr:
        raise HTTPException(status_code=404, detail="Attribute not found for this object type")

    dtype = attr.attribute_type.data_type if attr.attribute_type else None
    value_col = _get_value_column(dtype)

    # D-05 (317-03 exit 4): distinct applied neither predicate nor is_hidden —
    # a value distribution was a straight oracle over denied rows.
    role_ids = await resolve_principal_roles(
        db, str(current_user.id), current_user.is_superuser
    )
    conditions = await visible_value_conditions(
        db, str(current_user.id), current_user.is_superuser, role_ids=role_ids
    )
    query = (
        _with_visibility(
            sa_select(
                value_col.label("value"),
                func.count().label("count"),
            ).select_from(ObjectValue),
            conditions,
        )
        .where(
            Object.object_type_id == object_type_id,
            ObjectValue.object_type_attribute_id == attribute_id,
            value_col.isnot(None),
        )
        .group_by(value_col)
        .order_by(func.count().desc())
    )
    result = await db.execute(query)
    rows = result.all()
    return [{"value": row.value, "count": row.count} for row in rows]


@router.get("/{value_id}", response_model=ObjectValueResponse)
async def get_object_value_route(
    value_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-values", "read")),
    lang: str | None = Query(None, description="Resolve translated value content for this language code (BCP-47, e.g. 'de')"),
):
    value = await get_object_value(db, value_id, lang=lang)
    if not value:
        raise HTTPException(status_code=404, detail="Object value not found")
    # Security (F-88-03-05): exclude hidden-attribute values for non-superusers.
    # `value` is a live ORM instance on a cache miss, or a plain dict (from
    # object_value_service's jsonable_encoder cache) on a cache hit — normalize
    # attribute access so both shapes are checked consistently.
    if isinstance(value, dict):
        attr = value.get("object_type_attribute")
        is_hidden = bool(attr and attr.get("is_hidden"))
    else:
        attr = value.object_type_attribute
        is_hidden = bool(attr and attr.is_hidden)
    if not current_user.is_superuser and is_hidden:
        raise HTTPException(status_code=404, detail="Object value not found")
    # D-05 (317-03 exit 5): the single row is checked against the same SQL
    # predicate as the list forms — a DENY'd owning object or attribute type is
    # indistinguishable from a missing value. The check runs on cache hits too.
    if not current_user.is_superuser:
        role_ids = await resolve_principal_roles(db, str(current_user.id), False)
        conditions = await visible_value_conditions(
            db, str(current_user.id), False, role_ids=role_ids
        )
        visible = (
            await db.execute(
                _with_visibility(
                    sa_select(ObjectValue.id).select_from(ObjectValue), conditions
                ).where(ObjectValue.id == value_id)
            )
        ).scalar_one_or_none()
        if visible is None:
            raise HTTPException(status_code=404, detail="Object value not found")
    # V-15 strictness (222 D-05): the single value's schema scope is knowable —
    # unknown or not-enabled-for-schema language -> 400 before serving. Runs
    # on cache hits too, so a payload cached under a since-disabled language
    # can never be served.
    if lang:
        schema_id = await _get_entity_schema_id(db, "object_value", value_id)
        await validate_read_language(db, lang, schema_id)
    return value


@router.patch("/{value_id}", response_model=ObjectValueResponse)
async def update_object_value_route(
    value_id: uuid.UUID,
    value_update: ObjectValueUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-values", "update"))
):
    try:
        # D-05 (317-03 exit 6/7): the owning object and the attribute type are
        # both resolved before the row is touched. `update` expands to
        # {update, write}; `delete` stays a singleton (D-08: the write umbrella
        # covers create/update only, so a `write` DENY does not block DELETE).
        if not current_user.is_superuser:
            object_id = await _object_id_for_value(db, value_id)
            if object_id is not None:
                await assert_record_action_allowed(
                    str(current_user.id),
                    False,
                    object_id=object_id,
                    attribute_type_id=await _attribute_type_id_for_value(
                        db, value_id=value_id
                    ),
                    action="update",
                )
        value = await update_object_value(db, value_id, value_update, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not value:
        raise HTTPException(status_code=404, detail="Object value not found")
    return value


@router.delete("/{value_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_value_route(
    value_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-values", "delete"))
):
    try:
        if not current_user.is_superuser:
            object_id = await _object_id_for_value(db, value_id)
            if object_id is not None:
                await assert_record_action_allowed(
                    str(current_user.id),
                    False,
                    object_id=object_id,
                    attribute_type_id=await _attribute_type_id_for_value(
                        db, value_id=value_id
                    ),
                    action="delete",
                )
        deleted = await delete_object_value(db, value_id, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Object value not found")
    return None
