"""Public object read endpoint (READ-01/READ-02/READ-04).

Wires the Phase 201-02 assembly service (load_object_read) and the Phase
201-04 cache-aware wrapper (get_public_object) plus the Phase 201-01 shape
mapper (build_object_shape) to the public realm. The
require_public_object_read dependency runs the permission checks BEFORE the
route body — a record-level DENY yields the same 404 as a nonexistent object,
so no existence information leaks (T-201-07, READ-05), and the cache is never
consulted for a DENY-granted client (T-201-14).

READ-02: the `attributes` query param filters the response to the requested
Jira attribute names (case-insensitive, exact-name matching only — no
wildcard/prefix expansion). Unknown names → 400 listing them (no partial
data leak, T-201-08). Validation runs against list_attribute_names BEFORE
the cache wrapper, so a cached hit can never serve an unknown-name request
(T-201-12). Omitted → all attributes (Jira-compatible default).

READ-04: reads are cached under cache:public:object:{id}:{attrs_hash} (D-09)
(get_public_object) — a namespace separate from the internal cache:object:*
(cross-surface poisoning impossible); TTL 60s, fail-open on Redis errors.
The key carries NO user id: the assembled shape is user-independent and the
permission dependency runs before the cache wrapper (T-201-14), so all
callers of an object share one cache entry. The shared shape is filtered per
caller after the cache read (record_filter_object_shape), so a DENY'd
referenced object never reaches a restricted caller (quick 261012-blo).
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_object_read
from app.public_api.object_read_service import (
    get_public_object,
    list_attribute_names,
    record_filter_object_shape,
)

objects_router = APIRouter(prefix="/object", tags=["public-object"])


def _parse_attributes(raw: str | None) -> list[str] | None:
    """None → all attributes (Jira-compatible default); else split on ','.
    Empty list → 400."""
    if raw is None:
        return None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="attributes: empty list")
    return names


@objects_router.get("/{object_id}")
async def public_get_object(
    object_id: uuid.UUID,
    attributes: str | None = Query(None, description="Comma-separated Jira attribute names, case-insensitive"),
    lang: str | None = Query(None, description="Language code (BCP-47) for translated status names and SELECT option values"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_object_read()),
):
    # READ-02: unknown names → 400 BEFORE the cache wrapper (a cached hit must
    # never serve an unknown-name request — T-201-12). The names query is a
    # fixed single query (list_attribute_names), NOT per-attribute. CR-01: the
    # SAME query also carries the existence signal (None vs list[str]), so a
    # nonexistent object 404s here instead of falling through to the
    # unknown-name 400 — nonexistent and DENY-protected objects are now
    # indistinguishable (both 404) for every request shape.
    requested = _parse_attributes(attributes)
    if requested is not None:
        names = await list_attribute_names(db, object_id)
        if names is None:
            raise HTTPException(status_code=404, detail="Object not found")
        known = {n.lower() for n in names}
        unknown = [n for n in requested if n.lower() not in known]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown attributes: {unknown}",
            )
    # READ-04: cache-aware read (the permission dependency has already run —
    # T-201-14: a DENY-granted client never reaches this call, so the D-09
    # shared cache entry is never consulted without read permission). lang
    # (223-05 D-13) threads into the loader AND the cache key.
    shape = await get_public_object(db, object_id, requested, lang)
    if shape is None:
        raise HTTPException(status_code=404, detail="Object not found")
    return await record_filter_object_shape(
        db, shape, object_id, str(current_user.id), is_superuser=current_user.is_superuser
    )
