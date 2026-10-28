import asyncio

from fastapi import Depends, HTTPException
from strawberry.fastapi import GraphQLRouter

from app.config import settings
from app.graphql.context import get_context
from app.graphql.limits import (
    CODE_QUERY_TIMEOUT,
    TIMEOUT_SOURCE_WALL_CLOCK,
    _caller_id,
    _log_limit_rejection,
    _request_path,
    timeout_result,
)
from app.graphql.schema import schema
from app.plugin_system import get_plugin_registry
from app.services.rate_limit import limiter


async def graphql_endpoint_gate() -> None:
    """Request-time production gate for ``/api/v1/graphql`` (GQL-06/D-16/D-17).

    Outside production the endpoint is unchanged. In production it is served
    only while a plugin declaring ``enables_http_graphql`` is active; otherwise
    the request gets a plain 404 with no hint of plugin state. An uninitialized
    registry (startup not complete) fails closed to 404.

    Attached as a router-level dependency so it runs before the endpoint's own
    auth dependency: an unauthenticated production request without the plugin
    gets 404, not 401 (D-17). Evaluated per request, so a plugin
    activation/deactivation takes effect without a backend restart.
    """
    if settings.environment != "production":
        return
    try:
        registry = get_plugin_registry()
    except RuntimeError:
        # Startup has not built the singleton yet — fail closed in production.
        raise HTTPException(status_code=404, detail="Not Found")
    if await registry.has_active_plugin_declaring("enables_http_graphql"):
        return
    raise HTTPException(status_code=404, detail="Not Found")


class LimitedGraphQLRouter(GraphQLRouter):
    """GraphQLRouter with the GQLC-04/D-17 wall-clock bound.

    ``execute_operation`` is the one hook both HTTP routers own (the saved-query
    path bypasses the router entirely and gets its own wrapper in 312-06), so
    bounding it here bounds every HTTP operation. ``asyncio.timeout`` cancels
    the whole operation — resolvers, DataLoader dispatches and in-flight
    database work included; the timeout value is read from
    ``settings.graphql_execution_timeout`` at CALL time (a monkeypatching test
    reaches it; an operator restarts the backend).

    On expiry the operation ends as a normal HTTP 200 ``{data, errors}``
    response carrying the QUERY_TIMEOUT/wall_clock contract (D-17/D-18) — never
    an HTTP 504, and ``data`` is None (D-20). The one structured rejection
    record marks the source just like the response does (D-18).

    The base method is invoked with KEYWORD arguments (see
    ``AsyncBaseHTTPView.run``), so the override declares the same parameter
    names and forwards them by keyword; renaming one silently breaks the call.
    """

    async def execute_operation(
        self,
        request,
        request_adapter,
        request_data,
        context,
        root_value,
        sub_response,
    ):
        # The context manager object — not just its expiry — is kept: on
        # Python 3.11+ ``TimeoutError`` is the same class ``socket.timeout`` and
        # ``asyncio.TimeoutError`` use, so an operation can raise a TimeoutError
        # that has nothing to do with this wall clock. Only ``expired()`` tells
        # the two apart; rewriting an inner failure into a successful
        # QUERY_TIMEOUT envelope would hide a real error from operator and
        # client alike.
        timeout = asyncio.timeout(settings.graphql_execution_timeout)
        try:
            async with timeout:
                return await super().execute_operation(
                    request=request,
                    request_adapter=request_adapter,
                    request_data=request_data,
                    context=context,
                    root_value=root_value,
                    sub_response=sub_response,
                )
        except TimeoutError:
            if not timeout.expired():
                # An inner timeout, not the wall clock: propagate it untouched.
                raise
            # Header writes on sub_response survive: create_response() copies
            # sub_response.headers.raw onto the final Starlette response.
            sub_response.headers["X-GraphQL-Timeout"] = TIMEOUT_SOURCE_WALL_CLOCK
            _log_limit_rejection(
                CODE_QUERY_TIMEOUT,
                source=TIMEOUT_SOURCE_WALL_CLOCK,
                path=_request_path(context),
                user=_caller_id(context),
            )
            return timeout_result()


# Create the Strawberry GraphQL router
# Per D-179-01 and D-179-02: use GraphQLRouter (APIRouter), NOT app.mount()
# Registered directly via app.include_router() — FastAPI resolves Depends()
# in context_getter automatically.
# Phase 304 (GQL-06/D-16): always registered; the production gate above hides
# the endpoint unless a plugin declaring `enables_http_graphql` is active.
# The Core GraphiQL IDE was removed (plan 304-03) — `graphql_ide=None` always.
# Per D-185-01: introspection is gated by auth only (Depends(get_current_user)
# in get_context above), not by a DisableIntrospection extension on the schema.
# Phase 312 (GQLC-05/D-21): a `query` in the URL is refused with a
# plain-text 400 before execution, so query text stays out of access logs
# (T-312-01). Auth still runs first: an unauthenticated GET stays 401.
# Phase 312 (GQLC-04/D-17): `LimitedGraphQLRouter` wraps every operation in the
# wall clock; the public router is a second instance of the same class.
graphql_router = LimitedGraphQLRouter(
    schema,
    context_getter=get_context,
    graphql_ide=None,
    allow_queries_via_get=False,
    dependencies=[Depends(graphql_endpoint_gate)],
)

# Per D-179-07: Exempt GraphQL endpoint from SlowAPI rate limiting.
# Strawberry's GraphQLRouter registers internal route handlers at module
# strawberry.fastapi.router with names handle_http_get / handle_http_post.
# Adding them to limiter._exempt_routes causes SlowAPI to skip rate
# limiting for all GraphQL requests. Nginx perimeter rate limiting
# (1000r/m on /api/) still applies.
limiter._exempt_routes.update([
    "strawberry.fastapi.router.handle_http_get",
    "strawberry.fastapi.router.handle_http_post",
])