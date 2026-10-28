"""Phase 287: the ONE core-owned shared Alembic environment for plugin migrations.

No plugin ships its own ``env.py`` — the runner sets ``script_location`` to THIS
directory and ``version_locations`` to the plugin's migrations directory, then
hands everything plugin-specific through ``Config.attributes`` (``connection``,
``target_metadata``, ``version_table``, ``plugin_name``). A plugin directory
must never become an Alembic ``script_location``: the ownership boundary depends
on exactly one core-owned env (PLUGDB-03, RESEARCH Pitfall 2).

Phase 287.1 (R-08, variant (a)): the manual operator path is retained but
disarmed. The top-level ``load_dotenv()`` call is gone, and the no-injected-
connection branch no longer falls back to ``get_database_url()`` (which silently
defaults to ``postgres:postgres@postgres:5432/app``). A manual ``alembic upgrade
head`` therefore requires the operator to export ``DATABASE_URL`` explicitly,
while the hermetic revision child always injects a role connection and never
reaches this path.
"""

import asyncio
import os

from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import context

from app.plugin_system.manifest import plugin_table_prefix

# R-08(a): the only accepted source for a manual, non-injected run.
MANUAL_DATABASE_URL_ENV = "DATABASE_URL"


def _plugin_table_filter(obj, name, type_, reflected, compare_to) -> bool:
    """D-02 guard-rail: restrict an author's autogenerate sweep to the plugin's own tables.

    This is an authoring guard-rail, not execution-time enforcement — the
    upgrade itself runs whatever the revision scripts contain. It mirrors the
    core env's filter in reverse: only ``plugin_<name>_*`` tables are visible,
    so a plugin's own ``alembic revision --autogenerate`` never proposes a Core
    table change or another plugin's table.
    """
    if type_ == "table" and isinstance(name, str):
        plugin_name = context.config.attributes.get("plugin_name", "")
        return name.startswith(plugin_table_prefix(plugin_name))
    return True


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=context.config.attributes.get("target_metadata"),
        version_table=context.config.attributes["version_table"],
        version_table_schema=None,
        include_object=_plugin_table_filter,
        compare_type=False,
        include_schemas=False,
    )

    with context.begin_transaction():
        context.run_migrations()


def _manual_database_url() -> str:
    """Explicit-only URL for a manual (non-injected) operator run (R-08a).

    Deliberately NOT ``get_database_url()``: that reads ``DB_*`` and silently
    defaults to ``postgres:postgres@postgres:5432/app``, which must never be a
    reachable fallback on this path.
    """
    url = os.environ.get(MANUAL_DATABASE_URL_ENV)
    if not url:
        raise RuntimeError(
            "plugin_alembic has neither an injected migration connection nor an "
            "explicit DATABASE_URL; a manual run requires the operator to export "
            "DATABASE_URL explicitly."
        )
    return url


async def run_async_migrations() -> None:
    """Manual operator path: used only when no runner-supplied connection exists."""
    connectable = create_async_engine(
        _manual_database_url(), poolclass=pool.NullPool, hide_parameters=True
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    # Cookbook "Sharing a Connection ... With Asyncio": the runner hands the
    # per-plugin sync connection in via ``config.attributes["connection"]``.
    connection = context.config.attributes.get("connection")
    if connection is None:
        asyncio.run(run_async_migrations())
    else:
        do_run_migrations(connection)


run_migrations_online()
