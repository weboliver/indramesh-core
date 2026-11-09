import math
import uuid
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, func, or_, and_, cast
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models.attribute_type import AttributeType
from app.models.audit_log import AuditLog
from app.models.object import Object
from app.models.object_value import ObjectValue
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.user import User, UserProfile
from app.schemas.auth import UserResponse
from app.schemas.crud import AuditLogPaginatedResponse, AuditLogResponse
from app.services.auth_service import require_permission
from app.services.record_permission_service import build_record_permission_filter
from app.services.value_visibility import (
    resolve_principal_roles,
    visible_object_id_set,
    visible_object_ids_subquery,
)

router = APIRouter(prefix="/audit-logs", tags=["audit-logs"])

# Constants
DEFAULT_PAGE_SIZE = 50
MAX_PAGE_SIZE = 200


async def get_db() -> AsyncSession:
    async with async_session_maker() as session:
        yield session


@router.get("", response_model=AuditLogPaginatedResponse)
async def list_audit_logs(
    entity_type: str | None = Query(default=None, max_length=50, description="Filter by entity type"),
    object_id: uuid.UUID | None = Query(default=None, description="Filter by owning Object id (objects/object_values/object_refs history)"),
    actor_q: str | None = Query(default=None, max_length=100, description="Substring match on actor name/email"),
    start_date: str | None = Query(default=None, description="ISO datetime filter (inclusive start)"),
    end_date: str | None = Query(default=None, description="ISO datetime filter (inclusive end)"),
    page: int = Query(default=1, ge=1, description="Page number (1-indexed)"),
    page_size: int = Query(default=DEFAULT_PAGE_SIZE, ge=1, le=MAX_PAGE_SIZE, description="Items per page"),
    current_user: UserResponse = Depends(require_permission("audit-log", "read")),
    db: AsyncSession = Depends(get_db),
):
    """Return paginated audit log entries with optional filters.
    Per D-08: default sort is created_at DESC (newest first).
    Per D-04: offset-based pagination with page/page_size.
    Per D-06: actor_display_name and actor_initials resolved via LEFT JOIN on users table.

    Query param names and the "audit-log" (singular) permission resource
    MUST match Phase 167's already-planned frontend contract exactly —
    see 166-PLAN-REVIEW.md #1 and #4. "audit-logs" (plural) was never
    seeded in init_service.py and would 403 every non-superuser role.
    """
    # Base query: LEFT JOIN on users table and user_profiles for actor name resolution (D-06)
    # Attribute name resolution has three paths: the audit row's own
    # object_type_attribute_id (works even after the ObjectValue itself is
    # later deleted), for older rows written before that column existed a
    # fallback through the still-live ObjectValue row (entity_id) to its
    # current attribute — recovers the name as long as the value row itself
    # hasn't since been deleted too — and, for `object_refs` rows, the stored
    # (object_type_id, from_attribute_name) pair in the `changes` payload.
    # `object_refs` rows carry no object_type_attribute_id and are not
    # `object_values`, so without the third path both record arms below miss
    # them and a hidden or attribute-type-DENIED reference attribute leaks its
    # name (and the whole `changes` payload) to a restricted caller (WR-01).
    FallbackAttr = aliased(ObjectTypeAttribute)
    RefAttr = aliased(ObjectTypeAttribute)
    # CREATE rows store the pair under `new`, DELETE rows under `old`; an
    # UPDATE row only carries the fields it actually changed.
    ref_object_type_id = func.coalesce(
        AuditLog.changes["object_type_id"]["new"].astext,
        AuditLog.changes["object_type_id"]["old"].astext,
    )
    ref_attribute_name = func.coalesce(
        AuditLog.changes["from_attribute_name"]["new"].astext,
        AuditLog.changes["from_attribute_name"]["old"].astext,
    )
    ref_attribute_join = and_(
        AuditLog.entity_type == "object_refs",
        RefAttr.object_type_id == cast(ref_object_type_id, PG_UUID(as_uuid=True)),
        RefAttr.name == ref_attribute_name,
    )
    query = (
        select(
            AuditLog,
            UserProfile.first_name,
            UserProfile.last_name,
            User.username,
            ObjectTypeAttribute.name,
            FallbackAttr.name,
        )
        .outerjoin(User, AuditLog.actor_id == User.id)
        .outerjoin(UserProfile, User.id == UserProfile.user_id)
        .outerjoin(ObjectTypeAttribute, AuditLog.object_type_attribute_id == ObjectTypeAttribute.id)
        .outerjoin(ObjectValue, and_(
            AuditLog.object_type_attribute_id.is_(None),
            AuditLog.entity_type == "object_values",
            AuditLog.entity_id == ObjectValue.id,
        ))
        .outerjoin(FallbackAttr, ObjectValue.object_type_attribute_id == FallbackAttr.id)
        .outerjoin(RefAttr, ref_attribute_join)
        .order_by(AuditLog.created_at.desc())  # D-08: newest first
    )

    # Apply filters
    conditions = []

    if entity_type:
        # Validate against enum (optional — if invalid, query returns 0 results gracefully)
        conditions.append(AuditLog.entity_type == entity_type)

    if object_id:
        conditions.append(AuditLog.object_id == object_id)
        # Object History view only: ObjectValue CREATE/DELETE entries are
        # plumbing (e.g. a type-change delete+recreate), not meaningful
        # object history — excluded here (not just client-side) so `total`
        # matches what's actually rendered. The global Audit Log page never
        # sends object_id, so this leaves it unaffected.
        conditions.append(
            or_(
                AuditLog.entity_type != "object_values",
                AuditLog.action == "UPDATE",
            )
        )

    if actor_q:
        # Per D-05: substring match on user name/email
        search_pattern = f"%{actor_q}%"
        conditions.append(
            or_(
                UserProfile.first_name.ilike(search_pattern),
                UserProfile.last_name.ilike(search_pattern),
                User.email.ilike(search_pattern),
            )
        )

    if start_date:
        try:
            from datetime import datetime
            dt_from = datetime.fromisoformat(start_date)
            if dt_from.tzinfo is not None:
                dt_from = dt_from.replace(tzinfo=None)
            conditions.append(AuditLog.created_at >= dt_from)
        except (ValueError, TypeError):
            pass  # Invalid date format — ignore filter

    if end_date:
        try:
            from datetime import datetime
            dt_to = datetime.fromisoformat(end_date)
            if dt_to.tzinfo is not None:
                dt_to = dt_to.replace(tzinfo=None)
            conditions.append(AuditLog.created_at <= dt_to)
        except (ValueError, TypeError):
            pass  # Invalid date format — ignore filter

    # D-04 / AUTHZ-01: a non-superuser never sees an audit row whose owning
    # attribute carries a record DENY, and never sees one whose attribute is
    # `is_hidden`. The row is dropped whole — `changes` carries the old and new
    # values, so filtering the serialized payload would still leak the secret.
    # Appended to the shared `conditions` list, so the page query and the count
    # statement filter identically and `total` / `total_pages` shrink with it.
    role_ids = None
    if not current_user.is_superuser:
        # Resolved once per request: every fragment below binds the same
        # `:role_ids` value (pitfall P-08).
        role_ids = await resolve_principal_roles(db, str(current_user.id), False)

        # Object-level record DENY: a row bound to an object the caller cannot
        # read behaves like the object. Rows with a NULL `object_id` (the
        # schema/object-type/attribute-type CRUD rows, D-12) stay visible — that
        # `or_` arm is the point, not an oversight. This one predicate covers
        # both the `object_id`-scoped history and the global log, so an object
        # DENY answers an empty 200 page rather than a 403 (D-01/D-04).
        conditions.append(
            or_(
                AuditLog.object_id.is_(None),
                AuditLog.object_id.in_(
                    await visible_object_ids_subquery(
                        db, str(current_user.id), False, role_ids=role_ids
                    )
                ),
            )
        )

        # Attribute-level record DENY. The builder returns a **visibility**
        # expression (`NOT <denied>`), so membership is tested positively
        # against the visible set — inverting it would keep exactly the denied
        # type and drop every allowed one. `AttributeType` is not joined; the
        # clause's `attribute_types.id` reference resolves against this
        # subquery's own FROM.
        attr_clause = await build_record_permission_filter(
            db,
            str(current_user.id),
            "attribute_type",
            is_superuser=False,
            role_ids=role_ids,
        )
        if attr_clause is not None:
            visible_attr_types = select(AttributeType.id).where(attr_clause)
            owner_attr_type = func.coalesce(
                ObjectTypeAttribute.attribute_type_id,
                FallbackAttr.attribute_type_id,
                RefAttr.attribute_type_id,
            )
            # The `is_(None)` arm keeps rows with no resolvable owning
            # attribute (the schema/object-type/attribute-type CRUD rows, D-12).
            conditions.append(
                or_(
                    owner_attr_type.is_(None),
                    owner_attr_type.in_(visible_attr_types),
                )
            )

        # `is_hidden` for all three name-resolution paths. Deliberately NOT
        # `value_visibility._hidden_clause`: its `is_hidden IS FALSE` drops
        # every row that has no owning attribute under these OUTER JOINs, so
        # the coalesce + "no owning attribute" arm is required here.
        conditions.append(
            or_(
                and_(
                    ObjectTypeAttribute.id.is_(None),
                    FallbackAttr.id.is_(None),
                    RefAttr.id.is_(None),
                ),
                func.coalesce(
                    ObjectTypeAttribute.is_hidden,
                    FallbackAttr.is_hidden,
                    RefAttr.is_hidden,
                    False,
                ).is_(False),
            )
        )

    # Apply conditions to the main query
    if conditions:
        query = query.where(*conditions)

    # Count total over the SAME statement shape as the page query. Every join
    # must be present: the record-permission conditions reference
    # `object_type_attributes`, the aliased fallback join and the aliased
    # reference-attribute join, and an omitted join answers a "missing
    # FROM-clause entry" 500 for a non-superuser. The count must apply the same
    # predicates, or `total` keeps counting the rows the page drops (WR-01).
    count_sql = select(func.count()).select_from(
        select(AuditLog.id)
        .outerjoin(User, AuditLog.actor_id == User.id)
        .outerjoin(UserProfile, User.id == UserProfile.user_id)
        .outerjoin(ObjectTypeAttribute, AuditLog.object_type_attribute_id == ObjectTypeAttribute.id)
        .outerjoin(ObjectValue, and_(
            AuditLog.object_type_attribute_id.is_(None),
            AuditLog.entity_type == "object_values",
            AuditLog.entity_id == ObjectValue.id,
        ))
        .outerjoin(FallbackAttr, ObjectValue.object_type_attribute_id == FallbackAttr.id)
        .outerjoin(RefAttr, ref_attribute_join)
        .where(*conditions)
        .subquery()
    )
    total_result = await db.execute(count_sql)
    total = total_result.scalar() or 0

    # Pagination (D-04: offset-based)
    offset = (page - 1) * page_size
    query = query.offset(offset).limit(page_size)

    result = await db.execute(query)
    rows = result.all()

    reference_target_ids = set()
    for row in rows:
        audit_log: AuditLog = row[0]
        if audit_log.entity_type != "object_refs":
            continue
        target_change = audit_log.changes.get("to_object_id", {})
        for target_id in (target_change.get("old"), target_change.get("new")):
            try:
                reference_target_ids.add(uuid.UUID(target_id))
            except (TypeError, ValueError):
                continue

    reference_target_names = {}
    if reference_target_ids:
        # D-11: the history of a readable object must not name a reference
        # target the caller cannot read. Intersecting the id set means an
        # unreadable end serializes as None, exactly like a deleted target; the
        # row itself stays visible, only the far-end name lookup is narrowed.
        # A superuser gets the input set back unchanged (no extra query).
        reference_target_ids = await visible_object_id_set(
            db,
            str(current_user.id),
            current_user.is_superuser,
            reference_target_ids,
            role_ids=role_ids,
        )
    if reference_target_ids:
        target_result = await db.execute(
            select(Object.id, Object.name).where(Object.id.in_(reference_target_ids))
        )
        reference_target_names = {
            str(target_id): target_name
            for target_id, target_name in target_result.all()
        }

    # Build response with flat actor fields (D-07)
    items = []
    for row in rows:
        audit_log: AuditLog = row[0]
        first_name = row[1] or ""
        last_name = row[2] or ""
        username = row[3] or ""
        attribute_name = row[4] or row[5]
        reference_field_name = None
        reference_old_target_name = None
        reference_new_target_name = None
        if audit_log.entity_type == "object_refs":
            field_change = audit_log.changes.get("from_attribute_name", {})
            target_change = audit_log.changes.get("to_object_id", {})
            reference_field_name = field_change.get("new") or field_change.get("old")
            reference_old_target_name = reference_target_names.get(target_change.get("old"))
            reference_new_target_name = reference_target_names.get(target_change.get("new"))

        # Build actor_display_name: prefer "First Last", fall back to username
        if first_name and last_name:
            actor_display_name = f"{first_name} {last_name}".strip()
        elif first_name:
            actor_display_name = first_name
        elif last_name:
            actor_display_name = last_name
        else:
            actor_display_name = username or None

        # Build actor_initials: first letter of first_name + first letter of last_name
        if first_name and last_name:
            actor_initials = f"{first_name[0]}{last_name[0]}".upper()
        elif first_name:
            actor_initials = first_name[0].upper()
        else:
            actor_initials = None

        items.append(AuditLogResponse(
            id=audit_log.id,
            entity_type=audit_log.entity_type,
            entity_id=audit_log.entity_id,
            action=audit_log.action,
            actor_id=audit_log.actor_id,
            changes=audit_log.changes,
            created_at=audit_log.created_at,
            actor_display_name=actor_display_name,
            actor_initials=actor_initials,
            attribute_name=attribute_name,
            reference_field_name=reference_field_name,
            reference_old_target_name=reference_old_target_name,
            reference_new_target_name=reference_new_target_name,
        ))

    total_pages = math.ceil(total / page_size) if total > 0 else 0

    return AuditLogPaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
        total_pages=total_pages,
    )
