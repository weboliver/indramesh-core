from typing import List

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import PermissionResponse
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.models.permission import Permission
from app.models.plugin import Plugin

router = APIRouter(prefix="/permissions", tags=["permissions"])


def _owner_state(owner_plugin: str | None, plugin_states: dict[str, str]) -> str:
    """Closed owner-state vocabulary (review correction R01-3).

    ``core`` (``owner_plugin IS NULL``), ``active``, ``disabled``, or
    ``unavailable`` (the owner has no ``plugins`` row, is in ERROR, or is not
    discovered any more). An unavailable owner keeps its provenance and is never
    reported as Core.
    """
    if owner_plugin is None:
        return "core"
    state = plugin_states.get(owner_plugin)
    if state == "active":
        return "active"
    if state == "disabled":
        return "disabled"
    return "unavailable"


@router.get("/", response_model=List[PermissionResponse])
async def list_permissions_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("roles", "read")),
):
    result = await db.execute(
        select(Permission).order_by(Permission.resource, Permission.action)
    )
    permissions = result.scalars().all()

    # One lookup of the `plugins` table for every distinct plugin owner — never
    # a per-row query (R01-3). The stable plugin name doubles as the display
    # label because the manifest carries no separate display name.
    owner_names = {p.owner_plugin for p in permissions if p.owner_plugin}
    plugin_states: dict[str, str] = {}
    if owner_names:
        plugin_states = {
            name: state
            for name, state in (
                await db.execute(
                    select(Plugin.name, Plugin.state).where(Plugin.name.in_(owner_names))
                )
            ).all()
        }

    return [
        PermissionResponse(
            id=permission.id,
            name=permission.name,
            resource=permission.resource,
            action=permission.action,
            description=permission.description,
            owner_plugin=permission.owner_plugin,
            owner_state=_owner_state(permission.owner_plugin, plugin_states),
            owner_label=permission.owner_plugin,
        )
        for permission in permissions
    ]
