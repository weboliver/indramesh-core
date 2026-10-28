import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select as sa_select, func
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.db import get_db
from app.models import ObjectRef
from app.schemas.crud import (
    ObjectRefCreate, ObjectRefUpdate, ObjectRefResponse
)
from app.schemas.auth import UserResponse
from app.api.fields import object_refs_fields, filter_fields
from app.services.auth_service import get_current_user, require_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.object_ref_service import (
    create_object_ref, list_object_refs, get_object_ref,
    update_object_ref, delete_object_ref, object_ref_visible,
    visible_ref_conditions, hidden_ref_clause, ref_hidden_by_attribute,
    InvalidRefAttributeError,
)
from app.services.record_permission_service import build_record_permission_filter
from app.services.value_lock_service import ValueLockConflict

router = APIRouter(prefix="/object-refs", tags=["object-refs"])


@router.post("/", response_model=ObjectRefResponse, status_code=status.HTTP_201_CREATED)
async def create_object_ref_route(
    ref: ObjectRefCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-refs", "create"))
):
    try:
        return await create_object_ref(
            db, ref, current_user.id,
            user_id=str(current_user.id), is_superuser=current_user.is_superuser,
        )
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidRefAttributeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


@router.get("/", response_model=List[ObjectRefResponse])
async def list_object_refs_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-refs", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    from_object_id: uuid.UUID = Query(None, description="Filter by source object ID"),
    to_object_id: uuid.UUID = Query(None, description="Filter by target object ID (inbound refs)"),
    object_ids: List[uuid.UUID] = Query(None, description="Batch-filter by multiple source object IDs (returns all matches, ignores skip/limit)"),
    fields: list[str] | None = Depends(object_refs_fields),
    response: Response = None,
):
    if object_ids:
        query = (
            sa_select(ObjectRef)
            .where(ObjectRef.from_object_id.in_(object_ids))
            .options(selectinload(ObjectRef.to_object), selectinload(ObjectRef.from_object))
        )
        count_query = sa_select(func.count()).select_from(ObjectRef).where(ObjectRef.from_object_id.in_(object_ids))
        object_filter = await build_record_permission_filter(
            db, str(current_user.id), "object", is_superuser=current_user.is_superuser
        )
        if object_filter is not None:
            conditions = visible_ref_conditions(object_filter)
            query = query.where(*conditions)
            count_query = count_query.where(*conditions)
        if not current_user.is_superuser:
            hidden_clause = await hidden_ref_clause(db)
            if hidden_clause is not None:
                query = query.where(hidden_clause)
                count_query = count_query.where(hidden_clause)
        total = (await db.execute(count_query)).scalar()
        result = await db.execute(query)
        items = result.scalars().all()
    else:
        user_role = "admin" if current_user.is_superuser else "user"
        items, total = await list_object_refs(db, skip=skip, limit=limit,
            from_object_id=from_object_id, to_object_id=to_object_id,
            user_role=user_role,  # NEW per D-11
            user_id=str(current_user.id), is_superuser=current_user.is_superuser,
        )
    response.headers["X-Total-Count"] = str(total)
    if fields:
        filtered = [
            filter_fields(ObjectRefResponse.model_validate(item).model_dump(mode="json"), fields)
            for item in items
        ]
        return JSONResponse(content=filtered, headers=dict(response.headers))
    return items


@router.get("/{ref_id}", response_model=ObjectRefResponse)
async def get_object_ref_route(
    ref_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-refs", "read"))
):
    ref = await get_object_ref(db, ref_id)
    if not ref:
        raise HTTPException(status_code=404, detail="Object reference not found")
    if not await object_ref_visible(
        db, ref, str(current_user.id), is_superuser=current_user.is_superuser
    ):
        raise HTTPException(status_code=404, detail="Object reference not found")
    # SEC313-97 / WR-02: an id-based test over the same clause the list paths
    # use, so a cached dict served above cannot bypass it. Superusers keep their
    # zero-query path (RLR-05).
    if not current_user.is_superuser and await ref_hidden_by_attribute(db, ref_id):
        raise HTTPException(status_code=404, detail="Object reference not found")
    return ref


@router.patch("/{ref_id}", response_model=ObjectRefResponse)
async def update_object_ref_route(
    ref_id: uuid.UUID,
    ref_update: ObjectRefUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-refs", "update"))
):
    try:
        ref = await update_object_ref(
            db, ref_id, ref_update, current_user.id,
            user_id=str(current_user.id), is_superuser=current_user.is_superuser,
        )
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except InvalidRefAttributeError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    if not ref:
        raise HTTPException(status_code=404, detail="Object reference not found")
    result = await db.execute(
        sa_select(ObjectRef)
        .where(ObjectRef.id == ref.id)
        .options(selectinload(ObjectRef.to_object), selectinload(ObjectRef.from_object))
        .execution_options(populate_existing=True)
    )
    return result.scalar_one()


@router.delete("/{ref_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_ref_route(
    ref_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-refs", "delete"))
):
    try:
        deleted = await delete_object_ref(
            db, ref_id, current_user.id,
            user_id=str(current_user.id), is_superuser=current_user.is_superuser,
        )
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Object reference not found")
    return None
