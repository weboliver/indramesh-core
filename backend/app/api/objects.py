import json
import re
import uuid
from typing import List
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, status, Query, Response, UploadFile, File
from fastapi.responses import JSONResponse, StreamingResponse
from sqlalchemy import select as sa_select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.models import Object
from app.schemas.crud import (
    ObjectCreate, ObjectUpdate, ObjectResponse, ObjectGraphResponse
)
from app.schemas.auth import UserResponse
from app.schemas.object_file import ObjectFileResponse
from app.schemas.object_comment import ObjectCommentCreate, ObjectCommentResponse
from app.services.auth_service import get_current_user, require_permission, require_record_permission
from app.services.record_permission_service import resolve_record_permission
from app.services.value_visibility import assert_container_access
from app.services.filter_utils import build_filter_clause
from app.services.translation_service import validate_read_language
from app.api.fields import objects_fields, filter_fields
from app.api.pagination import OBJECTS_DEFAULT_LIMIT, MAX_LIMIT
from app.services.object_service import (
    create_object, list_objects, get_object, update_object, delete_object
)
from app.services.classification_service import attach_classifications
from app.services.value_lock_service import ValueLockConflict, released_attribute_ids
from app.services.graph_service import get_object_graph
from app.services.object_file_service import (
    create_object_file as create_object_file_svc,
    list_object_files as list_object_files_svc,
    get_object_file as get_object_file_svc,
    stream_object_file as stream_object_file_svc,
    delete_object_file as delete_object_file_svc,
)
from app.services.object_comment_service import (
    create_object_comment as create_object_comment_svc,
    list_object_comments as list_object_comments_svc,
    delete_object_comment as delete_object_comment_svc,
)

router = APIRouter(prefix="/objects", tags=["objects"])

#: Characters that must never appear inside the quoted ASCII fallback of a
#: Content-Disposition filename (T-279-07): controls, quote, backslash.
_UNSAFE_FILENAME_CHARS = re.compile(r'[\x00-\x1f\x7f"\\]')


def _attachment_disposition(filename: str) -> str:
    """RFC-safe `attachment` disposition preserving the stored original name
    (D-13/D-15, T-279-07): the untrusted filename is percent-encoded into the
    RFC 5987 `filename*` parameter, never interpolated as raw header text;
    the quoted fallback is transliterated to ASCII-clean characters (non-ASCII
    bytes become '?', unsafe ASCII characters become '_').
    """
    ascii_fallback = filename.encode("ascii", "replace").decode("ascii")
    fallback = _UNSAFE_FILENAME_CHARS.sub("_", ascii_fallback)
    encoded = quote(filename, safe="")
    if fallback:
        return f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{encoded}'
    return f"attachment; filename*=UTF-8''{encoded}"

_ALLOWED_SORT_BY = {"name", "created", "updated"}
_ALLOWED_SORT_ORDER = {"asc", "desc"}
_ALLOWED_OPERATORS = {"eq", "gt", "lt", "gte", "lte", "between", "contains", "starts_with", "before", "after"}


def _validate_tree_ops(tree: dict) -> None:
    """Recursively validate that all operators in the filter tree are valid.

    Raises HTTPException with 422 if any invalid operator is found.
    """
    op = tree.get("op")
    if op and op not in ("AND", "OR"):
        raise HTTPException(
            status_code=422,
            detail=f"filter_tree: invalid operator '{op}'. Must be 'AND' or 'OR'"
        )

    # Validate conditions
    for cond in tree.get("conditions", []):
        cond_op = cond.get("op")
        if cond_op not in _ALLOWED_OPERATORS:
            raise HTTPException(
                status_code=422,
                detail=f"filter_tree: unknown operator '{cond_op}'. Valid operators: {', '.join(sorted(_ALLOWED_OPERATORS))}"
            )

    # Recursively validate child groups
    for group in tree.get("groups", []):
        _validate_tree_ops(group)


@router.post("/", response_model=ObjectResponse, status_code=status.HTTP_201_CREATED)
async def create_object_route(
    obj: ObjectCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "create"))
):
    # AUTHZ-07 (D-01/D-03): the container id arrives in the body, so only the
    # route can decide. Readability first — an invisible container answers the
    # route's existing missing-container 404, so it is indistinguishable from a
    # nonexistent one — then the create action (403). The check precedes the
    # service call, so no partial Object can survive a refusal.
    if not current_user.is_superuser:
        await assert_container_access(
            str(current_user.id),
            current_user.is_superuser,
            entity_type="object_type",
            container_id=obj.object_type_id,
            action="create",
            not_found_detail="Schema or object type not found",
        )
    try:
        db_object = await create_object(db, obj, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not db_object:
        raise HTTPException(status_code=404, detail="Schema or object type not found")
    return db_object


@router.get("/", response_model=List[ObjectResponse])
async def list_objects_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
    skip: int = Query(0, ge=0, description="Number of records to skip"),
    limit: int = Query(OBJECTS_DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description="Maximum records to return"),
    sort_by: str | None = Query(None, description="Sort field: 'name', 'created', or 'updated'"),
    sort_order: str | None = Query(None, description="Sort direction: 'asc' or 'desc'"),
    object_type_id: uuid.UUID = Query(None, description="Filter by object type ID"),
    q: str = Query(None, description="Filter by object name (case-insensitive substring match)"),
    filter_tree: str = Query(None, description="JSON filter tree with AND/OR groups (ADV-01)"),
    attr_filters: str = Query(None, description="JSON object mapping attribute IDs to filter values"),
    fields: list[str] | None = Depends(objects_fields),
    lang: str | None = Query(None, description="Resolve translated object names for this language code (BCP-47, e.g. 'de')"),
    response: Response = None,
):
    if sort_by is not None and sort_by not in _ALLOWED_SORT_BY:
        raise HTTPException(
            status_code=422,
            detail=f"sort_by must be one of: {', '.join(sorted(_ALLOWED_SORT_BY))}"
        )
    if sort_order is not None and sort_order not in _ALLOWED_SORT_ORDER:
        raise HTTPException(
            status_code=422,
            detail=f"sort_order must be one of: {', '.join(sorted(_ALLOWED_SORT_ORDER))}"
        )
    parsed_tree = None
    if filter_tree:
        try:
            parsed_tree = json.loads(filter_tree)
        except (json.JSONDecodeError, TypeError):
            raise HTTPException(status_code=422, detail="filter_tree: invalid JSON")
        # Basic structural validation
        if not isinstance(parsed_tree, dict):
            raise HTTPException(status_code=422, detail="filter_tree: must be a JSON object")
        if "op" not in parsed_tree:
            raise HTTPException(status_code=422, detail="filter_tree: missing required 'op' field")
        # Validate operators in tree recursively to catch bad op names early
        _validate_tree_ops(parsed_tree)

    parsed_filters = None
    if attr_filters and not parsed_tree:
        try:
            parsed_filters = json.loads(attr_filters)
        except (json.JSONDecodeError, TypeError):
            pass
    # Validate structured filter operators early — rejects unknown operator names with 422.
    # This does NOT know the attribute's dtype yet (that requires a DB lookup that only
    # happens inside list_objects()/build_filter_clause) — so it can only catch op names
    # that don't exist at all (e.g. "regex"), not type/operator mismatches (e.g. "gt" on a
    # TEXT attribute). That case is caught by build_filter_clause's ValueError, below.
    if parsed_filters:
        for attr_id_str, filter_val in list(parsed_filters.items()):
            if isinstance(filter_val, dict):
                op = filter_val.get("op")
                if op is None:
                    raise HTTPException(
                        status_code=422,
                        detail=f"attr_filters: missing required 'op' field in filter for attribute {attr_id_str}"
                    )
                if op not in ("eq", "gt", "lt", "gte", "lte", "between", "contains", "starts_with", "before", "after"):
                    raise HTTPException(
                        status_code=422,
                        detail=f"attr_filters: unknown operator '{op}' in filter for attribute {attr_id_str}. Valid operators: eq, gt, lt, gte, lte, between, contains, starts_with, before, after"
                    )
    user_role = "admin" if current_user.is_superuser else "user"
    if lang:
        # 222 D-05 / V-15: this route has no single-schema filter scope — the
        # list validates the code exists (unknown/inactive -> 400) and resolves
        # per item; never a list-wide 400 for individual objects.
        await validate_read_language(db, lang, None)
    try:
        items, total = await list_objects(
            db, skip=skip, limit=limit,
            object_type_id=object_type_id, q=q,
            filter_tree=parsed_tree,
            attr_filters=parsed_filters,
            sort_by=sort_by,
            sort_order=sort_order,
            user_role=user_role,  # NEW per D-11
            user_id=current_user.id,
            is_superuser=current_user.is_superuser,
            lang=lang,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    response.headers["X-Total-Count"] = str(total)
    if fields:
        filtered = [
            filter_fields(ObjectResponse.model_validate(item).model_dump(mode="json"), fields)
            for item in items
        ]
        return JSONResponse(content=filtered, headers=dict(response.headers))
    return items


@router.get("/counts")
async def get_object_counts_route(
    object_type_ids: List[uuid.UUID] = Query(..., description="Object type IDs to count objects for (batch — avoids one /objects/?limit=1 request per type)"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
):
    result = await db.execute(
        sa_select(Object.object_type_id, func.count())
        .where(Object.object_type_id.in_(object_type_ids))
        .group_by(Object.object_type_id)
    )
    return {str(object_type_id): count for object_type_id, count in result.all()}


@router.get("/by-key/{key}", response_model=ObjectResponse)
async def get_object_by_key_route(
    key: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
    schema_id: uuid.UUID | None = Query(None, description="Restrict lookup to a specific schema"),
    object_type_id: uuid.UUID | None = Query(None, description="Restrict lookup to a specific object type"),
):
    # 246 D-07: dedicated by-key lookup — the key is a second stable address
    # alongside UUID. Declared BEFORE /{object_id} so FastAPI never tries to
    # parse "by-key" as a UUID (Pitfall 5).
    query = sa_select(Object).where(Object.key == key)
    if schema_id is not None:
        query = query.where(Object.schema_id == schema_id)
    if object_type_id is not None:
        query = query.where(Object.object_type_id == object_type_id)
    result = await db.execute(query)
    obj = result.scalar_one_or_none()
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    # Record-level gate consistent with /{object_id}: type-level already
    # enforced above; a record DENY overrides the permissive default.
    perm = await resolve_record_permission(
        str(current_user.id), "object", obj.id, "read"
    )
    if perm is False:
        raise HTTPException(status_code=403, detail="Not authorized.")
    # D-07: the by-key producer reports the same classification shape as
    # /{object_id}; the lookup is not served from the entity cache.
    await attach_classifications(db, "object", [obj])
    return obj


@router.get("/{object_id}", response_model=ObjectResponse)
async def get_object_route(
    object_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "read", "object", "object_id")),
    lang: str | None = Query(None, description="Resolve translated object name for this language code (BCP-47, e.g. 'de')"),
):
    if lang:
        # V-15 / 222 D-05: the single-object scope is strict — the object's
        # schema envelope must contain the requested language. Runs on cache
        # hits too, so a payload cached under a since-disabled language can
        # never be served (224-03 pattern).
        obj_schema_id = (
            await db.execute(sa_select(Object.schema_id).where(Object.id == object_id))
        ).scalar_one_or_none()
        if obj_schema_id is not None:
            await validate_read_language(db, lang, obj_schema_id)
    obj = await get_object(db, object_id, lang=lang)
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    return obj


@router.get("/{object_id}/graph", response_model=ObjectGraphResponse)
async def get_object_graph_route(
    object_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "read", "object", "object_id"))
):
    graph = await get_object_graph(
        db, object_id, str(current_user.id), current_user.is_superuser
    )
    if graph is None:
        raise HTTPException(status_code=404, detail="Object not found")
    return graph


@router.get("/{object_id}/released-attributes", response_model=List[uuid.UUID])
async def list_released_attributes_route(
    object_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "read", "object", "object_id"))
):
    if not await _object_exists(db, object_id):
        raise HTTPException(status_code=404, detail="Object not found")
    return await released_attribute_ids(db, object_id)


async def _object_exists(db: AsyncSession, object_id: uuid.UUID) -> bool:
    result = await db.execute(sa_select(Object.id).where(Object.id == object_id))
    return result.scalar_one_or_none() is not None


@router.post("/{object_id}/files/upload", response_model=ObjectFileResponse, status_code=status.HTTP_201_CREATED)
async def upload_object_file_route(
    object_id: uuid.UUID,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "update", "object", "object_id")),
):
    # 279 tracer: record-update gate runs before the stream is touched (T-279-02);
    # the body only reaches this route through the scoped Nginx/middleware
    # exemption — the bounded chunk counter in object_file_service is the
    # authoritative per-file limit (T-279-01, D-09).
    if not await _object_exists(db, object_id):
        raise HTTPException(status_code=404, detail="Object not found")
    return await create_object_file_svc(
        db, object_id=object_id, upload=file, uploader_id=current_user.id
    )


@router.get("/{object_id}/files", response_model=List[ObjectFileResponse])
async def list_object_files_route(
    object_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "read", "object", "object_id")),
):
    if not await _object_exists(db, object_id):
        raise HTTPException(status_code=404, detail="Object not found")
    return await list_object_files_svc(db, object_id)


@router.get("/{object_id}/files/{file_id}/download")
async def download_object_file_route(
    object_id: uuid.UUID,
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "read", "object", "object_id")),
):
    # 279-02: record-read gate (T-279-05) before any storage access; the file
    # lookup is constrained by BOTH object and file IDs, so a cross-object
    # probe 404s identically to a missing file (no existence leak).
    if not await _object_exists(db, object_id):
        raise HTTPException(status_code=404, detail="Object not found")
    file = await get_object_file_svc(db, object_id, file_id)
    if not file:
        raise HTTPException(status_code=404, detail="Object file not found")
    return StreamingResponse(
        stream_object_file_svc(file),
        media_type=file.content_type or "application/octet-stream",
        headers={
            "Content-Disposition": _attachment_disposition(file.filename),
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox",
        },
    )


@router.delete("/{object_id}/files/{file_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_file_route(
    object_id: uuid.UUID,
    file_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "update", "object", "object_id")),
):
    # 279-02: record-update gate (T-279-06) + object-scoped service delete —
    # metadata commit happens before the best-effort blob cleanup.
    deleted = await delete_object_file_svc(db, object_id, file_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Object file not found")
    return None


@router.post("/{object_id}/comments", response_model=ObjectCommentResponse, status_code=status.HTTP_201_CREATED)
async def create_object_comment_route(
    object_id: uuid.UUID,
    payload: ObjectCommentCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "update", "object", "object_id")),
):
    # 280-02: comment create gates on the existing objects:update record
    # permission (D-15, no new permission action); author_id is taken
    # exclusively from the authenticated current_user (T-280-06) — the
    # create DTO declares no author_id field at all.
    if not await _object_exists(db, object_id):
        raise HTTPException(status_code=404, detail="Object not found")
    return await create_object_comment_svc(
        db, object_id=object_id, body=payload.body, author_id=current_user.id
    )


@router.get("/{object_id}/comments", response_model=List[ObjectCommentResponse])
async def list_object_comments_route(
    object_id: uuid.UUID,
    order: str = Query("asc", pattern="^(asc|desc)$"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "read", "object", "object_id")),
):
    if not await _object_exists(db, object_id):
        raise HTTPException(status_code=404, detail="Object not found")
    return await list_object_comments_svc(db, object_id, order=order)


@router.delete("/{object_id}/comments/{comment_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_comment_route(
    object_id: uuid.UUID,
    comment_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "update", "object", "object_id")),
):
    # 280-02: record-update gate (T-280-07) + object-scoped service delete
    # (T-280-04 dual filter — a comment UUID belonging to a different Object
    # 404s identically to a missing comment, never an existence leak). The
    # hard delete is a single atomic DELETE statement (COMMENT-04); the 404
    # on rowcount 0 keeps repeated/concurrent deletes idempotent.
    deleted = await delete_object_comment_svc(db, object_id, comment_id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Object comment not found")
    return None


@router.patch("/{object_id}", response_model=ObjectResponse)
async def update_object_route(
    object_id: uuid.UUID,
    object_update: ObjectUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "update", "object", "object_id"))
):
    # IN-01 / D-17 (319 D-03): a move into another container gates the TARGET
    # container, not only the subject object the route dependency already
    # checked. Readability first — an invisible target answers the route's
    # existing missing-container 404, so it is indistinguishable from one that
    # does not exist — then the update action (403). The gate precedes
    # `update_object(` because the service commits its own transaction: a later
    # check would refuse after the row was already re-parented. Both
    # `object_type_id` and `schema_id` are container changes (the latter is the
    # same re-parenting operation, one line away in the same payload).
    if not current_user.is_superuser:
        for entity_type, container_id in (
            ("object_type", object_update.object_type_id),
            ("schema", object_update.schema_id),
        ):
            if container_id is None:
                continue
            await assert_container_access(
                str(current_user.id),
                current_user.is_superuser,
                entity_type=entity_type,
                container_id=container_id,
                action="update",
                not_found_detail="Object not found",
            )
    try:
        obj = await update_object(db, object_id, object_update, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not obj:
        raise HTTPException(status_code=404, detail="Object not found")
    return obj


@router.delete("/{object_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_object_route(
    object_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_record_permission("objects", "delete", "object", "object_id"))
):
    try:
        deleted = await delete_object(db, object_id, current_user.id)
    except ValueLockConflict as e:
        raise HTTPException(status_code=409, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Object not found")
    return None
