"""Public objecttype read endpoint (HIER-01, Plan 202-02).

Wires the Phase 202-01 shape mapper (build_object_type_shape), the canonical
ObjectType contract dict (_object_type_dict, re-exported from
object_read_service.py — this is the ONE canonical location, never redefine
it here) and a cache-aware wrapper to the public realm. The
require_public_hierarchy_read dependency runs the permission checks BEFORE
the route body — type-level RBAC gates the whole surface (T-202-02) and the
cache is never consulted for a denied client.

The loader is a fixed 2-query plan (1 type query with the selectin icon + 1
object count query) — the query count is INDEPENDENT of the number of
objects the type holds (no N+1, HIER-01 query budget). For non-superusers
objectCount counts only the objects the caller can see (quick 261011-wfr).

T-202-01: the wire shape is allow-list constructed via
build_object_type_shape — internal columns (schema_id raw key, created_at,
has_deny_*, ...) can never leak because they are never referenced as output
keys.

T-202-03: reads are cached under cache:public:objecttype:{id}:{user} — a
namespace separate from the internal cache surfaces, keyed with the client
identity for cross-client isolation; TTL 60s, fail-open on Redis errors.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Object, ObjectType
from app.public_api.object_read_service import _object_type_dict
from app.public_api.object_shape import build_object_type_shape
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_hierarchy_read
from app.services.cache_service import get_cached, set_cached
from app.services.record_permission_service import build_record_permission_filter

object_types_router = APIRouter(prefix="/objecttype", tags=["public-objecttype"])


async def _load_object_type(
    db: AsyncSession, object_type_id: uuid.UUID, object_filter=None
) -> dict | None:
    """Batched: 1 type query (+selectin icon) + 1 count query (record-filtered
    via object_filter when given — still one count query)."""
    result = await db.execute(select(ObjectType).where(ObjectType.id == object_type_id))
    ot = result.scalar_one_or_none()
    if ot is None:
        return None
    count_stmt = select(func.count()).select_from(Object).where(Object.object_type_id == object_type_id)
    if object_filter is not None:
        count_stmt = count_stmt.where(object_filter)
    count_result = await db.execute(count_stmt)
    data = _object_type_dict(ot)
    data["object_count"] = count_result.scalar()
    return data


def _build_public_objecttype_cache_key(object_type_id: str, user_id: str) -> str:
    return f"cache:public:objecttype:{object_type_id}:{user_id}"


async def get_public_object_type(db, object_type_id, user_id, *, is_superuser: bool) -> dict | None:
    key = _build_public_objecttype_cache_key(str(object_type_id), user_id)
    cached = await get_cached(key)
    if cached is not None:
        return cached
    object_filter = await build_record_permission_filter(
        db, user_id, "object", is_superuser=is_superuser)
    data = await _load_object_type(db, object_type_id, object_filter)
    if data is None:
        return None
    shape = build_object_type_shape(data)
    await set_cached(key, shape, ttl=60)
    return shape


@object_types_router.get("/{object_type_id}")
async def public_get_object_type(
    object_type_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("object-types", "object_type_id")),
):
    shape = await get_public_object_type(
        db, object_type_id, str(current_user.id), is_superuser=current_user.is_superuser)
    if shape is None:
        raise HTTPException(status_code=404, detail="Object type not found")
    return shape
