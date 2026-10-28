import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ObjectTypeAttributeCreate, ObjectTypeAttributeUpdate, ObjectTypeAttributeResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.object_type_attribute_service import (
    create_object_type_attribute, list_object_type_attributes,
    get_object_type_attribute, update_object_type_attribute,
    delete_object_type_attribute
)

router = APIRouter(prefix="/object-type-attributes", tags=["object-type-attributes"])


@router.post("/", response_model=ObjectTypeAttributeResponse, status_code=status.HTTP_201_CREATED)
async def create_object_type_attribute_route(
    attribute: ObjectTypeAttributeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "create"))
):
    try:
        db_attribute = await create_object_type_attribute(db, attribute, actor_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not db_attribute:
        raise HTTPException(status_code=404, detail="Referenced entity not found")
    return db_attribute


@router.get("/", response_model=List[ObjectTypeAttributeResponse])
async def list_object_type_attributes_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(100, ge=1, le=1000, description="Maximum records to return"),
    object_type_id: uuid.UUID = Query(None, description="Filter by object type ID"),
    lang: str | None = Query(None, description="Resolve name/description in the given language (falls back to schema default language)"),
    response: Response = None,
):
    items, total = await list_object_type_attributes(
        db, skip=skip, limit=limit, object_type_id=object_type_id,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
        lang=lang,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{attribute_id}", response_model=ObjectTypeAttributeResponse)
async def get_object_type_attribute_route(
    attribute_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "read")),
    lang: str | None = Query(None, description="Resolve name/description in the given language (falls back to schema default language)"),
):
    attribute = await get_object_type_attribute(db, attribute_id, lang)
    if not attribute:
        raise HTTPException(status_code=404, detail="Object type attribute not found")
    return attribute


@router.patch("/{attribute_id}", response_model=ObjectTypeAttributeResponse)
async def update_object_type_attribute_route(
    attribute_id: uuid.UUID,
    attribute_update: ObjectTypeAttributeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "update"))
):
    try:
        attribute = await update_object_type_attribute(db, attribute_id, attribute_update, actor_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not attribute:
        raise HTTPException(status_code=404, detail="Object type attribute not found")
    return attribute


@router.delete("/{attribute_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_type_attribute_route(
    attribute_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-type-attributes", "delete"))
):
    try:
        deleted = await delete_object_type_attribute(db, attribute_id, actor_id=current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Object type attribute not found")
    return None
