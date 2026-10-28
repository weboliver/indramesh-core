"""Plugin-declared route injection (D-14/D-16, PLUGCORE-09; CORESEAM-09/D-05).

An ``ApiRoutesPlugin`` builds its own ``APIRouter`` via ``register_routes(ctx)``,
but the CORE owns two things the plugin can never change:

* the URL prefix — ``/api/v1/plugins/<name>/`` (the published route-injection
  contract), and
* the forced dependency chain — ``get_current_user`` + ``plugin-runtime:use`` +
  the per-request state check — hard-prepended at mount time. The plugin's
  returned router is never trusted to include its own auth (T-282-02); whatever
  dependencies the plugin declared still run, but layered UNDER these.

Plan 289-08 (CORESEAM-09) narrowed the permission from the admin-only
``plugins:read`` to the runtime capability ``plugin-runtime:use``: using a
plugin is not administering it. ``plugins:read``/``plugins:manage`` keep gating
the admin API (``app/api/plugins.py``), while a plugin's OWN route dependency
(e.g. ``sync:trigger``) remains additive beneath this Core gate.

Routes mount ONCE at startup (D-14): no hot-add/remove at runtime. A disabled
plugin's route is not removed — its state dependency answers 503 per request,
so a kill-switch never races with in-flight requests or leaves dangling route
references.
"""

import logging
from dataclasses import dataclass
from typing import Iterator

from fastapi import Depends, FastAPI, HTTPException

from app.db import async_session_maker
from app.plugin_system import get_plugin_registry
from app.plugin_system.capabilities import ApiRoutesPlugin
from app.plugin_system.registry import PluginRegistry
from app.services.auth_service import (
    PERMISSION_MARKER_ATTR,
    get_current_user,
    require_permission,
)

logger = logging.getLogger(__name__)

# SEAM-02: the Core mount chain's own runtime gate. Using a plugin is NOT the
# plugin's business authorization — a route whose only Core-enforced protection
# is this capability is refused at mount.
RUNTIME_PERMISSION_NAME = "plugin-runtime:use"

# Bound the persisted refusal diagnosis so a hostile/large router cannot bloat
# the log/error record (SEAM-02/T-303-07).
_MAX_DIAGNOSED_OPERATIONS = 8


@dataclass(frozen=True)
class RoutePermissionIssue:
    """One plugin-declared operation without a known explicit business permission."""

    method: str
    path: str
    detail: str


def _iter_route_candidates(router) -> Iterator:
    """Yield the inspectable route objects of ``router``.

    Handles FastAPI >= 0.141, where ``include_router()`` wraps routers in an
    ``_IncludedRouter`` whose real paths/dependants live on
    ``effective_route_contexts()`` — the same unwrap ``_prefix_already_mounted``
    and ``main._compat_find_route_handler`` already use. A plain ``APIRouter``
    (or an app's ``router.routes``) yields its routes directly.
    """
    for route in getattr(router, "routes", ()) or ():
        contexts = getattr(route, "effective_route_contexts", None)
        if callable(contexts):
            yield from contexts()
        else:
            yield route


def _dependant_permission_names(dependant) -> set[str]:
    """Recursively collect every marker name reachable from ``dependant``.

    Walks the ACTUAL FastAPI dependency tree (router-level and nested
    ``Depends`` included) — a marker is found only when a
    ``require_permission()``-produced dependency is present in the tree.
    """
    names: set[str] = set()
    if dependant is None:
        return names
    marker = getattr(getattr(dependant, "call", None), PERMISSION_MARKER_ATTR, None)
    if marker is not None:
        names.add(marker.name)
    for child in getattr(dependant, "dependencies", ()) or ():
        names |= _dependant_permission_names(child)
    return names


def allowed_business_permissions(plugin) -> frozenset[str]:
    """The validated permission catalogue for ``plugin``'s own routes (SEAM-02).

    Core's static catalogue plus ONLY the declaring owner's manifest-declared
    permissions (never another plugin's) — so a route may enforce a Core
    permission or a permission its own plugin declared, but an unknown/typo
    name (or a foreign plugin's name) fails the gate. Delegates to the shared
    owner-scoped helper so the route gate and the task-type declaration check
    agree on one boundary.
    """
    from app.plugin_system.permission_registry import known_permission_names

    return known_permission_names(getattr(plugin, "manifest", None))


def route_permission_issues(router, allowed_names) -> list[RoutePermissionIssue]:
    """Every operation of ``router`` lacking a known explicit business permission.

    A route passes only when its effective dependency tree contains a
    factory-produced marker whose ``resource:action`` is (a) not the runtime
    ``plugin-runtime:use`` capability and (b) a member of ``allowed_names``. The
    runtime-only case and the missing-marker case are both refusals; an unknown
    declared permission (typo/foreign owner) is a distinct refusal.
    """
    allowed = set(allowed_names)
    issues: list[RoutePermissionIssue] = []
    for candidate in _iter_route_candidates(router):
        names = _dependant_permission_names(getattr(candidate, "dependant", None))
        business = names - {RUNTIME_PERMISSION_NAME}
        unknown = business - allowed
        if not business:
            detail = "no explicit business permission beyond plugin-runtime:use"
        elif unknown:
            detail = "unknown permission(s): " + ", ".join(sorted(unknown))
        else:
            continue
        methods = sorted(getattr(candidate, "methods", None) or ()) or ["*"]
        path = getattr(candidate, "path", "") or ""
        for method in methods:
            issues.append(RoutePermissionIssue(method=method, path=path, detail=detail))
    return issues


def format_route_diagnosis(issues: list[RoutePermissionIssue]) -> str:
    """A bounded, method/path diagnosis for a refused plugin candidate."""
    shown = issues[:_MAX_DIAGNOSED_OPERATIONS]
    rendered = "; ".join(
        f"{issue.method} {issue.path} ({issue.detail})" for issue in shown
    )
    if len(issues) > _MAX_DIAGNOSED_OPERATIONS:
        rendered += f"; +{len(issues) - _MAX_DIAGNOSED_OPERATIONS} more"
    return rendered


def state_check_dependency(name: str):
    """A FastAPI dependency factory: 503 when plugin ``name`` is not active.

    Mirrors ``require_permission(resource, action)``'s factory shape. The
    registry is resolved per request (never captured at mount time), so the
    cross-process state check (D-13) sees the latest Redis/DB truth.
    """

    async def _state_check() -> None:
        registry = get_plugin_registry()
        if not await registry.is_active(name):
            raise HTTPException(
                status_code=503, detail=f"Plugin '{name}' is disabled"
            )

    return _state_check


def _prefix_already_mounted(app: FastAPI, prefix: str) -> bool:
    """True when ``prefix`` already has at least one route in ``app.router``.

    Handles FastAPI >= 0.141, where ``include_router()`` wraps each included
    router in an ``_IncludedRouter`` with no ``.path`` — the real paths live on
    ``effective_route_contexts()`` (same unwrap ``main._compat_find_route_handler``
    documents). Keeps ``mount_plugin_routes()`` idempotent when called
    repeatedly against one module-global ``app`` (the in-process tests do).
    """

    def _matches(path: str | None) -> bool:
        return bool(path) and (path == prefix or path.startswith(prefix + "/"))

    for route in app.router.routes:
        if _matches(getattr(route, "path", None)):
            return True
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is not None:
            for context in contexts():
                if _matches(getattr(context, "path", None)):
                    return True
    return False


async def mount_plugin_routes(app: FastAPI, registry: PluginRegistry) -> None:
    """Mount every ACTIVE or DISABLED ``ApiRoutesPlugin``'s router under the core prefix.

    The forced dependency chain is prepended to each plugin router, so an
    injected route always requires authentication, the ``plugin-runtime:use``
    permission and an active plugin — regardless of what the plugin's own
    ``register_routes()`` declared (D-16, CORESEAM-09). A plugin disabled at
    startup is mounted behind the same state check (503 until reactivated), so
    activating it later needs no restart. Idempotent per plugin prefix.
    """
    for plugin in registry.get_plugins_for_capability(
        ApiRoutesPlugin, include_disabled=True
    ):
        name = plugin.manifest.name
        prefix = f"/api/v1/plugins/{name}"
        if _prefix_already_mounted(app, prefix):
            logger.info("Plugin routes for %r already mounted at %s", name, prefix)
            continue

        try:
            ctx = await registry.build_context(name)
            plugin_router = plugin.register_routes(ctx)
        except Exception:
            logger.exception(
                "Failed to mount routes for plugin %r — skipped", name
            )
            continue

        # SEAM-02 (T-303-06/T-303-07): validate the plugin's OWN router before
        # anything is mounted. Every plugin-declared business operation must
        # carry a factory-produced known permission other than
        # `plugin-runtime:use`; an unprotected, runtime-only or unknown/typo
        # route refuses the WHOLE candidate with a method/path diagnosis and
        # mounts nothing (never a partially-authorized plugin).
        issues = route_permission_issues(
            plugin_router, allowed_business_permissions(plugin)
        )
        if issues:
            diagnosis = format_route_diagnosis(issues)
            logger.error(
                "Refusing to mount plugin %r — route authorization incomplete: %s",
                name,
                diagnosis,
            )
            await _persist_mount_refusal(registry, name, plugin, diagnosis)
            continue

        try:
            app.include_router(
                plugin_router,
                prefix=prefix,
                dependencies=[
                    Depends(get_current_user),
                    Depends(require_permission("plugin-runtime", "use")),
                    Depends(state_check_dependency(name)),
                ],
            )
        except Exception:
            logger.exception(
                "Failed to mount routes for plugin %r — skipped", name
            )
            continue
        logger.info("Mounted plugin routes for %r at %s", name, prefix)


async def _persist_mount_refusal(
    registry: PluginRegistry, name: str, plugin, diagnosis: str
) -> None:
    """Persist a route-authorization refusal so plugin management shows it.

    Review correction R02-2: a refused candidate must be VISIBLE (persisted
    state=error), never a silently missing route. Runs in the backend process
    only (the only caller of ``mount_plugin_routes``); a persisted DISABLED row
    keeps the operator kill-switch, exactly like ``_mark_error`` elsewhere.
    """
    version = (
        plugin.manifest.version if getattr(plugin, "manifest", None) is not None
        else "unknown"
    )
    try:
        async with async_session_maker() as session:
            await registry._mark_error(
                session,
                name,
                version,
                f"route authorization incomplete: {diagnosis}",
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — never let bookkeeping break startup
        logger.exception("Failed to persist route refusal for plugin %r", name)
