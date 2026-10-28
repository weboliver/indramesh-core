"""Plugin system package — global ``PluginRegistry`` singleton + startup init.

Follows this codebase's established "global singleton + ``init_*()`` function"
pattern (Fernet, Redis, JOB_REGISTRY): built once in ``main.py``'s lifespan and
again in ``app/worker.py``'s ``startup_recovery``.
"""

import os

from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

if TYPE_CHECKING:
    from app.plugin_system.registry import PluginRegistry

_registry_instance: "PluginRegistry | None" = None

_DEFAULT_PLUGINS_DIR = "~/.indramesh/plugins/"


def get_plugins_dir() -> str:
    """Resolve the directory-scan root (``INDRAMESH_PLUGINS_DIR`` or the default)."""
    return os.environ.get("INDRAMESH_PLUGINS_DIR") or os.path.expanduser(_DEFAULT_PLUGINS_DIR)


async def init_plugin_registry(
    db: AsyncSession, *, is_worker: bool = False
) -> "PluginRegistry":
    """Build the process-wide ``PluginRegistry`` from every discovery source.

    ``discover_all()`` runs entry-point discovery first (PLUGCORE-04) and
    directory-scan second (PLUGCORE-05), rejecting a name collision as a loud
    error result rather than letting one source shadow the other.

    ``is_worker`` defaults ``False`` (the backend identity); only
    ``worker.py::startup_recovery`` passes ``True`` so the worker — and only the
    worker — writes the ``worker_loaded_at``/``worker_loaded_version`` snapshot
    (PLUGWORKER-02/D-01).
    """
    # Imported lazily: registry -> migrations -> bootstrap_role, and an eager
    # package-level import makes `python -m app.plugin_system.bootstrap_role`
    # execute an already-imported module (runpy RuntimeWarning).
    from app.plugin_system.loader import discover_all
    from app.plugin_system.registry import SCHEDULE_CACHE_ENTITY, PluginRegistry
    from app.services.cache_service import invalidate_entity_cache

    global _registry_instance
    registry = PluginRegistry(is_worker=is_worker)
    await registry.load_results(db, discover_all(get_plugins_dir()))
    await db.commit()
    # WR-03: startup activation/isolation rewrote plugin rows the cached
    # schedule runtime projection was derived from.
    await invalidate_entity_cache(SCHEDULE_CACHE_ENTITY)
    _registry_instance = registry

    # Phase 286 (PLUGTASK-03): publish this process's discovered task-type
    # snapshot to the process-local accessor. The backend and worker each build
    # their own registry at their own startup, so the two snapshots stay
    # independent — a plugin disabled in one process is never dispatchable in
    # the other merely because discovery listed it.
    from app.plugin_system import task_registry as task_registry_module

    if is_worker:
        task_registry_module.set_worker_task_type_registry(registry._task_types)
    else:
        task_registry_module.set_backend_task_type_registry(registry._task_types)
    # Phase 289 (289-04 review correction 6): record this process's role so
    # admission and the catalog resolve the snapshot THIS process populated —
    # an admission in the worker process must never read the backend snapshot.
    task_registry_module.set_process_role(is_worker=is_worker)

    # Phase 289 (CORESEAM-01): connector declarations need no analogous
    # publication step — PluginRegistry binds the process-local connector
    # registry that the Source/Target write paths already resolve
    # (`connector_registry.get_connector_type_registry()`) and activation
    # registers plugin identities directly into it. The lifecycle is the
    # authoritative publisher; backend and worker still hold independent
    # snapshots because they are separate processes.
    return registry


def get_plugin_registry() -> "PluginRegistry":
    if _registry_instance is None:
        raise RuntimeError(
            "PluginRegistry not initialized — call init_plugin_registry() during startup"
        )
    return _registry_instance
