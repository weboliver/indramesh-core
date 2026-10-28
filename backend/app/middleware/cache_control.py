"""Middleware adding Cache-Control and ETag headers to cacheable GET responses.

Phase 106 — API-01 (gzip, Nginx layer), API-03 (Cache-Control + ETag).
Phase 270 — K8SFIX-01: scoped Cache-Control to single-entity GETs only;
CR-01 code-review fix: private (not public) + Vary: Authorization, since
every cacheable entity type enforces per-record RBAC and a shared cache
serving one user's response to another would bypass that check.

Cache-Control: private, max-age=900 for single-entity GETs of static
entities (sources, schemas, attribute-types, reference-types, icons).
Weak ETags (W/"{entity_type}-{id}-{updated_at}") on the same single-entity
responses.
List responses (e.g. GET /api/v1/sources/) get neither header: a list is
exactly the resource a create/update/delete mutation needs the client to
see fresh, and a long-lived public Cache-Control on it lets the browser's
own HTTP cache silently absorb the post-mutation refetch ahead of any
application-level cache invalidation (K8SFIX-01).

T-106-01: Cache-Control scope limited to static entities — no cache headers
on objects/values/refs that carry user-sensitive data.
"""

import json
from typing import Callable

# Entity URL path prefixes mapped to entity type names for ETag generation.
# Matched via prefix (path.startswith(prefix)) so it catches both list
# (/api/v1/sources/) and detail (/api/v1/sources/{id}) endpoints.
CACHEABLE_PATHS: dict[str, str] = {
    "/api/v1/sources/": "source",
    "/api/v1/schemas/": "schema",
    "/api/v1/attribute-types/": "attribute_type",
    "/api/v1/reference-types/": "reference_type",
    "/api/v1/icons/": "icon",
}


class CacheControlMiddleware:
    """ASGI middleware that adds Cache-Control and ETag headers to
    cacheable GET responses for static entities.

    Follows the same ASGI middleware pattern as RequestContextMiddleware:
    wraps the send callable to intercept and modify response headers.
    """

    def __init__(self, app: Callable) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        # Only intercept GET HTTP requests
        if scope["type"] != "http" or scope.get("method") != "GET":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        entity_type = next(
            (etype for prefix, etype in CACHEABLE_PATHS.items() if path.startswith(prefix)),
            None,
        )
        if entity_type is None:
            await self.app(scope, receive, send)
            return

        # Cacheable path: buffer the entire response body so we can
        # inspect it for ETag computation *before* sending the start
        # message (which is when headers are serialized per ASGI spec).
        start_message: dict | None = None
        body_chunks: list[bytes] = []

        async def buffering_send(message: dict) -> None:
            nonlocal start_message

            if message["type"] == "http.response.start":
                # Hold back — we need the body to compute ETag
                start_message = message
                return

            if message["type"] == "http.response.body":
                body_chunks.append(message.get("body", b""))
                if message.get("more_body", False):
                    return  # wait for remaining chunks

                assert start_message is not None
                status_code = start_message.get("status", 200)
                headers = list(start_message.get("headers", []))
                body = b"".join(body_chunks)

                if 200 <= status_code < 300:
                    # Only single-entity responses (JSON dict with id and
                    # updated_at) get Cache-Control and an ETag — list
                    # responses get neither header.
                    try:
                        data = json.loads(body)
                    except json.JSONDecodeError:
                        data = None

                    if isinstance(data, dict) and "id" in data and "updated_at" in data:
                        headers.append((b"cache-control", b"private, max-age=900"))
                        headers.append((b"vary", b"Authorization"))
                        etag_value = (
                            f'W/"{entity_type}-{data["id"]}-{data["updated_at"]}"'
                        )
                        headers.append((b"etag", etag_value.encode()))

                start_message["headers"] = headers
                await send(start_message)
                await send({"type": "http.response.body", "body": body, "more_body": False})
                return

            await send(message)

        await self.app(scope, receive, buffering_send)
