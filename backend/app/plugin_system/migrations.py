"""Phase 287: the programmatic plugin migration runner (PLUGDB-03/PLUGDB-04).

One run per deploy: discover every database-owning plugin, then for each of them
run ``alembic upgrade head`` through ONE core-owned shared env
(``backend/plugin_alembic/``) against that plugin's OWN version table. The core
``alembic_version`` chain is never touched — the runner only ever writes the
plugin's declared ``version_table``.

Failure semantics (D-16): a plugin-attributable failure is isolated into that
plugin's :class:`PluginMigrationOutcome` and never propagates — a sibling plugin
still migrates and ``python -m app.plugin_system.migrate`` still exits 0.

Concurrency (D-09): the whole plugin loop runs under one bounded, session-level
advisory lock held on a DEDICATED connection, and each plugin is bounded by its
own timeout on its own fresh connection. Only cooperative DB waits are
cancellable — a revision blocking in sync Python (``time.sleep``, a CPU loop) is
not interrupted until it returns, the same limitation the plugin registry
documents for its own ``wait_for``.
"""

import ast
import asyncio
import importlib.resources
import logging
import re
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DataError, ProgrammingError
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db import async_session_maker, get_database_url, get_maintenance_database_url
from app.logging import resolve_log_format
from app.plugin_system.bootstrap_role import (
    PLUGIN_DB_PASSWORD_ENV,
    PLUGIN_DB_USER_ENV,
    PluginRoleConfigError,
    _quote_identifier,
    validate_plugin_role_identifier,
    verify_plugin_role_boundary,
)
from app.plugin_system.database_registry import (
    DatabaseTableRegistry,
    TableOwnershipError,
    core_table_names,
)
from app.plugin_system.loader import (
    DiscoveryResult,
    discover_all,
    resolve_migrations_dir,
)
from app.plugin_system.manifest import plugin_table_prefix

logger = logging.getLogger(__name__)

# The ONE core-owned shared Alembic env directory (RESEARCH Pitfall 2). Every
# plugin's ``script_location`` points here; only ``version_locations`` names the
# plugin directory.
PLUGIN_ALEMBIC_DIR = Path(__file__).resolve().parents[2] / "plugin_alembic"

# D-09: one fixed global key ("INDRAPLG") for the whole multi-plugin run.
PLUGIN_MIGRATION_LOCK_KEY = 0x494E445241504C47

# Bounded lock acquisition (D-09) and bounded per-plugin work (RESEARCH Pitfall
# 6, A-287-03). Code constants, deliberately not env vars — no new secret/config
# surface.
DEFAULT_LOCK_TIMEOUT_SECONDS = 30.0
DEFAULT_PER_PLUGIN_TIMEOUT_SECONDS = 300.0

# Outcome statuses. "migrated" means at least one revision was applied this run;
# "up_to_date" means the plugin's version table already equalled its script head.
STATUS_MIGRATED = "migrated"
STATUS_UP_TO_DATE = "up_to_date"
STATUS_FAILED = "failed"

# D-13/D-14: a CLOSED core vocabulary of persisted/visible reason strings. The
# runner never persists or returns ``str(exc)`` — full technical detail goes to
# ``logger.exception`` only. BOTH the deploy runner and the activation gate
# (``registry.py``) import these constants and :func:`safe_diagnostic`, so a
# diagnostic reads identically whether it came from the deploy run or from an
# activation refusal. Covers every outcome either side can produce.
REASON_MIGRATION_FAILED = "Plugin migration failed."
REASON_TIMEOUT = "Plugin migration exceeded the time limit."
REASON_MIGRATIONS_PENDING = "Plugin database migrations are not applied."
REASON_MULTIPLE_HEADS = "Plugin migration history has more than one head."
REASON_NO_HEAD = "Plugin migration history has no head."
REASON_UNKNOWN_REVISION = (
    "Plugin records a database revision this build does not know."
)
REASON_LOCK_TIMEOUT = "Plugin migration runner could not acquire the migration lock."
REASON_MIGRATIONS_DIR_MISSING = "Plugin migrations directory could not be resolved."
REASON_METADATA_MISSING = "Plugin declares database migrations but exposes no metadata."
REASON_METADATA_WITHOUT_CONTRACT = (
    "Plugin exposes database metadata but declares no database contract."
)
REASON_NO_TABLES = "Plugin declares a database contract but no tables."
REASON_TABLE_OUTSIDE_NAMESPACE = (
    "Plugin declares a table outside its own plugin namespace."
)
REASON_TABLE_CORE_COLLISION = (
    "Plugin declares a table that already exists in the core schema."
)
REASON_TABLE_OWNED_BY_ANOTHER = (
    "Plugin declares a table that is already owned by another plugin."
)
REASON_CONTRACT_CHANGED = (
    "Plugin database contract has changed since it was installed."
)
REASON_CONTRACT_SECTION_REMOVED = (
    "Plugin database contract was removed while its tables remain."
)
# Phase 292 (JDATA-05): a plugin whose manifest sets require_restricted_role may
# only be migrated by a verified restricted login role. Both reasons are
# credential-free and belong to the same closed vocabulary as the rest.
REASON_RESTRICTED_ROLE_REQUIRED = (
    "Plugin requires a restricted migration role that is not configured."
)
REASON_RESTRICTED_ROLE_INVALID = (
    "Plugin migration role is missing or not restricted."
)
# Phase 292 (JDATA-05): the owning child issues the runtime-role DML grants after
# a successful/up-to-date upgrade. A refused GRANT fails that plugin closed; it is
# never retried through the privileged application connection.
REASON_RUNTIME_GRANT_FAILED = (
    "Plugin runtime-role grant could not be applied."
)

# The reasons that mean "the revision child actually tried to execute and did not
# finish". They are runner-authoritative: the runner clears them on its next
# successful run. The activation gate must NEVER clear them (it can only see equal
# head sets, not whether the child ran), otherwise a real execution failure becomes
# invisible (287/287.1 integration gap GAP-1/GAP-2). A runtime-grant failure
# belongs here too: the revision may already have advanced the private head, so the
# activation gate would otherwise clear the only signal that runtime DML is missing.
RUNNER_EXECUTION_FAILURE_REASONS = frozenset(
    {REASON_MIGRATION_FAILED, REASON_TIMEOUT, REASON_RUNTIME_GRANT_FAILED}
)


def is_runner_execution_failure(diagnostic: str | None) -> bool:
    """True when ``diagnostic`` is a composed runner execution-failure string.

    The persisted value is :func:`safe_diagnostic` output ("<reason> (plugin
    '<name>')…"), never the bare reason constant, so detection is prefix-based
    against the closed vocabulary in ``RUNNER_EXECUTION_FAILURE_REASONS``.
    """
    if not diagnostic:
        return False
    return any(
        diagnostic.startswith(reason) for reason in RUNNER_EXECUTION_FAILURE_REASONS
    )

# D-14: the revision id is embedded only when it is a bare identifier. An
# unvalidated value is omitted from the composed string rather than escaped.
_REVISION_RE = re.compile(r"^[A-Za-z0-9_]+$")
REVISION_MAX_LENGTH = 64

# T-287-15: a pre-287 schema (a deploy/worker process that starts before the
# backend has applied the core revision) must degrade this write to a warning,
# exactly like the registry's WR-01/283 tolerance.
DIAGNOSTIC_COLUMNS_PENDING_WARNING = (
    "Could not persist a plugin database diagnostic — the plugins database_* "
    "columns are not yet migrated (migration 287_add_plugin_database_columns "
    "pending)"
)

# One NullPool engine for the whole run, built LAZILY (Pitfall 2, D-15).
# NullPool means a returned connection can never carry a dangling session-level
# advisory lock (RESEARCH Pitfall 4); each plugin gets its own fresh connection
# from it. It is NOT built at import time: ``registry.py`` imports this module in
# both the backend and the worker, and on k3s the worker holds only ``DB_APP_*``
# — a module-level engine would be constructed for an identity that process does
# not have. A process that never reaches a maintenance path never builds one.
_runner_engine = None


def _get_runner_engine():
    """Return the cached maintenance (``DB_USER``) engine, building it on first use.

    The privileged parent writes migration state and adopts plugin objects, which
    needs ownership rights the runtime identity does not have (D-08). Deliberately
    NOT the runtime builder: ``_resolve_runtime_role_name()`` and
    ``_database_locator()`` must keep reading :func:`get_database_url` (Pitfall 1)
    so the owner-issued plugin grant targets the application role and the child
    receives the application connection's locator.
    """
    global _runner_engine
    if _runner_engine is None:
        _runner_engine = create_async_engine(
            get_maintenance_database_url(), poolclass=NullPool, hide_parameters=True
        )
    return _runner_engine

# Phase 287.1 (D-02): the hermetic per-plugin revision child. The parent runs
# `python -m <module>` with a REPLACED environment carrying only the two plugin
# credentials; plugin identity + non-secret locator components travel as argv.
CHILD_MODULE = "app.plugin_system.migration_child"
BACKEND_DIR = Path(__file__).resolve().parents[2]

# Fixed child exit codes (T-287.1-03): the only IPC between parent and child.
CHILD_EXIT_MIGRATED = 0
CHILD_EXIT_UP_TO_DATE = 10
CHILD_EXIT_MIGRATION_FAILED = 20
CHILD_EXIT_CONFIG = 21
CHILD_EXIT_METADATA_MISSING = 22
CHILD_EXIT_NO_HEAD = 23
CHILD_EXIT_MULTIPLE_HEADS = 24
# Phase 292 (JDATA-05): the child reached the head (or was already there) but the
# owner-issued runtime-role GRANT failed. A distinct code so the outcome carries
# the dedicated reason instead of a generic migration failure.
CHILD_EXIT_RUNTIME_GRANT_FAILED = 25

_CHILD_EXIT_REASON = {
    CHILD_EXIT_MIGRATION_FAILED: REASON_MIGRATION_FAILED,
    CHILD_EXIT_CONFIG: REASON_MIGRATION_FAILED,
    CHILD_EXIT_METADATA_MISSING: REASON_METADATA_MISSING,
    CHILD_EXIT_NO_HEAD: REASON_NO_HEAD,
    CHILD_EXIT_MULTIPLE_HEADS: REASON_MULTIPLE_HEADS,
    CHILD_EXIT_RUNTIME_GRANT_FAILED: REASON_RUNTIME_GRANT_FAILED,
}


@dataclass(frozen=True)
class PluginRoleCredentials:
    """The plugin-role identity the deploy entry hands to the runner (D-16).

    ``password`` never reaches a log, a diagnostic or an argv entry — only the
    child process environment.
    """

    role_name: str
    password: str


class PluginMigrationError(RuntimeError):
    """A plugin-attributable migration failure carrying a fixed reason constant.

    ``revision`` optionally names the affected revision (D-14) — the head the
    run was moving to when the upgrade raised, or a specific pending/unknown
    revision. Only ever an identifier already validated against the manifest
    grammar or the revision tree.
    """

    def __init__(
        self, message: str, *, reason: str, revision: str | None = None
    ) -> None:
        super().__init__(message)
        self.reason = reason
        self.revision = revision


@dataclass
class PluginMigrationOutcome:
    """Result of migrating ONE database-owning plugin."""

    plugin_name: str
    status: str
    revision: str | None = None
    reason: str | None = None


@dataclass
class MigrationRunResult:
    """Aggregate result of one plugin migration run (D-16 partial success)."""

    outcomes: list[PluginMigrationOutcome] = field(default_factory=list)
    locked_out: bool = False
    # The database-owning plugin names the run intended to migrate, filled from
    # discovery BEFORE the lock attempt so a locked-out run can persist the lock
    # diagnostic against each of them (plan 287-03).
    discovered_plugin_names: list[str] = field(default_factory=list)
    # PostgreSQL row versions observed before the advisory-lock attempt. A
    # locked-out runner uses these to avoid writing over a concurrent winner's
    # diagnostic update or clear.
    diagnostic_row_xmins: dict[str, str] = field(default_factory=dict)

    def failed_outcomes(self) -> list[PluginMigrationOutcome]:
        return [outcome for outcome in self.outcomes if outcome.status == STATUS_FAILED]


def validated_revision(revision: str | None) -> str | None:
    """Return ``revision`` if it is a bare identifier, else ``None``.

    A revision id is embedded into the persisted/visible diagnostic only when it
    matches ``[A-Za-z0-9_]+`` (D-14); anything else is omitted rather than
    escaped (the closed-vocabulary rule — a scrubber is an endless whack-a-mole).
    """
    if revision is None:
        return None
    return (
        revision
        if len(revision) <= REVISION_MAX_LENGTH and _REVISION_RE.match(revision)
        else None
    )


def safe_diagnostic(
    plugin_name: str, reason: str, revision: str | None = None
) -> str:
    """Compose a bounded, core-authored migration diagnostic (D-13/D-14).

    Built ONLY from a fixed reason constant, the plugin name and an
    identifier-validated revision. No exception text, SQL fragment or secret can
    enter this string by construction; the raw detail goes to
    ``logger.exception`` and nowhere else.
    """
    validated = validated_revision(revision)
    detail = f" (revision {validated})" if validated else ""
    return f"{reason} (plugin {plugin_name!r}){detail}"


def ownership_refusal_reason(owner: str, manifest, metadata, exc: Exception) -> str:
    """Map a table-ownership/validation refusal to a fixed-vocabulary reason.

    Shared by the activation gate (``registry.py``) and the deploy runner's
    pre-flight, so a refusal reads identically whether it was authored at startup
    or before a migration. ``DatabaseTableRegistry.add_declaration()`` raises
    ``TableOwnershipError`` for a cross-plugin collision and a bare ``ValueError``
    for the remaining declaration faults. The exact fault is re-derived from the
    declaration itself (never from exception text), so the persisted diagnostic
    stays bounded and never carries an exception string.
    """
    if isinstance(exc, TableOwnershipError):
        return REASON_TABLE_OWNED_BY_ANOTHER

    declares_database = manifest is not None and manifest.database is not None

    if declares_database and metadata is None:
        return REASON_METADATA_MISSING
    if not declares_database and metadata is not None:
        return REASON_METADATA_WITHOUT_CONTRACT

    declared_tables = list(metadata.tables) if metadata is not None else []
    if not declares_database or not declared_tables:
        return REASON_NO_TABLES

    prefix = plugin_table_prefix(owner)
    core_tables = core_table_names()
    for table_name in declared_tables:
        if not isinstance(table_name, str) or not table_name.startswith(prefix):
            return REASON_TABLE_OUTSIDE_NAMESPACE
        if table_name in core_tables:
            return REASON_TABLE_CORE_COLLISION
    return REASON_TABLE_OWNED_BY_ANOTHER


async def _write_diagnostic_row(
    name: str,
    *,
    error: str | None,
    revision: str | None,
    expected_xmin: str | None = None,
) -> None:
    """Name-keyed UPDATE of the two diagnostic columns only (T-287-14/T-287-16).

    Never creates or deletes a ``plugins`` row: a zero-row result is a normal
    outcome (the backend creates the row after the deploy runner and then
    persists its own diagnostic through the gate). Cleared rows pass ``None`` for
    both values, so clearing touches exactly ``database_error`` and
    ``database_error_revision`` — never the lifecycle state or audit trail.
    """
    where_clause = "WHERE name = :name"
    parameters = {"error": error, "revision": revision, "name": name}
    if expected_xmin is not None:
        where_clause += " AND xmin::text = :expected_xmin"
        parameters["expected_xmin"] = expected_xmin

    async with async_session_maker() as session:
        await session.execute(
            text(
                "UPDATE plugins "
                "SET database_error = :error, "
                "database_error_revision = :revision "
                f"{where_clause}"
            ),
            parameters,
        )
        await session.commit()


async def persist_plugin_diagnostic(
    name: str,
    reason: str,
    revision: str | None = None,
    *,
    expected_xmin: str | None = None,
) -> None:
    """Persist a sanitized diagnostic for ``name`` (D-13/D-14).

    T-287-15: a ``ProgrammingError`` from a schema without the 287 columns
    downgrades to a warning naming ``287_add_plugin_database_columns`` and never
    aborts the run.
    """
    try:
        values = {
            "error": safe_diagnostic(name, reason, revision),
            "revision": validated_revision(revision),
        }
        if expected_xmin is None:
            await _write_diagnostic_row(name, **values)
        else:
            await _write_diagnostic_row(name, **values, expected_xmin=expected_xmin)
    except ProgrammingError:
        logger.warning(DIAGNOSTIC_COLUMNS_PENDING_WARNING + " (plugin %r)", name)
    except DataError:
        logger.warning(
            "Could not persist bounded plugin database diagnostic for %r", name
        )


async def clear_plugin_diagnostic(name: str) -> None:
    """Clear both diagnostic columns for ``name`` — a later success (D-15)."""
    try:
        await _write_diagnostic_row(name, error=None, revision=None)
    except ProgrammingError:
        logger.warning(DIAGNOSTIC_COLUMNS_PENDING_WARNING + " (plugin %r)", name)
    except DataError:
        logger.warning("Could not clear plugin database diagnostic for %r", name)


async def _persist_run_diagnostics(result: MigrationRunResult) -> None:
    """Persist/clear every plugin diagnostic after a run (D-13/D-15, D-09).

    A failed outcome writes its fixed reason; every successful or already-at-head
    outcome clears any stale diagnostic (D-15). On a ``locked_out`` run — which
    has an empty ``outcomes`` list — the lock reason is written against every
    database-owning plugin the run discovered, so D-09's lock diagnostic travels
    the same persisted-then-served path as every other reason.
    """
    if result.locked_out:
        for name in result.discovered_plugin_names:
            expected_xmin = result.diagnostic_row_xmins.get(name)
            if expected_xmin is not None:
                await persist_plugin_diagnostic(
                    name, REASON_LOCK_TIMEOUT, expected_xmin=expected_xmin
                )
        return
    for outcome in result.outcomes:
        if outcome.status == STATUS_FAILED:
            await persist_plugin_diagnostic(
                outcome.plugin_name,
                outcome.reason or REASON_MIGRATION_FAILED,
                outcome.revision,
            )
        else:
            await clear_plugin_diagnostic(outcome.plugin_name)


async def _warn_orphaned_contracts(discovered_names: list[str]) -> None:
    """Warn about a persisted contract whose plugin was not discovered (OQ2).

    A removed plugin's tables must remain until an operator cleans them up
    (D-11), so this only logs — it never touches that row or its tables.
    """
    try:
        async with async_session_maker() as session:
            rows = (
                await session.execute(
                    text(
                        "SELECT name, database_version_table FROM plugins "
                        "WHERE database_version_table IS NOT NULL"
                    )
                )
            ).all()
    except ProgrammingError:
        logger.warning(DIAGNOSTIC_COLUMNS_PENDING_WARNING)
        return

    discovered = set(discovered_names)
    for name, version_table in rows:
        if name not in discovered:
            logger.warning(
                "Plugin %r records a database contract (version table %r) but "
                "was not discovered — leaving its row and tables untouched "
                "(D-11)",
                name,
                version_table,
            )


def plugin_alembic_config(
    plugin_name: str,
    migrations_dir,
    version_table: str,
    metadata,
) -> Config:
    """Build a programmatic Alembic ``Config`` for one plugin.

    ``script_location`` is ALWAYS the shared core env; the plugin directory is
    named only in ``version_locations`` (RESEARCH Pitfall 2).
    """
    cfg = Config()
    cfg.set_main_option("script_location", str(PLUGIN_ALEMBIC_DIR))
    cfg.set_main_option("version_locations", str(migrations_dir))
    # Alembic 1.19 deprecated `version_path_separator` in favour of
    # `path_separator`; without either it falls back to legacy splitting and
    # emits a DeprecationWarning (RESEARCH State of the Art).
    cfg.set_main_option("path_separator", "os")
    cfg.attributes.update(
        plugin_name=plugin_name,
        version_table=version_table,
        target_metadata=metadata,
    )
    return cfg


def script_directory_for(migrations_dir) -> ScriptDirectory:
    """Return the plugin's revision directory as a :class:`ScriptDirectory`.

    WARNING (CR-01): constructing a ``ScriptDirectory`` with
    ``version_locations`` IMPORTS every revision module in that directory (its
    ``RevisionMap`` is populated through ``Script._from_path`` →
    ``util.load_python_file``). It must therefore only ever be called where
    importing untrusted plugin code is acceptable: inside the de-privileged role
    child, or in the documented pre-287.1 weakened fallback where the parent is
    already executing the plugin revision itself. The privileged parent in the
    configured-role path uses :func:`_static_revision_heads` instead.
    """
    return ScriptDirectory(
        dir=str(PLUGIN_ALEMBIC_DIR), version_locations=[str(migrations_dir)]
    )


def _static_revision_heads(revisions_dir) -> set[str]:
    """Return a plugin's Alembic head ids WITHOUT importing any revision (CR-01).

    A plugin revision module is untrusted code. The privileged parent must never
    execute it before the de-privileged role child runs, yet the pre-287.1 head
    computation used ``ScriptDirectory.get_heads()``, which imports every
    revision module's top level in whatever process calls it. This helper derives
    the same head set from a literal-only AST parse, which cannot execute code.

    ``revision`` and ``down_revision`` are module-level literals in the shared
    revision template (``plugin_alembic/script.py.mako``), so the parser reads
    them reliably. A file whose literals cannot be read contributes nothing, and
    an empty or branched resulting head set still fails closed exactly as before.
    The child keeps importing the revisions for real — it is de-privileged.
    """
    heads: set[str] = set()
    down_revisions: set[str] = set()

    for path in sorted(Path(revisions_dir).glob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (OSError, SyntaxError, UnicodeDecodeError):
            continue

        values: dict[str, object] = {}
        for node in tree.body:
            if isinstance(node, ast.Assign) and len(node.targets) == 1:
                target, value_node = node.targets[0], node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                target, value_node = node.target, node.value
            else:
                continue
            if not isinstance(target, ast.Name):
                continue
            try:
                values[target.id] = ast.literal_eval(value_node)
            except (ValueError, SyntaxError, TypeError):
                continue

        revision = values.get("revision")
        if isinstance(revision, str):
            heads.add(revision)
        elif isinstance(revision, (tuple, list)):
            heads.update(item for item in revision if isinstance(item, str))

        down_revision = values.get("down_revision")
        if isinstance(down_revision, str):
            down_revisions.add(down_revision)
        elif isinstance(down_revision, (tuple, list)):
            down_revisions.update(
                item for item in down_revision if isinstance(item, str)
            )

    return heads - down_revisions


def resolve_revisions_dir(migrations_root) -> Path:
    """Return the directory holding a plugin's Alembic revision files.

    ``migrations_root`` is the plugin's own migrations directory as resolved by
    :func:`app.plugin_system.loader.resolve_migrations_dir`; its revision files
    live in the conventional ``versions/`` subdirectory. A plugin that keeps its
    revisions directly in ``migrations_root`` is also accepted.
    """
    root = Path(migrations_root)
    versions = root / "versions"
    return versions if versions.is_dir() else root


def _current_heads(connection, version_table: str) -> set[str]:
    """DB-side head set for ``version_table`` (empty when the table is absent)."""
    context = MigrationContext.configure(
        connection, opts={"version_table": version_table}
    )
    return set(context.get_current_heads())


def _upgrade(connection, cfg: Config) -> tuple[str, str]:
    """Run one plugin's upgrade inside ``connection.run_sync``.

    Returns ``(status, revision)``. Raises on any Alembic/SQL failure — the
    caller isolates it into a failed outcome.
    """
    cfg.attributes["connection"] = connection
    script = script_directory_for(cfg.get_main_option("version_locations"))
    script_heads = set(script.get_heads())
    if not script_heads:
        # An existing but empty revisions directory has zero heads — a distinct
        # fault from a branched history. Reporting it as "more than one head"
        # would send the operator after a merge revision that cannot exist (WR-01).
        raise PluginMigrationError(
            f"plugin {cfg.attributes['plugin_name']!r} exposes no head",
            reason=REASON_NO_HEAD,
        )
    if len(script_heads) > 1:
        raise PluginMigrationError(
            f"plugin {cfg.attributes['plugin_name']!r} exposes {len(script_heads)} heads",
            reason=REASON_MULTIPLE_HEADS,
        )

    head = next(iter(script_heads))
    current_before = _current_heads(connection, cfg.attributes["version_table"])
    try:
        command.upgrade(cfg, "head")
    except Exception as exc:
        # D-14: the affected revision is the head this run was moving TO. The
        # raw exception is chained (``__cause__``) so the caller's
        # ``logger.exception`` still records the full technical detail.
        raise PluginMigrationError(
            f"plugin {cfg.attributes['plugin_name']!r} migration failed",
            reason=REASON_MIGRATION_FAILED,
            revision=head,
        ) from exc
    status = STATUS_UP_TO_DATE if current_before == script_heads else STATUS_MIGRATED
    return status, head


def _database_locator() -> tuple[str, str, int, str]:
    """The non-secret host/port/database components of the application URL.

    Deliberately parsed from :func:`get_database_url` here, in the privileged
    parent, so the child never receives a full URL (and never its password).
    Must stay on the RUNTIME builder (Pitfall 1): the locator describes the
    application connection, not the maintenance one.
    """
    url = make_url(get_database_url())
    return (
        url.drivername,
        url.host or "localhost",
        url.port or 5432,
        url.database or "app",
    )


def _resolve_runtime_role_name() -> str | None:
    """The application runtime role the plugin tables must admit (JDATA-05).

    The plugin migration role OWNS the plugin tables and is intentionally unable
    to touch Core; the application runtime role is a distinct identity that needs
    table-scoped DML on them. Only the runtime role NAME travels to the child (a
    non-secret argv entry) — never a credential. The name is derived from the
    application database URL's username and validated as a safe PostgreSQL
    identifier; anything unvalidated is returned as ``None`` (no GRANT) rather than
    interpolated into SQL or argv.

    Must stay on the RUNTIME builder (Pitfall 1): resolving this from the
    maintenance URL would return ``DB_USER`` and silently grant plugin DML to the
    administrator identity instead of the application role.
    """
    username = make_url(get_database_url()).username
    if not username:
        return None
    try:
        runtime_role = validate_plugin_role_identifier(username)
    except PluginRoleConfigError:
        logger.warning(
            "Application database user is not a safe PostgreSQL identifier; "
            "skipping plugin runtime-role grants"
        )
        return None
    logger.debug(
        "Plugin tables will grant SELECT, INSERT, UPDATE, DELETE to runtime role %r",
        runtime_role,
    )
    return runtime_role


def _child_argv(
    name: str,
    plugins_dir: str,
    drivername: str,
    host: str,
    port: int,
    database: str,
    runtime_role: str | None,
) -> list[str]:
    argv = [
        sys.executable,
        "-m",
        CHILD_MODULE,
        "--plugins-dir",
        str(plugins_dir),
        "--plugin-name",
        name,
        "--drivername",
        drivername,
        "--host",
        host,
        "--port",
        str(port),
        "--database",
        database,
    ]
    # The resolved output format is a non-secret name; it is passed explicitly so
    # the child's log format matches the parent's without widening its
    # two-credential environment (research Pitfall 9).
    argv += ["--log-format", resolve_log_format()]
    # JDATA-05: the runtime role name is not a secret; it is appended only when it
    # resolved, so an unvalidated name can never reach the child's SQL.
    if runtime_role is not None:
        argv += ["--runtime-role", runtime_role]
    return argv


def _child_env(plugin_role: PluginRoleCredentials) -> dict[str, str]:
    """The child's COMPLETE environment: only the two plugin credentials (D-02).

    Built from scratch — never a copy of ``os.environ`` — so the child can never
    inherit an application credential (``DB_PASSWORD`` / ``POSTGRES_PASSWORD`` /
    ``DATABASE_URL``) even when the parent process carries one. The non-secret
    locator components travel as explicit argv entries instead (``_child_argv``).
    """
    return {
        PLUGIN_DB_USER_ENV: plugin_role.role_name,
        PLUGIN_DB_PASSWORD_ENV: plugin_role.password,
    }


def _run_child_sync(
    args: list[str], env: dict, cwd: str, timeout: float
) -> subprocess.CompletedProcess:
    """Run the child with a fully REPLACED environment (observable test seam).

    stdout/stderr are captured but never surfaced by the caller (T-287.1-03).
    """
    return subprocess.run(
        args, env=env, cwd=cwd, capture_output=True, timeout=timeout
    )


async def _upgrade_with_role_child(
    result: DiscoveryResult,
    revisions_dir: Path,
    plugins_dir: str,
    plugin_role: PluginRoleCredentials,
) -> PluginMigrationOutcome:
    """Run one plugin's revision in the hermetic role child (D-02/D-11/D-16).

    The parent keeps discovery, preflight, the advisory lock and diagnostics; it
    delegates ONLY the Alembic upgrade. A child connect/auth failure, a non-zero
    exit or a timeout is isolated into this plugin's ``failed`` outcome — there is
    never a privileged application-connection retry for a configured role.
    """
    name = result.name

    # CR-01: derive the head from a non-executing AST parse. Building a
    # ScriptDirectory here would import every untrusted revision module in the
    # privileged parent — before the de-privileged child ever runs.
    script_heads = _static_revision_heads(revisions_dir)
    if not script_heads:
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_NO_HEAD)
    if len(script_heads) > 1:
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_MULTIPLE_HEADS
        )
    head = next(iter(script_heads))

    drivername, host, port, database = _database_locator()
    runtime_role = _resolve_runtime_role_name()
    args = _child_argv(
        name, plugins_dir, drivername, host, port, database, runtime_role
    )
    child_env = _child_env(plugin_role)

    try:
        completed = await asyncio.to_thread(
            _run_child_sync,
            args,
            child_env,
            str(BACKEND_DIR),
            DEFAULT_PER_PLUGIN_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        logger.error("Plugin %r role child exceeded the per-plugin time limit", name)
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_TIMEOUT)
    except OSError:
        logger.exception("Plugin %r role child could not be started", name)
        return PluginMigrationOutcome(name, STATUS_FAILED, head, REASON_MIGRATION_FAILED)

    code = completed.returncode
    if code == CHILD_EXIT_MIGRATED:
        return PluginMigrationOutcome(name, STATUS_MIGRATED, head, None)
    if code == CHILD_EXIT_UP_TO_DATE:
        return PluginMigrationOutcome(name, STATUS_UP_TO_DATE, head, None)

    reason = _CHILD_EXIT_REASON.get(code, REASON_MIGRATION_FAILED)
    revision = head if reason == REASON_MIGRATION_FAILED else None
    logger.error(
        "Plugin %r role child stopped (exit code %s, reason: %s)", name, code, reason
    )
    return PluginMigrationOutcome(name, STATUS_FAILED, revision, reason)


async def _migrate_one(
    result: DiscoveryResult,
    plugins_dir: str,
    plugin_role: PluginRoleCredentials | None,
) -> PluginMigrationOutcome:
    """Migrate a single discovered plugin, isolating every plugin-attributable failure.

    The plugin's migrations directory is resolved through the shared loader
    resolver — one guarded implementation for both discovery sources (T-287-09).
    An entry-point result whose package is not a real directory yields an
    ``importlib.resources`` traversable; it is materialised with ``as_file(...)``
    for the duration of that plugin's upgrade only. Every resolution failure is
    that plugin's own failed outcome, never a run-level abort.
    """
    name = result.name

    try:
        resolved = resolve_migrations_dir(result)
    except ValueError:
        logger.exception("Plugin %r migrations directory could not be resolved", name)
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_MIGRATIONS_DIR_MISSING
        )

    if resolved is None:
        logger.warning(
            "Plugin %r declares database migrations but no migrations dir "
            "could be resolved",
            name,
        )
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_MIGRATIONS_DIR_MISSING
        )

    if isinstance(resolved, Path):
        return await _upgrade_with_dir(result, resolved, plugins_dir, plugin_role)

    try:
        with importlib.resources.as_file(resolved) as materialized:
            return await _upgrade_with_dir(
                result, Path(materialized), plugins_dir, plugin_role
            )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        logger.exception(
            "Plugin %r packaged migrations directory could not be materialised", name
        )
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_MIGRATIONS_DIR_MISSING
        )


async def _upgrade_with_dir(
    result: DiscoveryResult,
    migrations_dir: Path,
    plugins_dir: str,
    plugin_role: PluginRoleCredentials | None,
) -> PluginMigrationOutcome:
    """Run one plugin's upgrade from an already-materialised migrations directory.

    With ``plugin_role`` set the actual Alembic upgrade is delegated to the
    hermetic role child (D-02); otherwise the pre-287.1 in-process privileged
    path runs unchanged.
    """
    name = result.name
    spec = result.manifest.database

    if not migrations_dir.is_dir():
        logger.warning(
            "Plugin %r migrations dir %s does not exist", name, migrations_dir
        )
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_MIGRATIONS_DIR_MISSING
        )

    revisions_dir = resolve_revisions_dir(migrations_dir)

    metadata = getattr(result.plugin_cls, "database_metadata", None)
    if metadata is None:
        logger.warning("Plugin %r exposes no database_metadata", name)
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_METADATA_MISSING)

    if plugin_role is not None:
        return await _upgrade_with_role_child(
            result, revisions_dir, plugins_dir, plugin_role
        )

    cfg = plugin_alembic_config(name, revisions_dir, spec.version_table, metadata)

    plugin_conn = await _get_runner_engine().connect()
    try:
        async with plugin_conn.begin():
            # Bounded per-plugin work (RESEARCH Pitfall 6 / A-287-03). The
            # timeout runs on THIS plugin's own fresh connection, so a cancelled
            # run can only invalidate this connection — never the dedicated lock
            # connection or a sibling's (verified live 2026-09-22: a cancelled
            # ``run_sync`` leaves its connection in ``PendingRollbackError``).
            status, revision = await asyncio.wait_for(
                plugin_conn.run_sync(_upgrade, cfg),
                timeout=DEFAULT_PER_PLUGIN_TIMEOUT_SECONDS,
            )
    except TimeoutError:
        logger.error("Plugin %r migration exceeded the per-plugin time limit", name)
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except PluginMigrationError as exc:
        if exc.reason == REASON_MIGRATION_FAILED:
            # Full technical detail (the chained original) goes to the log only.
            logger.exception("Plugin %r migration failed", name)
        else:
            logger.error("Plugin %r migration rejected: %s", name, exc)
        return PluginMigrationOutcome(
            name, STATUS_FAILED, exc.revision, exc.reason
        )
    except Exception:
        logger.exception("Plugin %r migration failed", name)
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_MIGRATION_FAILED)
    finally:
        try:
            await plugin_conn.close()
        except Exception:
            logger.exception("Could not close plugin %r migration connection", name)

    return PluginMigrationOutcome(name, status, revision, None)


async def _acquire_lock(connection, *, timeout_s: float) -> bool:
    """Bounded ``pg_try_advisory_lock`` acquisition (D-09).

    Only ever issues the ``try`` variant — session-level lock requests stack, so
    retrying the blocking ``pg_advisory_lock`` would leak a lock reference per
    retry (RESEARCH Pitfall 4).
    """
    deadline = time.monotonic() + timeout_s
    while True:
        got = (
            await connection.execute(
                text("SELECT pg_try_advisory_lock(:key)"),
                {"key": PLUGIN_MIGRATION_LOCK_KEY},
            )
        ).scalar()
        if got:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.5)


async def _diagnostic_row_xmins(names: list[str]) -> dict[str, str]:
    """Snapshot diagnostic rows before waiting for the migration lock.

    PostgreSQL increments a row's ``xmin`` on every UPDATE. A runner that times
    out acquiring the lock can therefore update only rows that no successful
    lock holder changed while it waited.
    """
    if not names:
        return {}

    async with async_session_maker() as session:
        rows = (
            await session.execute(
                text(
                    "SELECT name, xmin::text FROM plugins "
                    "WHERE name = ANY(CAST(:names AS text[]))"
                ),
                {"names": names},
            )
        ).all()
    return dict(rows)


def _preflight_database_plugins(
    database_plugins: list[DiscoveryResult],
) -> tuple[list[PluginMigrationOutcome], list[DiscoveryResult]]:
    """Refuse invalid table declarations BEFORE any plugin revision runs (CR-01).

    The deploy runner is the first place a discovered plugin's declared tables
    are seen — the activation gate's ownership inventory only runs later, at
    backend startup. Validating here (with the same shared registry / vocabulary
    the gate uses) means a plugin whose declaration violates the ownership or
    naming contract is isolated into that plugin's failed outcome and its
    migrations are never executed, instead of being committed and only then
    refused. First-valid-owner-wins is preserved: a refused plugin claims
    nothing, so a later sibling's legitimate declaration is unaffected.
    """
    registry = DatabaseTableRegistry()
    rejected: list[PluginMigrationOutcome] = []
    runnable: list[DiscoveryResult] = []

    for result in database_plugins:
        metadata = getattr(result.plugin_cls, "database_metadata", None)
        try:
            registry.add_declaration(result.name, result.manifest, metadata)
        except (TableOwnershipError, ValueError) as exc:
            reason = ownership_refusal_reason(
                result.name, result.manifest, metadata, exc
            )
            logger.error(
                "Plugin %r database declaration refused before migration: %s",
                result.name,
                exc,
            )
            rejected.append(
                PluginMigrationOutcome(result.name, STATUS_FAILED, None, reason)
            )
            continue
        runnable.append(result)

    return rejected, runnable


async def _adopt_plugin_objects(
    lock_conn,
    result: DiscoveryResult,
    plugin_role: PluginRoleCredentials,
) -> PluginMigrationOutcome | None:
    """Adopt pre-287.1 plugin objects onto the isolated role (PLUGDB-07, GAP-1).

    A plugin schema migrated BEFORE the role boundary existed is owned by the
    privileged application user. The hermetic child then cannot even read the
    plugin's version table (``permission denied``) and every run fails with
    child exit 20 — the normal state of an existing installation on its first
    deploy with the boundary enabled, because the ORM-created tables/version
    tables predate the role.

    The runner therefore transfers ownership of EXACTLY this plugin's declared
    tables and its own version table to the role before the child starts, on the
    privileged connection it already holds (PostgreSQL cascades owned
    sequences). Fresh installs are a no-op: the role already owns everything it
    created. Returns ``None`` on success, or the plugin's failed outcome when
    the adoption itself cannot be performed.
    """
    name = result.name
    metadata = getattr(result.plugin_cls, "database_metadata", None)
    if metadata is None:
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_METADATA_MISSING)

    targets = list(
        dict.fromkeys(
            [table.name for table in metadata.tables.values()]
            + [result.manifest.database.version_table]
        )
    )

    # Close any implicit transaction the lock/verification statements opened:
    # the adoption DDL below must be committed before the child opens its own
    # connection, otherwise the ACCESS EXCLUSIVE locks in this open transaction
    # block the child until the per-plugin timeout. The advisory lock is
    # session-level and survives the commit.
    await lock_conn.commit()

    try:
        for target in targets:
            row = (
                await lock_conn.execute(
                    text(
                        "SELECT c.relkind, pg_get_userbyid(c.relowner) AS owner "
                        "FROM pg_class c "
                        "JOIN pg_namespace n ON n.oid = c.relnamespace "
                        "WHERE n.nspname = 'public' AND c.relname = :name "
                        "AND c.relkind IN ('r', 'p', 'S')"
                    ),
                    {"name": target},
                )
            ).first()
            if row is None or row.owner == plugin_role.role_name:
                continue
            # pg_class.relkind is PostgreSQL's "char" type — asyncpg may hand it
            # back as bytes (same shape as existing confdeltype assertions).
            relkind = row.relkind
            if isinstance(relkind, bytes):
                relkind = relkind.decode()
            kind = "TABLE" if relkind in ("r", "p") else "SEQUENCE"
            await lock_conn.execute(
                text(
                    f"ALTER {kind} {_quote_identifier(target)} "
                    f"OWNER TO {_quote_identifier(plugin_role.role_name)}"
                )
            )
            logger.info(
                "Plugin %r object %r adopted by the migration role %r",
                name,
                target,
                plugin_role.role_name,
            )
        await lock_conn.commit()
    except Exception:
        logger.exception(
            "Plugin %r pre-existing objects could not be adopted by the "
            "migration role",
            name,
        )
        try:
            await lock_conn.rollback()
        except Exception:
            logger.exception("Could not roll back the failed adoption for %r", name)
        return PluginMigrationOutcome(name, STATUS_FAILED, None, REASON_MIGRATION_FAILED)
    return None


async def _restricted_role_refusal(
    lock_conn,
    result: DiscoveryResult,
    plugin_role: PluginRoleCredentials | None,
) -> PluginMigrationOutcome | None:
    """Per-plugin admission for a ``require_restricted_role`` plugin (JDATA-05).

    Runs BEFORE object adoption or any revision executes. A missing configured
    role yields ``REASON_RESTRICTED_ROLE_REQUIRED``; a configured but absent or
    elevated login role (superuser / CREATEDB / CREATEROLE / BYPASSRLS / NOLOGIN)
    yields ``REASON_RESTRICTED_ROLE_INVALID``. Ownership/ACL/membership drift
    stays the separate run-level, fatal ``verify_plugin_role_boundary`` check —
    this gate deliberately re-checks only the ``pg_roles`` attributes it owns, so
    it never duplicates that boundary. No password, DB URL, SQL text or exception
    string can reach the outcome.
    """
    name = result.name
    if plugin_role is None:
        logger.error(
            "Plugin %r requires a restricted migration role but none is configured",
            name,
        )
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_RESTRICTED_ROLE_REQUIRED
        )

    attributes = (
        await lock_conn.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolcreaterole, rolcreatedb, "
                "rolbypassrls FROM pg_roles WHERE rolname = :name"
            ),
            {"name": plugin_role.role_name},
        )
    ).first()
    if attributes is None or not (
        attributes.rolcanlogin
        and not attributes.rolsuper
        and not attributes.rolcreaterole
        and not attributes.rolcreatedb
        and not attributes.rolbypassrls
    ):
        logger.error(
            "Plugin %r migration role is missing or not a restricted login role",
            name,
        )
        return PluginMigrationOutcome(
            name, STATUS_FAILED, None, REASON_RESTRICTED_ROLE_INVALID
        )
    return None


async def run_plugin_migrations(
    *,
    plugins_dir=None,
    plugin_role: PluginRoleCredentials | None = None,
) -> MigrationRunResult:
    """Discover database-owning plugins and upgrade each to its own head.

    The whole plugin loop runs under ONE session-level advisory lock held on a
    DEDICATED connection (D-09). If the bounded acquisition fails, no plugin is
    migrated, ``locked_out`` is True and the caller still exits 0.

    ``plugin_role`` selects the hermetic role-child execution path (D-02/D-16).
    A direct caller that omits it keeps the pre-287.1 privileged in-process path;
    the deploy entry (``migrate.py``) resolves the credential matrix, supplies
    the credentials when configured, and emits the D-10 weakened-state warning
    once when both are absent.
    """
    if plugins_dir is None:
        from app.plugin_system import get_plugins_dir

        plugins_dir = get_plugins_dir()
    plugins_dir = str(plugins_dir)

    results = discover_all(plugins_dir)
    database_plugins = [
        result
        for result in results
        if result.error is None
        and result.manifest is not None
        and result.manifest.database is not None
    ]
    discovered_names = [result.name for result in database_plugins]
    diagnostic_row_xmins = await _diagnostic_row_xmins(discovered_names)

    # CR-01: validate every declared table contract BEFORE acquiring the lock or
    # running a single revision. A refused plugin contributes a failed outcome
    # (persisted below) and is never migrated; its revisions never execute.
    preflight_rejected, runnable_plugins = _preflight_database_plugins(
        database_plugins
    )

    async with _get_runner_engine().connect() as lock_conn:
        acquired = await _acquire_lock(
            lock_conn, timeout_s=DEFAULT_LOCK_TIMEOUT_SECONDS
        )
        if acquired:
            try:
                if plugin_role is not None:
                    # WR-03: re-assert the boundary in the runner itself, so
                    # EVERY path that can execute a revision — not only the
                    # separate bootstrap step — refuses a drifted role before a
                    # child starts. A not-yet-created role has no drift here; the
                    # child then fails closed on connect. A refusal propagates
                    # (fatal, D-07) rather than degrading to a per-plugin outcome.
                    await verify_plugin_role_boundary(
                        lock_conn, role_name=plugin_role.role_name
                    )
                outcomes: list[PluginMigrationOutcome] = list(preflight_rejected)
                for result in runnable_plugins:
                    if result.manifest.database.require_restricted_role:
                        # JDATA-05: refuse a flagged plugin BEFORE object adoption
                        # or any revision when the restricted role is absent or
                        # elevated. Unflagged plugins keep the legacy path.
                        refusal = await _restricted_role_refusal(
                            lock_conn, result, plugin_role
                        )
                        if refusal is not None:
                            outcomes.append(refusal)
                            continue
                    if plugin_role is not None:
                        # GAP-1: a schema that predates the boundary must be
                        # adopted onto the role before the hermetic child can
                        # read its own version table.
                        adoption = await _adopt_plugin_objects(
                            lock_conn, result, plugin_role
                        )
                        if adoption is not None:
                            outcomes.append(adoption)
                            continue
                    outcomes.append(
                        await _migrate_one(result, plugins_dir, plugin_role)
                    )
                run_result = MigrationRunResult(
                    outcomes=outcomes,
                    discovered_plugin_names=discovered_names,
                    diagnostic_row_xmins=diagnostic_row_xmins,
                )
            finally:
                # Exactly one unlock, on the same dedicated connection that
                # acquired it.
                await lock_conn.execute(
                    text("SELECT pg_advisory_unlock(:key)"),
                    {"key": PLUGIN_MIGRATION_LOCK_KEY},
                )
        else:
            logger.warning(
                "Plugin migration runner could not acquire the migration lock "
                "within %.1fs — skipping all plugin runs (D-09)",
                DEFAULT_LOCK_TIMEOUT_SECONDS,
            )
            run_result = MigrationRunResult(
                outcomes=[],
                locked_out=True,
                discovered_plugin_names=discovered_names,
                diagnostic_row_xmins=diagnostic_row_xmins,
            )

    # D-13/D-15: persist the bounded diagnostic for every outcome (or the lock
    # reason for every discovered plugin on a locked-out run), then warn about
    # any persisted contract this run did not discover (RESEARCH OQ2).
    await _persist_run_diagnostics(run_result)
    await _warn_orphaned_contracts(discovered_names)
    return run_result
