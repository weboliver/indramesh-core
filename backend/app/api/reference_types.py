import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    ReferenceTypeCreate, ReferenceTypeUpdate, ReferenceTypeResponse
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.reference_type_service import (
    create_reference_type, list_reference_types, get_reference_type,
    update_reference_type, delete_reference_type
)

router = APIRouter(prefix="/reference-types", tags=["reference-types"])


@router.post("/", response_model=ReferenceTypeResponse, status_code=status.HTTP_201_CREATED)
async def create_reference_type_route(
    reference_type: ReferenceTypeCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("reference-types", "create"))
):
    return await create_reference_type(db, reference_type, current_user.id)


@router.get("/", response_model=List[ReferenceTypeResponse])
async def list_reference_types_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("reference-types", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    schema_id: uuid.UUID = Query(None, description="Filter by schema"),
    lang: str | None = Query(None, description="Resolve translated reference/description for this language code (BCP-47, e.g. 'de')"),
    response: Response = None,
):
    items, total = await list_reference_types(db, skip=skip, limit=limit, schema_id=schema_id, lang=lang)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{reference_type_id}", response_model=ReferenceTypeResponse)
async def get_reference_type_route(
    reference_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("reference-types", "read")),
    lang: str | None = Query(None, description="Resolve translated reference/description for this language code (BCP-47, e.g. 'de')"),
):
    reference_type = await get_reference_type(db, reference_type_id, lang=lang)
    if not reference_type:
        raise HTTPException(status_code=404, detail="Reference type not found")
    return reference_type


@router.patch("/{reference_type_id}", response_model=ReferenceTypeResponse)
async def update_reference_type_route(
    reference_type_id: uuid.UUID,
    reference_type_update: ReferenceTypeUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("reference-types", "update"))
):
    reference_type = await update_reference_type(db, reference_type_id, reference_type_update, current_user.id)
    if not reference_type:
        raise HTTPException(status_code=404, detail="Reference type not found")
    return reference_type


@router.delete("/{reference_type_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_reference_type_route(
    reference_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("reference-types", "delete"))
):
    deleted = await delete_reference_type(db, reference_type_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Reference type not found")
    return None
