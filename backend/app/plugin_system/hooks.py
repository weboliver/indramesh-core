"""Hook bus (D-04 / PLUGCORE-07) — override introspection + fire-by-name.

Hooks are plain no-op methods on ``IndraMeshPlugin`` (D-04). There is no
manifest subscription list: the loader/bus detects by introspection which
methods a plugin class actually overrides and only those become subscribers.
New core hooks are added additively to ``_HOOK_NAMES`` and never require a
manifest update.

The bus is a plain explicit function called from the real fire points — never a
SQLAlchemy event listener (the same design rationale ``audit_service.audit_log``
documents: explicit call site, no ORM magic, no ContextVar).

Two invocation levels:

* ``fire_hook()`` — resolve subscribers once, then dispatch. The common
  no-subscriber case returns before any context is built (zero cost).
* ``dispatch_hook()`` — dispatch to an already-resolved subscriber list. Used by
  ``search_service.bulk_reindex()``, which resolves both index hooks once before
  its per-object loop instead of re-resolving per document.

Every subscriber call crosses ``PluginRegistry.call_capability()`` (Plan 282-02),
so it inherits the ``asyncio.wait_for`` timeout and exception isolation. A
raising/timing-out hook is logged and skipped: hooks are fire-and-forget
observers and must never gate the mutation or indexing operation that fired
them (T-282-09).

Each subscriber gets its OWN ``PluginContext`` built by
``PluginRegistry.build_context()`` — never one shared context. From Plan 282-06
on ``ctx.config`` is that plugin's private decrypted namespace, and
PLUGCORE-06 forbids one plugin seeing another's.
"""

import asyncio
import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.plugin import PluginState
from app.plugin_system.base import IndraMeshPlugin
from app.plugin_system.registry import PluginRegistry

logger = logging.getLogger(__name__)

# The D-04 fixed hook set. Additive-only: a new core hook is added here
# explicitly; plugin manifests never need to list hooks.
_HOOK_NAMES: frozenset[str] = frozenset(
    {
        "on_object_created",
        "on_object_updated",
        "on_object_deleted",
        "on_object_ref_created",
        "on_object_ref_updated",
        "on_object_ref_deleted",
        "before_index",
        "after_index",
    }
)


def is_hook_overridden(plugin: IndraMeshPlugin, hook_name: str) -> bool:
    """True when ``plugin``'s class overrides ``hook_name`` (D-04 introspection).

    Identity comparison against the base class: an un-overridden hook resolves
    to exactly the same function object as ``IndraMeshPlugin``'s no-op.
    """
    return getattr(type(plugin), hook_name) is not getattr(IndraMeshPlugin, hook_name)


def plugins_subscribed_to(
    registry: PluginRegistry, hook_name: str
) -> list[IndraMeshPlugin]:
    """Return the ACTIVE plugins that genuinely override ``hook_name``.

    ``hook_name`` must be one of the D-04 hooks: a typo'd name would otherwise
    silently match nothing and the hook would never fire — a bug, not a valid
    state, so it raises ``ValueError``.
    """
    if hook_name not in _HOOK_NAMES:
        raise ValueError(
            f"Unknown hook {hook_name!r} — known hooks: {sorted(_HOOK_NAMES)}"
        )

    return [
        plugin
        for name, plugin in registry._plugins.items()
        if registry.get_state(name) == PluginState.ACTIVE
        and is_hook_overridden(plugin, hook_name)
    ]


async def dispatch_hook(
    registry: PluginRegistry,
    hook_name: str,
    subscribers: list[IndraMeshPlugin],
    *,
    active_session: AsyncSession | None = None,
    **kwargs: Any,
) -> None:
    """Invoke ``hook_name`` on an already-resolved ``subscribers`` list.

    Each subscriber's own ``PluginContext`` is built with the caller's
    ``active_session`` (D-06 — the hook shares the caller's transaction). A
    subscriber that raises or times out is logged and skipped; the next
    subscriber still runs.
    """
    for plugin in subscribers:
        plugin_name = plugin.manifest.name if plugin.manifest is not None else None
        if plugin_name is None:
            logger.warning(
                "Hook %r: subscriber %r has no manifest — skipped",
                hook_name,
                type(plugin).__name__,
            )
            continue
        try:
            ctx = await registry.build_context(
                plugin_name, active_session=active_session
            )
            await registry.call_capability(plugin, hook_name, ctx, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "Hook %r subscriber %r failed (isolated): %s",
                hook_name,
                plugin_name,
                exc,
            )
            continue


async def fire_hook(
    registry: PluginRegistry,
    hook_name: str,
    *,
    active_session: AsyncSession | None = None,
    **kwargs: Any,
) -> None:
    """Resolve the subscribers for ``hook_name`` once, then dispatch to them.

    Returns immediately when nothing subscribes (the common case): no context is
    built and no DB read happens.
    """
    subscribers = plugins_subscribed_to(registry, hook_name)
    if not subscribers:
        return
    await dispatch_hook(
        registry, hook_name, subscribers, active_session=active_session, **kwargs
    )
