"""PluginRegistry — lifecycle states, persistence, dispatch and the kill-switch.

An in-memory map of loaded instances plus a parallel state map, mirrored into
the core ``plugins`` table. Capability dispatch (PLUGCORE-02), per-call
``asyncio.wait_for`` timeout + exception isolation and the kill-switch
(PLUGCORE-03) all live here.

Every capability/hook invocation crosses the single ``call_capability()``
chokepoint. It catches ``Exception`` only — never ``BaseException`` — and
re-raises ``asyncio.CancelledError`` explicitly, so the caller's own
cancellation (request disconnect, ARQ job cancel) is never swallowed.

Documented limitation (RESEARCH.md Pitfall 1): ``wait_for`` only ever cancels a
cooperative (awaiting) coroutine. CPU-bound, non-yielding plugin code is NOT
interrupted — process/sandbox isolation is PLUGISO-01/v2 scope.
"""

import asyncio
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import inspect, select, text
from sqlalchemy.exc import DataError, IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import defer
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.orm.base import NO_VALUE

from app.db import async_session_maker
from app.db.redis import get_redis
from app.models.plugin import Plugin, PluginState
from app.models.task import Task
from app.plugin_system.base import IndraMeshPlugin, capability_declaration_mismatch
from app.plugin_system.capabilities import ConnectorPlugin, TaskTypePlugin
from app.plugin_system.connector_registry import (
    ConnectorTypeCollisionError,
    ConnectorTypeRegistry,
    get_connector_type_registry,
)
from app.plugin_system.context import PluginContext
from app.plugin_system.database_registry import (
    DatabaseTableRegistry,
    TableOwnershipError,
)
from app.plugin_system.loader import DiscoveryResult, discover_directory_plugins
from app.plugin_system.migrations import (
    DIAGNOSTIC_COLUMNS_PENDING_WARNING,
    REASON_CONTRACT_CHANGED,
    REASON_CONTRACT_SECTION_REMOVED,
    REASON_MIGRATIONS_DIR_MISSING,
    REASON_MIGRATIONS_PENDING,
    REASON_MULTIPLE_HEADS,
    REASON_NO_HEAD,
    REASON_UNKNOWN_REVISION,
    ownership_refusal_reason,
    safe_diagnostic,
    validated_revision,
)
from app.plugin_system.task_registry import TaskTypeCollisionError, TaskTypeRegistry
from app.services.audit_service import AuditEntityType, audit_log
from app.services.cache_service import invalidate_entity_cache

logger = logging.getLogger(__name__)

# The schedule caches serialize the runtime_status/runtime_reason projection,
# which is derived from the ``plugins`` row (state, version,
# worker_loaded_version) — every committed write to those columns drops it.
SCHEDULE_CACHE_ENTITY = "schedule"

# Seconds a single capability/hook call may run before ``wait_for`` cuts it off.
DEFAULT_CAPABILITY_TIMEOUT = 30.0
# Shorter budget for lifecycle hooks (on_load/on_unload) — a hanging hook must
# never block a kill-switch or a reactivate.
LIFECYCLE_HOOK_TIMEOUT = 5.0
# Seconds a cross-process kill-switch/reactivation stays authoritative in the
# Redis state cache before the `plugins` table is consulted again (D-13).
STATE_CACHE_TTL = 8

# Phase 287 (D-06/D-11..D-14): the database gate's refusal reasons come from the
# ONE shared vocabulary in ``migrations.py`` (imported above), so a persisted
# diagnostic reads identically whether it was authored by the deploy runner or by
# an activation refusal. The full technical detail goes to the log via
# ``_mark_error``; only those stable strings (plus the plugin name and, where
# applicable, the revision) are ever recorded/returned.
def _ownership_refusal_reason(plugin: IndraMeshPlugin, exc: Exception) -> str:
    """Map a table-ownership/validation refusal to a fixed-vocabulary reason.

    Thin wrapper over :func:`app.plugin_system.migrations.ownership_refusal_reason`
    so the activation gate and the deploy runner's pre-flight share ONE mapping
    and a diagnostic reads identically wherever it was authored.
    """
    return ownership_refusal_reason(
        plugin.manifest.name if plugin.manifest is not None else type(plugin).__name__,
        plugin.manifest,
        getattr(plugin, "database_metadata", None),
        exc,
    )


@dataclass
class _DatabaseRefusal:
    """A gate refusal: the operator/log message plus the persisted-diagnostic parts."""

    message: str
    reason: str
    revision: str | None = None


def _database_refusal(
    reason: str, name: str, *revisions: str
) -> str:
    """Build a fixed-vocabulary refusal message naming the plugin (and revisions)."""
    detail = (
        "; revision(s): " + ", ".join(sorted(revisions)) if revisions else ""
    )
    return f"{reason} (plugin {name!r}){detail}"


def _gate_refusal(reason: str, name: str, *revisions: str) -> _DatabaseRefusal:
    """Build a :class:`_DatabaseRefusal` from the fixed vocabulary.

    The first (sorted) revision names the affected revision in the persisted
    diagnostic; the message keeps the existing multi-revision detail for the log.
    """
    revision = sorted(revisions)[0] if revisions else None
    return _DatabaseRefusal(
        message=_database_refusal(reason, name, *revisions),
        reason=reason,
        revision=revision,
    )


class PluginTimeoutError(Exception):
    """A capability/hook call exceeded its ``asyncio.wait_for`` budget."""

    def __init__(self, plugin_name: str, method_name: str, timeout: float) -> None:
        self.plugin_name = plugin_name
        self.method_name = method_name
        self.timeout = timeout
        super().__init__(
            f"Plugin {plugin_name!r}.{method_name} exceeded its {timeout}s timeout"
        )


class PluginCallError(Exception):
    """A capability/hook call raised; the original exception is preserved."""

    def __init__(self, plugin_name: str, method_name: str, original: Exception) -> None:
        self.plugin_name = plugin_name
        self.method_name = method_name
        self.original = original
        super().__init__(
            f"Plugin {plugin_name!r}.{method_name} raised "
            f"{type(original).__name__}: {original}"
        )


class PluginNotActiveError(PluginCallError):
    """A capability/hook call was refused because the plugin is not active.

    Deliberately a ``PluginCallError`` subclass: every existing isolation site
    (``hooks.dispatch_hook``, the health endpoint) catches ``PluginCallError``,
    so a cross-process kill-switch skips the one plugin instead of propagating
    an exception into core CRUD/hooks — D-13's check therefore needs zero
    changes to ``hooks.py``.
    """

    def __init__(self, plugin_name: str, method_name: str) -> None:
        super().__init__(
            plugin_name, method_name, RuntimeError("plugin is not active")
        )


def _worker_update_warning(plugin: IndraMeshPlugin) -> str | None:
    """Return the PLUGWORKER-02 consistency warning for ``plugin``, or ``None``.

    A plugin that relies on worker-side behavior — the ``task_type`` capability
    or an overridden ``before_index``/``after_index`` hook — should declare
    ``requires_worker_update = true`` in its manifest so the future admin badge
    (Phase 285) can flag a worker that still needs a reload. The warning is a
    non-fatal author-consistency hint: it never changes state, never persists
    anything and never blocks ``on_load()`` (D-05).

    ``is_hook_overridden`` is imported function-locally because ``hooks.py``
    imports ``PluginRegistry`` from this module — a top-level import would close
    the cycle (RESEARCH Pitfall 3).
    """
    from app.plugin_system.hooks import is_hook_overridden

    manifest = plugin.manifest
    if manifest is None or manifest.requires_worker_update:
        return None

    reasons: list[str] = []
    if "task_type" in manifest.capabilities:
        reasons.append("declares the 'task_type' capability")
    # Stable order (capability, then before_index, then after_index) so repeated
    # discoveries of the same plugin yield an identical message. Exactly one
    # message is built from the accumulated reasons — never one log per reason.
    for hook in ("before_index", "after_index"):
        if is_hook_overridden(plugin, hook):
            reasons.append(f"overrides the worker hook {hook!r}")
    if not reasons:
        return None
    return (
        f"Plugin {manifest.name!r} {'; '.join(reasons)} but does not declare "
        "requires_worker_update = true — the worker will not pick up the new "
        "version until it restarts"
    )


class PluginRegistry:
    """In-memory registry of loaded plugin instances, mirrored into Postgres."""

    def __init__(self, *, is_worker: bool = False) -> None:
        self.is_worker = is_worker
        self._plugins: dict[str, IndraMeshPlugin] = {}
        self._states: dict[str, PluginState] = {}
        # Phase 286 (PLUGTASK-02/D-02): this process's discovered Task-type
        # declarations, fed from ACTIVE TaskTypePlugin instances during
        # activation. Backend and worker hold independent snapshots.
        self._task_types = TaskTypeRegistry()
        # Phase 289 (CORESEAM-01): THIS process's connector-identity snapshot — the
        # very registry ``connector_registry.resolve_connector_type()`` consults for
        # Source/Target writes. Activation therefore publishes plugin declarations
        # directly into it (and removal withdraws them), so the lifecycle is the
        # authoritative publisher: an ACTIVE owner's connector is accepted on the
        # migrated storage, a deactivated owner's is a 422, and no publish step
        # can be forgotten between the two. Seeded from the one shared Core-owned
        # built-in set, so a plugin can never shadow a built-in.
        self._connectors: ConnectorTypeRegistry = get_connector_type_registry()
        # Phase 287 (PLUGDB-02/D-02): this process's table-ownership inventory,
        # fed from database-owning plugins during activation so a mis-named,
        # core-colliding or cross-plugin-colliding declaration is refused before
        # on_load(). Backend and worker hold independent snapshots.
        self._database = DatabaseTableRegistry()
        # Phase 287: the resolved on-disk plugin directory per discovered plugin
        # (``DiscoveryResult.base_dir``). Captured during activation — including
        # the DISABLED early return — so the reactivation gate can resolve the
        # plugin's own migrations directory without a fresh discovery pass.
        self._plugin_base_dirs: dict[str, str | None] = {}
        # WR-01: worker-snapshot readiness tri-state. None = not probed yet,
        # True = the worker_loaded_* columns are present, False = absent (a
        # pre-283 schema) and reads must use the deferred projection.
        self._worker_snapshot_ready: bool | None = None

    async def load_results(
        self, db: AsyncSession, results: list[DiscoveryResult]
    ) -> None:
        """Instantiate + activate every successful discovery result, isolate failures.

        ``on_load()`` runs inside its own try/except: a raising ``on_load()`` is
        the same isolated error outcome as a discovery failure (PLUGOPS-01's
        "isolate loading AND registration"). ``on_load`` receives a real
        ``PluginContext`` built by ``build_context()`` (D-06) — not ``None``.

        A loader-declared duplicate rejection (``duplicate_rejected``, NYQ-282-01)
        is logged and skipped instead of routed through ``_mark_error()``: the
        rejected candidate shares its manifest name with an already-accepted
        winner, so removing ``_plugins[name]``, setting ``_states[name]`` to ERROR
        and upserting the unique ``plugins.name`` row would all mutate the winner.
        """
        for result in results:
            if result.duplicate_rejected:
                logger.warning(
                    "Plugin %r rejected as a discovery duplicate: %s — "
                    "preserving the already-accepted winner",
                    result.name,
                    result.error,
                )
                continue
            if result.error is not None or result.plugin_cls is None or result.manifest is None:
                await self._mark_error(
                    db,
                    result.name,
                    result.manifest.version if result.manifest else "unknown",
                    result.error or "discovery produced no plugin class",
                )
                continue
            await self._activate(db, result)

    async def _activate(self, db: AsyncSession, result: DiscoveryResult) -> None:
        name = result.name
        # Phase 287: remember the on-disk base dir even for the DISABLED early
        # return below, so reactivate()/_reconcile_state() can resolve this
        # plugin's migrations directory through the shared resolver.
        self._plugin_base_dirs[name] = result.base_dir
        try:
            plugin = result.plugin_cls()
            plugin.manifest = result.manifest
        except Exception as exc:
            logger.exception("Plugin %r failed to construct", name)
            await self._mark_error(
                db,
                name,
                result.manifest.version,
                f"construction failed: {type(exc).__name__}: {exc}",
            )
            return

        existing = await self._get_row(db, name)
        if existing is None and not result.manifest.active_by_default:
            await self._upsert(
                db, name, result.manifest.version, PluginState.DISABLED
            )
            existing = await self._get_row(db, name)

        # Phase 303 (SEAM-01/D-04): register this plugin's manifest-declared
        # permissions BEFORE the disabled early return, so a disabled plugin
        # still reserves its ownership and its first-insert defaults land
        # (RESEARCH Pitfall 2). A collision refuses the whole plugin; on a
        # DISABLED plugin the operator kill-switch is preserved and the refusal
        # is re-checked on reactivation (R01-2).
        permission_error = await self._register_manifest_permissions(db, name, plugin)
        if permission_error is not None:
            if existing is not None and existing.state == PluginState.DISABLED:
                logger.error(
                    "Plugin %r permission registration refused while DISABLED: %s "
                    "— keeping DISABLED; re-checked on reactivation",
                    name,
                    permission_error,
                )
                # Fall through to the DISABLED-preservation block below.
            else:
                self._plugins.pop(name, None)
                await self._mark_error(
                    db, name, result.manifest.version, permission_error
                )
                return

        if existing is not None and existing.state == PluginState.DISABLED:
            # Operator kill-switch must survive a restart (PLUGCORE-10): keep the
            # instance constructed (so a later reactivate() can call on_load),
            # never call on_load, never overwrite the persisted state.
            self._plugins[name] = plugin
            self._states[name] = PluginState.DISABLED
            try:
                # Keep a disabled database plugin's declared tables reserved so a
                # sibling cannot claim them before this plugin is reactivated.
                self._database.add(plugin)
            except (TableOwnershipError, ValueError) as exc:
                self._database.remove_owner(name)
                logger.warning(
                    "Could not reserve disabled plugin %r database tables: %s",
                    name,
                    exc,
                )
            existing.version = result.manifest.version
            await db.flush()
            return

        # Phase 287 (D-11): a discovered plugin whose persisted row records a
        # database contract but whose manifest no longer declares a [database]
        # section is refused as a contract violation — its tables (and version
        # table) remain, so the declaration must never disappear silently. This
        # runs for EVERY plugin (not only database-declaring ones) immediately
        # after the DISABLED-preservation return, so a kill-switched plugin's
        # state is never re-evaluated here (D-10).
        _, recorded_version_table = self._recorded_database_contract(existing)
        if recorded_version_table is not None and result.manifest.database is None:
            self._plugins.pop(name, None)
            await self._mark_error(
                db,
                name,
                result.manifest.version,
                _database_refusal(REASON_CONTRACT_SECTION_REMOVED, name),
            )
            # D-13: record the gate refusal after the state write, so the row
            # exists and the diagnostic lands in the same activation.
            await self._record_database_diagnostic(
                db, name, REASON_CONTRACT_SECTION_REMOVED
            )
            return

        # PLUGWORKER-02 (D-05): a non-fatal author-consistency hint. Computed
        # after the DISABLED-preservation early return and before the D-02
        # mismatch check, so a plugin that also mismatches still emits the
        # warning before _mark_error() returns (RESEARCH Pitfall 4).
        warning = _worker_update_warning(plugin)
        if warning is not None:
            logger.warning("%s", warning)

        # D-02: a plugin whose declared capabilities disagree with its real class
        # hierarchy must never activate. Checked after the DISABLED-preservation
        # branch (a kill-switched plugin's state must never be clobbered) and
        # before the LOADED transition / on_load().
        mismatch = capability_declaration_mismatch(plugin)
        if mismatch is not None:
            self._plugins.pop(name, None)
            await self._mark_error(db, name, result.manifest.version, mismatch)
            return

        # Phase 287 (PLUGDB-02/D-02): enforce the table ownership + naming
        # contract BEFORE on_load()/the LOADED transition, in the same
        # failure-isolation slot as the capability/task-type checks. A plugin
        # declaring a table outside its own `plugin_<normalized-name>_`
        # namespace, a core table, or a table another already-loaded plugin owns
        # is refused atomically and never becomes a partially-registered owner.
        # Placed before the task-type block so a database-refused plugin also
        # leaves no task-type registrations behind. `add()` is a no-op for an
        # ordinary plugin, so this runs for every candidate.
        try:
            self._database.add(plugin)
        except TableOwnershipError as exc:
            self._database.remove_owner(name)
            self._plugins.pop(name, None)
            await self._mark_error(db, name, result.manifest.version, str(exc))
            await self._record_database_diagnostic(
                db, name, _ownership_refusal_reason(plugin, exc)
            )
            return
        except ValueError as exc:
            self._database.remove_owner(name)
            self._plugins.pop(name, None)
            await self._mark_error(
                db,
                name,
                result.manifest.version,
                f"invalid database declaration: {exc}",
            )
            await self._record_database_diagnostic(
                db, name, _ownership_refusal_reason(plugin, exc)
            )
            return

        # Phase 287 (PLUGDB-03/D-04/D-06): a database-owning plugin may only
        # activate when its OWN migration head is reached. The check is
        # read-only and per-plugin — it reads only that plugin's declared version
        # table, never the core chain. A missing/pending/unknown head, a
        # multi-head script tree or a changed/removed persisted contract
        # isolates into ERROR exactly like the capability/task-type checks and
        # rolls the owner back out of the inventory.
        if result.manifest.database is not None:
            refusal = await self._database_gate(
                db, name, plugin, result.base_dir
            )
            if refusal is not None:
                self._database.remove_owner(name)
                self._plugins.pop(name, None)
                await self._mark_error(
                    db, name, result.manifest.version, refusal.message
                )
                await self._record_database_diagnostic(
                    db, name, refusal.reason, refusal.revision
                )
                return

        # Phase 289 (CORESEAM-01): build this plugin's connector declarations and
        # enforce the exact-identity collision contract BEFORE on_load()/the
        # LOADED transition, in the same failure-isolation slot as the
        # capability/task-type checks. A losing or invalid owner — including a
        # declaration colliding with a Core built-in — is isolated via
        # _mark_error() before on_load ever runs, and publishes NO partial
        # entries (the registry's prepare-then-commit add() refuses the whole
        # owner, never a subset). Placed before the task-type block so a
        # connector-refused plugin also leaves no task types behind. A plugin
        # without the capability contributes nothing.
        if isinstance(plugin, ConnectorPlugin):
            declarations = list(plugin.connector_types())
            # Phase 296 (CLEAN-01/D-03): the Jira plugin is the sole owner of the
            # `jira` connector through this standard publication path; the
            # transitional no-publication seam is gone, so every ConnectorPlugin
            # must declare at least one type and a colliding/invalid declaration
            # isolates the owner before on_load.
            try:
                self._connectors.add(name, declarations)
            except ConnectorTypeCollisionError as exc:
                self._connectors.remove_owner(name)
                self._plugins.pop(name, None)
                await self._mark_error(db, name, result.manifest.version, str(exc))
                return
            except ValueError as exc:
                self._connectors.remove_owner(name)
                self._plugins.pop(name, None)
                await self._mark_error(
                    db,
                    name,
                    result.manifest.version,
                    f"invalid connector declaration: {exc}",
                )
                return

        # Phase 286 (PLUGTASK-02/D-02/D-05): build this plugin's Task-type
        # declarations and enforce the exact-name collision contract BEFORE
        # on_load()/the LOADED transition. A losing or invalid owner is isolated
        # via _mark_error() before on_load ever runs, so no ACTIVE upsert or
        # audit row has to be undone and the first owner remains the sole
        # dispatch target. A zero-declaration TaskTypePlugin is a registration
        # error; a plugin without the capability contributes an empty set.
        if isinstance(plugin, TaskTypePlugin):
            try:
                self._task_types.add(plugin)
            except TaskTypeCollisionError as exc:
                # Phase 289: roll the connector block above back out too — the
                # plugin lands ERROR and must leave no dispatchable declaration.
                self._connectors.remove_owner(name)
                self._plugins.pop(name, None)
                await self._mark_error(db, name, result.manifest.version, str(exc))
                return
            except ValueError as exc:
                self._connectors.remove_owner(name)
                self._plugins.pop(name, None)
                await self._mark_error(
                    db,
                    name,
                    result.manifest.version,
                    f"invalid task type declaration: {exc}",
                )
                return

        # Register the instance before on_load() so build_context() can resolve
        # its manifest's config_schema (D-06/D-12: on_load's ctx.config must be
        # decrypted and schema-defaulted, same as reactivate()/_reconcile_state()).
        # Rolled back on failure below.
        self._plugins[name] = plugin
        self._states[name] = PluginState.LOADED
        try:
            await asyncio.wait_for(
                plugin.on_load(await self.build_context(name)),
                timeout=LIFECYCLE_HOOK_TIMEOUT,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Plugin %r failed in on_load", name)
            self._plugins.pop(name, None)
            # Phase 286: roll back any declarations registered before on_load so
            # an ERROR plugin never leaves dispatchable task types behind.
            self._task_types.remove_owner(name)
            # Phase 289: same rollback for the connector-identity snapshot.
            self._connectors.remove_owner(name)
            # Phase 287: same rollback for the table-ownership inventory.
            self._database.remove_owner(name)
            await self._mark_error(
                db, name, result.manifest.version,
                f"on_load failed: {type(exc).__name__}: {exc}",
            )
            return

        self._states[name] = PluginState.ACTIVE
        await self._upsert(db, name, result.manifest.version, PluginState.ACTIVE)
        # PLUGWORKER-02/D-01: record the worker's own last-loaded snapshot on a
        # genuine LOADED -> ACTIVE transition. No-op for the backend registry.
        await self._record_worker_load(name, result.manifest.version, db)

    async def _record_worker_load(
        self, name: str, version: str, db: AsyncSession | None = None
    ) -> None:
        """Record when the worker process last activated ``name`` (D-01).

        Worker-exclusive by construction: returns immediately for a backend
        registry (``is_worker=False``) — the backend process must never write
        these columns (D-01/D-04). Called on a genuine transition into ACTIVE,
        from ``_activate()`` (with the caller's session), ``_reconcile_state()``
        and ``reactivate()`` (with no session, so this opens its own exactly
        like ``_persist_state()``). Never called from ``_upsert()``,
        ``_mark_error()``, ``_persist_state()`` or ``kill_switch()``, so a
        kill-switch preserves the historical snapshot (D-02). A missing row is
        a no-op, not an error.

        The worker container does not run migrations itself (only the backend
        runs ``alembic upgrade head``); if this fires before migration 283 has
        been applied, ``worker_loaded_at``/``worker_loaded_version`` don't
        exist yet. That's tolerated here — these columns are non-critical
        metadata (Phase 285's admin badge only) — rather than propagating and
        crashing worker startup (``startup_recovery`` awaits this unguarded).
        """
        if not self.is_worker:
            return
        now = datetime.now(timezone.utc).replace(tzinfo=None)
        if db is not None:
            # WR-01 durability: the caller-session write is confined to a
            # SAVEPOINT. A pre-283 schema rejects the snapshot UPDATE with
            # ProgrammingError; exiting the savepoint rolls back only this
            # write, never the caller's already-flushed ACTIVE state or its
            # audit rows (a full-session ``db.rollback()`` would discard both).
            try:
                async with db.begin_nested():
                    row = await self._get_row(db, name)
                    if row is None:
                        return
                    row.worker_loaded_at = now
                    row.worker_loaded_version = version
                    # WR-02: assigning updated_at to its own current value does
                    # NOT put it in the flush's SET clause -- SQLAlchemy's
                    # AttributeImpl compares by value and records no history for
                    # an identical assignment, so `onupdate` still fires on this
                    # worker-only snapshot UPDATE and silently re-stamps the
                    # API-exposed `updated_at`. flag_modified() forces the
                    # column into the SET clause as its own current value,
                    # which pins it against onupdate without changing it.
                    flag_modified(row, "updated_at")
                    await db.flush()
            except ProgrammingError:
                logger.warning(
                    "Could not record worker-load snapshot for %r — "
                    "worker_loaded_at/worker_loaded_version columns are not "
                    "yet migrated (migration 283_add_worker_loaded_cols "
                    "pending)",
                    name,
                )
            return
        async with async_session_maker() as session:
            row = await self._get_row(session, name)
            if row is None:
                return
            row.worker_loaded_at = now
            row.worker_loaded_version = version
            # WR-02: see the matching flag_modified() call above -- a plain
            # self-assignment does not stop `onupdate` from re-stamping this
            # worker-only snapshot write.
            flag_modified(row, "updated_at")
            try:
                await session.commit()
            except ProgrammingError:
                await session.rollback()
                logger.warning(
                    "Could not record worker-load snapshot for %r — "
                    "worker_loaded_at/worker_loaded_version columns are not "
                    "yet migrated (migration 283_add_worker_loaded_cols "
                    "pending)",
                    name,
                )
                return
        await invalidate_entity_cache(SCHEDULE_CACHE_ENTITY)

    async def _register_manifest_permissions(
        self, db: AsyncSession, name: str, plugin: IndraMeshPlugin
    ) -> str | None:
        """Register a plugin's declared permissions, or return a refusal message.

        Runs for every discovered plugin (including disabled ones) so ownership
        is reserved before the disabled early return (RESEARCH Pitfall 2). The
        worker runs the same validation/collision check read-only and inserts
        nothing (R01-1). A collision or write failure returns a message the
        caller turns into a whole-plugin refusal.
        """
        manifest = plugin.manifest
        if manifest is None or not manifest.permissions:
            return None

        # Imported function-locally: this module is on the plugin bootstrap path
        # and permission_registry pulls the Core model stack in.
        from app.plugin_system.permission_registry import (
            PermissionCollisionError,
            register_declared_permissions,
        )

        try:
            # Savepoint-isolated (review R01-2): a failed registration rolls
            # back any partial declaration so the lifecycle error persistence
            # below still works and no partially-seeded collision loser survives.
            async with db.begin_nested():
                await register_declared_permissions(
                    db, name, manifest.permissions, write=not self.is_worker
                )
        except PermissionCollisionError as exc:
            return str(exc)
        except Exception as exc:  # noqa: BLE001
            logger.exception("Plugin %r permission registration failed", name)
            return f"permission registration failed: {type(exc).__name__}: {exc}"
        return None

    async def _register_permissions_for_reactivation(
        self, name: str, plugin: IndraMeshPlugin, *, actor_id: uuid.UUID | None = None
    ) -> bool:
        """Re-check + register declared permissions on a transition into ACTIVE.

        Parity with the task-type/database/connector reactivation registrations:
        a plugin DISABLED when this process started returned from ``_activate()``
        after the permission reservation, so a collision detected there must
        refuse reactivation (R01-2), and actions added by a plugin update while
        disabled receive their first-insert defaults here. Idempotent — an
        existing row is never re-granted.
        """
        manifest = plugin.manifest
        if manifest is None or not manifest.permissions:
            return True

        from app.plugin_system.permission_registry import (
            PermissionCollisionError,
            register_declared_permissions,
        )

        async with async_session_maker() as session:
            try:
                # Savepoint-isolated (review WR-02): parity with the activation
                # twin. A failed declaration rolls back only the savepoint, so
                # the ERROR-state persistence + commit below cannot raise
                # PendingRollbackError out of reactivate/reconcile.
                async with session.begin_nested():
                    await register_declared_permissions(
                        session, name, manifest.permissions, write=not self.is_worker
                    )
            except PermissionCollisionError as exc:
                logger.warning(
                    "Plugin %r permission registration refused during "
                    "reactivation: %s",
                    name,
                    exc,
                )
                self._states[name] = PluginState.ERROR
                await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
                await session.commit()
                return False
            except Exception as exc:  # noqa: BLE001
                logger.exception(
                    "Plugin %r permission registration failed during reactivation",
                    name,
                )
                self._states[name] = PluginState.ERROR
                await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
                await session.commit()
                return False
            await session.commit()
        return True

    async def _mark_error(
        self, db: AsyncSession, name: str, version: str, message: str
    ) -> None:
        """Isolate a plugin failure: log it, persist state=error, never register it.

        The error string itself is not persisted — the ``plugins`` table has no
        error-message column (PLUGCORE-10); the log line is the record. A
        persisted ``DISABLED`` row (operator kill-switch) is preserved rather
        than clobbered by a discovery-time failure (WR-09) — mirrors
        ``_activate()``'s DISABLED-preservation branch, which only covers
        successful discovery results, not error ones.
        """
        logger.error("Plugin %r isolated in error state: %s", name, message)
        self._plugins.pop(name, None)

        existing = await self._get_row(db, name)
        if existing is not None and existing.state == PluginState.DISABLED:
            logger.warning(
                "Plugin %r failed discovery but is persisted DISABLED "
                "(operator kill-switch) — preserving DISABLED instead of "
                "overwriting with ERROR",
                name,
            )
            self._states[name] = PluginState.DISABLED
            return

        self._states[name] = PluginState.ERROR
        await self._upsert(db, name, version, PluginState.ERROR)

    async def _upsert(
        self,
        db: AsyncSession,
        name: str,
        version: str,
        state: PluginState,
        *,
        actor_id: uuid.UUID | None = None,
    ) -> None:
        existing = await self._get_row(db, name)
        if existing is None:
            try:
                async with db.begin_nested():
                    row = Plugin(name=name, version=version, state=state, config={})
                    db.add(row)
                    await db.flush()
            except IntegrityError:
                logger.info(
                    "Plugin %r row was created concurrently by the other process "
                    "— updating instead",
                    name,
                )
                existing = await self._get_row(db, name)
                if existing is None:
                    raise

        if existing is not None:
            old_state_value = (
                existing.state.value
                if isinstance(existing.state, PluginState)
                else existing.state
            )
            existing.version = version
            existing.state = state
            row = existing
            # Never overwrite a persisted `config` — owned by the config API.
        else:
            old_state_value = None
        await db.flush()

        new_state_value = state.value
        if old_state_value != new_state_value:
            await audit_log(
                db,
                AuditEntityType.PLUGINS.value,
                row.id,
                "CREATE" if existing is None else "UPDATE",
                actor_id,
                old_values={"state": old_state_value},
                new_values={"state": new_state_value},
            )

    async def _get_row(self, db: AsyncSession, name: str) -> Plugin | None:
        """Read ``name``'s ``plugins`` row, worker-aware for a pre-283 schema (WR-01).

        The worker container does not run migrations itself (only the backend
        runs ``alembic upgrade head``), so it can start against a schema where
        ``worker_loaded_at``/``worker_loaded_version`` do not exist yet. A full
        ``select(Plugin)`` would then raise ``ProgrammingError`` while loading
        the missing columns and abort worker startup. For a worker registry the
        first read probes the full projection behind a savepoint; if the columns
        are absent the registry permanently switches to a ``defer()``-projected
        SELECT, so the snapshot stays non-critical metadata. A backend registry
        (``is_worker=False``) always uses the full projection and still fails
        loudly on a genuinely broken schema.

        WR-04: the fallback is sticky for this registry's lifetime — once a
        worker downgrades to the projected SELECT it stays downgraded even
        after the backend later applies migration 283 (the columns then go
        unrecorded until the worker process is restarted). To avoid also
        misdiagnosing an unrelated schema fault (a genuinely broken table) as
        "migration 283 pending" and silently downgrading forever, the
        fallback only triggers when the ``ProgrammingError`` specifically
        names the two snapshot columns; any other error re-raises and aborts
        startup exactly like the backend registry would.
        """
        if not self.is_worker:
            return await self._select_full_row(db, name)

        if self._worker_snapshot_ready is None:
            try:
                async with db.begin_nested():
                    row = await self._select_full_row(db, name)
            except ProgrammingError as exc:
                if not self._is_missing_worker_snapshot_columns(exc):
                    raise
                self._worker_snapshot_ready = False
                logger.warning(
                    "Plugin %r: worker_loaded_at/worker_loaded_version columns "
                    "are absent (migration 283_add_worker_loaded_cols pending) "
                    "— using the deferred worker-snapshot projection instead of "
                    "aborting startup. This falls back permanently for this "
                    "process's lifetime: once the backend applies migration "
                    "283, this worker must be RESTARTED to resume recording "
                    "worker_loaded_at/worker_loaded_version (WR-04).",
                    name,
                )
            else:
                self._worker_snapshot_ready = True
                return row

        if self._worker_snapshot_ready is False:
            return await self._select_projected_row(db, name)
        return await self._select_full_row(db, name)

    @staticmethod
    def _is_missing_worker_snapshot_columns(exc: ProgrammingError) -> bool:
        """True only for an undefined-column error naming an optional plugin column.

        The name is retained for the Phase-283 tests that pin it, but the
        predicate now covers every optional ``plugins`` column a worker may read
        before the backend has applied its migration: the two
        ``worker_loaded_*`` snapshot columns (283) and the four ``database_*``
        contract/diagnostic columns (287). A worker starting against a pre-287
        schema must degrade to the projected SELECT rather than abort startup
        (WR-04 precedent).

        Any other ``ProgrammingError`` (a dropped/renamed table, a genuinely
        broken schema unrelated to a pending migration) must not be silently
        swallowed by the compatibility fallback (WR-04).
        """
        message = str(exc.orig) if exc.orig is not None else str(exc)
        return any(
            column in message
            for column in (
                "worker_loaded_at",
                "worker_loaded_version",
                "database_migrations_dir",
                "database_version_table",
                "database_error",
                "database_error_revision",
            )
        )

    async def _select_full_row(self, db: AsyncSession, name: str) -> Plugin | None:
        result = await db.execute(select(Plugin).where(Plugin.name == name))
        return result.scalar_one_or_none()

    async def _select_projected_row(self, db: AsyncSession, name: str) -> Plugin | None:
        result = await db.execute(
            select(Plugin)
            .options(
                defer(Plugin.worker_loaded_at),
                defer(Plugin.worker_loaded_version),
                defer(Plugin.database_migrations_dir),
                defer(Plugin.database_version_table),
                defer(Plugin.database_error),
                defer(Plugin.database_error_revision),
            )
            .where(Plugin.name == name)
        )
        return result.scalar_one_or_none()

    # --- Phase 287: database contract + head-currency gate (PLUGDB-03) --------

    @staticmethod
    def _recorded_database_contract(
        row: Plugin | None,
    ) -> tuple[str | None, str | None]:
        """Return ``(migrations_dir, version_table)`` recorded on ``row``.

        Reads the row's ``__dict__`` directly rather than via attribute access:
        for a worker on a pre-287 schema the ``database_*`` attributes are
        ``defer()``-projected and absent from the dict, and a normal attribute
        access would trigger a lazy load that raises ``MissingGreenlet`` under
        the async engine. An absent attribute therefore means "no recorded
        contract" (WR-04 shape).
        """
        if row is None:
            return (None, None)
        values = row.__dict__
        return (
            values.get("database_migrations_dir"),
            values.get("database_version_table"),
        )

    @staticmethod
    def _is_missing_version_table(exc: ProgrammingError, version_table: str) -> bool:
        """True only for the undefined-table error naming exactly ``version_table``.

        Any other ``ProgrammingError`` (a genuinely broken schema) must re-raise
        — a missing version table means "not migrated", never "broken schema"
        (WR-04). The identifier is safe to interpolate because the manifest
        grammar already validated it (T-287-08).
        """
        orig = exc.orig
        if orig is not None and type(orig).__name__ == "UndefinedTableError":
            return version_table in str(orig)
        message = str(orig) if orig is not None else str(exc)
        return f'relation "{version_table}" does not exist' in message

    async def _database_gate(
        self,
        db: AsyncSession,
        name: str,
        plugin: IndraMeshPlugin,
        base_dir: str | None,
    ) -> _DatabaseRefusal | None:
        """Head-currency + immutable-contract gate for a ``[database]`` plugin.

        Resolves the plugin's migrations directory through the shared loader
        resolver, then delegates to :meth:`_database_gate_with_dir`. Returns
        ``None`` when the plugin may activate, else a :class:`_DatabaseRefusal`
        whose ``message`` is the operator/log string and whose ``reason``/
        ``revision`` feed the persisted diagnostic. The caller owns the ERROR
        transition, the diagnostic write and the ownership rollback.
        """
        import importlib.resources

        from app.plugin_system.loader import resolve_migrations_dir

        discovery = DiscoveryResult(
            name=name,
            manifest=plugin.manifest,
            plugin_cls=type(plugin),
            base_dir=base_dir,
        )
        try:
            resolved = resolve_migrations_dir(discovery)
        except ValueError:
            logger.exception(
                "Plugin %r database migrations directory could not be resolved", name
            )
            return _gate_refusal(REASON_MIGRATIONS_DIR_MISSING, name)

        if resolved is None:
            return _gate_refusal(REASON_MIGRATIONS_DIR_MISSING, name)

        if isinstance(resolved, Path):
            return await self._database_gate_with_dir(db, name, plugin, resolved)

        try:
            with importlib.resources.as_file(resolved) as materialized:
                return await self._database_gate_with_dir(
                    db, name, plugin, Path(materialized)
                )
        except (FileNotFoundError, ModuleNotFoundError, OSError):
            logger.exception(
                "Plugin %r packaged migrations directory could not be materialised",
                name,
            )
            return _gate_refusal(REASON_MIGRATIONS_DIR_MISSING, name)

    async def _database_gate_with_dir(
        self,
        db: AsyncSession,
        name: str,
        plugin: IndraMeshPlugin,
        migrations_dir: Path,
    ) -> _DatabaseRefusal | None:
        from alembic.script import ScriptDirectory

        from app.plugin_system.migrations import (
            PLUGIN_ALEMBIC_DIR,
            REASON_MIGRATION_FAILED,
            resolve_revisions_dir,
        )

        spec = plugin.manifest.database
        if not migrations_dir.is_dir():
            return _gate_refusal(REASON_MIGRATIONS_DIR_MISSING, name)
        revisions_dir = resolve_revisions_dir(migrations_dir)
        try:
            script = ScriptDirectory(
                dir=str(PLUGIN_ALEMBIC_DIR), version_locations=[str(revisions_dir)]
            )
            script_heads = set(script.get_heads())
        except Exception:
            logger.exception("Plugin %r migration history could not be read", name)
            return _gate_refusal(REASON_MIGRATION_FAILED, name)

        # (a) exactly one head (D-06). An existing but empty revisions directory
        # (zero heads) is a distinct fault from a branched history; reporting it
        # as "more than one head" would point the operator at a merge revision
        # that cannot exist (WR-01). Multiple heads are invalid until the plugin
        # author supplies a merge revision.
        if not script_heads:
            return _gate_refusal(REASON_NO_HEAD, name)
        if len(script_heads) > 1:
            return _gate_refusal(REASON_MULTIPLE_HEADS, name)

        # (b) read the plugin's OWN version table rows. A missing table means
        # "not migrated" (empty head set); any other ProgrammingError re-raises.
        version_table = spec.version_table
        try:
            async with db.begin_nested():
                db_heads = set(
                    (
                        await db.execute(
                            text(f'SELECT version_num FROM "{version_table}"')
                        )
                    ).scalars().all()
                )
        except ProgrammingError as exc:
            if not self._is_missing_version_table(exc, version_table):
                raise
            db_heads = set()

        # (c) currency: the script head set must equal the database head set.
        # Never downgrade — a recorded revision this build does not know is a
        # refusal, not a rollback (D-04).
        pending = script_heads - db_heads
        if pending:
            return _gate_refusal(REASON_MIGRATIONS_PENDING, name, *pending)
        unknown = db_heads - script_heads
        if unknown:
            return _gate_refusal(REASON_UNKNOWN_REVISION, name, *unknown)

        # (d) the persisted contract is immutable for the installed identity
        # (D-11/D-12).
        existing = await self._get_row(db, name)
        recorded_dir, recorded_version_table = self._recorded_database_contract(existing)
        if recorded_version_table is not None or recorded_dir is not None:
            if (
                recorded_version_table != version_table
                or recorded_dir != spec.migrations_dir
            ):
                return _gate_refusal(REASON_CONTRACT_CHANGED, name)

        # At head: record (idempotently refresh) the contract and clear any stale
        # diagnostic the gate can re-derive (D-15); a runner execution failure
        # is preserved (GAP-2). Tolerates a pre-287 schema (worker).
        await self._persist_database_contract(
            db, name, plugin.manifest.version, spec.migrations_dir, version_table
        )
        return None

    async def _persist_database_contract(
        self,
        db: AsyncSession,
        name: str,
        version: str,
        migrations_dir: str,
        version_table: str,
    ) -> None:
        """Write the at-head contract, clearing only gate-clearable diagnostics.

        On the first activation the ``plugins`` row does not exist yet (it is
        created by the later ``_upsert(ACTIVE)``), so a minimal LOADED row is
        created first — the recorded contract then always has a row to live on.
        The contract UPDATE is confined to a SAVEPOINT (WR-01/WR-04 shape): a
        pre-287 schema rejects it with ``ProgrammingError``; that is logged and
        tolerated so the worker degrades to the projected SELECT and continues
        activation rather than poisoning the caller's session.

        At-head clearing (D-15) applies only to diagnostics the gate itself can
        re-derive. A runner execution failure (``database_error`` in
        ``RUNNER_EXECUTION_FAILURE_REASONS``) is runner-authoritative: the gate
        sees only that the head sets match, not whether the revision child
        actually ran — clearing it here masked a real role-child failure on
        every boot (287/287.1 integration gap GAP-2). Only the runner's own next
        successful run clears those.
        """
        from app.plugin_system.migrations import is_runner_execution_failure

        try:
            if await self._get_row(db, name) is None:
                await self._upsert(db, name, version, PluginState.LOADED)
            async with db.begin_nested():
                row = await self._get_row(db, name)
                if row is None:
                    return
                row.database_migrations_dir = migrations_dir
                row.database_version_table = version_table
                # Never trigger a lazy load of a deferred column (the pre-287
                # worker projection defers database_error* — a plain read would
                # raise MissingGreenlet). NO_VALUE means "column absent": there
                # is nothing to clear, so leave the attribute untouched.
                loaded_error = inspect(row).attrs.database_error.loaded_value
                if loaded_error is not NO_VALUE and not is_runner_execution_failure(
                    loaded_error
                ):
                    row.database_error = None
                    row.database_error_revision = None
                await db.flush()
        except ProgrammingError:
            logger.warning(
                "Could not record the database contract for %r — the plugins "
                "database_* columns are not yet migrated (migration "
                "287_add_plugin_database_columns pending)",
                name,
            )
        except DataError:
            logger.warning("Could not record the database contract for %r", name)

    async def _record_database_diagnostic(
        self,
        db: AsyncSession,
        name: str,
        reason: str,
        revision: str | None = None,
    ) -> None:
        """Persist a bounded gate refusal on ``name``'s row (D-13/D-14).

        Precedence rule (review 2026-09-22): the deploy runner is the
        authoritative first writer for a migration failure and always clears on
        success, and in Compose it runs BEFORE the backend registry. So this
        writes ONLY when ``database_error`` is currently NULL — otherwise the
        registry's generic "migrations pending" refusal (merely the *consequence*
        of the failed revision) would overwrite the runner's precise "migration
        failed at revision X" on every startup and hide the actual cause. The
        conditional UPDATE is atomic, so a row is never resurrected or fabricated
        and a zero-row result is a normal no-op.

        A ``ProgrammingError`` (a pre-287 schema) downgrades to a warning, exactly
        like the contract write above — never aborting activation.
        """
        try:
            async with db.begin_nested():
                await db.execute(
                    text(
                        "UPDATE plugins "
                        "SET database_error = :error, "
                        "database_error_revision = :revision "
                        "WHERE name = :name AND database_error IS NULL"
                    ),
                    {
                        "error": safe_diagnostic(name, reason, revision),
                        "revision": validated_revision(revision),
                        "name": name,
                    },
                )
        except ProgrammingError:
            logger.warning(DIAGNOSTIC_COLUMNS_PENDING_WARNING + " (plugin %r)", name)
        except DataError:
            logger.warning(
                "Could not persist bounded plugin database diagnostic for %r", name
            )

    async def load_directory(self, db: AsyncSession, plugins_dir: str) -> list[DiscoveryResult]:
        """Discover plugins in ``plugins_dir`` and load them."""
        results = discover_directory_plugins(plugins_dir)
        await self.load_results(db, results)
        return results

    def get_plugin(self, name: str) -> IndraMeshPlugin | None:
        return self._plugins.get(name)

    def get_state(self, name: str) -> PluginState | None:
        return self._states.get(name)

    def get_task_type(self, literal: str):
        """Resolve an exact qualified Task-type literal in this snapshot (D-03).

        Read-only: the admission gate and the worker wrapper both look up the
        exact persisted name — never a derived, prefixed or aliased variant.
        Returns a ``TaskTypeEntry`` or ``None``.
        """
        return self._task_types.resolve(literal)

    def get_connector_type(self, identifier: str):
        """Resolve an exact connector identity in this snapshot (CORESEAM-01).

        Read-only: write validation remains
        ``connector_registry.resolve_connector_type()``. This exposes the
        lifecycle-published snapshot — the entry (declaration + owner) exists
        exactly while its owner is ACTIVE. Returns a ``ConnectorTypeEntry`` or
        ``None``; identifiers are never aliased or derived.
        """
        return self._connectors.resolve(identifier)

    async def build_context(
        self,
        name: str,
        *,
        active_session: AsyncSession | None = None,
        task_id: uuid.UUID | None = None,
        step_sequence: tuple[str, ...] | None = None,
        task: Task | None = None,
    ) -> PluginContext:
        """Build the ``PluginContext`` for plugin ``name``.

        Reads the plugin's current ``Plugin.config`` row value through
        ``active_session`` when given, else through a fresh
        ``async_session_maker()`` session — one indexed SELECT by unique
        ``name``. Always DB truth: a config written via the other process's PUT
        endpoint is visible without any cache/propagation machinery.

        This is the ONLY place a ``PluginContext`` is constructed for a real
        plugin — isolation by construction (PLUGCORE-06) follows from it only
        ever reading the one row named by ``name``.

        ``task`` is the caller's already-loaded Task projection (CORESEAM-04):
        the worker passes the very row it marked RUNNING, so the context's
        task-bound members never look a task up themselves. A route/hook call
        passes neither, and its context refuses those members (review 1).
        """
        if active_session is not None:
            row = await self._get_row(active_session, name)
        else:
            async with async_session_maker() as session:
                row = await self._get_row(session, name)

        raw_config = dict(row.config) if row is not None and row.config else {}
        plugin = self._plugins.get(name)
        config_schema = (
            plugin.manifest.config_schema
            if plugin is not None and plugin.manifest is not None
            else {}
        )
        return PluginContext(
            plugin_name=name,
            raw_config=raw_config,
            active_session=active_session,
            task_id=task_id,
            config_schema=config_schema,
            step_sequence=step_sequence,
            task=task,
        )

    def get_plugins_for_capability(
        self, capability_cls: type, *, include_disabled: bool = False
    ) -> list[IndraMeshPlugin]:
        """Return active plugins implementing ``capability_cls`` (PLUGCORE-02).

        ``include_disabled`` also returns constructed DISABLED plugins, for
        callers whose own runtime gate re-checks the state (route mounting).

        The legal capability types are whatever ``__init_subclass__`` registered
        in ``IndraMeshPlugin._capability_interfaces`` — this method never
        hardcodes an interface name (no ``if isinstance(plugin,
        SearchProviderPlugin): elif ...`` chain), so a capability class defined
        outside this package dispatches with zero registry/loader change.
        """
        registered = tuple(IndraMeshPlugin._capability_interfaces.values())
        if not any(cls is capability_cls for cls in registered):
            raise ValueError(
                f"{capability_cls!r} is not a registered capability interface "
                f"(registered: {sorted(c.__name__ for c in registered)})"
            )

        states = {PluginState.ACTIVE}
        if include_disabled:
            states.add(PluginState.DISABLED)
        return [
            plugin
            for name, plugin in self._plugins.items()
            if self._states.get(name) in states
            and isinstance(plugin, capability_cls)
        ]

    async def has_active_plugin_declaring(self, flag: str) -> bool:
        """True iff an ACTIVE plugin's manifest declares the named boolean flag.

        The HTTP GraphQL production gate (Phase 304, GQL-06/D-16) uses this to
        decide whether ``/api/v1/graphql`` is served, reading only a
        manifest-declared boolean — Core never names a plugin (D-18). The
        per-candidate ``is_active`` call keeps the decision cross-process (D-13):
        a plugin activated or deactivated in another process is honoured without
        a restart. Fails closed (``False``) when the in-process snapshot is
        empty, so a not-yet-loaded process hides the endpoint in production.
        """
        candidates = [
            name
            for name, plugin in self._plugins.items()
            if plugin.manifest is not None
            and getattr(plugin.manifest, flag, False) is True
        ]
        for name in candidates:
            if await self.is_active(name):
                return True
        return False

    async def call_capability(
        self,
        plugin: IndraMeshPlugin,
        method_name: str,
        ctx: PluginContext,
        *args: Any,
        timeout: float = DEFAULT_CAPABILITY_TIMEOUT,
        **kwargs: Any,
    ) -> Any:
        """Invoke one capability/hook method under timeout + exception isolation.

        A timeout raises ``PluginTimeoutError``; any other exception raises
        ``PluginCallError`` wrapping the original. ``asyncio.CancelledError`` is
        re-raised — it is a ``BaseException`` since Python 3.8, and catching it
        would break the caller's own cancellation.
        """
        method = getattr(plugin, method_name)
        plugin_name = (
            plugin.manifest.name
            if plugin.manifest is not None
            else type(plugin).__name__
        )

        # D-13: the cross-process state check runs BEFORE dispatch, always. A
        # short-circuit on in-memory DISABLED would hide a reactivation issued
        # by the other process; the Redis hit path is one sub-millisecond GET.
        if not await self.is_active(plugin_name):
            raise PluginNotActiveError(plugin_name, method_name)

        try:
            return await asyncio.wait_for(
                method(ctx, *args, **kwargs), timeout=timeout
            )
        except asyncio.TimeoutError as exc:
            raise PluginTimeoutError(plugin_name, method_name, timeout) from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise PluginCallError(plugin_name, method_name, exc) from exc

    async def is_active(self, name: str) -> bool:
        """Cross-process state check (D-13) — Redis TTL cache, DB fallback.

        The Redis key wins while it lives (``STATE_CACHE_TTL`` seconds); on a
        miss — or when Redis is briefly unavailable — the ``plugins`` table's
        ``state`` column is the truth, and is written back into the cache for
        the next call. The in-memory state is then reconciled in BOTH
        directions, so a kill AND a reactivation issued by the other OS process
        both become effective here within the TTL window.

        A name with no ``plugins`` row at all (directly-registered test plugins
        never touch ``load_results``) has no DB truth to defer to: this
        process's own in-memory state is returned unchanged rather than failing
        closed.
        """
        state_value: str | None = None
        try:
            redis = await get_redis()
            cached = await redis.get(f"plugin_state:{name}")
        except Exception:
            logger.exception(
                "plugin_state cache read failed for %r — using DB truth", name
            )
            cached = None

        if cached is not None:
            state_value = cached
        else:
            async with async_session_maker() as session:
                row = await self._get_row(session, name)

            if row is None:
                return self._states.get(name) == PluginState.ACTIVE

            state_value = (
                row.state.value if isinstance(row.state, PluginState) else row.state
            )
            try:
                redis = await get_redis()
                await redis.setex(
                    f"plugin_state:{name}", STATE_CACHE_TTL, state_value
                )
            except Exception:
                logger.exception(
                    "plugin_state cache write failed for %r — DB truth still applies",
                    name,
                )

        await self._reconcile_state(name, state_value)
        return self._states.get(name) == PluginState.ACTIVE

    async def reconcile_all(self) -> None:
        """Run the D-13 cross-process check for every plugin this process holds.

        ``is_active()`` otherwise only runs on dispatch, so a plugin DISABLED
        when the worker started never reaches ``_reconcile_state()``: its task
        types stay unregistered, nothing is dispatched to it and the worker
        snapshot is never written. The worker calls this periodically so a
        state change issued by the backend lands here without a restart.
        """
        for name in list(self._plugins):
            try:
                await self.is_active(name)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Plugin %r state reconciliation failed", name)

    async def _reconcile_state(self, name: str, state_value: str) -> None:
        """Make this process's in-memory state match ``state_value`` (D-13).

        Both directions reconcile: a kill issued elsewhere lands DISABLED here,
        and a reactivation issued elsewhere lands ACTIVE here. An in-memory
        ERROR state is never revived by reconciliation — only a restart reloads
        the plugin.
        """
        current = self._states.get(name)
        if current is PluginState.ERROR:
            return

        plugin = self._plugins.get(name)
        if plugin is None:
            return

        if state_value == "active" and current == PluginState.DISABLED:
            if not await self._register_permissions_for_reactivation(name, plugin):
                return
            if not await self._register_task_types_for_reactivation(name, plugin):
                return
            if not await self._register_database_for_reactivation(name, plugin):
                return
            # Phase 289 (CORESEAM-01): restore this owner's connector identities
            # for the DISABLED -> ACTIVE transition (the plugin returned from
            # _activate() before its connector block while it was disabled).
            if not await self._register_connectors_for_reactivation(name, plugin):
                return
            try:
                ctx = await self.build_context(name)
                await asyncio.wait_for(
                    plugin.on_load(ctx), timeout=LIFECYCLE_HOOK_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Plugin %r on_load failed during state reconciliation: %s",
                    name,
                    exc,
                )
                self._task_types.remove_owner(name)
                self._connectors.remove_owner(name)
                self._database.remove_owner(name)
                self._states[name] = PluginState.ERROR
                # WR-03: without this, only the in-memory state moves to
                # ERROR — the `plugins` row stays ACTIVE (the value the
                # *other* process wrote that triggered this reconciliation).
                # An in-memory ERROR is never revived by reconciliation
                # (see the guard at the top of this method), so this process
                # would otherwise permanently refuse to dispatch the plugin
                # while every other process and the API/UI report ACTIVE,
                # with no audit row recording the divergence.
                await self._persist_state(name, PluginState.ERROR)
                return
            self._states[name] = PluginState.ACTIVE
            # PLUGWORKER-02/D-01: the worker's REAL reactivation path — refresh
            # the worker snapshot here (no-op for a backend registry). No `db`
            # argument: this method has no session, so the helper opens its own,
            # exactly like _persist_state(). The ACTIVE -> disabled branch below
            # must never write these columns (D-02).
            if plugin.manifest is not None:
                await self._record_worker_load(name, plugin.manifest.version)
        elif state_value != "active" and current == PluginState.ACTIVE:
            try:
                ctx = await self.build_context(name)
                await asyncio.wait_for(
                    plugin.on_unload(ctx), timeout=LIFECYCLE_HOOK_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Plugin %r on_unload failed during state reconciliation: %s",
                    name,
                    exc,
                )
            self._states[name] = (
                PluginState.ERROR if state_value == "error" else PluginState.DISABLED
            )
            # Phase 289 (CORESEAM-01): a kill/error issued by the OTHER process
            # lands here — removing the owner's connector identities keeps "an
            # inactive owner can never accept a new write" true across processes
            # (the D-13 reconciliation contract), mirroring kill_switch().
            self._connectors.remove_owner(name)

    async def kill_switch(
        self, name: str, *, actor_id: uuid.UUID | None = None
    ) -> None:
        """Kill-switch one plugin (D-15/PLUGCORE-03).

        ``on_unload()`` runs best-effort under a short ``wait_for`` and is fully
        exception-isolated (a hanging or throwing ``on_unload`` must never block
        the kill). Afterwards the in-memory state AND the persisted row are
        ALWAYS set to DISABLED, regardless of that call's outcome. The row is
        upserted by name so this also works for a plugin registered directly
        without going through ``load_results()``'s row-creation path.

        WR-02: ``on_unload()`` fires only on a REAL ``ACTIVE -> DISABLED``
        transition. A repeated same-state kill still re-persists DISABLED (so a
        stale cross-process row converges, and ``_upsert()``'s same-state guard
        writes no audit row) but never re-runs the hook.
        """
        plugin = self._plugins.get(name)
        if plugin is not None and self._states.get(name) == PluginState.ACTIVE:
            try:
                ctx = await self.build_context(name)
                await asyncio.wait_for(
                    plugin.on_unload(ctx), timeout=LIFECYCLE_HOOK_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Plugin %r on_unload failed during kill_switch (ignored): %s",
                    name,
                    exc,
                )

        self._states[name] = PluginState.DISABLED
        # Phase 289 (CORESEAM-01): a deactivated owner may never accept a NEW
        # write — drop its connector identities from this snapshot. Already
        # persisted rows stay readable (the read paths never resolve the
        # registry) and reactivate() restores the declarations.
        self._connectors.remove_owner(name)
        await self._persist_state(name, PluginState.DISABLED, actor_id=actor_id)

    async def reactivate(
        self, name: str, *, actor_id: uuid.UUID | None = None
    ) -> None:
        """Re-activate a disabled plugin by calling ``on_load()`` again (D-15).

        Same ``build_context``/``wait_for`` treatment as ``kill_switch``; a
        raising ``on_load`` lands the plugin in ERROR, not ACTIVE.

        WR-02: ``on_load()`` fires only when the plugin is NOT already ACTIVE.
        An already-ACTIVE plugin still runs the success tail (state set ACTIVE
        and persisted + worker snapshot refreshed) so a stale persisted row
        converges, but the hook is not re-invoked.
        """
        plugin = self._plugins.get(name)
        if plugin is None:
            raise ValueError(f"Plugin {name!r} is not loaded — cannot reactivate")

        if self._states.get(name) != PluginState.ACTIVE:
            if not await self._register_permissions_for_reactivation(
                name, plugin, actor_id=actor_id
            ):
                return
            if not await self._register_task_types_for_reactivation(
                name, plugin, actor_id=actor_id
            ):
                return
            if not await self._register_database_for_reactivation(
                name, plugin, actor_id=actor_id
            ):
                return
            # Phase 289 (CORESEAM-01): restore this owner's connector identities
            # (parity with the task-type/database re-registration above).
            if not await self._register_connectors_for_reactivation(
                name, plugin, actor_id=actor_id
            ):
                return
            try:
                ctx = await self.build_context(name)
                await asyncio.wait_for(
                    plugin.on_load(ctx), timeout=LIFECYCLE_HOOK_TIMEOUT
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Plugin %r on_load failed during reactivate: %s", name, exc
                )
                self._task_types.remove_owner(name)
                self._connectors.remove_owner(name)
                self._database.remove_owner(name)
                self._states[name] = PluginState.ERROR
                await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
                return

        self._states[name] = PluginState.ACTIVE
        await self._persist_state(name, PluginState.ACTIVE, actor_id=actor_id)
        # PLUGWORKER-02/D-01: keep the snapshot correct on any genuine ACTIVE
        # transition — symmetric with _reconcile_state()'s DISABLED -> ACTIVE
        # branch. No-op for a backend registry (is_worker=False).
        if plugin.manifest is not None:
            await self._record_worker_load(name, plugin.manifest.version)

    async def _register_task_types_for_reactivation(
        self, name: str, plugin: IndraMeshPlugin, *, actor_id: uuid.UUID | None = None
    ) -> bool:
        """(Re)register ``plugin``'s Task-type declarations before its ``on_load``.

        Phase 286 (MJ-01): a plugin that was DISABLED when this process started
        returned from ``_activate()`` before its declaration block, so the
        process-local ``_task_types`` snapshot holds nothing for it. Every
        transition into ACTIVE therefore registers here; ``add()`` is idempotent
        for the same owner, so the in-process kill -> reactivate path is a no-op
        replace. A failing registration lands the plugin in ERROR (persisted),
        mirroring ``_activate()``'s isolation, and returns ``False``.
        """
        if not isinstance(plugin, TaskTypePlugin):
            return True
        try:
            self._task_types.add(plugin)
        except ValueError as exc:
            logger.warning(
                "Plugin %r task type registration failed during reactivation: %s",
                name,
                exc,
            )
            self._states[name] = PluginState.ERROR
            await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
            return False
        return True

    async def _register_connectors_for_reactivation(
        self, name: str, plugin: IndraMeshPlugin, *, actor_id: uuid.UUID | None = None
    ) -> bool:
        """(Re)register ``plugin``'s connector declarations before its ``on_load``.

        Phase 289 (CORESEAM-01, parity with Phase 286's MJ-01): a plugin that
        was DISABLED when this process started returned from ``_activate()``
        before its connector block, so the process-local connector snapshot
        holds nothing for it. Every transition into ACTIVE therefore registers
        here; ``add()`` is idempotent for the same owner, so an in-process
        kill -> reactivate cycle is a replace, never a collision against
        itself. A failing registration removes any partial entries, lands the
        plugin in ERROR (persisted) and returns ``False``.
        """
        if not isinstance(plugin, ConnectorPlugin):
            return True
        declarations = list(plugin.connector_types())
        try:
            self._connectors.add(name, declarations)
        except ValueError as exc:
            self._connectors.remove_owner(name)
            logger.warning(
                "Plugin %r connector registration failed during reactivation: %s",
                name,
                exc,
            )
            self._states[name] = PluginState.ERROR
            await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
            return False
        return True

    async def _register_database_for_reactivation(
        self, name: str, plugin: IndraMeshPlugin, *, actor_id: uuid.UUID | None = None
    ) -> bool:
        """(Re)register + gate ``plugin``'s database contract before its ``on_load``.

        Phase 287 (parity with Phase 286's MJ-01): a plugin DISABLED when this
        process started returned from ``_activate()`` before the database block,
        so its tables were never claimed and its head was never checked. Every
        transition into ACTIVE therefore runs the same ownership + head-currency
        + contract gate here — otherwise an operator ``reactivate`` (or the other
        process's reconciliation) could activate plugin code against a schema
        that is not at head. A refusal removes the owner, lands ERROR (persisted)
        and returns ``False``; the at-head success path records the contract
        exactly like ``_activate()``.
        """
        if plugin.manifest is None:
            return True

        async with async_session_maker() as session:
            if plugin.manifest.database is None:
                # A plugin that no longer declares [database] but records a
                # contract cannot be reactivated past it (D-11).
                existing = await self._get_row(session, name)
                _, recorded_version_table = self._recorded_database_contract(existing)
                if recorded_version_table is not None:
                    logger.warning(
                        "Plugin %r records a database contract but no longer "
                        "declares [database] — refusing reactivation (D-11)",
                        name,
                    )
                    self._states[name] = PluginState.ERROR
                    await self._persist_state(
                        name, PluginState.ERROR, actor_id=actor_id
                    )
                    await self._record_database_diagnostic(
                        session, name, REASON_CONTRACT_SECTION_REMOVED
                    )
                    await session.commit()
                    return False
                return True

            try:
                self._database.add(plugin)
            except (TableOwnershipError, ValueError) as exc:
                self._database.remove_owner(name)
                logger.warning(
                    "Plugin %r database registration failed during reactivation: %s",
                    name,
                    exc,
                )
                self._states[name] = PluginState.ERROR
                await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
                await self._record_database_diagnostic(
                    session, name, _ownership_refusal_reason(plugin, exc)
                )
                await session.commit()
                return False

            refusal = await self._database_gate(
                session, name, plugin, self._plugin_base_dirs.get(name)
            )
            if refusal is not None:
                self._database.remove_owner(name)
                logger.warning(
                    "Plugin %r database gate refused reactivation: %s",
                    name,
                    refusal.message,
                )
                self._states[name] = PluginState.ERROR
                await self._persist_state(name, PluginState.ERROR, actor_id=actor_id)
                await self._record_database_diagnostic(
                    session, name, refusal.reason, refusal.revision
                )
                await session.commit()
                return False
            await session.commit()
        return True

    async def _persist_state(
        self, name: str, state: PluginState, *, actor_id: uuid.UUID | None = None
    ) -> None:
        """UPSERT ``name``'s state into the ``plugins`` table via a fresh session."""
        plugin = self._plugins.get(name)
        version = (
            plugin.manifest.version
            if plugin is not None and plugin.manifest is not None
            else "unknown"
        )
        async with async_session_maker() as session:
            await self._upsert(session, name, version, state, actor_id=actor_id)
            await session.commit()
        await invalidate_entity_cache(SCHEDULE_CACHE_ENTITY)
