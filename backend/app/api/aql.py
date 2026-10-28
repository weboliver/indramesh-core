"""AQL REST API router.

Provides the POST /aql/objects endpoint that accepts AQL query strings,
executes them via the existing AQL compiler pipeline, and returns paginated
ObjectResponse results with structured error handling.

See Phase 191 decisions D-01 through D-08 in 191-CONTEXT.md.
"""
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.schemas.crud import AQLQuery, AQLResponse, AQLSuggestRequest, AQLSuggestResponse, ObjectResponse
from app.schemas.auth import UserResponse
from app.services.auth_service import require_permission
from app.services.classification_service import attach_classifications
from app.aql.compiler import execute_aql
from app.aql.errors import AQLParseError, AQLValidationError
from app.aql.suggest import get_suggestions


router = APIRouter(prefix="/aql", tags=["aql"])


@router.post("/objects", response_model=AQLResponse, status_code=status.HTTP_200_OK)
async def execute_aql_query(
    query: AQLQuery,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
):
    """Execute an AQL query and return paginated object results.

    AQL is a read surface over objects, so it asks for the same type-level
    ``objects:read`` the REST object read routes ask for (317-04 D-06). The
    gate replaces the bare ``get_current_user`` dependency, so an
    authenticated principal without the grant is refused before the query is
    compiled.

    Accepts a JSON body with:
    - qlQuery: the AQL query string
    - page: 1-based page number (default 1)
    - pageSize: results per page (default 100, max 500)

    Error handling:
    - AQLParseError → 422 with PARSE_ERROR code, line, column (D-02)
    - AQLValidationError → 422 with VALIDATION_ERROR code (D-03)
    """
    # Execute query
    user_role = "admin" if current_user.is_superuser else "user"
    skip = (query.page - 1) * query.pageSize

    try:
        items, total = await execute_aql(
            db,
            aql_string=query.qlQuery,
            skip=skip,
            limit=query.pageSize,
            user_role=user_role,
            user_id=str(current_user.id),
            is_superuser=current_user.is_superuser,
        )
    except AQLParseError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "PARSE_ERROR",
                    "message": str(e),
                    "line": e.line,
                    "column": e.column,
                }
            },
        )
    except AQLValidationError as e:
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": str(e),
                }
            },
        )
    except ValueError as e:
        # Backstop (317-04 Task 4): the filter layer raises ValueError for an
        # operator its type dispatch does not implement. A grammatically valid
        # query must answer the same 422 shape, never a 500 — so a future
        # operator added to the grammar before the filter layer learns it is a
        # validation error, not a crash.
        raise HTTPException(
            status_code=422,
            detail={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": str(e),
                }
            },
        )

    # D-07: the AQL producer returns the same object shape as the REST list —
    # labels are attached to ORM rows (fast path) or cached dicts (slow path)
    # before the response model validates them.
    await attach_classifications(db, "object", list(items))
    return AQLResponse(
        items=[ObjectResponse.model_validate(item) for item in items],
        total=total,
        page=query.page,
        page_size=query.pageSize,
    )


@router.post("/suggest", response_model=AQLSuggestResponse, status_code=status.HTTP_200_OK)
async def suggest_aql(
    query: AQLSuggestRequest,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("objects", "read")),
):
    """Return classified next-token autocomplete suggestions for a partial AQL query.

    Advisory-only, but it still reads object-type and attribute names, so the
    same ``objects:read`` gate as ``/aql/objects`` applies (317-04 D-06).

    Read-only and advisory-only (D-03): a structurally-invalid pre-cursor
    prefix returns an empty `suggestions` list, never a 422 parse error.
    Sits behind the existing global 200/min rate limiter with no exemption
    and no dedicated override (D-02/AC-10).
    """
    suggestions = await get_suggestions(
        db,
        ql_query=query.qlQuery,
        cursor_pos=query.cursorPos,
        user_id=current_user.id,
        is_superuser=current_user.is_superuser,
    )
    return AQLSuggestResponse(suggestions=suggestions)
