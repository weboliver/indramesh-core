"""Permission-gated runtime logging-level API (D-01/D-12/D-13/D-21)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.logging.levels import LevelController
from app.schemas.auth import UserResponse
from app.schemas.logging import LogLevelsResponse, LogLevelsUpdate
from app.services.auth_service import require_permission
from app.services.logging_service import (
    get_log_levels,
    local_logger_categories,
    update_log_levels,
)

router = APIRouter(prefix="/logging", tags=["logging"])


def get_level_controller(request: Request) -> LevelController | None:
    """The process's singleton controller, or ``None`` when not started."""
    return getattr(request.app.state, "level_controller", None)


def get_logging_discovery(request: Request):
    """The process's discovery reporter, or the local-logger fallback.

    Falls back to ``local_logger_categories`` when the app lifespan did not
    build a reporter (e.g. a test harness), so GET/PUT still offer real local
    categories.
    """
    discovery = getattr(request.app.state, "logging_discovery", None)
    return discovery if discovery is not None else local_logger_categories


def _request_context(request: Request, user: UserResponse) -> dict:
    client = request.client
    return {
        "client_ip": client.host if client is not None else None,
        "method": request.method,
        "route": request.url.path,
        "status": 200,
        "user_id": str(user.id),
    }


@router.get("/levels", response_model=LogLevelsResponse)
async def list_log_levels(
    db: AsyncSession = Depends(get_db),
    discovery=Depends(get_logging_discovery),
    controller: LevelController | None = Depends(get_level_controller),
    current_user: UserResponse = Depends(require_permission("settings", "read")),
):
    policy = controller.policy if controller is not None else None
    return await get_log_levels(db, discovery, policy)


@router.put("/levels", response_model=LogLevelsResponse)
async def put_log_levels(
    request: Request,
    body: LogLevelsUpdate,
    db: AsyncSession = Depends(get_db),
    discovery=Depends(get_logging_discovery),
    controller: LevelController | None = Depends(get_level_controller),
    current_user: UserResponse = Depends(require_permission("settings", "update")),
):
    return await update_log_levels(
        db,
        body,
        current_user.id,
        controller=controller,
        discovery=discovery,
        request_context=_request_context(request, current_user),
    )
