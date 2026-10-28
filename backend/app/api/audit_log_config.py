from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.db import async_session_maker
from app.schemas.auth import UserResponse
from app.schemas.crud import AuditLogConfigResponse, AuditLogConfigToggle
from app.services.auth_service import require_permission
from app.services.audit_service import (
    list_audit_log_configs, toggle_audit_log_config, AuditEntityType,
    HISTORY_ENTITY_TYPES, get_history_enabled, ALWAYS_ON_ENTITY_TYPES,
)

router = APIRouter(prefix="/audit-log-config", tags=["audit-log-config"])


async def get_db() -> AsyncSession:
    async with async_session_maker() as session:
        yield session


@router.get("", response_model=dict[str, bool])
async def get_audit_log_configs(
    current_user: UserResponse = Depends(require_permission("audit-log-config", "read")),
    db: AsyncSession = Depends(get_db),
):
    """Return all audit log config rows as an entity_type -> enabled map.

    Per D-03: admins see everything. Response shape is a flat dict (NOT a
    list of objects) per 166-PLAN-REVIEW.md #2 — Phase 167's
    useAuditLogConfig() hook and AdminAuditConfigSection.jsx were already
    planned against `{ entity_type: boolean }`; returning a list here would
    silently break every config[entityType] lookup in the frontend.
    """
    configs = await list_audit_log_configs(db)
    return {c.entity_type: c.audit_log_enabled for c in configs}


@router.patch("/{entity_type}", response_model=AuditLogConfigResponse)
async def update_audit_log_config(
    entity_type: str,
    body: AuditLogConfigToggle,
    current_user: UserResponse = Depends(require_permission("audit-log-config", "update")),
    db: AsyncSession = Depends(get_db),
):
    """Toggle audit_log_enabled for a given entity_type.
    Validates entity_type against AuditEntityType enum (returns 422 for unknown types).
    Invalidates Redis cache per D-11.
    """
    # Validate entity_type against the enum (D-16: StrEnum is source of truth)
    valid_types = {et.value for et in AuditEntityType}
    if entity_type not in valid_types:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid entity_type '{entity_type}'. Must be one of: {', '.join(sorted(valid_types))}",
        )

    if not body.audit_log_enabled and entity_type in ALWAYS_ON_ENTITY_TYPES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="This audit toggle is always on and cannot be disabled.",
        )

    if not body.audit_log_enabled and entity_type in HISTORY_ENTITY_TYPES:
        if await get_history_enabled(db):
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="Cannot disable this audit toggle while History is active. Disable History first.",
            )

    config = await toggle_audit_log_config(
        db, entity_type=entity_type, enabled=body.audit_log_enabled,
        updated_by=current_user.id,
    )
    if config is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Audit config for '{entity_type}' not found",
        )

    return config
