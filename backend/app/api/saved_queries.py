import uuid
from typing import List
from fastapi import APIRouter, Depends, HTTPException, Request, status, Query, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import (
    SavedQueriesExportDocument,
    SavedQueriesImportResult,
    SavedQueryCreate,
    SavedQueryExecuteRequest,
    SavedQueryUpdate,
    SavedQueryResponse,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import get_current_user, require_permission
from app.services.saved_query_service import (
    create_saved_query, list_saved_queries, get_saved_query, update_saved_query, delete_saved_query,
    execute_saved_query, export_saved_queries, import_saved_queries, SavedQueryValidationError,
)

router = APIRouter(prefix="/saved-queries", tags=["saved-queries"])


def _validation_error_detail(exc: SavedQueryValidationError) -> dict:
    """The one 422 payload shape for an invalid saved-query document (D-07).

    Every failure carries ``path`` and ``message``; a limit breach adds the SAME
    four machine-readable fields an execute-time rejection carries
    (``code``/``limit``/``actual``/``allowed``), because both come from the same
    analyzer output — never two hand-written dicts.
    """
    return {"path": exc.path, "message": exc.reason, **(exc.extensions or {})}


@router.post("/", response_model=SavedQueryResponse, status_code=status.HTTP_201_CREATED)
async def create_saved_query_route(
    query_data: SavedQueryCreate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "create"))
):
    try:
        return await create_saved_query(db, query_data, current_user.id)
    except SavedQueryValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_error_detail(exc))


@router.get("/", response_model=List[SavedQueryResponse])
async def list_saved_queries_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "read")),
    skip: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=1000),
    name: str | None = Query(None),
    response: Response = None,
):
    items, total = await list_saved_queries(db, skip=skip, limit=limit, name=name)
    response.headers["X-Total-Count"] = str(total)
    return items


# 304 Pitfall 5: the literal /export and /import paths MUST be declared before
# the parameterized /{query_id} route, or FastAPI would try to parse "export"
# as a UUID and shadow these handlers.
@router.get("/export", response_model=SavedQueriesExportDocument)
async def export_saved_queries_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "read")),
):
    """Export every saved query as the versioned portability envelope (GQL-04)."""
    return await export_saved_queries(db)


@router.post("/import", response_model=SavedQueriesImportResult)
async def import_saved_queries_route(
    document: SavedQueriesExportDocument,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "create")),
):
    """Import a document all-or-nothing (304 D-09/D-10 / GQL-04).

    An entry that fails validation (non-query GraphQL, oversized text/variables)
    rejects the whole document with HTTP 422 carrying a ``queries[i].<field>``
    path and imports nothing.
    """
    try:
        return await import_saved_queries(db, document, current_user.id)
    except SavedQueryValidationError as exc:
        raise HTTPException(
            status_code=422, detail=_validation_error_detail(exc)
        )


@router.get("/{query_id}", response_model=SavedQueryResponse)
async def get_saved_query_route(
    query_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "read"))
):
    query_item = await get_saved_query(db, query_id)
    if not query_item:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return query_item


@router.patch("/{query_id}", response_model=SavedQueryResponse)
async def update_saved_query_route(
    query_id: uuid.UUID,
    update_data: SavedQueryUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "update"))
):
    try:
        query_item = await update_saved_query(db, query_id, update_data, current_user.id)
    except SavedQueryValidationError as exc:
        raise HTTPException(status_code=422, detail=_validation_error_detail(exc))
    if not query_item:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return query_item


@router.delete("/{query_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_saved_query_route(
    query_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("saved-queries", "delete"))
):
    deleted = await delete_saved_query(db, query_id, current_user.id)
    if not deleted:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return None


@router.post("/{query_id}/execute")
async def execute_saved_query_route(
    query_id: uuid.UUID,
    body: SavedQueryExecuteRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _objects_read: UserResponse = Depends(require_permission("objects", "read")),
    current_user: UserResponse = Depends(require_permission("saved-queries", "execute"))
):
    """Run a stored query in-process in the caller's context (304 GQL-02/D-06).

    Request-level failures map to 4xx (422 invalid document, 404 missing query,
    403 without ``saved-queries:execute``); GraphQL-level failures stay HTTP 200
    with a ``{data, errors}`` envelope.

    ``objects:read`` is required IN ADDITION to ``saved-queries:execute``
    (317-04 D-06): whatever the stored document is, executing it is a read of
    objects through the schema, and the type-level gate must not be skippable
    by wrapping the query in a saved-query row. It is declared first so a
    principal holding neither grant is told about the object-read gate.
    """
    try:
        result = await execute_saved_query(
            db,
            query_id,
            body.variables,
            body.operation_name,
            current_user,
            request=request,
            response=response,
            lang=request.headers.get("X-Lang"),
        )
    except SavedQueryValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.reason)
    if result is None:
        raise HTTPException(status_code=404, detail="Saved query not found")
    return result
