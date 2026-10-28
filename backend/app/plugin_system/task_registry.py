"""Per-process qualified-name Task-type registry (PLUGTASK-02/PLUGTASK-03).

This is the plugin-task analogue of ``services/scheduler_manager.py``'s lazy
``JOB_REGISTRY``: a data-only map from the exact declared literal
(``<plugin-name>.<task-name>``) to the immutable declaration plus its owning
plugin. It never imports concrete plugins and never touches
``WorkerSettings.functions`` — the one static Core ARQ wrapper resolves a
persisted Task's literal here at execution time.

Backend and worker each hold their own snapshot. ``PluginRegistry`` owns a
``TaskTypeRegistry`` instance and feeds it from lifecycle activation; the
module-level ``get_backend_task_type_registry()`` / ``get_worker_task_type_registry()``
accessors expose the process-local one to the admission gate and the worker
wrapper respectively. Nothing here mutates a snapshot — a plugin rename is a
new declaration, never an alias (D-03).
"""

import html
import re
from dataclasses import dataclass

from app.plugin_system import validation
from app.plugin_system.base import IndraMeshPlugin
from app.plugin_system.capabilities import TaskTypeDeclaration

# The task-name segment is strict lowercase kebab-case (D-01). The plugin-name
# segment is validated as lowercase too, but tolerates ``_`` because existing
# plugin manifest names (``^[a-zA-Z0-9_-]+$``) and the loader's directory names
# already use it as an identifier character.
_TASK_SEGMENT_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
_OWNER_SEGMENT_RE = re.compile(r"^[a-z0-9_-]+$")

# Parameter keys are spread as ``run(ctx, task_id, **parameters)`` through
# ``PluginRegistry.call_capability(plugin, method_name, ctx, *args, timeout=...)``;
# any of these names would shadow a positional/named argument of that call.
RESERVED_PARAMETER_NAMES = frozenset({
    "self", "plugin", "method_name", "ctx", "task_id", "timeout", "args", "kwargs",
})

# Task.current_step is String(255) and TaskUpdate.current_step is an
# EscapedStr(max_length=255) — html.escape() runs before the length check, so a
# declared step is bounded on its escaped form or every report_step() for it
# would fail at runtime.
STEP_MAX_LENGTH = 255

# CORESEAM-02: the scope columns a declaration may require. These are exactly
# the Task/Schedule scope columns the create paths already carry.
ALLOWED_SCOPE_REQUIREMENTS = frozenset({
    "source_id", "schema_id", "object_type_id", "target_id",
})


def _known_permission_names(manifest=None) -> frozenset[str]:
    """The permissions a declaration may require, scoped to its owner (R02-4).

    Core's static catalogue PLUS only the declaring plugin's own
    manifest-declared permissions (never another plugin's). ``required_permission``
    must name one of these: an unknown/foreign name would make the task
    unstartable for every non-superuser (or silently reference another plugin's
    capability), so it is rejected at activation. Delegates to the shared
    owner-scoped helper; imported lazily — this module is on the plugin
    bootstrap path.
    """
    from app.plugin_system.permission_registry import known_permission_names

    return known_permission_names(manifest)


def known_cache_entity_types() -> frozenset[str]:
    """Every Core cache entity type a declaration/context may invalidate (review 3).

    The vocabulary is the cache service's own per-entity TTL table — exactly the
    keys ``cache_service.invalidate_entity_cache()`` acts on — so a typo fails
    activation/run time instead of silently invalidating nothing. Imported
    lazily for the same reason ``_known_permission_names()`` is: this module is
    imported by the plugin bootstrap path and must stay light.
    """
    from app.services.cache_service import ENTITY_TTLS

    return frozenset(ENTITY_TTLS)


class TaskTypeCollisionError(ValueError):
    """A declaration's exact name is already owned by another plugin (D-02)."""

    def __init__(self, name: str, owner: str) -> None:
        self.name = name
        self.owner = owner
        super().__init__(
            f"task type {name!r} is already declared by plugin {owner!r}"
        )


@dataclass(frozen=True)
class TaskTypeEntry:
    """A resolved declaration plus the name of the plugin that owns it."""

    declaration: TaskTypeDeclaration
    plugin_name: str


def validate_declaration(
    declaration: TaskTypeDeclaration, manifest=None
) -> None:
    """Fail closed on a declaration the Core cannot safely dispatch (D-01/D-05).

    Rejects a name that is not exactly two lowercase dot-separated segments, a
    task segment that is not strict kebab-case, and a parameter schema that
    carries a ``format: password`` marker the Core cannot protect (D-05: task
    parameters are never secret).

    ``manifest`` is the declaring plugin's manifest: a ``required_permission``
    or an ``admission_permissions`` entry may name a Core permission OR a
    permission that same plugin declared (R02-4), never another plugin's.
    """
    if not isinstance(declaration.name, str) or declaration.name.count(".") != 1:
        raise ValueError(
            f"task type name {declaration.name!r} must be "
            "'<plugin-name>.<task-name>'"
        )
    owner, task_name = declaration.name.split(".", 1)
    if not _OWNER_SEGMENT_RE.match(owner):
        raise ValueError(
            f"task type owner segment {owner!r} in {declaration.name!r} must be "
            "lowercase kebab-case"
        )
    if not _TASK_SEGMENT_RE.match(task_name):
        raise ValueError(
            f"task type name segment {task_name!r} in {declaration.name!r} must be "
            "lowercase kebab-case"
        )

    schema = declaration.parameter_schema or {}
    unsupported = validation.secret_field_names(schema) | (
        validation.unsupported_secret_field_paths(schema)
    )
    if unsupported:
        raise ValueError(
            f"task type {declaration.name!r} declares a secret parameter field at: "
            + ", ".join(sorted(unsupported))
            + " — task parameters must never contain secrets (D-05)"
        )
    reserved = set(schema.get("properties") or {}) & RESERVED_PARAMETER_NAMES
    if reserved:
        raise ValueError(
            f"task type {declaration.name!r} declares reserved parameter name(s): "
            + ", ".join(sorted(reserved))
        )

    for step in declaration.steps:
        if not isinstance(step, str) or not step:
            raise ValueError(
                f"task type {declaration.name!r} declares an empty step"
            )
        if len(html.escape(step)) > STEP_MAX_LENGTH:
            raise ValueError(
                f"task type {declaration.name!r} declares step {step[:40]!r}... "
                f"longer than {STEP_MAX_LENGTH} characters"
            )

    # CORESEAM-02 empty: a declared capability must be dispatchable — a missing
    # runner (or a non-callable admit hook) is an authoring error, not a
    # silently-unusable declaration.
    if not callable(declaration.run):
        raise ValueError(
            f"task type {declaration.name!r} has no callable runner"
        )
    if declaration.admit is not None and not callable(declaration.admit):
        raise ValueError(
            f"task type {declaration.name!r} declares a non-callable admit hook"
        )

    # CORESEAM-02 encoding: a supplied label is a non-blank Unicode string
    # returned verbatim; None means "fall back to the exact literal".
    if declaration.label is not None and (
        not isinstance(declaration.label, str) or not declaration.label.strip()
    ):
        raise ValueError(f"task type {declaration.name!r} declares a blank label")

    if declaration.required_permission is not None:
        permission = declaration.required_permission
        if (
            not isinstance(permission, str)
            or permission not in _known_permission_names(manifest)
        ):
            raise ValueError(
                f"task type {declaration.name!r} declares unknown "
                f"required_permission {permission!r} — it must be an existing "
                "Core '<resource>:<action>' permission or one this plugin declared"
            )

    # D-13/D-15: each additive admission permission is validated exactly like
    # required_permission — an unknown/foreign name would make the task
    # unstartable for every non-superuser, so it fails activation instead.
    for admission_permission in declaration.admission_permissions or ():
        if (
            not isinstance(admission_permission, str)
            or admission_permission not in _known_permission_names(manifest)
        ):
            raise ValueError(
                f"task type {declaration.name!r} declares unknown "
                f"admission_permissions entry {admission_permission!r} — it must "
                "be an existing Core '<resource>:<action>' permission or one this "
                "plugin declared"
            )

    for scope_field in declaration.scope_requirements or ():
        if scope_field not in ALLOWED_SCOPE_REQUIREMENTS:
            raise ValueError(
                f"task type {declaration.name!r} declares unsupported scope "
                f"requirement {scope_field!r}"
            )

    known_invalidation_targets = known_cache_entity_types()
    for entity_type in declaration.invalidates or ():
        if not isinstance(entity_type, str) or not entity_type.strip():
            raise ValueError(
                f"task type {declaration.name!r} declares a blank invalidation target"
            )
        # Review 3: invalidates keys are Core cache entity types — an unknown
        # key would silently invalidate nothing after every successful run.
        if entity_type not in known_invalidation_targets:
            raise ValueError(
                f"task type {declaration.name!r} declares unknown invalidation "
                f"target {entity_type!r} — it must be a Core cache entity type"
            )


class TaskTypeRegistry:
    """An in-memory, per-process map of exact literal -> owning declaration."""

    def __init__(self) -> None:
        self._entries: dict[str, TaskTypeEntry] = {}

    def add(self, plugin: IndraMeshPlugin) -> None:
        """Validate and register every declaration owned by ``plugin``.

        Validation and collision checks complete for ALL of a plugin's
        declarations before any entry is written, so a plugin with one bad or
        colliding declaration is rejected atomically — it never becomes a
        partially-registered owner.

        Re-adding the same owner is idempotent: its previous entries are
        replaced, never treated as a collision against itself (reactivation
        re-registers a plugin that was disabled at process startup).

        An empty declaration set is ALWAYS an authoring error (PLUGTASK-02):
        the Phase 292 transitional no-publication seam is deleted in the Phase
        296 cut-over (CLEAN-01/D-03), so every TaskTypePlugin must declare at
        least one type — there is no Core-owned-identity exception left.
        """
        declarations = list(plugin.task_types())
        owner = (
            plugin.manifest.name
            if plugin.manifest is not None
            else type(plugin).__name__
        )
        if not declarations:
            raise ValueError(
                f"TaskTypePlugin {owner!r} declares zero task types "
                "(PLUGTASK-02)"
            )

        prepared: list[TaskTypeDeclaration] = []
        seen: set[str] = set()
        for declaration in declarations:
            validate_declaration(declaration, plugin.manifest)
            # D-01: the owner segment IS the declaring plugin — a plugin can
            # never register a literal inside another plugin's namespace.
            owner_segment = declaration.name.split(".", 1)[0]
            if owner_segment != owner:
                raise ValueError(
                    f"task type {declaration.name!r} must be namespaced under "
                    f"the declaring plugin {owner!r}"
                )
            if declaration.name in seen:
                raise ValueError(
                    f"TaskTypePlugin {owner!r} declares task type "
                    f"{declaration.name!r} more than once"
                )
            seen.add(declaration.name)
            existing = self._entries.get(declaration.name)
            if existing is not None and existing.plugin_name != owner:
                raise TaskTypeCollisionError(declaration.name, existing.plugin_name)
            prepared.append(declaration)

        self.remove_owner(owner)
        for declaration in prepared:
            self._entries[declaration.name] = TaskTypeEntry(
                declaration=declaration, plugin_name=owner
            )

    def remove_owner(self, plugin_name: str) -> None:
        """Drop every entry owned by ``plugin_name`` (rollback on activation failure)."""
        for name in [
            name
            for name, entry in self._entries.items()
            if entry.plugin_name == plugin_name
        ]:
            del self._entries[name]

    def resolve(self, literal: str) -> TaskTypeEntry | None:
        """Return the entry for the exact literal, or ``None`` (D-03: no aliasing)."""
        return self._entries.get(literal)

    def entries(self) -> tuple[TaskTypeEntry, ...]:
        """Every owning entry in a stable (name-sorted) order (CORESEAM-03).

        The task-type catalog iterates this; a stable order keeps the API
        response deterministic without the registry itself imposing a policy.
        """
        return tuple(self._entries[name] for name in sorted(self._entries))

    def names(self) -> frozenset[str]:
        """The exact declared literals in this snapshot."""
        return frozenset(self._entries)


_backend_registry: TaskTypeRegistry | None = None
_worker_registry: TaskTypeRegistry | None = None
_process_is_worker: bool = False


def get_backend_task_type_registry() -> TaskTypeRegistry:
    """The backend process's read-only task-type snapshot (lazily created)."""
    global _backend_registry
    if _backend_registry is None:
        _backend_registry = TaskTypeRegistry()
    return _backend_registry


def get_worker_task_type_registry() -> TaskTypeRegistry:
    """The worker process's read-only task-type snapshot (lazily created)."""
    global _worker_registry
    if _worker_registry is None:
        _worker_registry = TaskTypeRegistry()
    return _worker_registry


def set_process_role(*, is_worker: bool) -> None:
    """Record which role THIS process registered its snapshot for.

    Called by ``init_plugin_registry()`` only. The worker process never
    populates the backend snapshot, so every consumer that must work in either
    process (admission, the worker wrapper, the catalog) resolves through
    :func:`get_process_task_type_registry` instead of a fixed role.
    """
    global _process_is_worker
    _process_is_worker = is_worker


def get_process_task_type_registry() -> TaskTypeRegistry:
    """The snapshot of the process's OWN role (review correction 6).

    A backend request and a worker-scheduled admission each resolve the
    declarations their own process actually discovered — an admission executed
    in the worker must never read the (empty) backend snapshot.
    """
    if _process_is_worker:
        return get_worker_task_type_registry()
    return get_backend_task_type_registry()


def set_backend_task_type_registry(registry: TaskTypeRegistry) -> None:
    """Replace the backend process's snapshot (called by startup init only)."""
    global _backend_registry
    _backend_registry = registry


def set_worker_task_type_registry(registry: TaskTypeRegistry) -> None:
    """Replace the worker process's snapshot (called by startup init only)."""
    global _worker_registry
    _worker_registry = registry
