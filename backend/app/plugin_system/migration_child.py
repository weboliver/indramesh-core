"""Phase 287.1: the hermetic child that runs exactly one plugin Alembic revision.

D-02: the parent runner spawns this module once per database-owning plugin with
a completely replaced environment that carries ONLY ``PLUGIN_DB_USER`` and
``PLUGIN_DB_PASSWORD``. Everything else the child needs — the plugin identity and
the non-secret connection locator components — arrives as explicit argv entries,
never as a full URL and never as a password argument. The child builds its own
engine with ``URL.create(...)`` and ``NullPool`` and reaches Alembic only through
an injected connection, so the shared env (``plugin_alembic/env.py``) can never
open an application-credential fallback.

The child speaks only fixed exit codes (T-287.1-03): stdout/stderr are never a
protocol and are never read by the parent.
"""

import argparse
import asyncio
import importlib.resources
import logging
import os
import sys
from pathlib import Path

from sqlalchemy.engine import URL
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool
from sqlalchemy import text

from app.logging import configure_logging
from app.logging.fields import LOG_FORMATS
from app.plugin_system.bootstrap_role import (
    PLUGIN_DB_PASSWORD_ENV,
    PLUGIN_DB_USER_ENV,
    _quote_identifier,
)
from app.plugin_system.loader import DiscoveryResult, discover_all, resolve_migrations_dir
from app.plugin_system.migrations import (
    CHILD_EXIT_CONFIG,
    CHILD_EXIT_METADATA_MISSING,
    CHILD_EXIT_MIGRATED,
    CHILD_EXIT_MIGRATION_FAILED,
    CHILD_EXIT_MULTIPLE_HEADS,
    CHILD_EXIT_NO_HEAD,
    CHILD_EXIT_RUNTIME_GRANT_FAILED,
    CHILD_EXIT_UP_TO_DATE,
    REASON_MULTIPLE_HEADS,
    REASON_NO_HEAD,
    STATUS_MIGRATED,
    PluginMigrationError,
    _upgrade,
    plugin_alembic_config,
    resolve_revisions_dir,
)


logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="app.plugin_system.migration_child",
        description="Run one plugin's Alembic revision as the plugin role.",
    )
    parser.add_argument("--plugins-dir", required=True)
    parser.add_argument("--plugin-name", required=True)
    parser.add_argument("--drivername", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--database", required=True)
    # JDATA-05: the non-secret application runtime role the plugin tables must
    # admit. Absent means "no grant" (the legacy no-plugin-role path, or an
    # unvalidated application user).
    parser.add_argument("--runtime-role", default=None)
    # The parent's resolved format travels as a non-secret argv entry so the
    # child's stdout is in the same format without widening its environment.
    parser.add_argument("--log-format", choices=LOG_FORMATS, default=None)
    return parser


def _find_plugin(plugins_dir: str, plugin_name: str) -> DiscoveryResult | None:
    for result in discover_all(plugins_dir):
        if (
            result.error is None
            and result.manifest is not None
            and result.manifest.database is not None
            and result.name == plugin_name
        ):
            return result
    return None


async def _grant_runtime_dml(
    connection,
    *,
    runtime_role: str | None,
    plugin_role: str,
    metadata,
) -> None:
    """Owner-issued, table-scoped DML grant for the runtime role (JDATA-05).

    PostgreSQL only lets the table OWNER (or a superuser) GRANT, and the owner is
    the plugin role — which is why this runs here, in the de-privileged child,
    after the upgrade, and never in the privileged parent. Exactly the plugin's
    declared ``database_metadata`` tables (never the version table — the runtime
    reads the head through the parent's privileged gate) receive
    ``SELECT, INSERT, UPDATE, DELETE``; no GRANT OPTION, nothing on Core, no role
    membership. Re-running is idempotent and harmless, so the ``up_to_date`` path
    reconciles grants too. The runtime role equal to the plugin role is skipped
    (it already owns the tables).
    """
    if not runtime_role or runtime_role == plugin_role:
        return
    tables = sorted({table.name for table in metadata.tables.values()})
    if not tables:
        return
    table_list = ", ".join(_quote_identifier(name) for name in tables)
    await connection.execute(
        text(
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE {table_list} "
            f"TO {_quote_identifier(runtime_role)}"
        )
    )


async def _upgrade_from_dir(
    args: argparse.Namespace,
    result: DiscoveryResult,
    migrations_dir,
    user: str,
    password: str,
) -> int:
    metadata = getattr(result.plugin_cls, "database_metadata", None)
    if metadata is None:
        return CHILD_EXIT_METADATA_MISSING

    revisions_dir = resolve_revisions_dir(migrations_dir)
    cfg = plugin_alembic_config(
        result.name, revisions_dir, result.manifest.database.version_table, metadata
    )

    url = URL.create(
        drivername=args.drivername,
        username=user,
        password=password,
        host=args.host,
        port=args.port,
        database=args.database,
    )
    engine = create_async_engine(url, poolclass=NullPool, hide_parameters=True)
    try:
        async with engine.connect() as connection:
            async with connection.begin():
                status, _revision = await connection.run_sync(_upgrade, cfg)
            # JDATA-05: the runtime grant runs in its OWN transaction after the
            # (idempotent) upgrade commits, so a refused grant never rolls back an
            # already-applied revision and is retried on the next run. The loop
            # reaches here on both `migrated` and `up_to_date`.
            try:
                async with connection.begin():
                    await _grant_runtime_dml(
                        connection,
                        runtime_role=getattr(args, "runtime_role", None),
                        plugin_role=user,
                        metadata=metadata,
                    )
            except Exception:
                return CHILD_EXIT_RUNTIME_GRANT_FAILED
    except PluginMigrationError as exc:
        if exc.reason == REASON_NO_HEAD:
            return CHILD_EXIT_NO_HEAD
        if exc.reason == REASON_MULTIPLE_HEADS:
            return CHILD_EXIT_MULTIPLE_HEADS
        return CHILD_EXIT_MIGRATION_FAILED
    except Exception:
        return CHILD_EXIT_MIGRATION_FAILED
    finally:
        await engine.dispose()

    return CHILD_EXIT_MIGRATED if status == STATUS_MIGRATED else CHILD_EXIT_UP_TO_DATE


async def _run(args: argparse.Namespace) -> int:
    user = os.environ.get(PLUGIN_DB_USER_ENV)
    password = os.environ.get(PLUGIN_DB_PASSWORD_ENV)
    if not user or password is None:
        return CHILD_EXIT_CONFIG

    result = _find_plugin(args.plugins_dir, args.plugin_name)
    if result is None:
        return CHILD_EXIT_METADATA_MISSING

    migrations_dir = resolve_migrations_dir(result)
    if migrations_dir is None:
        return CHILD_EXIT_METADATA_MISSING

    if isinstance(migrations_dir, Path):
        return await _upgrade_from_dir(args, result, migrations_dir, user, password)

    try:
        with importlib.resources.as_file(migrations_dir) as materialized:
            return await _upgrade_from_dir(
                args, result, Path(materialized), user, password
            )
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        return CHILD_EXIT_METADATA_MISSING


def main(argv=None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    # D-18: the child configures the central logging itself, with the parent's
    # resolved format passed as a non-secret argv entry (its environment carries
    # only the two plugin credentials, so LOG_FORMAT is not available to it).
    configure_logging("migration-child", fmt=args.log_format)
    logger.info("plugin migration child starting", extra={"plugin": args.plugin_name})
    try:
        exit_code = asyncio.run(_run(args))
    except Exception:
        # The parent never treats child output as a protocol (T-287.1-03); this
        # record exists for direct invocation and the per-process JSON check.
        logger.error("plugin migration child failed", exc_info=True)
        return CHILD_EXIT_MIGRATION_FAILED
    logger.info("plugin migration child finished", extra={"exit_code": exit_code})
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
