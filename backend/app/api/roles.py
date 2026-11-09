import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    RoleCreate, RoleUpdate, RoleListResponse, RoleResponse, RolePermissionAssign,
    GroupResponse,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission, resolve_user_permissions
from app.services.user_admin_service import granted_permission_names
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.role_admin_service import (
    create_role, list_roles, get_role, update_role, delete_role,
    assign_permission_to_role, remove_permission_from_role,
)
from app.services.group_service import list_role_groups

router = APIRouter(prefix="/roles", tags=["roles"])


@router.post("/", response_model=RoleResponse, status_code=status.HTTP_201_CREATED)
async def create_role_route(
    role_data: RoleCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "create")),
):
    return await create_role(db, role_data, actor_id=current_user.id)


@router.get("/", response_model=List[RoleListResponse])
async def list_roles_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    q: str = Query(None, description="Filter by name (case-insensitive substring match)"),
    group_id: uuid.UUID = Query(None, description="Group-editor filter: only consider this group's assignment"),
    assigned: bool = Query(None, description="With group_id: True=only roles the group holds, False=only roles it doesn't"),
    response: Response = None,
):
    items, total = await list_roles(db, skip=skip, limit=limit, q=q, group_id=group_id, assigned=assigned)
    response.headers["X-Total-Count"] = str(total)

    # SESS-06 advisory projection (T-321-07): the SAME rule the enforcement
    # uses — `granted_permission_names` against the actor's resolved,
    # deny-aware, group-inclusive set — so the list can never advertise a role
    # that `assign_role_to_user` would refuse. A superuser bypasses it and sees
    # everything as assignable; for everyone else the actor's permissions are
    # resolved ONCE per request, never once per role.
    if current_user.is_superuser:
        return [
            RoleListResponse.model_validate(role).model_copy(update={"assignable": True})
            for role in items
        ]

    held = await resolve_user_permissions(current_user.id)
    return [
        RoleListResponse.model_validate(role).model_copy(
            update={"assignable": granted_permission_names([role]) <= held}
        )
        for role in items
    ]


@router.get("/{role_id}", response_model=RoleResponse)
async def get_role_route(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "read")),
):
    role = await get_role(db, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.patch("/{role_id}", response_model=RoleResponse)
async def update_role_route(
    role_id: uuid.UUID,
    role_update: RoleUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "update")),
):
    role = await update_role(db, role_id, role_update, actor_id=current_user.id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    return role


@router.delete("/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_role_route(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "delete")),
):
    role = await get_role(db, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    if role.is_system:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot delete a system role.",
        )
    await delete_role(db, role_id, actor_id=current_user.id)
    return None


@router.post("/{role_id}/permissions", status_code=status.HTTP_201_CREATED)
async def assign_permission_route(
    role_id: uuid.UUID,
    assign: RolePermissionAssign,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    # Verify the role exists
    role = await get_role(db, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    rp = await assign_permission_to_role(db, role_id, assign, actor_id=current_user.id)
    return {
        "id": str(rp.id),
        "role_id": str(rp.role_id),
        "permission_id": str(rp.permission_id),
        "is_deny": rp.is_deny,
    }


@router.delete("/{role_id}/permissions/{permission_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_permission_route(
    role_id: uuid.UUID,
    permission_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    removed = await remove_permission_from_role(db, role_id, permission_id, actor_id=current_user.id)
    if not removed:
        raise HTTPException(
            status_code=404,
            detail="Permission assignment not found",
        )
    return None


@router.get("/{role_id}/groups", response_model=List[GroupResponse])
async def list_role_groups_route(
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    """Flat group list for a role (D-17, role->groups direction) — the read
    counterpart to POST/DELETE /groups/{id}/roles/{role_id} from the role
    side. Gated by roles:read (the role editor surface is roles:manage, and
    the list needs roles:read to see which groups hold a role)."""
    role = await get_role(db, role_id)
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")
    groups = await list_role_groups(db, role_id, skip=skip, limit=limit)
    return groups
