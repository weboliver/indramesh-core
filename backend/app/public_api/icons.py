"""Public icon read endpoints (HIER-05, Plan 202-05).

Serves the Jira icon golden shape — a bare array of {id, name, url16, url48}.
Locally both url sizes carry the single Icon.url value (no size variants —
documented deviation, matches the Phase 201 avatar decision).

T-202-02: require_public_hierarchy_read("icons") runs the permission check
BEFORE the route body — type-level RBAC gates the whole surface and the
cache is never consulted for a denied client.

T-202-01: the wire shape is allow-list constructed via build_icon_shape —
internal columns (local_name, type, schema_id raw key, raw url key) can
never leak because they are never referenced as output keys.

T-202-03: reads are cached under cache:public:icons:global:{user} /
cache:public:icon:{id}:{user} — a namespace separate from the internal
cache surfaces, keyed with the client identity for cross-client isolation;
TTL 60s, fail-open on Redis errors.

Route order: the concrete /global route MUST be declared before the
/{icon_id} single route so it is never shadowed.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Icon
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_hierarchy_read
from app.services.cache_service import get_cached, set_cached

icons_router = APIRouter(prefix="/icon", tags=["public-icon"])


def build_icon_shape(icon: Icon) -> dict:
    """url16/url48 both carry the single local url — no size variants locally
    (documented deviation, matches the Phase 201 avatar decision)."""
    return {
        "id": str(icon.id),
        "name": icon.name,
        "url16": icon.url,
        "url48": icon.url,
    }


@icons_router.get("/global")
async def public_list_global_icons(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("icons")),
):
    key = f"cache:public:icons:global:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    result = await db.execute(select(Icon).order_by(Icon.name.asc()))
    icons = result.scalars().all()
    shape = [build_icon_shape(i) for i in icons]
    await set_cached(key, shape, ttl=60)
    return shape


@icons_router.get("/{icon_id}")
async def public_get_icon(
    icon_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("icons", "icon_id")),
):
    key = f"cache:public:icon:{icon_id}:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    result = await db.execute(select(Icon).where(Icon.id == icon_id))
    icon = result.scalar_one_or_none()
    if icon is None:
        raise HTTPException(status_code=404, detail="Icon not found")
    shape = build_icon_shape(icon)
    await set_cached(key, shape, ttl=60)
    return shape
