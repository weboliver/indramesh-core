"""Outermost request-correlation middleware (LOG-04, LOG-06, D-14/D-17).

Pure ASGI so it wraps every inner middleware, including the BaseHTTPMiddleware
ones. It assigns the ``X-Request-ID``, emits exactly one ``access`` record,
serves the shared 500 envelope and (in development) adds the timing headers.
"""

import json
import logging
import os
import re
import time
import uuid
from typing import Callable

from starlette.requests import HTTPConnection
from starlette.responses import Response
from prometheus_fastapi_instrumentator.routing import get_route_name

from app.config import settings
from app.logging.context import (
    RequestLogState,
    reset_request_state,
    set_request_state,
)
from app.logging.security import emit_security_record
from app.middleware.query_profiler import get_query_count, reset_query_count

_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9-]{8,64}$")

# Probe endpoints keep exactly one access record but at DEBUG, so it stays
# invisible at the default INFO level (D-13).
PROBE_PATHS = frozenset({"/health", "/metrics"})

_DEFAULT_SLOW_THRESHOLD_MS = 500

_access_logger = logging.getLogger("access")
_module_logger = logging.getLogger("app.middleware.request_context")


def resolve_request_id(raw: bytes | None) -> str:
    value = raw.decode("latin-1") if raw else ""
    return value if _REQUEST_ID_RE.fullmatch(value) else uuid.uuid4().hex


def internal_error_response(request_id: str) -> Response:
    """The one shared 500 envelope: fixed detail plus the correlation id (D-12)."""
    body = json.dumps(
        {"detail": "Internal server error", "request_id": request_id}
    ).encode("utf-8")
    return Response(content=body, status_code=500, media_type="application/json")


def _resolve_route(scope: dict) -> str | None:
    try:
        return get_route_name(HTTPConnection(scope), False)
    except Exception:
        return None


class RequestContextMiddleware:
    """Assign a validated request id, echo it, and emit one access record."""

    def __init__(self, app: Callable) -> None:
        self.app = app
        # T-102-01: the dev-only timing headers expose internal timing/query
        # metrics, so they are read once at construction and only emitted when
        # ENVIRONMENT=development — never in production.
        self._timing_enabled = settings.environment == "development"
        self._slow_threshold_ms = float(
            os.environ.get("SLOW_REQUEST_THRESHOLD_MS", str(_DEFAULT_SLOW_THRESHOLD_MS))
        )

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        if self._timing_enabled:
            reset_query_count(
                threshold_ms=float(os.environ.get("SLOW_QUERY_THRESHOLD_MS", "100"))
            )

        raw = None
        for name, value in scope.get("headers", []):
            if name == b"x-request-id":
                raw = value
                break

        request_id = resolve_request_id(raw)
        state = RequestLogState(request_id=request_id)
        token = set_request_state(state)

        start = time.perf_counter()
        status = 500
        response_started = False

        async def send_wrapper(message):
            nonlocal status, response_started
            if message["type"] == "http.response.start":
                response_started = True
                status = message["status"]
                headers = [
                    (key, value)
                    for key, value in message.get("headers", [])
                    if key.lower() != b"x-request-id"
                ]
                headers.append((b"x-request-id", request_id.encode("ascii")))
                if self._timing_enabled:
                    duration_ms = (time.perf_counter() - start) * 1000
                    query_count = get_query_count()
                    headers.append(
                        (b"x-response-time", f"{duration_ms:.1f}".encode())
                    )
                    headers.append(
                        (b"x-db-queries", str(query_count).encode())
                    )
                    if duration_ms > self._slow_threshold_ms:
                        _module_logger.debug(
                            "Slow request (%s %.1fms, %d queries): %s",
                            scope.get("method", "UNKNOWN"),
                            duration_ms,
                            query_count,
                            scope.get("path", "unknown"),
                        )
                message["headers"] = headers
            await send(message)

        try:
            try:
                await self.app(scope, receive, send_wrapper)
            except Exception as exc:
                if response_started:
                    raise
                # An exception raised outside ExceptionHandlerMiddleware (e.g.
                # a non-numeric Content-Length in MaximumContentLengthMiddleware)
                # must still yield the JSON 500 envelope — and must not be
                # re-raised, or Starlette's ServerErrorMiddleware would send a
                # second plain-text 500 and uvicorn a second error record.
                _module_logger.error(
                    "unhandled exception outside the exception handler",
                    exc_info=exc,
                )
                status = 500
                await internal_error_response(request_id)(
                    scope, receive, send_wrapper
                )
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000, 1)
            route = _resolve_route(scope)
            level = (
                logging.DEBUG
                if scope.get("path") in PROBE_PATHS
                else logging.INFO
            )
            _access_logger.log(
                level,
                "request completed",
                extra={
                    "method": scope.get("method"),
                    "route": route,
                    "status": status,
                    "duration_ms": duration_ms,
                },
            )
            # Exactly one security record per request, after the access record
            # and while the state is still set (D-08). ``scope["client"]`` was
            # rewritten in place by ProxyHeadersMiddleware for trusted proxies —
            # the same value get_remote_address uses.
            emit_security_record(
                state,
                method=scope.get("method"),
                route=route,
                status=status,
                client_ip=(scope.get("client") or (None,))[0],
            )
            reset_request_state(token)
