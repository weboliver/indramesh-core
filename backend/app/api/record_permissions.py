import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.auth import UserResponse
from app.schemas.crud import (
    RecordPermissionCreate,
    RecordPermissionResponse,
    RecordPermissionListResponse,
    EffectivePermissionResponse,
)
from app.services.auth_service import require_permission
from app.services.record_permission_service import (
    create_record_permission,
    delete_record_permission,
    list_record_permissions,
    get_effective_permissions,
)
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT

router = APIRouter(prefix="/record-permissions", tags=["record-permissions"])


@router.post("/", response_model=RecordPermissionResponse, status_code=status.HTTP_201_CREATED)
async def create_record_permission_route(
    perm_data: RecordPermissionCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    """
    Create a single RecordPermission entry. Per D-02: top-level /record-permissions resource.
    Per D-03: resource is derived server-side from record_entity_type (not in request body).
    Per D-06: single entry per request (no bulk).
    """
    return await create_record_permission(
        db=db,
        role_id=perm_data.role_id,
        record_entity_type=perm_data.record_entity_type,
        record_id=perm_data.record_id,
        action=perm_data.action,
        is_deny=perm_data.is_deny,
    )


@router.get("/", response_model=RecordPermissionListResponse)
async def list_record_permissions_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
    role_id: uuid.UUID | None = Query(None, description="Filter by role ID"),
    record_entity_type: str | None = Query(None, description="Filter by entity type (requires record_id)"),
    record_id: uuid.UUID | None = Query(None, description="Filter by record ID (requires record_entity_type)"),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    """
    List RecordPermission entries. Scoped by role_id and/or by record (entity_type + entity_id).
    Both-or-neither validation for record_entity_type + record_id (per CONTEXT.md discretion).
    Returns X-Total-Count header for pagination.
    """
    # Both-or-neither validation (per CONTEXT.md OpenCode's Discretion)
    if (record_entity_type is not None) != (record_id is not None):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="record_entity_type and record_id must be provided together",
        )

    items, total = await list_record_permissions(
        db=db,
        role_id=role_id,
        record_entity_type=record_entity_type,
        record_id=record_id,
        skip=skip,
        limit=limit,
    )
    response.headers["X-Total-Count"] = str(total)
    return RecordPermissionListResponse(
        items=[RecordPermissionResponse.model_validate(item) for item in items],
        total=total,
    )


@router.delete("/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_record_permission_route(
    permission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    """
    Delete a RecordPermission entry by its primary key (D-07).
    Returns 404 if the entry doesn't exist.
    """
    deleted = await delete_record_permission(db, permission_id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Record permission not found",
        )
    return None


@router.get("/effective", response_model=EffectivePermissionResponse)
async def get_effective_permissions_route(
    record_entity_type: str = Query(..., description="Entity type (one of: source, schema, object_type, attribute_type, object)"),
    record_id: uuid.UUID = Query(..., description="Record ID"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    """Return the resolved (inheritance-aware) effective permissions for a record,
    per role/action, with direct vs. inherited indication. Management view — returns
    entries for ALL roles, not just the calling user's own roles (see task 2 CRITICAL
    note: this must NOT be scoped by current_user's role_ids)."""
    try:
        items = await get_effective_permissions(
            db=db,
            entity_type=record_entity_type,
            record_id=record_id,
        )
    except ValueError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        )
    return EffectivePermissionResponse(items=items)
