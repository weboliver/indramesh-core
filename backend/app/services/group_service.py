import uuid
from typing import List

from fastapi import HTTPException, status
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, func, exists
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.group import Group
from app.models.user import User
from app.models.user_group import UserGroup
from app.models.group_role import GroupRole
from app.models.role import Role
from app.schemas.crud import GroupCreate, GroupUpdate
from app.schemas.auth import UserResponse
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    invalidate_user_permissions_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.services.audit_service import audit_log, model_to_dict

ENTITY_TYPE = "group"


async def create_group(
    db: AsyncSession, group_data: GroupCreate, actor_id: uuid.UUID
) -> Group:
    db_group = Group(name=group_data.name)
    db.add(db_group)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Group name '{group_data.name}' already exists.",
        )
    await audit_log(db, "groups", db_group.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(db_group))
    await db.commit()
    await db.refresh(db_group)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_group


async def list_groups(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    ids: List[uuid.UUID] | None = None, q: str | None = None,
    role_id: uuid.UUID | None = None, assigned: bool | None = None,
) -> tuple[List[Group], int]:
    # ids-filter (245-05 GROUP read-back resolution, parallel to the users
    # picker): when present, returns exactly the requested groups regardless
    # of skip/limit, and is never cached with the plain skip/limit list —
    # each distinct ids set gets its own cache key. q/role_id/assigned are
    # folded into the cache key too (build_list_cache_key hashes **params
    # generically) so each distinct filter combination gets its own entry.
    cache_key = build_list_cache_key(
        ENTITY_TYPE, skip=skip, limit=limit,
        ids=",".join(sorted(str(i) for i in ids)) if ids else None,
        q=q, role_id=str(role_id) if role_id else None, assigned=assigned,
    )
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]

    count_query = select(func.count()).select_from(Group)
    query = select(Group).order_by(Group.name)
    if ids:
        count_query = count_query.where(Group.id.in_(ids))
        query = query.where(Group.id.in_(ids))
        count_result = await db.execute(count_query)
        total = count_result.scalar()
        result = await db.execute(query)
    else:
        if q:
            search_filter = Group.name.ilike(f"%{q}%")
            query = query.where(search_filter)
            count_query = count_query.where(search_filter)
        # Roles-editor / group-editor "assigned"/"not assigned" filter (D-XX):
        # role_id selects which role's assignment to check; assigned=True/False
        # picks the EXISTS/NOT-EXISTS branch. Both must be given together —
        # role_id alone (assigned=None) is a no-op, matching list_users below.
        if role_id is not None and assigned is not None:
            role_exists = exists().where(
                GroupRole.group_id == Group.id, GroupRole.role_id == role_id
            )
            role_filter = role_exists if assigned else ~role_exists
            query = query.where(role_filter)
            count_query = count_query.where(role_filter)
        count_result = await db.execute(count_query)
        total = count_result.scalar()
        result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    await set_cached_with_tags(
        cache_key,
        {"items": jsonable_encoder(items), "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_group(db: AsyncSession, group_id: uuid.UUID) -> Group | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(group_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if group:
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(group),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return group


async def update_group(
    db: AsyncSession, group_id: uuid.UUID, group_update: GroupUpdate, actor_id: uuid.UUID
) -> Group | None:
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        return None

    old_values = model_to_dict(group)
    if group_update.name is not None:
        group.name = group_update.name

    new_values = model_to_dict(group)
    await audit_log(db, "groups", group_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Group name '{group_update.name}' already exists.",
        )
    await db.refresh(group)
    await invalidate_entity_cache(ENTITY_TYPE)
    return group


async def delete_group(
    db: AsyncSession, group_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(select(Group).where(Group.id == group_id))
    group = result.scalar_one_or_none()
    if not group:
        return False

    old_values = model_to_dict(group)
    await audit_log(db, "groups", group_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(group)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True


async def _group_member_ids(db: AsyncSession, group_id: uuid.UUID) -> list[uuid.UUID]:
    """All user_ids currently members of the group (single query, no N+1)."""
    result = await db.execute(
        select(UserGroup.user_id).where(UserGroup.group_id == group_id)
    )
    return [row[0] for row in result.all()]


async def _assert_actor_holds_group_roles(
    db: AsyncSession, group_id: uuid.UUID, actor_id: uuid.UUID
) -> None:
    """TRI317-50 privilege ceiling — now permission-based (SESS-06 / D-09).

    A membership hands out every role of the group, so the grant path shares
    the one ceiling helper with direct role assignment: a non-superuser may
    only grant roles whose granted permissions they hold themselves (direct or
    group-inherited). Comparing permission sets instead of role ids means a
    group membership and a direct assignment can never disagree.
    """
    from app.services.user_admin_service import assert_actor_may_grant_roles

    group_role_ids = list(
        (
            await db.execute(
                select(GroupRole.role_id).where(GroupRole.group_id == group_id)
            )
        ).scalars().all()
    )
    await assert_actor_may_grant_roles(
        db,
        group_role_ids,
        actor_id,
        detail="Cannot grant a group whose roles you do not hold yourself.",
    )


async def add_user_to_group(
    db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID, actor_id: uuid.UUID
) -> UserGroup:
    """Add a user to a group (246 D-14/D-19).

    Verifies group + user exist (404). Inserts UserGroup; on IntegrityError
    (duplicate via uq_user_group -> 409). Mirrors user_admin_service's
    assign_role_to_user shape, including the WR-04 service-account guard
    (groups are an access-control entity — a synthetic api_client user must
    never gain group membership that grants permissions). Invalidates the
    affected user's permission cache after commit (D-19).
    """
    group_result = await db.execute(select(Group).where(Group.id == group_id))
    group = group_result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    user_result = await db.execute(select(User).where(User.id == user_id))
    user = user_result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.is_service_account:
        raise HTTPException(
            status_code=400, detail="Cannot add a service account to a group"
        )
    await _assert_actor_holds_group_roles(db, group_id, actor_id)

    membership = UserGroup(user_id=user_id, group_id=group_id)
    db.add(membership)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="User is already a member.",
        )
    await audit_log(db, "users_groups", membership.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(membership))
    await db.commit()

    await invalidate_user_permissions_cache([user_id])
    return membership


async def remove_user_from_group(
    db: AsyncSession, group_id: uuid.UUID, user_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    """Remove a user from a group. Returns False if the membership is absent.

    Invalidates the affected user's permission cache after commit (D-19).
    """
    result = await db.execute(
        select(UserGroup).where(
            UserGroup.group_id == group_id, UserGroup.user_id == user_id
        )
    )
    membership = result.scalar_one_or_none()
    if not membership:
        return False

    old_values = model_to_dict(membership)
    await audit_log(db, "users_groups", membership.id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(membership)
    await db.commit()

    await invalidate_user_permissions_cache([user_id])
    return True


async def assign_group_role(
    db: AsyncSession, group_id: uuid.UUID, role_id: uuid.UUID, actor_id: uuid.UUID
) -> GroupRole:
    """Assign a role to a group (groups_roles, D-15).

    Verifies group + role exist (404). Inserts GroupRole; on IntegrityError
    (duplicate via uq_group_role -> 409). Invalidates the permission cache
    of ALL current group members after commit (D-19 — a group-role change
    alters every member's effective permission set).
    """
    group_result = await db.execute(select(Group).where(Group.id == group_id))
    group = group_result.scalar_one_or_none()
    if not group:
        raise HTTPException(status_code=404, detail="Group not found")

    role_result = await db.execute(select(Role).where(Role.id == role_id))
    role = role_result.scalar_one_or_none()
    if not role:
        raise HTTPException(status_code=404, detail="Role not found")

    group_role = GroupRole(group_id=group_id, role_id=role_id)
    db.add(group_role)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Role already assigned to this group.",
        )
    await audit_log(db, "groups_roles", group_role.id, "CREATE", actor_id,
                    old_values=None, new_values=model_to_dict(group_role))
    await db.commit()

    member_ids = await _group_member_ids(db, group_id)
    await invalidate_user_permissions_cache(member_ids)
    # list_groups' role_id/assigned filter (roles-editor group picker) reads
    # through the groups_roles join, so a cached "groups assigned to role X"
    # page would otherwise stay stale until its TTL expires.
    await invalidate_entity_cache(ENTITY_TYPE)
    return group_role


async def remove_group_role(
    db: AsyncSession, group_id: uuid.UUID, role_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    """Remove a role from a group. Returns False if the assignment is absent.

    Invalidates the permission cache of ALL current group members (D-19).
    """
    result = await db.execute(
        select(GroupRole).where(
            GroupRole.group_id == group_id, GroupRole.role_id == role_id
        )
    )
    group_role = result.scalar_one_or_none()
    if not group_role:
        return False

    old_values = model_to_dict(group_role)
    await audit_log(db, "groups_roles", group_role.id, "DELETE", actor_id,
                    old_values=old_values, new_values=None)
    await db.delete(group_role)
    await db.commit()

    member_ids = await _group_member_ids(db, group_id)
    await invalidate_user_permissions_cache(member_ids)
    await invalidate_entity_cache(ENTITY_TYPE)
    return True


async def list_group_roles(
    db: AsyncSession, group_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[Role]:
    """Flat role list for a group (groups_roles) — List[Role] via join, no N+1.

    Single join query over groups_roles->roles (D-17). Callers gate on
    roles:read. RoleListResponse serializes id/name/is_system/timestamps
    from_attributes, so raw Role rows serialize cleanly.
    """
    query = (
        select(Role)
        .join(GroupRole, GroupRole.role_id == Role.id)
        .where(GroupRole.group_id == group_id)
        .order_by(Role.is_system.desc(), Role.name)
    )
    result = await db.execute(query.offset(skip).limit(limit))
    return list(result.scalars().all())


async def list_role_groups(
    db: AsyncSession, role_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[Group]:
    """Flat group list for a role (groups_roles) — List[Group] via join, no N+1.

    Single join groups_roles->groups (D-17). Callers gate on roles:read.
    GroupResponse serializes id/name/timestamps from_attributes directly.
    """
    query = (
        select(Group)
        .join(GroupRole, GroupRole.group_id == Group.id)
        .where(GroupRole.role_id == role_id)
        .order_by(Group.name)
    )
    result = await db.execute(query.offset(skip).limit(limit))
    return list(result.scalars().all())


async def list_group_members(
    db: AsyncSession, group_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[UserResponse]:
    """Flat member list (List[UserResponse]) via join users_groups->users.

    Single join query — no N+1 (D-17). Callers gate on groups:read.
    Builds UserResponse explicitly (UserResponse.id is EscapedStr and rejects
    the ORM UUID — same manual-construction pattern as auth_service).
    """
    query = (
        select(User)
        .join(UserGroup, UserGroup.user_id == User.id)
        .where(UserGroup.group_id == group_id)
        .order_by(User.username)
    )
    result = await db.execute(query.offset(skip).limit(limit))
    users = result.scalars().all()
    return [
        UserResponse(
            id=str(u.id), username=u.username, email=u.email,
            is_active=u.is_active, is_superuser=u.is_superuser,
        )
        for u in users
    ]


async def list_user_groups(
    db: AsyncSession, user_id: uuid.UUID, skip: int = 0, limit: int = 100
) -> list[Group]:
    """Flat group list (List[GroupResponse]) via join users_groups->groups.

    Single join query — no N+1 (D-17). Callers gate on users:read.
    """
    query = (
        select(Group)
        .join(UserGroup, UserGroup.group_id == Group.id)
        .where(UserGroup.user_id == user_id)
        .order_by(Group.name)
    )
    result = await db.execute(query.offset(skip).limit(limit))
    return list(result.scalars().all())
