import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, status, Query, Response
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import GroupCreate, GroupUpdate, GroupResponse, RoleListResponse
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.group_service import (
    create_group, list_groups, get_group, update_group, delete_group,
    add_user_to_group, remove_user_from_group,
    assign_group_role, remove_group_role,
    list_group_members, list_group_roles,
)
from app.services.user_admin_service import resolve_effective_role_ids
from app.models.user_group import UserGroup

router = APIRouter(prefix="/groups", tags=["groups"])


async def _user_is_member(db: AsyncSession, user_id, group_id) -> bool:
    """True when the user is currently a member of the group (for the
    246-03 Pitfall 7 self-lockout guard on group/group-role removal)."""
    result = await db.execute(
        select(UserGroup).where(
            UserGroup.user_id == user_id, UserGroup.group_id == group_id
        )
    )
    return result.scalar_one_or_none() is not None


async def _would_self_lockout(db, current_user, group_id, exclude_role_id=None) -> bool:
    """CR-01 fix (246-03 Pitfall 7): simulate the effective-role set the user
    would have AFTER this specific removal, instead of comparing the current
    total against a fixed threshold of 1 — the old approach assumed every
    guarded action costs at most one role, which is false for deleting a
    group or removing membership from one: either action drops EVERY role
    that group grants at once, so a user whose only access comes from a
    single group holding 2+ roles could still be locked out completely even
    though `count_effective_roles() <= 1` was False beforehand.

    `exclude_role_id=None` simulates losing the group's ENTIRE contribution
    (group deletion / membership removal). `exclude_role_id=<id>` simulates
    removing just that one groups_roles row (single role stripped from the
    group), which may still leave the user with the group's other roles.
    """
    if current_user.is_superuser:
        return False
    if not await _user_is_member(db, uuid.UUID(current_user.id), group_id):
        return False
    if exclude_role_id is not None:
        remaining = await resolve_effective_role_ids(
            db, uuid.UUID(current_user.id),
            exclude_group_role=(group_id, exclude_role_id),
        )
    else:
        remaining = await resolve_effective_role_ids(
            db, uuid.UUID(current_user.id), exclude_group_id=group_id,
        )
    return len(remaining) == 0


async def _enforce_self_membership_guard(db, current_user, target_user_id, group_id):
    """Self-lockout guard for member removal (246-03 Pitfall 7): a non-
    superuser removing their OWN membership loses every role that group
    grants at once, so simulate the removal (CR-01) rather than assuming it
    costs at most one role."""
    if str(target_user_id) == str(current_user.id):
        if await _would_self_lockout(db, current_user, group_id):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot remove your own last access-granting group.",
            )


async def _ensure_self_group_role_guard(db, current_user, group_id, exclude_role_id=None):
    """Self-lockout guard for group/group-role removal (246-03 Pitfall 7): a
    non-superuser who is a member of the affected group must keep at least
    one effective role after the removal actually completes. Deleting a
    group removes ALL of its groups_roles rows at once, so that call site
    must simulate losing the group's entire contribution
    (`exclude_role_id=None`); stripping a single role from a group only
    loses that one role, so that call site passes the specific
    `exclude_role_id` (CR-01)."""
    if await _would_self_lockout(db, current_user, group_id, exclude_role_id=exclude_role_id):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot remove your own last access-granting group.",
        )


@router.post("/", response_model=GroupResponse, status_code=status.HTTP_201_CREATED)
async def create_group_route(
    group_data: GroupCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "create"))
):
    # create_group raises HTTPException(409) itself on a duplicate name
    # (IntegrityError -> 409, mirrors language_service.create_language).
    return await create_group(db, group_data, current_user.id)


@router.get("/", response_model=List[GroupResponse])
async def list_groups_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    ids: List[uuid.UUID] = Query(None, description="Comma-separated/repeated group ids — returns exactly the requested groups, ignoring skip/limit"),
    q: str = Query(None, description="Filter by name (case-insensitive substring match)"),
    role_id: uuid.UUID = Query(None, description="Roles-editor filter: only consider this role's assignment"),
    assigned: bool = Query(None, description="With role_id: True=only groups holding it, False=only groups not holding it"),
    response: Response = None,
):
    items, total = await list_groups(db, skip=skip, limit=limit, ids=ids, q=q, role_id=role_id, assigned=assigned)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{group_id}", response_model=GroupResponse)
async def get_group_route(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "read")),
):
    group = await get_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.patch("/{group_id}", response_model=GroupResponse)
async def update_group_route(
    group_id: uuid.UUID,
    group_update: GroupUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "update"))
):
    # update_group raises HTTPException(409) itself on a duplicate name.
    group = await update_group(db, group_id, group_update, current_user.id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    return group


@router.delete("/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_group_route(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "delete"))
):
    """Delete a group. Self-lockout guard (Pitfall 7): a non-superuser member
    cannot delete a group that grants their only effective role."""
    await _ensure_self_group_role_guard(db, current_user, group_id)
    deleted = await delete_group(db, group_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Group not found")
    return None


@router.get("/{group_id}/members", response_model=List[UserResponse])
async def list_group_members_route(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    """Flat member list (D-17) — join users_groups->users, no N+1."""
    group = await get_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    members = await list_group_members(db, group_id, skip=skip, limit=limit)
    return members


@router.post("/{group_id}/members/{user_id}", status_code=status.HTTP_201_CREATED)
async def add_group_member_route(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "manage")),
):
    """Add a user to a group (D-14, group-oriented direction). Gated by
    groups:manage (D-16). Returns 409 on duplicate membership."""
    membership = await add_user_to_group(db, group_id, user_id, actor_id=current_user.id)
    return {
        "id": str(membership.id),
        "group_id": str(membership.group_id),
        "user_id": str(membership.user_id),
    }


@router.delete("/{group_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_member_route(
    group_id: uuid.UUID,
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "manage")),
):
    """Remove a user from a group (D-14). Gated by groups:manage (D-16).
    Self-lockout guard (Pitfall 7): a non-superuser removing their own
    membership must keep at least one effective role."""
    await _enforce_self_membership_guard(db, current_user, user_id, group_id)
    removed = await remove_user_from_group(db, group_id, user_id, actor_id=current_user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Group membership not found.")
    return None


@router.post("/{group_id}/roles/{role_id}", status_code=status.HTTP_201_CREATED)
async def assign_group_role_route(
    group_id: uuid.UUID,
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    """Assign a role to a group (groups_roles, D-15). Gated by roles:manage
    (D-16 — Rollen-Bearbeitung stays at roles:manage). Returns 409 on
    duplicate assignment. Invalidates all members' permission cache (D-19)."""
    group_role = await assign_group_role(db, group_id, role_id, actor_id=current_user.id)
    return {
        "id": str(group_role.id),
        "group_id": str(group_role.group_id),
        "role_id": str(group_role.role_id),
    }


@router.delete("/{group_id}/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_group_role_route(
    group_id: uuid.UUID,
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "manage")),
):
    """Remove a role from a group (D-15). Gated by roles:manage (D-16).
    Invalidates all members' permission cache (D-19). Self-lockout guard
    (Pitfall 7): a non-superuser member cannot strip their own last
    access-granting role."""
    await _ensure_self_group_role_guard(db, current_user, group_id, exclude_role_id=role_id)
    removed = await remove_group_role(db, group_id, role_id, actor_id=current_user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Group role assignment not found.")
    return None


@router.get("/{group_id}/roles", response_model=List[RoleListResponse])
async def list_group_roles_route(
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    """Flat role list for a group (D-17, group->roles direction) — the read
    counterpart to POST/DELETE /groups/{id}/roles/{role_id}. Gated by
    roles:read (roles data, matches GET /roles/)."""
    group = await get_group(db, group_id)
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")
    roles = await list_group_roles(db, group_id, skip=skip, limit=limit)
    return roles
