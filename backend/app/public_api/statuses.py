"""Public status-type read endpoints (HIER-04, Plan 202-05).

Serves the Jira statustype golden shape — a bare array of
{id, name, category, objectSchemaId} — where category is the Jira int
(0=Inactive, 1=Active, 2=Pending, via status_category_to_jira) and
objectSchemaId is ALWAYS set: IndraMesh has no global-status concept
(Status.schema_id is NOT NULL), a deliberate deviation from the literal
REQUIREMENTS.md HIER-04 wording "objectSchemaId: null" (assumption A4 —
documented in the 202-05 SUMMARY).

T-202-02: require_public_hierarchy_read("statuses") runs the permission
check BEFORE the route body — type-level RBAC gates the whole surface and
the cache is never consulted for a denied client. Statuses of a DENY'd schema
are invisible: the list omits them, ?objectSchemaId= of such a schema answers
like an unknown schema id (including the ?lang= 400), and by-id returns 404
(quick 261011-wfr).

T-202-01: the wire shape is allow-list constructed via build_status_shape —
internal columns (color, order, jira_status_id, status_category raw key,
schema_id raw key) can never leak because they are never referenced as
output keys.

T-202-03: reads are cached under cache:public:statustypes:* /
cache:public:statustype:{id}:{user} — a namespace separate from the
internal cache surfaces, keyed with the schema filter + client identity for
cross-client isolation; TTL 60s, fail-open on Redis errors. Since Plan
223-05 (D-13, 222 D-02 closure) the keys gain a `:{lang}` component when
?lang= is set — translated payloads are never served cross-language
(T-223-22) — and the golden shape's status names resolve per the 221/222
contract (unknown code 400, strict D-06 when the schema filter is present,
per-entity fallback on the unfiltered list).

Route order: the empty-path list route ("") MUST be declared before the
/{status_id} single route so it is never shadowed.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Schema, Status
from app.public_api.jira_shape import status_category_to_jira
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_hierarchy_read
from app.services.cache_service import get_cached, set_cached
from app.services.record_permission_service import build_record_permission_filter
from app.services.translation_service import (
    resolve_translations,
    validate_read_language,
)

statuses_router = APIRouter(prefix="/config/statustype", tags=["public-statustype"])


def build_status_shape(s: Status) -> dict:
    return {
        "id": str(s.id),
        "name": s.name,
        "category": status_category_to_jira(s.status_category),  # 0/1/2 int
        "objectSchemaId": str(s.schema_id),  # always set locally (no global statuses — A4)
    }


@statuses_router.get("")
async def public_list_status_types(
    objectSchemaId: uuid.UUID | None = Query(None, alias="objectSchemaId"),
    lang: str | None = Query(None, description="Language code (BCP-47) for translated status names"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("statuses")),
):
    filter_part = str(objectSchemaId) if objectSchemaId else "all"
    lang_part = f":{lang}" if lang else ""
    # T-223-22: lang is part of the cache key — a de payload never serves an
    # en request. The no-lang key keeps its pre-phase format.
    key = f"cache:public:statustypes:{filter_part}{lang_part}:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    schema_filter = await build_record_permission_filter(
        db, str(current_user.id), "schema", is_superuser=current_user.is_superuser)
    query = select(Status).order_by(Status.order.asc(), Status.name.asc())
    if objectSchemaId is not None:
        query = query.where(Status.schema_id == objectSchemaId)
    if schema_filter is not None:
        query = query.where(Status.schema_id.in_(select(Schema.id).where(schema_filter)))
    result = await db.execute(query)
    statuses = result.scalars().all()
    shape = [build_status_shape(s) for s in statuses]
    if lang:
        # 222 D-05: strict D-06 when the schema filter is present (lang must
        # be in that schema's enabled subset), per-entity fallback on the
        # unfiltered list; unknown/inactive code always 400 (D-05).
        if objectSchemaId is not None and schema_filter is not None:
            probe = await db.execute(
                select(Schema.id).where(Schema.id == objectSchemaId).where(schema_filter))
            if probe.scalar_one_or_none() is None:
                # DENY'd (or unknown) schema: answer exactly like an unknown
                # schema id — never let the not-enabled branch print its name
                await validate_read_language(db, lang)
                raise HTTPException(
                    status_code=400,
                    detail=f"Language '{lang}' is not enabled for schema '{objectSchemaId}'",
                )
        await validate_read_language(db, lang, objectSchemaId)
        resolved = await resolve_translations(
            db, "status", [s.id for s in statuses], ["name"],
            lang, schema_id=objectSchemaId,
        )
        for entry in shape:
            entity_resolution = resolved.get(uuid.UUID(entry["id"]), {})
            if "name" in entity_resolution:
                entry["name"] = entity_resolution["name"][0]
    await set_cached(key, shape, ttl=60)
    return shape


@statuses_router.get("/{status_id}")
async def public_get_status_type(
    status_id: uuid.UUID,
    lang: str | None = Query(None, description="Language code (BCP-47) for translated status names"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("statuses", "status_id")),
):
    lang_part = f":{lang}" if lang else ""
    key = f"cache:public:statustype:{status_id}{lang_part}:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    schema_filter = await build_record_permission_filter(
        db, str(current_user.id), "schema", is_superuser=current_user.is_superuser)
    query = select(Status).where(Status.id == status_id)
    if schema_filter is not None:
        query = query.where(Status.schema_id.in_(select(Schema.id).where(schema_filter)))
    result = await db.execute(query)
    s = result.scalar_one_or_none()
    if s is None:
        raise HTTPException(status_code=404, detail="Status type not found")
    shape = build_status_shape(s)
    if lang:
        # D-06 strict: the entity's own schema scope is known (Status.schema_id
        # NOT NULL) — unknown/inactive code and not-enabled both 400.
        await validate_read_language(db, lang, s.schema_id)
        resolved = await resolve_translations(
            db, "status", [status_id], ["name"], lang, schema_id=s.schema_id
        )
        entity_resolution = resolved.get(status_id, {})
        if "name" in entity_resolution:
            shape["name"] = entity_resolution["name"][0]
    await set_cached(key, shape, ttl=60)
    return shape
