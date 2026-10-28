import logging
import uuid
from typing import Callable

from starlette.exceptions import HTTPException
from starlette.responses import JSONResponse, Response

from app.logging.context import get_request_state
from app.middleware.request_context import internal_error_response

logger = logging.getLogger(__name__)


class ExceptionHandlerMiddleware:
    """Catches unhandled exceptions and returns a consistent 500 JSON response.

    Registered *inside* RequestContextMiddleware, not outermost: the outer
    layer is the one that sees every status (413/429/500) for the access
    record and 500 fallback. An HTTPException that reaches this layer is
    rendered as its own response instead of being re-raised: a re-raise would
    hand it to the outer RequestContextMiddleware's generic 500 fallback and
    turn a 4xx into a 500. In practice route/dependency HTTPExceptions are
    handled by Starlette's innermost ExceptionMiddleware before they arrive
    here. The fixed 500 envelope carrying ``request_id`` is shared with the
    outer layer through ``internal_error_response`` (D-12).
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except HTTPException as exc:
            # Preserve the HTTPException's status/detail (per D-03) by sending
            # its own response, instead of re-raising it through the outer 500
            # fallback.
            if exc.status_code in (204, 304):
                response = Response(status_code=exc.status_code, headers=exc.headers)
            else:
                response = JSONResponse(
                    {"detail": exc.detail},
                    status_code=exc.status_code,
                    headers=exc.headers,
                )
            await response(scope, receive, send)
        except Exception as exc:
            # The traceback is rendered without frame locals and redacted by the
            # logging pipeline (LOG-07, D-19).
            logger.error("unhandled exception", exc_info=exc)

            state = get_request_state()
            request_id = (
                state.request_id if state is not None else uuid.uuid4().hex
            )
            await internal_error_response(request_id)(scope, receive, send)
