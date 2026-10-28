import uuid

from sqlalchemy import text, select, func, bindparam, union
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from fastapi import HTTPException, status

from app.api.pagination import REFERENCE_DEFAULT_LIMIT
from app.db import async_session_maker
from app.models import RecordEntityType, UserRole, RecordPermission, GroupRole, UserGroup
from app.models.role import Role
from app.models.source import Source
from app.models.schema import Schema
from app.models.object_type import ObjectType
from app.models.attribute_type import AttributeType
from app.models.object import Object
from app.services import cache_service
from app.schemas.crud import EffectivePermissionItem

CACHE_TTL_RECORD_PERMISSION = 60  # 60s TTL matching user_permissions precedent (D-03/D-06 area)
VALID_RECORD_ENTITY_TYPES = {e.value for e in RecordEntityType}

# Cascade order: source -> schema -> object_type -> object. A permission change on an
# ancestor changes the resolved visibility of every descendant type's list endpoint
# (build_record_permission_filter walks the ancestor chain), so their list caches must
# be busted too. attribute_type is standalone (no cascade).
_ENTITY_TYPE_DESCENDANTS = {
    "source": ["schema", "object_type", "object"],
    "schema": ["object_type", "object"],
    "object_type": ["object"],
    "object": [],
    "attribute_type": [],
}

# D-08 / SEC313-14 / EXT-04-H1: the record-permission UI stores the vocabulary
# `read`, `write`, `delete`, `*` (`RecordPermissionCreate.action`). Routes ask
# for the finer-grained `create`/`update` actions, so a stored `write` DENY
# never met an `update` gate. `write` is the umbrella for **create and update**:
# expanding the requested action here makes every already-stored row take effect
# without a data migration.
#
# `delete` stays a singleton on purpose. The stored vocabulary lists `delete`
# separately, so D-08 scopes the umbrella to create+update only — broadening it
# to `delete` would silently turn a write DENY into a delete DENY. Do not
# "simplify" this table by folding delete into `write`.
_ACTION_ALIASES: dict[str, tuple[str, ...]] = {
    "read": ("read",),
    "write": ("write",),
    "delete": ("delete",),
    "update": ("update", "write"),
    "create": ("create", "write"),
}


def _expand_action(action: str) -> tuple[str, ...]:
    """Accepted stored actions for a requested action (D-08, one normalisation).

    Both resolvers (`_resolve_record_permission_from_db` and
    `_level_denied_sql`) call this, so the in-Python decision and the SQL
    fragment can never drift apart.
    """
    try:
        return _ACTION_ALIASES[action]
    except KeyError:
        raise ValueError(f"Unsupported action: {action}") from None


def _build_record_permission_cache_key(
    user_id: str,
    entity_type: str,
    record_id: uuid.UUID,
    action: str,
) -> str:
    """Cache key scoped to user_id so it varies per user, not per role.

    The resolved result aggregates across all of the user's roles, so the cache
    entry must vary with the user. Pattern: cache:record_permission:{user_id}:{entity_type}:{record_id}:{action}
    """
    return f"cache:record_permission:{user_id}:{entity_type}:{record_id}:{action}"


def _build_record_permission_tag(entity_type: str, record_id: uuid.UUID) -> str:
    """Tag for targeted invalidation of all cache entries for a given record.

    Pattern: tag:record_permission:{entity_type}:{record_id}
    """
    return f"tag:record_permission:{entity_type}:{record_id}"


async def _resolve_user_role_ids(db: AsyncSession, user_id: str) -> list[uuid.UUID]:
    """Resolve a user's effective role ids: direct user_roles UNION
    group-inherited roles (users_groups -> groups_roles).

    D-18 correction (246-03): record-level ALLOW/DENY must inherit through
    groups, so BOTH role_ids resolution sites (resolve_record_permission and
    build_record_permission_filter) consult the union — never the direct
    user_roles-only join. UNION dedupes the two sources.
    """
    direct = select(UserRole.role_id).where(UserRole.user_id == user_id)
    grouped = (
        select(Role.id)
        .join(GroupRole, GroupRole.role_id == Role.id)
        .join(UserGroup, UserGroup.group_id == GroupRole.group_id)
        .where(UserGroup.user_id == user_id)
    )
    result = await db.execute(union(direct, grouped))
    return [row[0] for row in result.all()]


async def _resolve_record_permission_from_db(
    db: AsyncSession,
    role_ids: list[uuid.UUID],
    entity_type: str,
    record_id: uuid.UUID,
    action: str,
) -> bool | None:
    """Resolve effective record-level permission from the database.

    For cascade-capable levels (source/schema/object_type/object): walks the
    container-hierarchy ancestor chain via a recursive CTE.

    For attribute_type (RLR-08): single-level query, no cascade.

    Returns:
        True  -> explicit ALLOW at the most-specific level (RLR-03)
        False -> explicit DENY at any level (RLR-02: global deny-override wins)
        None  -> no record-level entry at any level (permissive default: defer to type-level RBAC)
    """
    # D-08: one expansion, consumed by the SQL predicate below (and by the
    # `has_explicit` tie-break after it). `:actions` carries the accepted set.
    accepted_actions = _expand_action(action)

    if entity_type == "attribute_type":
        # RLR-08: AttributeType is standalone — no cascade, single-level query
        result = await db.execute(
            text("""
                SELECT rp.is_deny, rp.action
                FROM record_permissions rp
                WHERE rp.record_entity_type = 'attribute_type'
                  AND rp.record_id = :record_id
                  AND rp.role_id = ANY(:role_ids)
                  AND (rp.action = ANY(CAST(:actions AS VARCHAR[])) OR rp.action = '*')
            """),
            {
                "record_id": record_id,
                "role_ids": role_ids,
                "actions": list(accepted_actions),
            }
        )
        rows = result.all()
    else:
        # Cascade levels (source/schema/object_type/object): recursive-CTE ancestor walk.
        # Uses CAST() in the anchor for explicit type inference (asyncpg requires this
        # for recursive CTEs), and a single recursive branch with CASE + LEFT JOINs
        # (PostgreSQL rejects multiple UNION ALL recursive branches with different
        # FROM clauses referencing the CTE — see executor log 2026-07-15).
        result = await db.execute(
            text("""
                WITH RECURSIVE ancestor_chain AS (
                    SELECT
                        CAST(:record_id AS UUID) AS id,
                        CAST(:entity_type AS VARCHAR) AS entity_type,
                        0 AS depth

                    UNION ALL

                    SELECT
                        CASE
                            WHEN ac.entity_type = 'object'       THEN o.object_type_id
                            WHEN ac.entity_type = 'object_type'  THEN ot.schema_id
                            WHEN ac.entity_type = 'schema'       THEN s.source_id
                        END,
                        CASE
                            WHEN ac.entity_type = 'object'       THEN 'object_type'
                            WHEN ac.entity_type = 'object_type'  THEN 'schema'
                            WHEN ac.entity_type = 'schema'       THEN 'source'
                        END,
                        ac.depth + 1
                    FROM ancestor_chain ac
                    LEFT JOIN objects o      ON o.id = ac.id AND ac.entity_type = 'object'
                    LEFT JOIN object_types ot ON ot.id = ac.id AND ac.entity_type = 'object_type'
                    LEFT JOIN schemas s      ON s.id = ac.id AND ac.entity_type = 'schema'
                    WHERE ac.depth < 100
                      AND ac.entity_type IN ('object', 'object_type', 'schema')
                )
                SELECT rp.is_deny, rp.action, ac.depth
                FROM ancestor_chain ac
                LEFT JOIN record_permissions rp
                    ON rp.record_entity_type = ac.entity_type
                    AND rp.record_id = ac.id
                    AND rp.role_id = ANY(CAST(:role_ids AS UUID[]))
                    AND (rp.action = ANY(CAST(:actions AS VARCHAR[])) OR rp.action = '*')
                ORDER BY ac.depth ASC
            """),
            {
                "record_id": record_id,
                "entity_type": entity_type,
                "role_ids": role_ids,
                "actions": list(accepted_actions),
            }
        )
        rows = result.all()

    # Post-processing (reconciles D-02 "specific beats wildcard at same level"
    # with RLR-02 "deny-override wins across levels"):
    #
    # 1. Collapse to one decision per depth. For each depth present, if a row
    #    with an accepted explicit action (D-08: `update` accepts `update` and
    #    `write`, `create` accepts `create` and `write`) exists at that depth,
    #    that row's is_deny is the level's decision — ignore any action = '*'
    #    row at the same depth. Only use the wildcard row's is_deny when no
    #    explicit-action row exists at that depth. When two DIFFERENT roles both
    #    hold an explicit-action row at the same depth with conflicting is_deny
    #    values (CR-01), the explicit DENY always wins over the explicit ALLOW,
    #    independent of which row PostgreSQL returns first (row order across
    #    roles via `rp.role_id = ANY(:role_ids)` is not deterministic).
    # 2. Global deny-override (RLR-02): if any level's collapsed decision is
    #    is_deny = True, return False immediately — regardless of depth.
    # 3. Most-specific ALLOW (RLR-03): scan collapsed decisions in depth ASC
    #    order; return True on the first level with is_deny = False.
    # 4. If no level produced a collapsed decision at all, return None.
    depth_decisions: dict[int, tuple[bool, bool]] = {}
    for row in rows:
        is_deny, row_action = row[0], row[1]
        depth = row[2] if len(row) > 2 else 0

        # Skip rows where no RecordPermission matched (LEFT JOIN miss).
        # The CTE always returns a row for each depth in the ancestor chain,
        # but rp.is_deny is NULL when no record_permissions entry exists at
        # that depth — only process rows with an actual record permission match.
        if is_deny is None:
            continue

        has_explicit = row_action in accepted_actions

        if depth not in depth_decisions:
            depth_decisions[depth] = (is_deny, has_explicit)
        else:
            _, existing_has_explicit = depth_decisions[depth]
            if has_explicit and not existing_has_explicit:
                # Explicit beats wildcard at same level (D-02)
                depth_decisions[depth] = (is_deny, has_explicit)
            elif has_explicit and existing_has_explicit and is_deny:
                # CR-01: two roles both hold an explicit-action row at the same
                # depth with conflicting is_deny values — DENY always wins over
                # ALLOW, regardless of DB row-processing order.
                depth_decisions[depth] = (is_deny, has_explicit)

    if not depth_decisions:
        # No record-level entries at any level — permissive default
        return None

    # Step 2: Global deny-override (RLR-02)
    for depth, (is_deny, _) in depth_decisions.items():
        if is_deny:
            return False

    # Step 3: Most-specific ALLOW (RLR-03)
    for depth in sorted(depth_decisions.keys()):
        is_deny, _ = depth_decisions[depth]
        if not is_deny:
            return True

    raise AssertionError("Unreachable: depth_decisions must resolve to True or False for every entry")


async def resolve_record_permission(
    user_id: str,
    entity_type: str,
    record_id: uuid.UUID,
    action: str,
) -> bool | None:
    """Resolve effective record-level permission for a user, with caching.

    Cache-first (RLR-06):
    1. Build cache key via _build_record_permission_cache_key
    2. Try cache_service.get_cached() — on hit, unwrap and return cached result
    3. On miss: open DB session, resolve user's role_ids, call
       _resolve_record_permission_from_db()
    4. Write wrapped result ({"result": True/False/None}) to Redis cache with
       60s TTL and tag for targeted invalidation
    5. Return result

    Returns:
        True  -> explicit ALLOW (most-specific level wins)
        False -> explicit DENY at any level (deny-override wins)
        None  -> no record-level entries (permissive default: defer to type-level RBAC)
    """
    if entity_type not in VALID_RECORD_ENTITY_TYPES:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    cache_key = _build_record_permission_cache_key(user_id, entity_type, record_id, action)

    # Cache hit check — wrapped in dict to distinguish None from cache miss
    # (cache_service.get_cached() returns Python None both when the key is absent
    # AND when correctly round-tripping a cached JSON "null")
    cached = await cache_service.get_cached(cache_key)
    if cached is not None:
        return cached.get("result")

    # Cache miss: resolve from DB
    async with async_session_maker() as db:
        # Resolve user's role IDs across direct user_roles + group-inherited
        # roles (D-18 correction — record ALLOW/DENY inherits through groups)
        role_ids = await _resolve_user_role_ids(db, user_id)

        if not role_ids:
            # User has no roles — no record permissions can apply
            return None

        resolved = await _resolve_record_permission_from_db(
            db=db,
            role_ids=role_ids,
            entity_type=entity_type,
            record_id=record_id,
            action=action,
        )

    # Cache the wrapped result with tag for targeted invalidation
    tag = _build_record_permission_tag(entity_type, record_id)
    await cache_service.set_cached_with_tags(
        cache_key,
        {"result": resolved},
        tags=[tag],
        ttl=CACHE_TTL_RECORD_PERMISSION,
        entity_type="record_permission",
        is_list=False,
    )

    return resolved


_EXPANDABLE_ACTIONS = ("read", "write", "delete")


async def get_effective_permissions(
    db: AsyncSession,
    entity_type: str,
    record_id: uuid.UUID,
) -> list[EffectivePermissionItem]:
    """Resolve the full set of effective record-level permissions for a record.

    Management view (no user_id): unlike `resolve_record_permission`, this is
    NOT scoped to a calling user's role_ids — it returns entries for ALL roles
    across the ancestor chain, so an admin managing e.g. the "Editor" role's
    record-level entries sees them regardless of which roles the admin
    personally holds. Access control is enforced by the route's
    `require_permission("roles", "manage")` gate, not by role scoping here.

    Deduplicates per (role_id, action): keeps the most-specific (lowest depth)
    entry when the same role+action appears at multiple levels; depth=0 is
    always direct and wins over any inherited entry. Wildcard action='*'
    entries are expanded into read/write/delete entries per role.

    Does NOT cache — low-traffic management view queried once per dialog open.
    """
    if entity_type not in VALID_RECORD_ENTITY_TYPES:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    if entity_type == "attribute_type":
        # RLR-08: AttributeType is standalone — no cascade, direct rows only.
        result = await db.execute(
            text("""
                SELECT rp.role_id, rp.action, rp.is_deny
                FROM record_permissions rp
                WHERE rp.record_entity_type = 'attribute_type'
                  AND rp.record_id = :record_id
            """),
            {"record_id": record_id}
        )
        rows = [
            (row.role_id, row.action, row.is_deny, "attribute_type", record_id, 0)
            for row in result.all()
        ]
    else:
        result = await db.execute(
            text("""
                WITH RECURSIVE ancestor_chain AS (
                    SELECT
                        CAST(:record_id AS UUID) AS id,
                        CAST(:entity_type AS VARCHAR) AS entity_type,
                        0 AS depth

                    UNION ALL

                    SELECT
                        CASE
                            WHEN ac.entity_type = 'object'       THEN o.object_type_id
                            WHEN ac.entity_type = 'object_type'  THEN ot.schema_id
                            WHEN ac.entity_type = 'schema'       THEN s.source_id
                        END,
                        CASE
                            WHEN ac.entity_type = 'object'       THEN 'object_type'
                            WHEN ac.entity_type = 'object_type'  THEN 'schema'
                            WHEN ac.entity_type = 'schema'       THEN 'source'
                        END,
                        ac.depth + 1
                    FROM ancestor_chain ac
                    LEFT JOIN objects o      ON o.id = ac.id AND ac.entity_type = 'object'
                    LEFT JOIN object_types ot ON ot.id = ac.id AND ac.entity_type = 'object_type'
                    LEFT JOIN schemas s      ON s.id = ac.id AND ac.entity_type = 'schema'
                    WHERE ac.depth < 100
                      AND ac.entity_type IN ('object', 'object_type', 'schema')
                )
                SELECT rp.role_id, rp.action, rp.is_deny, ac.entity_type AS source_entity_type,
                       ac.id AS source_record_id, ac.depth
                FROM ancestor_chain ac
                LEFT JOIN record_permissions rp
                    ON rp.record_entity_type = ac.entity_type
                    AND rp.record_id = ac.id
                ORDER BY ac.depth ASC
            """),
            {
                "record_id": record_id,
                "entity_type": entity_type,
            }
        )
        rows = result.all()

    # Wildcard expansion + collect all rows keyed by (role_id, action). Tracks
    # is_explicit (True for a literal action row, False for a wildcard-expanded
    # one) so the dedup step below can honor D-02 ("explicit action beats
    # wildcard at the same depth") — the same tie-break
    # `_resolve_record_permission_from_db` applies via `has_explicit`.
    expanded_rows: list[tuple[uuid.UUID, str, bool, str, uuid.UUID, int, bool]] = []
    for row in rows:
        role_id, action, is_deny, source_entity_type, source_record_id, depth = row
        if role_id is None:
            # LEFT JOIN miss — no record_permissions entry at this ancestor level
            continue
        if action == "*":
            for expanded_action in _EXPANDABLE_ACTIONS:
                expanded_rows.append(
                    (role_id, expanded_action, is_deny, source_entity_type, source_record_id, depth, False)
                )
        else:
            expanded_rows.append((role_id, action, is_deny, source_entity_type, source_record_id, depth, True))

    # Dedupe per (role_id, action): keep the most-specific (lowest depth) entry;
    # at equal depth, an explicit-action row wins over a wildcard-expanded one (D-02).
    best: dict[tuple[uuid.UUID, str], tuple[bool, str, uuid.UUID, int, bool]] = {}
    for role_id, action, is_deny, source_entity_type, source_record_id, depth, is_explicit in expanded_rows:
        key = (role_id, action)
        current = best.get(key)
        if (
            current is None
            or depth < current[3]
            or (depth == current[3] and is_explicit and not current[4])
        ):
            best[key] = (is_deny, source_entity_type, source_record_id, depth, is_explicit)

    items: list[EffectivePermissionItem] = []
    for (role_id, action), (is_deny, source_entity_type, source_record_id, depth, _is_explicit) in best.items():
        is_direct = depth == 0
        items.append(
            EffectivePermissionItem(
                role_id=role_id,
                action=action,
                is_deny=is_deny,
                is_direct=is_direct,
                source_entity_type=None if is_direct else source_entity_type,
                source_record_id=None if is_direct else source_record_id,
            )
        )

    return items


_ENTITY_TYPE_TO_RESOURCE = {
    "source": "sources",
    "schema": "schemas",
    "object_type": "object-types",
    "attribute_type": "attribute-types",
    "object": "objects",
}

_ENTITY_TYPE_TO_MODEL = {
    "source": Source,
    "schema": Schema,
    "object_type": ObjectType,
    "attribute_type": AttributeType,
    "object": Object,
}


async def invalidate_record_permission_cache(entity_type: str, record_id: uuid.UUID) -> None:
    """Invalidate record permission cache tags for a specific record.

    Tag-based invalidation: deletes all cache entries tagged with the
    record's tag, plus the tag itself. Wired to HTTP endpoints in Phase 131.

    Also busts the entity's own list cache and every descendant type's list
    cache (see _ENTITY_TYPE_DESCENDANTS), since build_record_permission_filter
    resolves visibility through the ancestor chain — a DENY/ALLOW change on
    this record can flip what a descendant's list endpoint returns.

    Note: this does not invalidate other *records'* per-record
    resolve_record_permission() cache entries for descendants (e.g. a
    specific object's cached GET/PATCH/DELETE decision) — those expire on
    their own CACHE_TTL_RECORD_PERMISSION (60s), an accepted eventual-
    consistency bound rather than a per-descendant-record DB walk at write time.
    """
    tag = _build_record_permission_tag(entity_type, record_id)
    await cache_service.invalidate_by_tags([tag])
    await cache_service.invalidate_entity_cache(entity_type)
    for descendant in _ENTITY_TYPE_DESCENDANTS.get(entity_type, []):
        await cache_service.invalidate_entity_cache(descendant)


async def create_record_permission(
    db: AsyncSession,
    role_id: uuid.UUID,
    record_entity_type: str,
    record_id: uuid.UUID,
    action: str = "read",
    is_deny: bool = False,
) -> RecordPermission:
    """Create a RecordPermission row.

    Validates entity_type, derives resource server-side (D-03),
    validates target record existence (D-06/D-07), catches duplicate (409).
    """
    if record_entity_type not in VALID_RECORD_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid record_entity_type: {record_entity_type}",
        )

    # D-03: Derive resource server-side
    resource = _ENTITY_TYPE_TO_RESOURCE[record_entity_type]

    # D-06/D-07: Validate record existence
    model_class = _ENTITY_TYPE_TO_MODEL.get(record_entity_type)
    if model_class:
        result = await db.execute(
            select(model_class.id).where(model_class.id == record_id)
        )
        if result.scalar_one_or_none() is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"{record_entity_type.replace('_', ' ').title()} not found",
            )

    db_assignment = RecordPermission(
        role_id=role_id,
        record_entity_type=record_entity_type,
        record_id=record_id,
        resource=resource,
        action=action,
        is_deny=is_deny,
    )
    db.add(db_assignment)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Record permission already exists for this role, record, and action",
        )
    await db.refresh(db_assignment)

    # RLA-04: Invalidate cache for this record
    await invalidate_record_permission_cache(record_entity_type, record_id)

    return db_assignment


async def delete_record_permission(
    db: AsyncSession, permission_id: uuid.UUID
) -> bool:
    """Delete a RecordPermission row by its primary key.

    Returns False if not found. Invalidates cache on success.
    """
    result = await db.execute(
        select(RecordPermission).where(RecordPermission.id == permission_id)
    )
    perm = result.scalar_one_or_none()
    if not perm:
        return False

    entity_type = perm.record_entity_type
    perm_record_id = perm.record_id

    await db.delete(perm)
    await db.commit()

    # RLA-04: Invalidate cache for this record
    await invalidate_record_permission_cache(entity_type, perm_record_id)

    return True


async def list_record_permissions(
    db: AsyncSession,
    role_id: uuid.UUID | None = None,
    record_entity_type: str | None = None,
    record_id: uuid.UUID | None = None,
    skip: int = 0,
    limit: int = REFERENCE_DEFAULT_LIMIT,
) -> tuple[list[RecordPermission], int]:
    """List RecordPermission rows with optional filters.

    Supports filtering by role_id and/or by record (entity_type + record_id).
    Both-or-neither validation for record_entity_type + record_id.
    """
    # Both-or-neither validation
    if (record_entity_type is None) != (record_id is None):
        raise ValueError(
            "record_entity_type and record_id must be provided together"
        )

    # Count query
    count_query = select(func.count()).select_from(RecordPermission)
    # Data query
    data_query = select(RecordPermission)

    if role_id is not None:
        count_query = count_query.where(RecordPermission.role_id == role_id)
        data_query = data_query.where(RecordPermission.role_id == role_id)

    if record_entity_type is not None and record_id is not None:
        count_query = count_query.where(
            RecordPermission.record_entity_type == record_entity_type,
            RecordPermission.record_id == record_id,
        )
        data_query = data_query.where(
            RecordPermission.record_entity_type == record_entity_type,
            RecordPermission.record_id == record_id,
        )

    count_result = await db.execute(count_query)
    total = count_result.scalar()

    data_query = data_query.order_by(RecordPermission.created_at.desc())
    data_result = await db.execute(data_query.offset(skip).limit(limit))
    items = data_result.scalars().all()

    return list(items), total


def _level_denied_sql(
    entity_type_expr: str, id_expr: str, action: str = "read"
) -> str:
    """Build a CASE-based deny-check SQL fragment for a single record-permission level.

    `entity_type_expr`/`id_expr` are raw SQL fragments (a quoted literal or a
    column reference) identifying the level to check (record_entity_type match
    + record_id match). `action` is the requested action; it is expanded through
    `_expand_action` (D-08) so the fragment matches exactly what
    `_resolve_record_permission_from_db` matches — a stored `write` DENY bites a
    requested `update`, and vice versa.

    Honors D-02 ("explicit action beats wildcard at the same depth"): if any
    explicit accepted-action row exists for the resolved role set at this
    level, only those rows' ALLOW/DENY matter — the wildcard (`action = '*'`)
    row is only consulted when no explicit row exists at this level. This
    mirrors the `has_explicit` tie-break in `_resolve_record_permission_from_db`.

    Returns True (denied) only when the resolved decision at this level is an
    explicit DENY — a level with zero matching rows (no entry at all) is never
    "denied" by this fragment, matching the permissive default.
    """
    # Values come from the module-level table only (never caller input), so the
    # quoted list is safe to interpolate into the raw fragment.
    explicit_actions = ", ".join(
        f"'{accepted}'" for accepted in _expand_action(action)
    )
    explicit_predicate = f"rp.action IN ({explicit_actions})"
    return f"""(CASE WHEN EXISTS (
            SELECT 1 FROM record_permissions rp
            WHERE rp.record_entity_type = {entity_type_expr}
              AND rp.record_id = {id_expr}
              AND rp.role_id = ANY(CAST(:role_ids AS UUID[]))
              AND {explicit_predicate}
        ) THEN EXISTS (
            SELECT 1 FROM record_permissions rp
            WHERE rp.record_entity_type = {entity_type_expr}
              AND rp.record_id = {id_expr}
              AND rp.role_id = ANY(CAST(:role_ids AS UUID[]))
              AND {explicit_predicate}
              AND rp.is_deny = True
        ) ELSE EXISTS (
            SELECT 1 FROM record_permissions rp
            WHERE rp.record_entity_type = {entity_type_expr}
              AND rp.record_id = {id_expr}
              AND rp.role_id = ANY(CAST(:role_ids AS UUID[]))
              AND rp.action = '*'
              AND rp.is_deny = True
        ) END)"""


_SINGLE_LEVEL_TABLES = {
    "source": "sources",
    "attribute_type": "attribute_types",
}

_CASCADE_ANCHOR_TABLES = {
    "schema": "schemas",
    "object_type": "object_types",
    "object": "objects",
}


async def _batch_check_has_deny_record_permission(
    db: AsyncSession,
    record_ids: list[uuid.UUID],
    entity_type: str,
) -> set[str]:
    """Check which records have any active DENY record permission.

    Returns a set of string-formatted record IDs that have at least one DENY entry
    at any level of the ancestor chain (for cascade-capable types) or directly
    (for standalone types like attribute_type), regardless of role assignment.

    This is an informational indicator — it shows that a DENY exists on the record,
    not whether the current user is personally affected.

    - For 'attribute_type': single-level, no cascade
    - For 'object' (and other cascade types): walk the ancestor chain and
      check for any DENY at any level
    """
    if entity_type not in VALID_RECORD_ENTITY_TYPES:
        raise ValueError(f"Unsupported entity_type: {entity_type}")
    if not record_ids:
        return set()

    if entity_type == "attribute_type":
        result = await db.execute(
            text("""
                SELECT DISTINCT rp.record_id
                FROM record_permissions rp
                WHERE rp.record_entity_type = 'attribute_type'
                  AND rp.record_id = ANY(:record_ids)
                  AND rp.is_deny = TRUE
            """),
            {"record_ids": [str(rid) for rid in record_ids]},
        )
        return {str(row[0]) for row in result.all()}

    result = await db.execute(
        text("""
            WITH RECURSIVE ancestor_chain AS (
                SELECT
                    o.id AS leaf_id,
                    o.id AS node_id,
                    CAST('object' AS VARCHAR) AS entity_type,
                    0 AS depth
                FROM objects o
                WHERE o.id = ANY(:record_ids)

                UNION ALL

                SELECT
                    ac.leaf_id,
                    CASE
                        WHEN ac.entity_type = 'object'       THEN o.object_type_id
                        WHEN ac.entity_type = 'object_type'  THEN ot.schema_id
                        WHEN ac.entity_type = 'schema'       THEN s.source_id
                    END,
                    CASE
                        WHEN ac.entity_type = 'object'       THEN 'object_type'
                        WHEN ac.entity_type = 'object_type'  THEN 'schema'
                        WHEN ac.entity_type = 'schema'       THEN 'source'
                    END,
                    ac.depth + 1
                FROM ancestor_chain ac
                LEFT JOIN objects o       ON o.id = ac.node_id AND ac.entity_type = 'object'
                LEFT JOIN object_types ot ON ot.id = ac.node_id AND ac.entity_type = 'object_type'
                LEFT JOIN schemas s       ON s.id = ac.node_id AND ac.entity_type = 'schema'
                WHERE ac.depth < 100
                  AND ac.entity_type IN ('object', 'object_type', 'schema')
            )
            SELECT ac.leaf_id
            FROM ancestor_chain ac
            WHERE EXISTS (
                SELECT 1 FROM record_permissions rp
                WHERE rp.record_entity_type = ac.entity_type
                  AND rp.record_id = ac.node_id
                  AND rp.is_deny = TRUE
            )
            GROUP BY ac.leaf_id
        """),
        {"record_ids": [str(rid) for rid in record_ids]},
    )
    return {str(row[0]) for row in result.all()}


async def _batch_check_has_record_permission(
    db: AsyncSession,
    record_ids: list[uuid.UUID],
    entity_type: str,
) -> set[str]:
    """Check which records have any active record permission (ALLOW or DENY).

    Returns a set of string-formatted record IDs that have at least one
    record_permissions entry at any level of the ancestor chain (for
    cascade-capable types) or directly (for standalone types like
    attribute_type), regardless of role assignment.

    This is an informational indicator — it shows that *some* permission is
    configured on the record, not whether the current user is affected.
    """
    if entity_type not in VALID_RECORD_ENTITY_TYPES:
        raise ValueError(f"Unsupported entity_type: {entity_type}")
    if not record_ids:
        return set()

    if entity_type == "attribute_type":
        result = await db.execute(
            text("""
                SELECT DISTINCT rp.record_id
                FROM record_permissions rp
                WHERE rp.record_entity_type = 'attribute_type'
                  AND rp.record_id = ANY(:record_ids)
            """),
            {"record_ids": [str(rid) for rid in record_ids]},
        )
        return {str(row[0]) for row in result.all()}

    result = await db.execute(
        text("""
            WITH RECURSIVE ancestor_chain AS (
                SELECT
                    o.id AS leaf_id,
                    o.id AS node_id,
                    CAST('object' AS VARCHAR) AS entity_type,
                    0 AS depth
                FROM objects o
                WHERE o.id = ANY(:record_ids)

                UNION ALL

                SELECT
                    ac.leaf_id,
                    CASE
                        WHEN ac.entity_type = 'object'       THEN o.object_type_id
                        WHEN ac.entity_type = 'object_type'  THEN ot.schema_id
                        WHEN ac.entity_type = 'schema'       THEN s.source_id
                    END,
                    CASE
                        WHEN ac.entity_type = 'object'       THEN 'object_type'
                        WHEN ac.entity_type = 'object_type'  THEN 'schema'
                        WHEN ac.entity_type = 'schema'       THEN 'source'
                    END,
                    ac.depth + 1
                FROM ancestor_chain ac
                LEFT JOIN objects o       ON o.id = ac.node_id AND ac.entity_type = 'object'
                LEFT JOIN object_types ot ON ot.id = ac.node_id AND ac.entity_type = 'object_type'
                LEFT JOIN schemas s       ON s.id = ac.node_id AND ac.entity_type = 'schema'
                WHERE ac.depth < 100
                  AND ac.entity_type IN ('object', 'object_type', 'schema')
            )
            SELECT ac.leaf_id
            FROM ancestor_chain ac
            WHERE EXISTS (
                SELECT 1 FROM record_permissions rp
                WHERE rp.record_entity_type = ac.entity_type
                  AND rp.record_id = ac.node_id
            )
            GROUP BY ac.leaf_id
        """),
        {"record_ids": [str(rid) for rid in record_ids]},
    )
    return {str(row[0]) for row in result.all()}


async def build_record_permission_filter(
    db: AsyncSession,
    user_id: str,
    entity_type: str,
    is_superuser: bool = False,
    alias: str | None = None,
    role_ids: list[uuid.UUID] | None = None,
    action: str = "read",
):
    """Build a SQL WHERE clause that filters to records the user may see.

    Mirrors `resolve_record_permission()`'s permissive-default resolution:
    a record is VISIBLE unless the resolved record-permission decision (across
    its own level and, for cascade-capable entity types, every ancestor level)
    is an explicit DENY. A record with zero `record_permissions` rows anywhere
    in its chain stays visible — this function never gates visibility on the
    presence of an ALLOW row, only on the absence-of-DENY.

    Returns a SQLAlchemy expression suitable for use in `.where()` on a list
    query, or `None` when no filtering is needed:
      - `is_superuser=True` (RLR-05 parity: zero DB/cache queries, matches the
        bypass behavior of `require_permission`/`require_record_permission`)
      - the user holds no roles at all — every record then resolves to
        "no record-permission entry" (permissive), not deny-everything.

    `alias` overrides the table name used to correlate the outer query's row
    (defaults to the entity's own table name — callers using an un-aliased
    `select(Model)` don't need to pass it).

    `role_ids` lets a caller resolve the principal's roles **once** and reuse
    them across several fragments in the same statement (317-03: the value
    visibility predicate composes two fragments). When omitted, the roles are
    resolved internally exactly as before, so every existing call site is
    untouched.

    `action` is the requested action (default `read`, the historical
    list-visibility case). It is expanded through `_expand_action` (D-08), so
    asking for `update` also honours a stored `write` DENY. `delete` stays a
    singleton.
    """
    if entity_type not in VALID_RECORD_ENTITY_TYPES:
        raise ValueError(f"Unsupported entity_type: {entity_type}")

    # RLR-05 parity: superuser bypass — zero DB/cache queries, no filter applied
    if is_superuser:
        return None

    # D-18 correction: role_ids from direct user_roles UNION group-inherited
    # roles so the record-level filter matches group-held roles too.
    # 317-03 A.3: a supplied list is used as-is (resolve once per request).
    if role_ids is None:
        role_ids = await _resolve_user_role_ids(db, user_id)

    if not role_ids:
        # User has no roles — they can never match a record_permissions row,
        # so every record resolves to "no entry" (permissive). Do not filter.
        return None

    if entity_type in _SINGLE_LEVEL_TABLES:
        # source / attribute_type: standalone, no ancestor cascade.
        table_name = alias or _SINGLE_LEVEL_TABLES[entity_type]
        level_denied = _level_denied_sql(
            f"'{entity_type}'", f"{table_name}.id", action
        )
        sql = f"NOT {level_denied}"
        return text(sql).bindparams(bindparam("role_ids", value=role_ids))

    # Cascade-capable: schema, object_type, object — walk the ancestor chain
    # (same shape as _resolve_record_permission_from_db's recursive CTE) and
    # check for a DENY at any level (RLR-02: deny-override wins at any depth).
    table_name = alias or _CASCADE_ANCHOR_TABLES[entity_type]
    level_denied = _level_denied_sql("ac.entity_type", "ac.id", action)
    sql = f"""NOT EXISTS (
            WITH RECURSIVE ancestor_chain AS (
                SELECT
                    {table_name}.id AS id,
                    CAST('{entity_type}' AS VARCHAR) AS entity_type,
                    0 AS depth

                UNION ALL

                SELECT
                    CASE
                        WHEN ac.entity_type = 'object'       THEN o.object_type_id
                        WHEN ac.entity_type = 'object_type'  THEN ot.schema_id
                        WHEN ac.entity_type = 'schema'       THEN s.source_id
                    END,
                    CASE
                        WHEN ac.entity_type = 'object'       THEN 'object_type'
                        WHEN ac.entity_type = 'object_type'  THEN 'schema'
                        WHEN ac.entity_type = 'schema'       THEN 'source'
                    END,
                    ac.depth + 1
                FROM ancestor_chain ac
                LEFT JOIN objects o       ON o.id = ac.id AND ac.entity_type = 'object'
                LEFT JOIN object_types ot ON ot.id = ac.id AND ac.entity_type = 'object_type'
                LEFT JOIN schemas s       ON s.id = ac.id AND ac.entity_type = 'schema'
                WHERE ac.depth < 100
                  AND ac.entity_type IN ('object', 'object_type', 'schema')
            )
            SELECT 1 FROM ancestor_chain ac
            WHERE {level_denied}
        )"""
    return text(sql).bindparams(bindparam("role_ids", value=role_ids))
