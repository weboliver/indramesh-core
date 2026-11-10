import json
import logging
import os
import re
from contextlib import asynccontextmanager

from app.logging import configure_logging

configure_logging("backend")

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from uvicorn.middleware.proxy_headers import ProxyHeadersMiddleware
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIASGIMiddleware
from app.services.rate_limit import limiter, login_limiter

from app.api import (
    auth, sources, schemas,
    languages,
    translations, translation_types,
    ui_translations,
    attribute_types, audit_log_config, audit_logs, reference_types, object_types, objects,
    object_type_attributes, object_values, object_refs, statuses,
    classification,
    imports, exports, import_statuses, export_statuses,
    icons,
    saved_queries,
    schedules,
    tasks, targets, service_accounts, users, roles,
    api_clients,
    permissions,
    record_permissions,
    search,
    templates,
    schema_templates, config,
    settings,
    export_definitions,
    import_definitions,
    aql,
    groups,
    plugins,
)
from app.api import logging as logging_api
from app.graphql.router import graphql_router
from app.public_api.router import public_router
from app.middleware.exception_handler import ExceptionHandlerMiddleware
from secure.middleware import SecureASGIMiddleware
from app.middleware.security_headers import get_secure_headers
from app.middleware.cache_control import CacheControlMiddleware
from app.middleware.request_context import RequestContextMiddleware
from app.db.opensearch import init_opensearch, close_opensearch
from app.db.redis import init_redis, close_redis, close_arq_pool
from app.db import async_session_maker, log_identity_split_warning
from app.services.init_service import init_db_on_startup
from app.logging.levels import LevelController
from app.logging.discovery import LoggingCategoryDiscovery
from app.services.scheduler_manager import init_job_registry
from app.plugin_system import init_plugin_registry
from app.plugin_system.mounting import mount_plugin_routes
from app.crypto import init_fernet
from app.services.cache_service import invalidate_entity_cache
from app.services.secret_validation import validate_jwt_secret_key
from app.version import __version__

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # D-08 / Pitfall 10: an explicit DATABASE_URL silently disables the
    # runtime/maintenance identity split. Make the weakened state visible at
    # startup; never abort — the operator's URL is an allowed configuration.
    log_identity_split_warning(logger)

    # Phase 80: Validate security-critical environment variables BEFORE any initialization.
    # RuntimeErrors here are caught by ExceptionHandlerMiddleware (outermost middleware).

    # Validate JWT_SECRET_KEY: present AND at least 32 characters (SESS-04). One
    # helper, shared with auth_service.get_secret_key, names the variable and the
    # minimum; it never echoes the value.
    validate_jwt_secret_key(os.environ.get("JWT_SECRET_KEY"))

    # Validate FERNET_KEY (presence + format)
    fernet_key = os.environ.get("FERNET_KEY")
    if not fernet_key:
        raise RuntimeError(
            "FERNET_KEY environment variable is required but not set. "
            "Generate a valid key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    # Validate FERNET_KEY format by attempting to construct Fernet
    try:
        from cryptography.fernet import Fernet
        Fernet(fernet_key.encode() if isinstance(fernet_key, str) else fernet_key)
    except Exception as e:
        raise RuntimeError(
            f"FERNET_KEY is invalid: {e}. "
            "Generate a valid key with: python -c \"from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())\""
        )

    # Phase 95 — Validate CORS_ALLOWED_ORIGINS (security finding F-88-02-01)
    # The CORSMiddleware is configured at module import time below, so this
    # validation also happens implicitly there. We do it here too to give
    # the lifespan a single canonical place to fail-fast on env issues.
    _validate_cors_origins(os.environ.get("CORS_ALLOWED_ORIGINS"))

    # Initialize cryptographic module (creates global Fernet singleton)
    init_fernet()

    # Standard startup sequence
    await init_redis()
    await init_opensearch()
    # Seeds reference data (canonical attribute types, roles, catalogs, ...).
    # Alembic migrations are NOT run here — start.sh / the Helm migration job
    # applies them before the process starts (299 plan review correction).
    await init_db_on_startup()

    # Phase 299 (D-13/D-16/D-20): start the per-process runtime level controller
    # once Redis + DB are available. It reads the DB snapshot immediately
    # (applying it to the loggers that already exist), reports this process's
    # real logger projection as a bounded discovery lease, then polls every ~5s.
    level_discovery = LoggingCategoryDiscovery(
        session_factory=async_session_maker, role="backend"
    )
    level_controller = LevelController(
        session_factory=async_session_maker, discovery=level_discovery
    )
    app.state.logging_discovery = level_discovery
    app.state.level_controller = level_controller
    await level_controller.start()

    # Phase 104 — log connection pool configuration for production troubleshooting
    # of connection exhaustion issues (BLOCKER #2: explicit db/__init__.py -> main.py wiring)
    from app.db import get_pool_config
    pool_size, max_overflow, pool_recycle, pool_pre_ping = get_pool_config()
    logger.info(
        f"Database connection pool configured: "
        f"pool_size={pool_size}, max_overflow={max_overflow}, "
        f"pool_recycle={pool_recycle}s, pool_pre_ping={pool_pre_ping}"
    )

    # Flush stale Redis cache entries that may contain plaintext tokens
    # Must run AFTER init_db_on_startup() to ensure any cache writes during migration are covered
    await invalidate_entity_cache("service_account")

    init_job_registry()

    # PLUGCORE-10 / D-13: build this process's PluginRegistry from the core
    # `plugins` table. Must run after init_db_on_startup() (the table must
    # exist). Mirrors init_job_registry()'s dual-process wiring — the worker
    # does the same in startup_recovery(), so a kill-switch issued via the API
    # reaches both processes.
    async with async_session_maker() as plugin_session:
        plugin_registry = await init_plugin_registry(plugin_session)

    # PLUGCORE-09 / D-14/D-16: mount every plugin-declared route ONCE at startup
    # with the core-forced auth + state dependency chain. No hot-add/remove at
    # runtime — a killed plugin's route answers 503 (never disappears).
    await mount_plugin_routes(app, plugin_registry)

    # Plugin import created new module loggers; re-apply the active policy so a
    # pre-existing DB override reaches them before their first emit (D-15) and
    # refresh the discovery report so plugin-only categories are offered (D-20).
    level_controller.policy.apply_all()
    await level_discovery.heartbeat(force=True)

    yield

    # Shutdown: stop the level controller BEFORE closing Redis (the poll task
    # reads the version key) and let it remove its own discovery lease, then
    # close connections.
    await level_controller.stop()
    app.state.level_controller = None
    app.state.logging_discovery = None
    await close_arq_pool()
    await close_opensearch()
    await close_redis()


MAX_CONTENT_LENGTH = 1_048_576  # 1 MB

# Phase 279 (R-01): the object-file upload POST is exempt from the generic 1 MiB
# ceiling — its bounded chunk counter in object_file_service is the authoritative
# per-file limit (D-09, T-279-01). Nginx's matching location-level
# `client_max_body_size 0` is this middleware's gateway twin (nginx/nginx.conf).
_OBJECT_FILE_UPLOAD_PATH = re.compile(r"^/api/v1/objects/[^/]+/files/upload$")

# Phase 317 (CR314-24): the two CSV import uploads are exempt for the same
# reason, one layer down — both stream into the handler, whose own
# `IMPORT_MAX_FILE_SIZE` check (default 50 MiB, `imports.py` / the folder-upload
# route) is the authoritative limit. Without this, every import between 1 MiB
# and the documented 50 MiB was refused here before the route ran.
# Everything else keeps the global guard: this tuple is exact, anchored,
# POST-only paths — never a prefix and never method-agnostic, because a prefix
# would exempt GET/PATCH/DELETE on /api/v1/imports/* as well and the guard is
# the only body ceiling most routes have.
_IMPORT_CSV_UPLOAD_PATH = re.compile(r"^/api/v1/imports/csv/upload$")
_IMPORT_FOLDER_UPLOAD_PATH = re.compile(r"^/api/v1/import-definitions/[^/]+/folder-files$")

_BODY_LIMIT_EXEMPT_POST_PATHS = (
    _OBJECT_FILE_UPLOAD_PATH,
    _IMPORT_CSV_UPLOAD_PATH,
    _IMPORT_FOLDER_UPLOAD_PATH,
)


class MaximumContentLengthMiddleware(BaseHTTPMiddleware):
    """Reject request bodies exceeding 1 MB with HTTP 413.

    Only inspects Content-Length; chunked/absent-header requests are not
    caught here — Nginx's client_max_body_size is the backstop for that.
    """

    async def dispatch(self, request: Request, call_next):
        content_length = request.headers.get("content-length")
        if content_length is not None and int(content_length) > MAX_CONTENT_LENGTH:
            exempt = request.method == "POST" and any(
                pattern.match(request.url.path)
                for pattern in _BODY_LIMIT_EXEMPT_POST_PATHS
            )
            if not exempt:
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
        return await call_next(request)


app = FastAPI(title="API", version=__version__, lifespan=lifespan)


def _validate_cors_origins(raw: str | None) -> list[str]:
    """Parse CORS_ALLOWED_ORIGINS env var. Fail-fast on missing/empty/wildcard.

    Phase 95 — security finding F-88-02-01. Wildcard origins ("*") are forbidden
    when allow_credentials=True (a well-known CORS misconfiguration pattern that
    the spec says browsers should reject, but some clients/proxies handle
    inconsistently and it signals no origin restriction was actually designed).
    """
    if not raw:
        raise RuntimeError(
            "CORS_ALLOWED_ORIGINS environment variable is required but not set. "
            "Provide a comma-separated list of trusted origins, e.g. "
            "CORS_ALLOWED_ORIGINS=https://app.example.com,https://staging.example.com"
        )
    origins = [o.strip() for o in raw.split(",") if o.strip()]
    if not origins:
        raise RuntimeError(
            f"CORS_ALLOWED_ORIGINS is set to {raw!r} but contains no valid origins "
            "after parsing. Provide at least one origin (scheme + host)."
        )
    if "*" in origins:
        raise RuntimeError(
            "CORS_ALLOWED_ORIGINS must not contain '*'. "
            "Wildcard origins are forbidden when allow_credentials=True."
        )
    return origins


# Parse CORS_ALLOWED_ORIGINS at import time (covers the case where someone
# imports app.main without going through lifespan, e.g. test suites that
# inspect the middleware config directly).
_parsed_cors_origins = _validate_cors_origins(os.environ.get("CORS_ALLOWED_ORIGINS"))

app.add_middleware(
    CORSMiddleware,
    allow_origins=_parsed_cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

def _compat_find_route_handler(routes, scope):
    """FastAPI >= 0.141 compat for slowapi 0.1.10 (T-213-08, deferred-items.md #1).

    FastAPI's include_router() wraps every included APIRouter in an
    _IncludedRouter BaseRoute dataclass that has NO .endpoint attribute.
    slowapi 0.1.10's _find_route_handler() only matches routes with
    hasattr(route, "endpoint"), so the global limiter middleware resolved
    handler=None -> _should_exempt() -> True and silently bypassed every
    API route (regression since the Phase 210 FastAPI bump; the login
    limiter kept working only because it is a decorator, not middleware).
    Unwrap via _IncludedRouter.effective_route_contexts() to reach the real
    endpoint and restore pre-0.141 middleware behavior."""
    from starlette.routing import Match

    handler = None
    for route in routes:
        match, _ = route.matches(scope)
        if match == Match.FULL and hasattr(route, "endpoint"):
            handler = route.endpoint
        elif hasattr(route, "effective_route_contexts"):
            for ctx in route.effective_route_contexts():
                if ctx.endpoint is not None and ctx.matches(scope)[0] == Match.FULL:
                    handler = ctx.endpoint
                    break
    return handler


import slowapi.middleware as _slowapi_middleware
_slowapi_middleware._find_route_handler = _compat_find_route_handler

app.add_middleware(SlowAPIASGIMiddleware)

app.add_middleware(ExceptionHandlerMiddleware)

app.add_middleware(SecureASGIMiddleware, secure=get_secure_headers())

app.add_middleware(CacheControlMiddleware)

_trusted_proxy = os.getenv("NGINX_TRUSTED_IP")
if not _trusted_proxy:
    raise RuntimeError(
        "NGINX_TRUSTED_IP environment variable is required but not set. "
        "It must be Nginx's static IP on the Docker Compose network (see docker-compose.yml)."
    )
app.add_middleware(ProxyHeadersMiddleware, trusted_hosts=[_trusted_proxy])

# Outer middleware pair, innermost of the two first (registered last =
# outermost, per Starlette's add_middleware/build_middleware_stack ordering):
# MaximumContentLengthMiddleware rejects oversized request bodies before any
# other middleware or route handler runs. RequestContextMiddleware is registered
# last at the bottom of this module and is the true outermost layer.
app.add_middleware(MaximumContentLengthMiddleware)

app.state.limiter = limiter

async def _dispatch_rate_limit_exceeded_handler(request: Request, exc: RateLimitExceeded):
    """Phase 96 — dispatch header injection to whichever limiter actually
    enforced the block. slowapi's own _rate_limit_exceeded_handler hardcodes
    request.app.state.limiter, which silently drops X-RateLimit-*/Retry-After
    headers on login_limiter's 429s whenever the global limiter is disabled
    (RATE_LIMIT_ENABLED=false) — Limiter._inject_headers gates on the calling
    limiter's own .enabled flag, not the one that actually raised the exception.
    """
    response = JSONResponse(
        {"error": f"Rate limit exceeded: {exc.detail}"}, status_code=429
    )
    target = login_limiter if request.url.path == "/api/v1/auth/login" else limiter
    return target._inject_headers(response, request.state.view_rate_limit)

app.add_exception_handler(RateLimitExceeded, _dispatch_rate_limit_exceeded_handler)


async def _dispatch_request_validation_error(
    request: Request, exc: RequestValidationError
):
    """Phase 280 (T-280-08) — render the standard 422 envelope safely.

    FastAPI's default handler returns ``{"detail": jsonable_encoder(...)}`` via
    Starlette's JSONResponse, which serializes with ``ensure_ascii=False`` and
    then UTF-8-encodes. A rejected request body that legitimately reaches
    validation but cannot be UTF-8 encoded — e.g. a lone UTF-16 surrogate, valid
    JSON but invalid UTF-8 — is echoed back in the error's ``input`` field, so
    the *error response itself* raised UnicodeEncodeError and surfaced as an
    unhandled 500 instead of the correct 422. Serializing with
    ``ensure_ascii=True`` escapes any such character while preserving FastAPI's
    exact status code, media type, and ``detail`` shape.
    """
    content = json.dumps(
        {"detail": jsonable_encoder(exc.errors())},
        ensure_ascii=True,
        allow_nan=False,
    ).encode("utf-8")
    return Response(content=content, status_code=422, media_type="application/json")


app.add_exception_handler(RequestValidationError, _dispatch_request_validation_error)

app.include_router(auth.router, prefix="/api/v1")
app.include_router(sources.router, prefix="/api/v1")
app.include_router(schemas.router, prefix="/api/v1")
app.include_router(languages.router, prefix="/api/v1")
app.include_router(translations.router, prefix="/api/v1")
app.include_router(translation_types.router, prefix="/api/v1")
app.include_router(ui_translations.router, prefix="/api/v1")
app.include_router(templates.router, prefix="/api/v1")
app.include_router(attribute_types.router, prefix="/api/v1")
app.include_router(audit_log_config.router, prefix="/api/v1")
app.include_router(audit_logs.router, prefix="/api/v1")
app.include_router(reference_types.router, prefix="/api/v1")
app.include_router(object_types.router, prefix="/api/v1")
app.include_router(objects.router, prefix="/api/v1")
app.include_router(object_type_attributes.router, prefix="/api/v1")
app.include_router(object_values.router, prefix="/api/v1")
app.include_router(object_refs.router, prefix="/api/v1")
app.include_router(statuses.router, prefix="/api/v1")
app.include_router(imports.router, prefix="/api/v1")
app.include_router(exports.router, prefix="/api/v1")
app.include_router(import_statuses.router, prefix="/api/v1")
app.include_router(export_statuses.router, prefix="/api/v1")
app.include_router(icons.router, prefix="/api/v1")
app.include_router(saved_queries.router, prefix="/api/v1")
app.include_router(schedules.router, prefix="/api/v1")
app.include_router(tasks.router, prefix="/api/v1")
app.include_router(targets.router, prefix="/api/v1")
app.include_router(export_definitions.router, prefix="/api/v1")
app.include_router(import_definitions.router, prefix="/api/v1")
app.include_router(service_accounts.router, prefix="/api/v1")
app.include_router(api_clients.router, prefix="/api/v1")
app.include_router(users.router, prefix="/api/v1")
app.include_router(groups.router, prefix="/api/v1")
app.include_router(roles.router, prefix="/api/v1")
app.include_router(permissions.router, prefix="/api/v1")
app.include_router(search.router, prefix="/api/v1")
app.include_router(schema_templates.router, prefix="/api/v1")
app.include_router(config.router, prefix="/api/v1")
app.include_router(record_permissions.router, prefix="/api/v1")
app.include_router(settings.router, prefix="/api/v1")
app.include_router(classification.families_router, prefix="/api/v1")
app.include_router(classification.labels_router, prefix="/api/v1")
app.include_router(logging_api.router, prefix="/api/v1")
app.include_router(aql.router, prefix="/api/v1")

# Phase 282 (PLUGCORE-10): plugin management API — list/detail/health here,
# config GET/PUT and (Plan 282-08) the kill-switch build on this same router.
app.include_router(plugins.router, prefix="/api/v1")

# GraphQL API v2 — read-only query interface. Phase 304 (GQL-06/D-16): always
# registered; the production gate lives in the router (graphql_endpoint_gate)
# and serves the endpoint only while a plugin declaring `enables_http_graphql`
# is active (404 otherwise, no plugin-state hint).
app.include_router(graphql_router, prefix="/api/v1/graphql")

# Public REST realm (Phase 200+) — service-account bearer auth
app.include_router(public_router)


@app.get("/health")
@limiter.exempt
async def health_check():
    """Unauthenticated probe: exactly ``status`` and ``version``, nothing else.

    Threat T-318-01 — no build sha, environment name, dependency list or commit
    id. The SPA reads ``version`` from here at runtime (D-06), which is why the
    nginx configs in front of this route carry a ``location = /health`` block.
    """
    return {"status": "healthy", "version": __version__}


@app.get("/api/v1/")
@limiter.exempt
async def root():
    return {"message": "Welcome to API"}


# Phase 261 (MON-02, D-01/D-02): public /metrics via the pinned
# prometheus-fastapi-instrumentator. Registered AFTER all include_router /
# add_middleware calls and after the /health route so the middleware is
# outermost and /metrics is a real route, never shadowed. Default metrics
# only (D-01). /metrics and /health are excluded so kubelet probe / scrape
# noise never pollutes http_requests_total (Pitfall 4) — and the route stays
# public by default (D-02), protected by the Phase 259 default-deny NetPol.
from prometheus_fastapi_instrumentator import Instrumentator

instrumentator = Instrumentator(
    should_group_status_codes=True,
    should_ignore_untemplated=True,
    excluded_handlers=["/metrics", "/health"],
)
instrumentator.instrument(app).expose(
    app, endpoint="/metrics", include_in_schema=False
)

# Registered last so it stays the outermost user middleware — every response
# (including 413/429/500) carries the correlation id and exactly one access record.
app.add_middleware(RequestContextMiddleware)