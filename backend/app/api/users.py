import os
import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    UserCreate, UserUpdate, UserAdminResponse, UserPickerResponse,
    UserRoleAssign, ProfileUpdate, PasswordChange, PasswordReset, UserProfileResponse,
    GroupResponse,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import (
    get_current_user, require_permission, bump_token_version,
)
from app.services.rate_limit import limiter
from app.api.pagination import REFERENCE_DEFAULT_LIMIT, MAX_LIMIT
from app.services.user_admin_service import (
    create_user, list_users, list_users_picker, get_user, update_user, delete_user,
    assign_role_to_user, remove_role_from_user, count_effective_roles,
    resolve_effective_role_ids, reset_user_password,
)
from app.services.group_service import (
    add_user_to_group, remove_user_from_group, list_user_groups,
)
from app.services.user_profile_service import (
    get_or_create_profile, update_profile, change_password,
)
from app.services.password_policy_service import enforce_password_policy, get_password_policy

router = APIRouter(prefix="/users", tags=["users"])


@router.get("/password-policy")
async def get_password_policy_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """The active password policy, readable by every signed-in user so the
    password forms can show the rule before submitting."""
    return await get_password_policy(db)


@router.get("/me/profile", response_model=UserProfileResponse)
async def get_my_profile(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Get the authenticated user's own profile. Auto-creates if none exists."""
    profile = await get_or_create_profile(db, uuid.UUID(current_user.id))
    return profile


@router.patch("/me/profile", response_model=UserProfileResponse)
async def update_my_profile(
    profile_update: ProfileUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Update the authenticated user's own profile fields."""
    profile = await update_profile(db, uuid.UUID(current_user.id), profile_update)
    return profile


@router.post("/me/change-password", status_code=status.HTTP_200_OK)
@limiter.limit(os.getenv("RATE_LIMIT_PASSWORD_CHANGE", "5/minute"))
async def change_my_password(
    request: Request,
    response: Response,
    password_change: PasswordChange,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(get_current_user),
):
    """Change the authenticated user's password. Requires current_password verification.

    Revokes the token used to make this request (mirrors /auth/revoke) so a
    stolen 30-day token can't outlive a deliberate password change, and bumps
    the user's token version (D-09) so every OTHER live session of this user
    ends as well.
    """
    await enforce_password_policy(db, password_change.new_password)
    await change_password(db, uuid.UUID(current_user.id), password_change)

    # D-09 — the token epoch is the whole mechanism: raising token_version ends
    # every live token of this user (get_current_user and /auth/refresh both
    # compare the claim against the row), so no blocklist entry is written here.
    # That keeps a single-session password change free of orphan revocation keys.
    await bump_token_version(db, current_user.id)

    return {"message": "Password changed successfully"}


@router.post("/", response_model=UserAdminResponse, status_code=status.HTTP_201_CREATED)
async def create_user_route(
    user_data: UserCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "create"))
):
    await enforce_password_policy(db, user_data.password)
    return await create_user(
        db, user_data,
        actor_id=current_user.id,
        actor_is_superuser=current_user.is_superuser,
    )


@router.get("/", response_model=List[UserAdminResponse])
async def list_users_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    q: str = Query(None, description="Filter by username/email (case-insensitive substring match)"),
    role_id: uuid.UUID = Query(None, description="Roles-editor filter: only consider this role's direct assignment"),
    assigned: bool = Query(None, description="With role_id: True=only users assigned to it, False=only users not assigned"),
    response: Response = None,
):
    items, total = await list_users(db, skip=skip, limit=limit, q=q, role_id=role_id, assigned=assigned)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/picker", response_model=List[UserPickerResponse])
async def list_users_picker_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    q: str = Query(None, description="Filter by username/email (case-insensitive substring match)"),
    ids: List[uuid.UUID] = Query(None, description="Comma-separated/repeated user ids — returns exactly the requested users, ignoring q/skip/limit"),
    group_id: uuid.UUID = Query(None, description="Group-editor filter: only consider this group's membership"),
    assigned: bool = Query(None, description="With group_id: True=only members, False=only non-members"),
    response: Response = None,
):
    """Content-gated (objects:read) user picker for USER-typed attribute
    values (245-05 D-03) — deliberately NOT users:read like GET /users/,
    since editors/viewers assign USER values in the object editor without
    holding admin permissions."""
    items, total = await list_users_picker(db, skip=skip, limit=limit, q=q, ids=ids, group_id=group_id, assigned=assigned)
    response.headers["X-Total-Count"] = str(total)
    return items


@router.get("/{user_id}", response_model=UserAdminResponse)
async def get_user_route(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "read"))
):
    user = await get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    # WR-04: surface the same effective-role figure the self-lockout guard
    # actually authorizes against (direct + group-inherited), so the
    # frontend can match it instead of approximating from roles.length.
    response = UserAdminResponse.model_validate(user)
    response.effective_role_count = len(await resolve_effective_role_ids(db, user_id))
    return response


@router.patch("/{user_id}", response_model=UserAdminResponse)
async def update_user_route(
    user_id: uuid.UUID,
    user_update: UserUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "update"))
):
    if str(user_id) == str(current_user.id):
        if user_update.is_active is False:
            raise HTTPException(status_code=400, detail="Cannot deactivate your own account.")
        if user_update.is_superuser is False:
            raise HTTPException(status_code=400, detail="Cannot remove your own superuser flag.")
    user = await update_user(
        db, user_id, user_update,
        actor_id=current_user.id,
        actor_is_superuser=current_user.is_superuser,
    )
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.post("/{user_id}/reset-password", status_code=status.HTTP_200_OK)
async def reset_user_password_route(
    user_id: uuid.UUID,
    password_reset: PasswordReset,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "update")),
):
    """Superuser-only password reset for another account. Every live session
    of that user ends through the token version bump (D-09)."""
    if not current_user.is_superuser:
        raise HTTPException(status_code=403, detail="Only a superuser can reset a password.")
    if str(user_id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="Use the password change for your own account.")
    await enforce_password_policy(db, password_reset.new_password)
    if not await reset_user_password(db, user_id, password_reset.new_password, current_user.id):
        raise HTTPException(status_code=404, detail="User not found")
    await bump_token_version(db, str(user_id))
    return {"message": "Password reset successfully"}


@router.delete("/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_user_route(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "delete"))
):
    if str(user_id) == str(current_user.id):
        raise HTTPException(status_code=400, detail="Cannot delete your own account.")
    deleted = await delete_user(
        db, user_id, actor_id=current_user.id,
        actor_is_superuser=current_user.is_superuser,
    )
    if not deleted:
        raise HTTPException(status_code=404, detail="User not found")
    return None


@router.post("/{user_id}/roles", status_code=status.HTTP_201_CREATED)
async def assign_role_route(
    user_id: uuid.UUID,
    assign: UserRoleAssign,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "manage")),
):
    """Assign a role to a user. Requires users:manage. Returns 409 on duplicate."""
    # Verify user exists
    user = await get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return await assign_role_to_user(db, user_id, assign, actor_id=current_user.id)


@router.delete("/{user_id}/roles/{role_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_role_route(
    user_id: uuid.UUID,
    role_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "manage")),
):
    """Remove a role assignment. Self-lockout guard: non-superuser removing own
    LAST effective role (direct user_roles + group-inherited) → 403 (D-01–D-04,
    Pitfall 7 extended to group roles in 246-03). Superusers exempt; other
    users unrestricted."""
    # Self-lockout guard (D-01–D-04 / 246-03 Pitfall 7): count EFFECTIVE roles
    # (direct + group-inherited) so removing a role the user holds only via a
    # group is still blocked when it is their last access-granting role.
    if str(user_id) == str(current_user.id) and not current_user.is_superuser:
        count = await count_effective_roles(db, user_id)
        if count <= 1:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot remove your own last role.",
            )

    removed = await remove_role_from_user(db, user_id, role_id, actor_id=current_user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Role assignment not found.")
    return None


@router.get("/{user_id}/groups", response_model=List[GroupResponse])
async def list_user_groups_route(
    user_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("users", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(REFERENCE_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    response: Response = None,
):
    """Flat group list for a user (D-17) — join users_groups->groups, no N+1."""
    user = await get_user(db, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    groups = await list_user_groups(db, user_id, skip=skip, limit=limit)
    return groups


@router.post("/{user_id}/groups/{group_id}", status_code=status.HTTP_201_CREATED)
async def add_user_group_route(
    user_id: uuid.UUID,
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "manage")),
):
    """Add a user to a group (D-14, user-oriented direction). Gated by
    groups:manage (D-16). Returns 409 on duplicate membership."""
    membership = await add_user_to_group(db, group_id, user_id, actor_id=current_user.id)
    return {
        "id": str(membership.id),
        "group_id": str(membership.group_id),
        "user_id": str(membership.user_id),
    }


@router.delete("/{user_id}/groups/{group_id}", status_code=status.HTTP_204_NO_CONTENT)
async def remove_user_group_route(
    user_id: uuid.UUID,
    group_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("groups", "manage")),
):
    """Remove a user from a group (D-14, user-oriented direction). Gated by
    groups:manage (D-16). Self-lockout guard (Pitfall 7): a non-superuser
    removing their own membership loses every role that group grants at
    once, so simulate the removal (CR-01) rather than assuming it costs at
    most one role."""
    if str(user_id) == str(current_user.id) and not current_user.is_superuser:
        remaining = await resolve_effective_role_ids(db, user_id, exclude_group_id=group_id)
        if not remaining:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="Cannot remove your own last access-granting group.",
            )
    removed = await remove_user_from_group(db, group_id, user_id, actor_id=current_user.id)
    if not removed:
        raise HTTPException(status_code=404, detail="Group membership not found.")
    return None
