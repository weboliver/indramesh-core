"""Public schema read endpoints (HIER-02, Plan 202-03).

The schema is the top of the hierarchy — Jira-shaped reads:

- GET /objectschema/{id} — the golden objectschema shape with
  objectSchemaKey, objectCount, objectTypeCount (real counts, HIER-02)
- GET /objectschema/{id}/objecttypes — a BARE ARRAY of golden objectType
  shapes, each with a real per-type objectCount (ONE grouped count query,
  not N — the query count is INDEPENDENT of the number of types)
- GET /objectschema/list — the only envelope list on the public surface:
  {"objectschemas": [...]} (task 2; registered BEFORE /{schema_id} so a
  literal "list" path segment can never be captured by the id route)

T-202-01: the wire shape is allow-list constructed via build_schema_shape /
build_object_type_shape — internal columns (source_id, key, created_at,
jira_schema_id, ...) are never referenced as output keys.

T-202-02: require_public_hierarchy_read("schemas", ...) runs BEFORE the
route body — schemas:read type-level RBAC gates the whole surface; the
cache is never consulted for a denied client. The by-id routes add a
record-level read step (a DENY'd schema answers 404), and the list and
objecttypes routes omit DENY'd schemas/object types (quick 261011-tv1). The
objectCount/objectTypeCount aggregates count only the objects and object
types the caller can see for non-superusers (quick 261011-wfr).

T-202-03: reads are cached under cache:public:schema:{id}:{user} / a
namespace separate from the internal cache surfaces, keyed with the client
identity for cross-client isolation; TTL 60s, fail-open on Redis errors.
"""

import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Object, ObjectType, Schema
from app.public_api.object_read_service import _object_type_dict
from app.public_api.object_shape import build_object_type_shape
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_hierarchy_read
from app.services.cache_service import get_cached, set_cached
from app.services.record_permission_service import build_record_permission_filter

schemas_router = APIRouter(prefix="/objectschema", tags=["public-schema"])


def _schema_dict(s: Schema) -> dict:
    return {"id": s.id, "name": s.name, "key": s.key,
            "created_at": s.created_at, "updated_at": s.updated_at}


def build_schema_shape(schema_data: dict) -> dict:
    """Jira objectschema shape — status "Ok" constant (no local concept),
    archivedObjectCount 0 (no archive concept) — documented deviations."""
    return {
        "id": str(schema_data["id"]),
        "name": schema_data["name"],
        "objectSchemaKey": schema_data.get("key") or "",
        "status": "Ok",
        "created": schema_data["created_at"].isoformat(),
        "updated": schema_data["updated_at"].isoformat(),
        "objectCount": schema_data.get("object_count", 0),
        "archivedObjectCount": 0,
        "objectTypeCount": schema_data.get("object_type_count", 0),
    }


async def _load_schema(
    db: AsyncSession, schema_id: uuid.UUID, object_filter=None, type_filter=None
) -> dict | None:
    result = await db.execute(select(Schema).where(Schema.id == schema_id))
    s = result.scalar_one_or_none()
    if s is None:
        return None
    obj_stmt = select(func.count()).select_from(Object).where(Object.schema_id == schema_id)
    if object_filter is not None:
        obj_stmt = obj_stmt.where(object_filter)
    type_stmt = select(func.count()).select_from(ObjectType).where(ObjectType.schema_id == schema_id)
    if type_filter is not None:
        type_stmt = type_stmt.where(type_filter)
    obj_count = await db.execute(obj_stmt)
    type_count = await db.execute(type_stmt)
    data = _schema_dict(s)
    data["object_count"] = obj_count.scalar()
    data["object_type_count"] = type_count.scalar()
    return data


async def _load_schema_object_types(
    db: AsyncSession, schema_id: uuid.UUID, type_filter=None, object_filter=None
) -> list[dict] | None:
    # existence probe + types in one round-trip pair
    exists = await db.execute(select(Schema.id).where(Schema.id == schema_id))
    if exists.scalar_one_or_none() is None:
        return None
    stmt = select(ObjectType).where(ObjectType.schema_id == schema_id).order_by(ObjectType.sort)
    if type_filter is not None:
        stmt = stmt.where(type_filter)
    result = await db.execute(stmt)
    types = result.scalars().all()
    # objectCount per type — ONE grouped query, not N
    if types:
        ids = [t.id for t in types]
        count_stmt = (
            select(Object.object_type_id, func.count())
            .where(Object.object_type_id.in_(ids)).group_by(Object.object_type_id))
        if object_filter is not None:
            count_stmt = count_stmt.where(object_filter)
        counts = await db.execute(count_stmt)
        count_map = {str(rid): c for rid, c in counts.all()}
    else:
        count_map = {}
    return [_object_type_dict(t) | {"object_count": count_map.get(str(t.id), 0)} for t in types]


async def _load_schema_list(
    db: AsyncSession, schema_filter=None, object_filter=None, type_filter=None
) -> list[dict]:
    stmt = select(Schema).order_by(Schema.created_at.asc(), Schema.id.asc())
    if schema_filter is not None:
        stmt = stmt.where(schema_filter)
    result = await db.execute(stmt)
    schemas = result.scalars().all()
    if not schemas:
        return []
    ids = [s.id for s in schemas]
    obj_stmt = (
        select(Object.schema_id, func.count())
        .where(Object.schema_id.in_(ids)).group_by(Object.schema_id))
    if object_filter is not None:
        obj_stmt = obj_stmt.where(object_filter)
    type_stmt = (
        select(ObjectType.schema_id, func.count())
        .where(ObjectType.schema_id.in_(ids)).group_by(ObjectType.schema_id))
    if type_filter is not None:
        type_stmt = type_stmt.where(type_filter)
    obj_counts = await db.execute(obj_stmt)
    type_counts = await db.execute(type_stmt)
    oc = {str(rid): c for rid, c in obj_counts.all()}
    tc = {str(rid): c for rid, c in type_counts.all()}
    out = []
    for s in schemas:
        d = _schema_dict(s)
        d["object_count"] = oc.get(str(s.id), 0)
        d["object_type_count"] = tc.get(str(s.id), 0)
        out.append(d)
    return out


# NOTE: registered BEFORE /{schema_id} — a literal "list" path segment must
# never be captured by the id route (FastAPI matches in registration order).
@schemas_router.get("/list")
async def public_list_schemas(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("schemas")),
):
    key = f"cache:public:schemas:list:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    schema_filter = await build_record_permission_filter(
        db, str(current_user.id), "schema", is_superuser=current_user.is_superuser)
    object_filter = await build_record_permission_filter(
        db, str(current_user.id), "object", is_superuser=current_user.is_superuser)
    type_filter = await build_record_permission_filter(
        db, str(current_user.id), "object_type", is_superuser=current_user.is_superuser)
    data = await _load_schema_list(db, schema_filter, object_filter, type_filter)
    shape = {"objectschemas": [build_schema_shape(d) for d in data]}
    await set_cached(key, shape, ttl=60)
    return shape


@schemas_router.get("/{schema_id}")
async def public_get_schema(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("schemas", "schema_id")),
):
    key = f"cache:public:schema:{schema_id}:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    object_filter = await build_record_permission_filter(
        db, str(current_user.id), "object", is_superuser=current_user.is_superuser)
    type_filter = await build_record_permission_filter(
        db, str(current_user.id), "object_type", is_superuser=current_user.is_superuser)
    data = await _load_schema(db, schema_id, object_filter, type_filter)
    if data is None:
        raise HTTPException(status_code=404, detail="Schema not found")
    shape = build_schema_shape(data)
    await set_cached(key, shape, ttl=60)
    return shape


@schemas_router.get("/{schema_id}/objecttypes")
async def public_get_schema_object_types(
    schema_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("schemas", "schema_id")),
):
    key = f"cache:public:schema:{schema_id}:objecttypes:{current_user.id}"
    cached = await get_cached(key)
    if cached is not None:
        return cached
    type_filter = await build_record_permission_filter(
        db, str(current_user.id), "object_type", is_superuser=current_user.is_superuser)
    object_filter = await build_record_permission_filter(
        db, str(current_user.id), "object", is_superuser=current_user.is_superuser)
    types = await _load_schema_object_types(db, schema_id, type_filter, object_filter)
    if types is None:
        raise HTTPException(status_code=404, detail="Schema not found")
    shape = [build_object_type_shape(t) for t in types]
    await set_cached(key, shape, ttl=60)
    return shape
