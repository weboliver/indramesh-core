"""Public saved-query execution (quick 261011-sww).

Service accounts run admin-curated saved GraphQL queries against the internal
schema in the service user's context — a deliberate, saved-query-only
deviation from Phase 205 D-05 (reduced public GraphQL schema). Rate limiting
comes from the global per-token SlowAPI middleware (D-16, no route decorator).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.auth import UserResponse
from app.schemas.crud import SavedQueryExecuteRequest
from app.services.auth_service import require_public_hierarchy_read, require_public_permission
from app.services.saved_query_service import SavedQueryValidationError, execute_saved_query

saved_queries_router = APIRouter(prefix="/saved-queries", tags=["public-saved-queries"])


@saved_queries_router.post("/{query_id}/execute", status_code=status.HTTP_200_OK)
async def public_execute_saved_query(
    query_id: uuid.UUID,
    body: SavedQueryExecuteRequest,
    request: Request,
    response: Response,
    db: AsyncSession = Depends(get_db),
    _objects_read: UserResponse = Depends(require_public_hierarchy_read("objects")),
    current_user: UserResponse = Depends(require_public_permission("saved-queries", "execute")),
):
    """Execute a saved query as the calling service account (317-04 D-06).

    The public realm's own ``objects:read`` gate (``require_public_hierarchy_read``)
    runs first, alongside the existing ``saved-queries:execute`` grant: both
    are required, neither is a substitute for the other.
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
