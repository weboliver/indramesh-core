import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ObjectTypeCreate, ObjectTypeUpdate, ObjectTypeResponse, GraphResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission, require_record_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.object_type_service import (
    create_object_type, list_object_types, get_object_type,
    update_object_type, delete_object_type, ObjectTypeNameConflict,
)
from app.services.translation_service import SchemaNotMultilingual
from app.services.value_lock_service import ValueLockConflict
from app.services.graph_service import get_object_type_graph

router = APIRouter(prefix="/object-types", tags=["object-types"])


@router.post("/", response_model=ObjectTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_object_type_route(
    object_type: ObjectTypeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-types", "create"))
):
    try:
        db_object_type = await create_object_type(db, object_type, current_user.id)
    except ObjectTypeNameConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except SchemaNotMultilingual as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not db_object_type:
        raise HTTPException(status_code=404, detail="Schema or parent object type not found")
    return db_object_type


@router.get("/", response_model=List[ObjectTypeResponse])
async def list_object_types_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("object-types", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    schema_id: uuid.UUID | None = Query(None, description="Filter by schema ID"),
    name: str | None = Query(None, description="Filter by name (case-insensitive substring)"),
    lang: str | None = Query(None, description="Resolve name/description in the given language (falls back to schema default language)"),
    response: Response = None,
):
    items, total = await list_object_types(
        db, skip=skip, limit=limit, schema_id=schema_id,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
        name=name, lang=lang,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{object_type_id}", response_model=ObjectTypeResponse)
async def get_object_type_route(
    object_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("object-types", "read", "object_type", "object_type_id")),
    lang: str | None = Query(None, description="Resolve name/description in the given language (falls back to schema default language)"),
):
    object_type = await get_object_type(db, object_type_id, lang)
    if not object_type:
        raise HTTPException(status_code=404, detail="Object type not found")
    return object_type


@router.get("/{object_type_id}/graph", response_model=GraphResponse)
async def get_object_type_graph_route(
    object_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("object-types", "read", "object_type", "object_type_id"))
):
    graph = await get_object_type_graph(
        db, object_type_id, str(current_user.id), current_user.is_superuser
    )
    if graph is None:
        raise HTTPException(status_code=404, detail="Object type not found")
    return graph


@router.patch("/{object_type_id}", response_model=ObjectTypeResponse)
async def update_object_type_route(
    object_type_id: uuid.UUID,
    object_type_update: ObjectTypeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("object-types", "update", "object_type", "object_type_id"))
):
    try:
        object_type = await update_object_type(db, object_type_id, object_type_update, current_user.id)
    except ObjectTypeNameConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not object_type:
        raise HTTPException(status_code=404, detail="Object type not found")
    return object_type


@router.delete("/{object_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_type_route(
    object_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("object-types", "delete", "object_type", "object_type_id"))
):
    try:
        deleted = await delete_object_type(db, object_type_id, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Object type not found")
    return None
