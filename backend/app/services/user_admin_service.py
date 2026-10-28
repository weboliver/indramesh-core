import uuid

from fastapi import HTTPException, status
from sqlalchemy import select, func, or_, union, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.role import UserRole, Role
from app.models.user_group import UserGroup
from app.models.group_role import GroupRole
from app.services.auth_service import get_password_hash
from app.services.cache_service import invalidate_user_permissions_cache
from app.services.audit_service import audit_log, model_to_dict
from app.schemas.crud import UserCreate, UserUpdate, UserRoleAssign


def _eager_query():
    return select(User).options(
        selectinload(User.profile),
        selectinload(User.roles).selectinload(UserRole.role),
    )


async def create_user(
    db: AsyncSession,
    user_data: UserCreate,
    actor_id: uuid.UUID | None = None,
    actor_is_superuser: bool = False,
) -> User:
    # SEC313-40 / EXT-05-S1 (D-09) — the superuser flag bypasses every gate, so
    # only a superuser may grant it. The guard lives here (not in the route) so
    # a future route cannot forget it; the actor's own flag is passed in
    # explicitly instead of re-reading the actor row (no second query).
    if user_data.is_superuser and not actor_is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a superuser can grant the superuser flag.",
        )
    db_user = User(
        username=user_data.username,
        email=user_data.email,
        password_hash=get_password_hash(user_data.password),
        is_active=user_data.is_active if user_data.is_active is not None else True,
        is_superuser=user_data.is_superuser if user_data.is_superuser is not None else False,
    )
    db.add(db_user)
    try:
        await db.flush()
    except IntegrityError:
        # WR-03: users.username / users.email are unique=True — a duplicate
        # raises here, not as an unhandled 500. Roll back so the audit_log
        # call below never writes into the failed transaction.
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already exists.",
        )
    await audit_log(db, "users", db_user.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(db_user))
    await db.commit()
    result = await db.execute(_eager_query().where(User.id == db_user.id))
    return result.scalar_one()


async def list_users(
    db: AsyncSession, skip: int = 0, limit: int = 200, q: str | None = None,
    role_id: uuid.UUID | None = None, assigned: bool | None = None,
) -> tuple[list[User], int]:
    query = _eager_query()
    count_query = select(func.count()).select_from(User)
    # D-06 — service-account users are filtered out of the user-management
    # surface (both result and count stay consistent)
    query = query.where(User.is_service_account == False)
    count_query = count_query.where(User.is_service_account == False)
    if q:
        search_filter = or_(User.username.ilike(f"%{q}%"), User.email.ilike(f"%{q}%"))
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    # Roles-editor "assigned"/"not assigned" filter: role_id picks which
    # role's direct assignment (user_roles, not group-inherited) to check;
    # both role_id and assigned must be given together, mirroring
    # group_service.list_groups' role_id/assigned filter.
    if role_id is not None and assigned is not None:
        role_exists = exists().where(
            UserRole.user_id == User.id, UserRole.role_id == role_id
        )
        role_filter = role_exists if assigned else ~role_exists
        query = query.where(role_filter)
        count_query = count_query.where(role_filter)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    return result.scalars().all(), total


async def list_users_picker(
    db: AsyncSession, skip: int = 0, limit: int = 200,
    q: str | None = None, ids: list[uuid.UUID] | None = None,
    group_id: uuid.UUID | None = None, assigned: bool | None = None,
) -> tuple[list[User], int]:
    """Content-gated picker source for USER attribute values (245-05 D-03):
    same shape as list_users (excludes service accounts), but a lean query
    with no eager profile/roles loads — callers only need id/username/email.
    When ids is present, returns exactly those users regardless of q/skip/
    limit, mirroring group_service.list_groups' read-back resolution filter.

    group_id/assigned is the group-editor "assigned"/"not assigned" member
    filter (users_groups) — the picker's editors-without-users:read path
    needs the same filter GET /users/ offers via role_id/assigned.
    """
    query = select(User).order_by(User.username)
    count_query = select(func.count()).select_from(User)
    query = query.where(User.is_service_account == False)
    count_query = count_query.where(User.is_service_account == False)

    if ids:
        query = query.where(User.id.in_(ids))
        count_query = count_query.where(User.id.in_(ids))
        count_result = await db.execute(count_query)
        total = count_result.scalar()
        result = await db.execute(query)
        return result.scalars().all(), total

    if q:
        search_filter = or_(User.username.ilike(f"%{q}%"), User.email.ilike(f"%{q}%"))
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)

    if group_id is not None and assigned is not None:
        group_exists = exists().where(
            UserGroup.user_id == User.id, UserGroup.group_id == group_id
        )
        group_filter = group_exists if assigned else ~group_exists
        query = query.where(group_filter)
        count_query = count_query.where(group_filter)

    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    return result.scalars().all(), total


async def get_user(db: AsyncSession, user_id: uuid.UUID) -> User | None:
    result = await db.execute(_eager_query().where(User.id == user_id))
    return result.scalar_one_or_none()


async def update_user(
    db: AsyncSession, user_id: uuid.UUID, user_update: UserUpdate,
    actor_id: uuid.UUID | None = None, actor_is_superuser: bool = False,
) -> User | None:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return None
    # D-06 / WR-04 — synthetic service users are never mutable from the
    # user-management surface; None maps to the router's 404 pattern exactly
    # like delete_user's False, so PATCH is_superuser/is_active elevation of
    # a service account is impossible. This guard is checked BEFORE the
    # superuser guard on purpose: the not-found signal is strictly more
    # restrictive than a 403, and the direct service-level caller in
    # tests/test_api_clients.py relies on it.
    if user.is_service_account:
        return None
    # SEC313-40 / EXT-05-S1 (D-09) — only a superuser may change the flag, in
    # either direction. A payload that repeats the current value is not a
    # change, so a non-superuser admin's ordinary edit (the user form always
    # sends ``is_superuser``) keeps working; a grant or a demotion is refused.
    if (
        user_update.is_superuser is not None
        and user_update.is_superuser != user.is_superuser
        and not actor_is_superuser
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a superuser can change the superuser flag.",
        )
    # TRI317-53 — a superuser account (name, email, active flag) is changed
    # only by a superuser, so a non-superuser admin cannot lock one out.
    if user.is_superuser and not actor_is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a superuser can change a superuser account.",
        )

    old_values = model_to_dict(user)
    if user_update.username is not None:
        user.username = user_update.username
    if user_update.email is not None:
        user.email = user_update.email
    if user_update.is_active is not None:
        user.is_active = user_update.is_active
    if user_update.is_superuser is not None:
        user.is_superuser = user_update.is_superuser

    try:
        await db.flush()
    except IntegrityError:
        # WR-03: renaming a user onto an existing username/email violates
        # the unique constraints — surface as 409, not an unhandled 500.
        # Roll back so the audit_log call below never writes into the
        # failed transaction (mirrors assign_role_to_user's pattern).
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Username or email already exists.",
        )
    await audit_log(db, "users", user_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=model_to_dict(user))
    await db.commit()
    return await get_user(db, user_id)


async def reset_user_password(
    db: AsyncSession, user_id: uuid.UUID, new_password: str, actor_id: uuid.UUID
) -> bool:
    """Set a new password for another user without knowing the old one.

    The route restricts this to superusers. Service users have no password
    login and stay untouchable here, like in update_user. The audit row
    records the change; the hash itself is masked by SENSITIVE_AUDIT_FIELDS.
    """
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user or user.is_service_account:
        return False
    old_values = model_to_dict(user)
    user.password_hash = get_password_hash(new_password)
    await db.flush()
    await audit_log(db, "users", user_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=model_to_dict(user))
    await db.commit()
    return True


async def delete_user(
    db: AsyncSession, user_id: uuid.UUID, actor_id: uuid.UUID | None = None,
    actor_is_superuser: bool = False,
) -> bool:
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        return False
    # D-06 — synthetic service users are never deletable from the
    # user-management surface; False maps to the router's 404 pattern
    if user.is_service_account:
        return False
    # TRI317-53 — only a superuser deletes a superuser account.
    if user.is_superuser and not actor_is_superuser:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only a superuser can delete a superuser account.",
        )
    old_values = model_to_dict(user)
    await audit_log(db, "users", user_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(user)
    await db.commit()
    return True


async def assign_role_to_user(
    db: AsyncSession, user_id: uuid.UUID, assign: UserRoleAssign, actor_id: uuid.UUID | None = None
) -> UserRole:
    """Assign a role to a user.

    Verifies user and role exist. Inserts UserRole; on IntegrityError
    (duplicate via uq_user_role constraint → 409). Invalidates the
    affected user's permission cache after commit.
    """
    # D-06 / WR-04 — verify the target user exists and is not a synthetic
    # service account BEFORE any role lookup/insert: an admin holding
    # users:manage must never be able to grant a role (e.g. Administrator)
    # to a service account created by an api_client.
    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_service_account:
        raise HTTPException(
            status_code=400, detail="Cannot assign roles to a service account"
        )

    # Verify role exists
    role_result = await db.execute(select(Role).where(Role.id == assign.role_id))
    role = role_result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    user_role = UserRole(user_id=user_id, role_id=assign.role_id)
    db.add(user_role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role already assigned to this user.",
        )
    await audit_log(db, "users", user_role.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(user_role))
    await db.commit()

    await invalidate_user_permissions_cache([user_id])
    return user_role


async def remove_role_from_user(
    db: AsyncSession, user_id: uuid.UUID, role_id: uuid.UUID, actor_id: uuid.UUID | None = None
) -> bool:
    """Remove a role assignment from a user. Returns False if not found."""
    result = await db.execute(
        select(UserRole).where(
            UserRole.user_id == user_id, UserRole.role_id == role_id
        )
    )
    user_role = result.scalar_one_or_none()
    if not user_role:
        return False

    old_values = model_to_dict(user_role)
    await audit_log(db, "users", user_role.id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(user_role)
    await db.commit()

    await invalidate_user_permissions_cache([user_id])
    return True


async def count_effective_roles(db: AsyncSession, user_id: uuid.UUID) -> int:
    """Number of distinct effective roles for a user: direct user_roles UNION
    group-inherited roles (users_groups -> groups_roles) — 246-03 Pitfall 7.

    The self-lockout guard consumes this so an actor cannot remove their last
    access-granting group/role even when that role is held via group
    membership rather than a direct user_roles row.
    """
    direct = select(UserRole.role_id).where(UserRole.user_id == user_id)
    grouped = (
        select(Role.id)
        .join(GroupRole, GroupRole.role_id == Role.id)
        .join(UserGroup, UserGroup.group_id == GroupRole.group_id)
        .where(UserGroup.user_id == user_id)
    )
    result = await db.execute(union(direct, grouped))
    return len(result.all())


async def resolve_effective_role_ids(
    db: AsyncSession,
    user_id: uuid.UUID,
    exclude_group_id: uuid.UUID | None = None,
    exclude_group_role: tuple[uuid.UUID, uuid.UUID] | None = None,
) -> set:
    """Set of distinct effective role ids for a user: direct user_roles UNION
    group-inherited roles, optionally simulating a removal before computing
    the union (CR-01 fix for 246-03 Pitfall 7).

    `exclude_group_id` drops the ENTIRE group's contribution — use this to
    simulate deleting a group outright or removing a user's membership from
    it, either of which removes every role that group grants at once (not
    just one). `exclude_group_role` drops a single (group_id, role_id)
    groups_roles row — use this to simulate removing one specific role from
    a group, which may still leave the user with other roles the group
    grants. The self-lockout guards use this to check what the user's
    effective roles WOULD BE after a specific action, instead of comparing
    the current total against a fixed threshold (which is wrong whenever a
    single action removes more than one role's worth of access).
    """
    direct = select(UserRole.role_id).where(UserRole.user_id == user_id)
    grouped = (
        select(Role.id)
        .join(GroupRole, GroupRole.role_id == Role.id)
        .join(UserGroup, UserGroup.group_id == GroupRole.group_id)
        .where(UserGroup.user_id == user_id)
    )
    if exclude_group_id is not None:
        grouped = grouped.where(UserGroup.group_id != exclude_group_id)
    if exclude_group_role is not None:
        ex_group_id, ex_role_id = exclude_group_role
        grouped = grouped.where(
            ~((UserGroup.group_id == ex_group_id) & (GroupRole.role_id == ex_role_id))
        )
    result = await db.execute(union(direct, grouped))
    return {row[0] for row in result.all()}
