import uuid

from sqlalchemy import select, func, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.models.role import Role, UserRole
from app.models.group_role import GroupRole
from app.models.user_group import UserGroup
from app.models.permission import RolePermission, Permission
from app.schemas.crud import RoleCreate, RoleUpdate, RolePermissionAssign
from app.services.cache_service import invalidate_user_permissions_cache
from app.services.audit_service import audit_log, model_to_dict
from app.api.pagination import REFERENCE_DEFAULT_LIMIT


def _eager_role_query():
    return select(Role).options(
        selectinload(Role.permissions).selectinload(RolePermission.permission),
    )


async def create_role(db: AsyncSession, role_data: RoleCreate, actor_id: uuid.UUID | None = None) -> Role:
    db_role = Role(
        name=role_data.name,
        description=role_data.description,
    )
    db.add(db_role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role name already exists.",
        )
    await audit_log(db, "roles", db_role.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(db_role))
    await db.commit()
    result = await db.execute(_eager_role_query().where(Role.id == db_role.id))
    return result.scalar_one()


async def list_roles(
    db: AsyncSession, skip: int = 0, limit: int = REFERENCE_DEFAULT_LIMIT, q: str | None = None,
    group_id: uuid.UUID | None = None, assigned: bool | None = None,
) -> tuple[list[Role], int]:
    query = _eager_role_query()
    count_query = select(func.count()).select_from(Role)
    if q:
        search_filter = Role.name.ilike(f"%{q}%")
        query = query.where(search_filter)
        count_query = count_query.where(search_filter)
    # Group-editor "assigned"/"not assigned" filter (groups_roles) — mirrors
    # list_users/list_groups' role_id/assigned filter for the roles-editor.
    if group_id is not None and assigned is not None:
        group_exists = exists().where(
            GroupRole.role_id == Role.id, GroupRole.group_id == group_id
        )
        group_filter = group_exists if assigned else ~group_exists
        query = query.where(group_filter)
        count_query = count_query.where(group_filter)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    query = query.order_by(Role.is_system.desc(), Role.created_at.desc())
    result = await db.execute(query.offset(skip).limit(limit))
    return result.scalars().all(), total


async def get_role(db: AsyncSession, role_id: uuid.UUID) -> Role | None:
    result = await db.execute(_eager_role_query().where(Role.id == role_id))
    return result.scalar_one_or_none()


async def update_role(
    db: AsyncSession, role_id: uuid.UUID, role_update: RoleUpdate, actor_id: uuid.UUID | None = None
) -> Role | None:
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        return None

    # D-12: System role name lock — prevent renaming a system role
    if role.is_system and role_update.name is not None and role_update.name != role.name:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cannot rename a system role.",
        )

    old_values = model_to_dict(role)
    if role_update.name is not None:
        role.name = role_update.name
    if role_update.description is not None:
        role.description = role_update.description

    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role name already exists.",
        )
    await audit_log(db, "roles", role_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=model_to_dict(role))
    await db.commit()
    return await get_role(db, role_id)


async def delete_role(db: AsyncSession, role_id: uuid.UUID, actor_id: uuid.UUID | None = None) -> bool:
    result = await db.execute(select(Role).where(Role.id == role_id))
    role = result.scalar_one_or_none()
    if not role:
        return False
    old_values = model_to_dict(role)
    await audit_log(db, "roles", role_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    # The permissions collection may already be loaded in this session (e.g. by
    # get_role's eager load in the route). passive_deletes only guards the lazy
    # load path — a loaded collection gets disassociated via UPDATE
    # role_permissions SET role_id = NULL, which violates the NOT NULL
    # constraint. Delete the assignments explicitly so both loaded and
    # unloaded sessions behave identically. Same hazard for user_roles:
    # Role.users has no passive_deletes and UserRole.role_id is NOT NULL.
    user_assignments = await db.execute(
        select(UserRole).where(UserRole.role_id == role_id)
    )
    for user_assignment in user_assignments.scalars().all():
        await db.delete(user_assignment)
    assignments = await db.execute(
        select(RolePermission).where(RolePermission.role_id == role_id)
    )
    for assignment in assignments.scalars().all():
        await db.delete(assignment)
    await db.delete(role)
    await db.commit()
    return True


async def _affected_user_ids(db: AsyncSession, role_id: uuid.UUID) -> list:
    """User ids whose effective permissions flow through ``role_id``.

    A role reaches a user either directly (``user_roles``) or via a group
    membership (``users_groups`` -> ``groups_roles``). The effective permission
    resolver joins BOTH, so a role-permission change must invalidate the cache
    of both populations (Phase 303, SEAM-02/D-01) — otherwise a group member
    keeps a stale grant/deny after an administrative change.
    """
    direct = (
        await db.execute(select(UserRole.user_id).where(UserRole.role_id == role_id))
    ).scalars().all()
    group_members = (
        await db.execute(
            select(UserGroup.user_id)
            .join(GroupRole, GroupRole.group_id == UserGroup.group_id)
            .where(GroupRole.role_id == role_id)
        )
    ).scalars().all()
    return list({*direct, *group_members})


async def assign_permission_to_role(
    db: AsyncSession, role_id: uuid.UUID, assign: RolePermissionAssign, actor_id: uuid.UUID | None = None
) -> RolePermission:
    """Assign a permission to a role. Raises 404 if permission_id not found, 409 on duplicate."""

    # Verify permission exists (role existence checked by the router)
    perm_result = await db.execute(
        select(Permission).where(Permission.id == assign.permission_id)
    )
    if not perm_result.scalar_one_or_none():
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Permission not found",
        )

    # Upsert: the permission matrix's 3-state toggle (unassigned -> granted ->
    # denied -> unassigned) re-assigns an already-assigned permission to flip
    # its is_deny flag (granted -> denied) rather than removing it first — a
    # blind INSERT here hits the (role_id, permission_id) unique constraint
    # and 409s, leaving the UI stuck showing the old state.
    existing_result = await db.execute(
        select(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == assign.permission_id,
        )
    )
    db_assignment = existing_result.scalar_one_or_none()
    old_values = model_to_dict(db_assignment) if db_assignment else None
    action = "UPDATE" if db_assignment else "CREATE"
    if db_assignment:
        db_assignment.is_deny = assign.is_deny
    else:
        db_assignment = RolePermission(
            role_id=role_id,
            permission_id=assign.permission_id,
            is_deny=assign.is_deny,
        )
        db.add(db_assignment)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Permission already assigned to this role.",
        )
    await audit_log(db, "permissions", db_assignment.id, action, actor_id,
                    old_values=old_values, new_values=model_to_dict(db_assignment))
    await db.commit()
    await db.refresh(db_assignment)

    # D-05/D-06: Invalidate permission cache for all users holding this role,
    # directly or through a group membership (Phase 303, SEAM-02/D-01).
    affected_user_ids = await _affected_user_ids(db, role_id)
    if affected_user_ids:
        await invalidate_user_permissions_cache(affected_user_ids)

    return db_assignment


async def remove_permission_from_role(
    db: AsyncSession, role_id: uuid.UUID, permission_id: uuid.UUID, actor_id: uuid.UUID | None = None
) -> bool:
    """Remove a permission from a role. Returns False if the assignment doesn't exist."""
    result = await db.execute(
        select(RolePermission).where(
            RolePermission.role_id == role_id,
            RolePermission.permission_id == permission_id,
        )
    )
    assignment = result.scalar_one_or_none()
    if not assignment:
        return False

    old_values = model_to_dict(assignment)
    await audit_log(db, "permissions", assignment.id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(assignment)
    await db.commit()

    # D-05/D-06: Invalidate permission cache for all users holding this role,
    # directly or through a group membership (Phase 303, SEAM-02/D-01).
    affected_user_ids = await _affected_user_ids(db, role_id)
    if affected_user_ids:
        await invalidate_user_permissions_cache(affected_user_ids)

    return True
