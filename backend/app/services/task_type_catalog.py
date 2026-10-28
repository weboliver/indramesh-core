"""Core Task-type descriptor table + the permission-filtered catalog projection.

Phase 289 (CORESEAM-02/CORESEAM-03, D-04/D-05/D-10). Two responsibilities:

1. ONE authoritative Core descriptor table (``CORE_TASK_TYPE_DESCRIPTORS``)
   owns every Core task type's catalog metadata — label, schedulability and
   scope requirements. ``schedule_service`` imports its schedulable set and
   scope map from here instead of keeping a second copy, and Plan 289-07 reuses
   the same table for plugin-aware schedule validation.
2. :func:`build_task_type_catalog` projects the Core descriptors plus every
   ACTIVE plugin declaration the caller is allowed to see (``GET
   /api/v1/tasks/types``). Plugin declarations themselves stay owned by
   ``app.plugin_system.task_registry`` — this module never duplicates one.

The catalog's ``dispatchable`` value is an informative snapshot; the central
``task_service.admit_plugin_task()`` gate remains authoritative immediately
before a Task row is persisted (T-289-04-03).
"""

from dataclasses import dataclass, field

from app.models.task import TaskType


@dataclass(frozen=True)
class CoreTaskTypeDescriptor:
    """One Core task type's catalog metadata (CORESEAM-03)."""

    name: str
    label: str
    schedulable: bool
    scope_requirements: tuple[str, ...] = ()
    parameter_schema: dict = field(default_factory=dict)


# The Core task types in catalog order. Labels are Core-authored display names
# (the frontend's existing default labels for the same types); scope columns
# match the Schedule/Task scope columns the create paths already require.
CORE_TASK_TYPE_DESCRIPTORS: tuple[CoreTaskTypeDescriptor, ...] = (
    CoreTaskTypeDescriptor(
        name=TaskType.IMPORT.value,
        label="Import",
        schedulable=False,
        scope_requirements=("schema_id",),
    ),
    CoreTaskTypeDescriptor(
        name=TaskType.EXPORT.value,
        label="Export",
        schedulable=True,
        scope_requirements=("schema_id",),
    ),
    CoreTaskTypeDescriptor(
        name=TaskType.REINDEX.value,
        label="Reindex",
        schedulable=True,
        # schema_id is optional — unset means "reindex all schemas".
        scope_requirements=(),
    ),
)

# The Core task-type value set (any persisted Core tasktype is a member).
CORE_TASK_TYPES: frozenset[str] = frozenset(
    descriptor.name for descriptor in CORE_TASK_TYPE_DESCRIPTORS
)

def plugin_version_conflict_reason(
    plugin_name: str, loaded_version: str | None, active_version: str | None
) -> str:
    """The ONE bounded version-conflict wording (D-08/D-12).

    Names only the plugin and both versions plus the next step — never raw
    exception text, plugin config, or task parameters (T-289-04-04). Shared by
    the catalog's ``unavailable_reason`` and the admission 409 so the two can
    never drift apart.
    """
    return (
        f"plugin {plugin_name!r} is not dispatchable in the worker: "
        f"worker_loaded_version={loaded_version!r}, active version="
        f"{active_version!r} — restart or update the worker"
    )


async def build_task_type_catalog(db, current_user) -> list:
    """Project the caller's visible task types (CORESEAM-03, D-04/D-05/D-10).

    Core descriptors are always visible to a caller holding ``tasks:read``.
    A plugin descriptor is included only when its plugin row is ACTIVE and the
    caller's effective permission set holds the declaration's
    ``required_permission`` (superusers bypass that check). A stale worker does
    NOT hide an ACTIVE entry — it is reported with ``dispatchable=False`` and a
    bounded Core-authored reason (D-10). Returns ``TaskTypeCatalogEntry`` rows
    (imported lazily so this service keeps no import cycle on the API schemas).

    The plugin-system and auth imports are function-local, matching
    ``task_service.admit_plugin_task()``'s no-import-cycle discipline.
    """
    from sqlalchemy import select

    from app.models.plugin import Plugin, PluginState
    from app.plugin_system.task_registry import get_process_task_type_registry
    from app.schemas.crud import TaskTypeCatalogEntry
    from app.services.auth_service import resolve_user_permissions

    if current_user.is_superuser:
        permissions: set[str] | None = None  # superuser: no per-declaration filter
    else:
        permissions = await resolve_user_permissions(current_user.id)

    entries = [
        TaskTypeCatalogEntry(
            name=descriptor.name,
            label=descriptor.label,
            source="core",
            plugin_name=None,
            schedulable=descriptor.schedulable,
            scope_requirements=list(descriptor.scope_requirements),
            parameter_schema=dict(descriptor.parameter_schema),
            dispatchable=True,
            unavailable_reason=None,
            catalog_visible=True,
        )
        for descriptor in CORE_TASK_TYPE_DESCRIPTORS
    ]

    snapshot = get_process_task_type_registry()
    registry_entries = snapshot.entries()
    plugin_names = sorted({entry.plugin_name for entry in registry_entries})
    rows_by_name = {}
    if plugin_names:
        result = await db.execute(select(Plugin).where(Plugin.name.in_(plugin_names)))
        rows_by_name = {row.name: row for row in result.scalars().all()}

    for entry in registry_entries:
        declaration = entry.declaration
        required_permission = declaration.required_permission
        if (
            required_permission is not None
            and permissions is not None
            and required_permission not in permissions
        ):
            continue

        row = rows_by_name.get(entry.plugin_name)
        if row is None:
            continue
        state_value = (
            row.state.value if isinstance(row.state, PluginState) else row.state
        )
        if state_value != PluginState.ACTIVE.value:
            continue

        if row.worker_loaded_version != row.version:
            dispatchable = False
            unavailable_reason = plugin_version_conflict_reason(
                entry.plugin_name, row.worker_loaded_version, row.version
            )
        else:
            dispatchable = True
            unavailable_reason = None

        entries.append(
            TaskTypeCatalogEntry(
                name=declaration.name,
                # A declaration without a label falls back to its exact literal
                # (never a derived/aliased name); a supplied label is returned
                # verbatim.
                label=declaration.label or declaration.name,
                source="plugin",
                plugin_name=entry.plugin_name,
                schedulable=declaration.schedulable,
                scope_requirements=list(declaration.scope_requirements),
                parameter_schema=dict(declaration.parameter_schema or {}),
                dispatchable=dispatchable,
                unavailable_reason=unavailable_reason,
                catalog_visible=declaration.catalog_visible,
            )
        )

    return entries
