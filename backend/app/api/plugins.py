"""Core plugin management API (PLUGCORE-10, D-11).

Mirrors the established router shape (``service_accounts.py``'s CRUD scaffolding
+ ``config.py``'s "compute a masked view, never leak the secret" settings-style
GET). The MANAGEMENT routes are gated by ``require_permission("plugins", ...)`` —
the ``plugins`` RBAC resource is seeded in ``init_service.py`` and classified
admin-only, so the seeded Editor role never gains plugin management
(RESEARCH.md Pitfall 6: a superuser bypasses every check, which is why the RBAC
tests use a real non-superuser). The bundle route is the deliberate exception
(CORESEAM-09/D-05): serving plugin code is RUNTIME use, gated by
``plugin-runtime:use`` — a capability the seeded Editor AND Viewer hold.

``plugins`` has no persisted ``capabilities``/``has_frontend`` column
(PLUGCORE-10): those are read from the live in-process registry when the plugin
happens to be loaded in THIS process and default to ``[]``/``False`` otherwise.
The registry is resolved fresh per request (never a cached reference), and a
``RuntimeError`` from an uninitialized registry is treated exactly like "not
currently loaded".
"""

import hashlib
import logging
import tomllib
from pathlib import Path
from typing import List, Literal

import jsonschema
from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db
from app.db.redis import get_redis
from app.llm import get_llm_client
from app.models.plugin import Plugin, PluginState
from app.plugin_system import crypto, get_plugin_registry, get_plugins_dir, validation
from app.plugin_system.loader import validate_plugin_name
from app.plugin_system.manifest import load_manifest_toml
from app.plugin_system.registry import (
    STATE_CACHE_TTL,
    PluginCallError,
    PluginTimeoutError,
)
from app.schemas.auth import UserResponse
from app.schemas.crud import (
    PluginConfigUpdate,
    PluginFrontendResponse,
    PluginResponse,
    PluginRuntimeResponse,
)
from app.services.auth_service import require_permission

router = APIRouter(prefix="/plugins", tags=["plugins"])

logger = logging.getLogger(__name__)

# D-12: the closed MIME allowlist the public asset route serves. Keyed on the
# resolved file's own suffix — never on a client-supplied Accept header.
MIME_ALLOWLIST: dict[str, str] = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
    ".map": "application/json",
    ".json": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".svg": "image/svg+xml",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}


class PluginStateUpdate(BaseModel):
    """Body for the kill-switch/reactivate endpoint (D-13/D-15)."""

    state: Literal["active", "disabled"]


def _registry_or_none():
    """The process-wide registry, or ``None`` when it was never initialized.

    ``get_plugin_registry()`` raises ``RuntimeError`` outside the app/worker
    lifespan (unit tests, ad-hoc scripts) — that is the same situation as "no
    plugin is loaded in this process", so it degrades to ``None`` rather than a
    500.
    """
    try:
        return get_plugin_registry()
    except RuntimeError:
        return None


def _loaded_plugin(name: str):
    """The in-process plugin instance for ``name``, or ``None`` when not loaded."""
    registry = _registry_or_none()
    return registry.get_plugin(name) if registry is not None else None


def _plugin_response(row: Plugin) -> PluginResponse:
    """Map a ``plugins`` row to its response, enriched from the live registry."""
    plugin = _loaded_plugin(row.name)
    manifest = plugin.manifest if plugin is not None else None
    state = row.state.value if isinstance(row.state, PluginState) else row.state
    requires_llm = manifest.requires_llm if manifest is not None else False
    llm = get_llm_client() if requires_llm else None

    return PluginResponse(
        id=row.id,
        name=row.name,
        version=row.version,
        state=state,
        capabilities=list(manifest.capabilities) if manifest is not None else [],
        has_frontend=manifest.frontend is not None if manifest is not None else False,
        frontend=(
            PluginFrontendResponse(
                bundle=manifest.frontend.bundle,
                extension_points=list(manifest.frontend.extension_points),
                deferred_assets=dict(manifest.frontend.deferred_assets),
            )
            if manifest is not None and manifest.frontend is not None
            else None
        ),
        author=manifest.author if manifest is not None else None,
        api_version=manifest.api_version if manifest is not None else None,
        requires_worker_update=(
            manifest.requires_worker_update if manifest is not None else False
        ),
        enables_http_graphql=(
            manifest.enables_http_graphql if manifest is not None else False
        ),
        requires_llm=requires_llm,
        llm_status=llm.status if llm is not None else None,
        llm_config_error=llm.config_error if llm is not None else None,
        config_schema=dict(manifest.config_schema) if manifest is not None else {},
        worker_loaded_at=row.worker_loaded_at,
        worker_loaded_version=row.worker_loaded_version,
        # Phase 287 (D-13/D-14): the three bounded diagnostic/contract fields are
        # read straight off the row so a plugin not loaded in this process still
        # reports them. ``database_migrations_dir`` is deliberately not mapped
        # (A-287-07).
        database_version_table=row.database_version_table,
        database_error=row.database_error,
        database_error_revision=row.database_error_revision,
        installed_at=row.installed_at,
        updated_at=row.updated_at,
    )


def _manifest_for_asset(plugin_dir: Path, name: str):
    """The plugin's manifest — in-process registry first, on-disk fallback (D-09).

    A plugin loaded in this process answers from the live registry; a plugin that
    is installed but not loaded here is read from its ``plugin.toml``. An
    unreadable or invalid manifest is indistinguishable from an unknown plugin
    (404) at this route.
    """
    plugin = _loaded_plugin(name)
    if plugin is not None and plugin.manifest is not None:
        return plugin.manifest

    try:
        return load_manifest_toml((plugin_dir / "plugin.toml").read_bytes())
    except (OSError, tomllib.TOMLDecodeError, ValueError):
        raise HTTPException(status_code=404, detail="Plugin not found")


def resolve_plugin_asset(plugins_dir: str, name: str, path: str) -> tuple[Path, str]:
    """Resolve one allowlisted asset inside a plugin's declared bundle directory.

    Pure and testable: returns ``(resolved_path, media_type)`` for a valid
    request and raises ``HTTPException`` for every rejection — invalid plugin
    name, unknown/unreadable plugin, no declared frontend half, empty path,
    traversal/symlink escape, non-file target and a suffix outside
    ``MIME_ALLOWLIST`` (D-09/D-12). ``path`` is plugin-relative (the loader
    requests the manifest's own ``bundle`` value, e.g. ``static/example.js``)
    and the containment bound is the bundle's OWN directory — clamped to the
    plugin directory, so an absolute or ``..``-bearing ``bundle`` declaration is
    rejected rather than trusted (WR-06) and ``plugin.toml``/``module.py`` are
    never served. The plugin directory is always derived
    from ``plugins_dir/name`` — an entry-point-installed plugin without a
    directory there therefore 404s its assets (documented limitation).

    Phase 303 (SEAM-06/D-14): ``path`` may instead be a logical deferred-asset
    id declared in ``manifest.frontend.deferred_assets``; it resolves to that
    declaration's plugin-relative path, still clamped to the bundle directory.
    An id the manifest did not declare falls through to ordinary path
    resolution, so nothing outside the declared map is served by id.
    """
    if validate_plugin_name(name) is not None:
        raise HTTPException(status_code=404, detail="Plugin not found")

    plugin_dir = Path(plugins_dir) / name
    manifest = _manifest_for_asset(plugin_dir, name)
    if manifest.frontend is None:
        raise HTTPException(status_code=404, detail="Plugin has no frontend bundle")

    # WR-06: the manifest's ``bundle`` is plugin-declared input. An absolute path
    # or ``..`` segments would move the containment root outside the plugin
    # directory, after which a request path could reach any allowlisted-suffix
    # file under it through the public route.
    bundle = Path(manifest.frontend.bundle)
    if bundle.is_absolute() or ".." in bundle.parts:
        raise HTTPException(status_code=404, detail="Plugin not found")
    try:
        bundle_dir = (plugin_dir / bundle.parent).resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Plugin not found")
    if not bundle_dir.is_relative_to(plugin_dir.resolve()):
        raise HTTPException(status_code=404, detail="Plugin not found")
    if path == "":
        raise HTTPException(status_code=404, detail="Asset not found")

    # SEAM-06/D-14: a declared deferred-asset id resolves to its declared
    # heavy-payload path; an undeclared id falls through to ordinary path
    # resolution (and 404s unless a real file of that name exists).
    path = manifest.frontend.deferred_assets.get(path, path)

    # WR-05: a percent-encoded NUL (``%00``) arrives here decoded and makes
    # ``resolve()`` raise ValueError; an unresolvable path is simply not found.
    try:
        candidate = (plugin_dir / path).resolve()
    except (ValueError, OSError):
        raise HTTPException(status_code=404, detail="Asset not found")
    if not candidate.is_relative_to(bundle_dir) or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Asset not found")

    media_type = MIME_ALLOWLIST.get(candidate.suffix.lower())
    if media_type is None:
        raise HTTPException(status_code=415, detail="Unsupported asset type")

    return candidate, media_type


def _runtime_response(row: Plugin) -> PluginRuntimeResponse:
    """Map one ACTIVE ``plugins`` row to the runtime discovery view (D-05).

    Enriched from the live in-process registry exactly like ``_plugin_response``:
    the ``frontend`` block exists only on the in-memory manifest, so a plugin
    installed but not loaded in THIS process reports ``None`` rather than
    guessing a bundle path.
    """
    plugin = _loaded_plugin(row.name)
    manifest = plugin.manifest if plugin is not None else None
    return PluginRuntimeResponse(
        name=row.name,
        version=row.version,
        enables_http_graphql=(
            manifest.enables_http_graphql if manifest is not None else False
        ),
        frontend=(
            PluginFrontendResponse(
                bundle=manifest.frontend.bundle,
                extension_points=list(manifest.frontend.extension_points),
                deferred_assets=dict(manifest.frontend.deferred_assets),
            )
            if manifest is not None and manifest.frontend is not None
            else None
        ),
    )


async def _get_row(db: AsyncSession, name: str) -> Plugin | None:
    result = await db.execute(select(Plugin).where(Plugin.name == name))
    return result.scalar_one_or_none()


@router.get("/", response_model=List[PluginResponse])
async def list_plugins_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("plugins", "read")),
):
    result = await db.execute(select(Plugin).order_by(Plugin.name))
    return [_plugin_response(row) for row in result.scalars().all()]


@router.get("/runtime", response_model=List[PluginRuntimeResponse])
async def list_runtime_plugins_route(
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("plugin-runtime", "use")),
):
    """Discover the plugin bundles a runtime user may load (CORESEAM-09/D-05).

    Declared BEFORE ``/{name}`` so the literal path wins over the parameter
    route. Returns only ACTIVE plugins and only what the browser runtime needs
    to import a bundle (name/version/frontend) — never config, secrets, state
    detail, worker/database diagnostics or error text (T-289-08-03). The admin
    ``GET /plugins/`` list keeps its ``plugins:read`` gate unchanged, so an
    Editor/Viewer can discover and load bundles without admin visibility.
    """
    result = await db.execute(
        select(Plugin)
        .where(Plugin.state == PluginState.ACTIVE)
        .order_by(Plugin.name)
    )
    return [_runtime_response(row) for row in result.scalars().all()]


@router.get("/{name}", response_model=PluginResponse)
async def get_plugin_route(
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("plugins", "read")),
):
    row = await _get_row(db, name)
    if row is None:
        raise HTTPException(status_code=404, detail="Plugin not found")
    return _plugin_response(row)


def _etag_matches(if_none_match: str | None, etag: str) -> bool:
    """RFC 9110 weak comparison of an ``If-None-Match`` header against one ETag."""
    if not if_none_match:
        return False
    if if_none_match.strip() == "*":
        return True

    def opaque(value: str) -> str:
        value = value.strip()
        return value[2:].lstrip() if value.startswith("W/") else value

    target = opaque(etag)
    return any(opaque(candidate) == target for candidate in if_none_match.split(","))


@router.get("/{name}/assets/{path:path}")
async def get_plugin_asset_route(
    name: str,
    path: str,
    request: Request,
    current_user: UserResponse = Depends(require_permission("plugin-runtime", "use")),
):
    """Serve one plugin bundle asset same-origin, gated by runtime use (D-09).

    Plan 289-08 (CORESEAM-09/D-05) replaced the former anonymous access with the
    runtime capability ``plugin-runtime:use``: only an authenticated caller who
    may USE plugins receives bundle bytes, while ``plugins:read``/``manage``
    stay the separate admin surface. Authorization runs BEFORE
    ``resolve_plugin_asset()``, so an unauthorized caller never triggers path
    resolution (T-289-08-03); containment, the MIME allowlist, the content-digest
    ``ETag`` (with a matching ``If-None-Match`` answered ``304 Not Modified``)
    and ``nosniff`` remain. The bundle URL is not user-specific: CR-03 requires
    per-request authorization for delivery, not just cache population. Both
    responses are private and non-storable, with ``Vary: Authorization`` recording
    their credential dependence. Clients that still send a validator are
    re-authorized before the handler can return 304.
    """
    candidate, media_type = resolve_plugin_asset(get_plugins_dir(), name, path)
    body = candidate.read_bytes()
    etag = 'W/"sha256:' + hashlib.sha256(body).hexdigest()[:32] + '"'
    headers = {
        "Cache-Control": "private, no-store",
        "Vary": "Authorization",
        "ETag": etag,
        "X-Content-Type-Options": "nosniff",
    }
    if _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers=headers)
    return Response(content=body, media_type=media_type, headers=headers)


@router.get("/{name}/health")
async def get_plugin_health_route(
    name: str,
    current_user: UserResponse = Depends(require_permission("plugins", "read")),
):
    """Run the loaded plugin's ``health_check()`` under the dispatch chokepoint.

    ``build_context(name)`` hands the plugin its own decrypted config (D-12), so
    a health check may verify its configured endpoint/credentials. A
    ``PluginCallError``/``PluginTimeoutError`` maps to ``{"healthy": false}``
    with the error message — never a 500.
    """
    registry = _registry_or_none()
    plugin = registry.get_plugin(name) if registry is not None else None
    if plugin is None:
        raise HTTPException(status_code=404, detail="Plugin not currently loaded")

    ctx = await registry.build_context(name)
    try:
        result = await registry.call_capability(plugin, "health_check", ctx)
    except (PluginCallError, PluginTimeoutError) as exc:
        return {"healthy": False, "error": str(exc)}
    return {"healthy": bool(result)}


def _loaded_manifest_schema(name: str) -> dict:
    """The loaded plugin's ``config_schema``, or 404 when it is not loaded.

    The schema exists only on the in-memory manifest — PLUGCORE-10 persists no
    schema column — so masking, defaulting and validation are all impossible for
    a plugin that is not loaded in this process.
    """
    plugin = _loaded_plugin(name)
    if plugin is None or plugin.manifest is None:
        raise HTTPException(status_code=404, detail="Plugin not currently loaded")
    return plugin.manifest.config_schema


def _masked_config_view(stored_config: dict, config_schema: dict) -> dict:
    """The D-12 read view: schema defaults applied, then secrets sentinel-masked."""
    resolved = validation.apply_schema_defaults(config_schema, dict(stored_config or {}))
    return crypto.mask_secret_fields(resolved, config_schema)


@router.get("/{name}/config")
async def get_plugin_config_route(
    name: str,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("plugins", "manage")),
):
    """The masked config view (D-11) — never a decrypted secret.

    Gated by ``plugins:manage`` (not ``read``): even masked, a plugin's config is
    a stronger-than-read surface.
    """
    row = await _get_row(db, name)
    if row is None:
        raise HTTPException(status_code=404, detail="Plugin not found")

    config_schema = _loaded_manifest_schema(name)
    return _masked_config_view(row.config, config_schema)


@router.put("/{name}/config")
async def update_plugin_config_route(
    name: str,
    payload: PluginConfigUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("plugins", "manage")),
):
    """Validate, merge/encrypt and persist a plugin config payload (D-09/D-11).

    ``validate_config_payload`` runs BEFORE anything is persisted — an invalid
    payload never reaches the row. ``encrypt_config_secrets`` then owns D-11's
    PUT semantics: a submitted sentinel, an omitted secret key or ``None``
    carries over the stored ciphertext, anything else is new plaintext encrypted
    on the spot.
    """
    row = await _get_row(db, name)
    if row is None:
        raise HTTPException(status_code=404, detail="Plugin not found")

    config_schema = _loaded_manifest_schema(name)

    try:
        validation.validate_config_payload(config_schema, payload.config)
    except (
        jsonschema.exceptions.ValidationError,
        jsonschema.exceptions.SchemaError,
        jsonschema.exceptions.RefResolutionError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc))

    row.config = crypto.encrypt_config_secrets(
        payload.config, config_schema, dict(row.config or {})
    )
    await db.commit()

    return _masked_config_view(row.config, config_schema)


@router.put("/{name}/state", response_model=PluginResponse)
async def update_plugin_state_route(
    name: str,
    payload: PluginStateUpdate,
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_permission("plugins", "manage")),
):
    """Kill-switch or reactivate a plugin, then push the cross-process cache key.

    ``kill_switch()``/``reactivate()`` change THIS process's state and persist
    the ``plugins`` row. The ``setex`` that follows proactively publishes the
    new value so the other OS process sees it on its next ``is_active()`` call
    within the TTL window (D-13) — the point of D-13 is that the triggering
    process pushes the change, it does not merely wait for the other process to
    miss the cache and re-read the table.
    """
    row = await _get_row(db, name)
    if row is None:
        raise HTTPException(status_code=404, detail="Plugin not found")

    registry = _registry_or_none()
    if registry is None:
        raise HTTPException(status_code=503, detail="Plugin registry not initialized")

    try:
        if payload.state == "disabled":
            await registry.kill_switch(name, actor_id=current_user.id)
        else:
            await registry.reactivate(name, actor_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    state_value = registry.get_state(name)
    state_str = (
        state_value.value if isinstance(state_value, PluginState) else state_value
    )
    if state_str == payload.state:
        try:
            redis = await get_redis()
            await redis.setex(f"plugin_state:{name}", STATE_CACHE_TTL, state_str)
        except Exception:
            logger.exception(
                "plugin_state cache push failed for %r — the plugins row is already "
                "updated, so the other process still converges on its DB miss",
                name,
            )
    else:
        logger.warning(
            "Plugin %r landed in state %r instead of requested %r — skipping "
            "cache push so the other process falls back to DB truth",
            name, state_str, payload.state,
        )

    await db.refresh(row)
    return _plugin_response(row)
