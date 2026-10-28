import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import SourceCreate, SourceUpdate, SourceResponse
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission, require_record_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.source_service import (
    create_source, list_sources, get_source, update_source, delete_source
)
from app.services.value_lock_service import ValueLockConflict

router = APIRouter(prefix="/sources", tags=["sources"])


@router.post("/", response_model=SourceResponse, status_code=status.HTTP_201_CREATED)
async def create_source_route(
    source: SourceCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("sources", "create"))
):
    try:
        return await create_source(db, source, current_user.id)
    except ValueError as e:
        # CORESEAM-01: write-time connector-registry rejections (blank,
        # malformed, unregistered, or wrong-entity-kind `type`) are client
        # input errors, mirrored on the Target routes' ValueError mapping.
        raise HTTPException(status_code=422, detail=str(e))


@router.get("/", response_model=List[SourceResponse])
async def list_sources_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
    name: str | None = Query(None, max_length=255, description="Filter sources by name (case-insensitive substring match)"),
    # CORESEAM-01 (Phase 289 Plan 03): an OPEN normalized string — never a
    # closed enum and never registry-validated. Unknown/historical connector
    # values must stay filterable.
    type: str | None = Query(None, max_length=50, description="Filter sources by connector type (normalized lowercase string)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    items, total = await list_sources(
        db, name=name, type=type.strip().lower() if type else None, skip=skip, limit=limit,
        user_id=current_user.id, is_superuser=current_user.is_superuser,
    )
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{source_id}", response_model=SourceResponse)
async def get_source_route(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("sources", "read", "source", "source_id", require_type_level=False))
):
    source = await get_source(db, source_id)
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.patch("/{source_id}", response_model=SourceResponse)
async def update_source_route(
    source_id: uuid.UUID,
    source_update: SourceUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("sources", "update", "source", "source_id"))
):
    try:
        source = await update_source(db, source_id, source_update, current_user.id)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    if not source:
        raise HTTPException(status_code=404, detail="Source not found")
    return source


@router.delete("/{source_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_source_route(
    source_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("sources", "delete", "source", "source_id"))
):
    # 290 D-15 (WR-05): a locked descendant ObjectType answers the same 409
    # contract as DELETE /schemas/{id} — the source-level cascade must not
    # silently destroy externally managed types.
    try:
        deleted = await delete_source(db, source_id, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Source not found")
    return None
