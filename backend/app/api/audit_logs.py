import math
import uuid
from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, func, or_, and_
from sqlalchemy.orm import aliased
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import async_session_maker
from app.models.audit_log import AuditLog
from app.models.object import Object
from app.models.object_value import ObjectValue
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.user import User, UserProfile
from app.schemas.auth import UserResponse
from app.schemas.crud import AuditLogPaginatedResponse, AuditLogResponse
from app.services.auth_service import require_permission

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
    # Attribute name resolution has two paths: the audit row's own
    # object_type_attribute_id (works even after the ObjectValue itself is
    # later deleted) and, for older rows written before that column existed,
    # a fallback through the still-live ObjectValue row (entity_id) to its
    # current attribute — recovers the name as long as the value row itself
    # hasn't since been deleted too.
    FallbackAttr = aliased(ObjectTypeAttribute)
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

    # Apply conditions to the main query
    if conditions:
        query = query.where(*conditions)

    # Count total — use a subquery since actor_q filters join users/profiles
    count_sql = select(func.count()).select_from(
        select(AuditLog.id)
        .outerjoin(User, AuditLog.actor_id == User.id)
        .outerjoin(UserProfile, User.id == UserProfile.user_id)
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
