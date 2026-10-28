import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    AttributeTypeCreate, AttributeTypeUpdate, AttributeTypeResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission, require_record_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.attribute_type_service import (
    create_attribute_type, list_attribute_types, get_attribute_type,
    update_attribute_type, delete_attribute_type
)

router = APIRouter(prefix="/attribute-types", tags=["attribute-types"])


@router.post("/", response_model=AttributeTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_attribute_type_route(
    attribute_type: AttributeTypeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("attribute-types", "create"))
):
    return await create_attribute_type(db, attribute_type, current_user.id)


@router.get("/", response_model=List[AttributeTypeResponse])
async def list_attribute_types_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("attribute-types", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_attribute_types(
        db, skip=skip, limit=limit,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{attribute_type_id}", response_model=AttributeTypeResponse)
async def get_attribute_type_route(
    attribute_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("attribute-types", "read", "attribute_type", "attribute_type_id"))
):
    attribute_type = await get_attribute_type(db, attribute_type_id)
    if not attribute_type:
        raise HTTPException(status_code=404, detail="Attribute type not found")
    return attribute_type


@router.patch("/{attribute_type_id}", response_model=AttributeTypeResponse)
async def update_attribute_type_route(
    attribute_type_id: uuid.UUID,
    attribute_type_update: AttributeTypeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("attribute-types", "update", "attribute_type", "attribute_type_id"))
):
    try:
        attribute_type = await update_attribute_type(db, attribute_type_id, attribute_type_update, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not attribute_type:
        raise HTTPException(status_code=404, detail="Attribute type not found")
    return attribute_type


@router.delete("/{attribute_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_attribute_type_route(
    attribute_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("attribute-types", "delete", "attribute_type", "attribute_type_id"))
):
    try:
        deleted = await delete_attribute_type(db, attribute_type_id, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Attribute type not found")
    return None
