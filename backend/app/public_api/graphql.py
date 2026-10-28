"""Public GraphQL router (Plan 205-02) — D-01/D-11/D-12.

Second ``GraphQLRouter`` instance over the D-05-reduced ``public_schema``
(Plan 205-01), mounted at ``/public/api/v1/graphql`` via
``public_api/router.py`` with an explicit ``prefix="/graphql"``
(``GraphQLRouter`` registers routes at ``/`` internally — unlike the REST
sub-routers it does NOT self-carry a prefix; mirror ``main.py``'s
``app.include_router(graphql_router, prefix="/api/v1/graphql")``).

Auth swap: ``context_getter=get_public_context`` resolves the service-user
identity through ``get_public_api_consumer`` (imsh_ bearer fast-path)
instead of ``get_current_user`` — a user JWT is rejected with 401 before
Strawberry executes (GQL-01, T-205-04).

D-12: ``graphql_ide=None`` ALWAYS — no GraphiQL for the public endpoint,
regardless of ``settings.environment`` (T-205-06).  Introspection stays
gated behind the SA-token context_getter: only authenticated consumers
reach the schema.

Rate limiting (Plan 205-03): SlowAPI's ``_exempt_routes`` is identity-scoped
by (module, function name) on Strawberry's shared ``handle_http_get/post``
handlers, so the internal router's exemption is silently inherited by this
public router — neither ``@limiter.limit`` nor ``_exempt_routes`` can
discriminate between the two ``GraphQLRouter`` instances (RESEARCH.md).
Enforcement is therefore a pre-Strawberry FastAPI dependency attached at
the include site (``dependencies=[Depends(enforce_public_graphql_rate_limit)]``
in ``public_api/router.py``): it runs before Strawberry, ONLY on the public
routes (path scoping by attachment, not path-string matching), and its
``RateLimitExceeded`` is caught by ``main.py``'s existing handler, which
reuses the standard public 429 envelope/headers (D-09).
"""

from fastapi import Request
from limits import parse
from slowapi.errors import RateLimitExceeded
from slowapi.wrappers import Limit

from app.graphql.context import get_public_context
from app.graphql.router import LimitedGraphQLRouter
from app.public_api.graphql_schema import public_schema
from app.services.rate_limit import (
    PUBLIC_API_RATE_LIMIT,
    get_rate_limit_key,
    limiter,
)

# Phase 312 (GQLC-05/D-21): query-over-GET is refused on the public path too
# (plain-text 400 from Strawberry's GET handler, T-312-01) — the SA-token
# context_getter still runs first, so an invalid token stays 401.
# Phase 312 (GQLC-04/D-17): the SAME `LimitedGraphQLRouter` subclass as the
# internal router — the wall clock cannot be path-dependent. The import
# direction is safe: nothing under app/graphql imports app.public_api.
graphql_router = LimitedGraphQLRouter(
    public_schema,
    context_getter=get_public_context,
    graphql_ide=None,
    allow_queries_via_get=False,
)

# T-205-07 — the public GraphQL per-token rate-limit bucket. Parsed once at
# import time from the same PUBLIC_API_RATE_LIMIT env the REST public routes
# use (D-10), so changing the env var changes the enforced limit live.
_PUBLIC_GQL_RATE_ITEM = parse(PUBLIC_API_RATE_LIMIT)


async def enforce_public_graphql_rate_limit(request: Request) -> None:
    """Pre-Strawberry per-token rate enforcement for the public GraphQL path.

    Runs as a FastAPI dependency attached to the public ``GraphQLRouter``
    include ONLY (``public_api/router.py``): the internal
    ``/api/v1/graphql`` mount carries no such dependency and stays
    unlimited (T-205-09).  Keying reuses ``get_rate_limit_key`` verbatim,
    so imsh_ tokens bucket into ``public:<sha256(token)>`` — two service
    accounts behind one egress IP are limited independently (AUTH-06,
    T-205-07 per-token keying).

    ``request.state.view_rate_limit`` MUST be set before raising
    ``RateLimitExceeded``: main.py's ``_dispatch_rate_limit_exceeded_handler``
    reads it to inject the X-RateLimit-*/Retry-After headers via
    ``limiter._inject_headers`` — without it the handler errors (D-09
    consistent public 429 envelope/headers).
    """
    if not limiter.enabled:
        # Respects RATE_LIMIT_ENABLED, matching the rest of the stack.
        return
    key = get_rate_limit_key(request)
    request.state.view_rate_limit = (_PUBLIC_GQL_RATE_ITEM, [key])
    if not limiter.limiter.hit(_PUBLIC_GQL_RATE_ITEM, key, cost=1):
        raise RateLimitExceeded(
            Limit(
                _PUBLIC_GQL_RATE_ITEM,
                get_rate_limit_key,
                None,
                False,
                None,
                None,
                None,
                1,
                False,
            )
        )
