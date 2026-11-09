"""Phase 287.1: the idempotent plugin migration role bootstrap (PLUGDB-07).

The PostgreSQL-enforced plugin/core boundary (D-05/D-06/D-07) needs one login
role that owns no core table and holds only ``SELECT`` + ``REFERENCES`` on every
table in :func:`core_table_names`, plus ``USAGE, CREATE`` on ``public`` so it can
still create its own ``plugin_<name>_*`` tables. This module reconciles that role
on every deploy, between the core ``alembic upgrade head`` and the plugin runner
(D-03).

Identity (D-08, revised 2026-09-22): the role name is resolved from
``PLUGIN_DB_USER`` and defaults to :data:`DEFAULT_PLUGIN_DB_USER`; any value must
be a valid, safe PostgreSQL identifier and an invalid one is fatal. Credential
matrix (D-04/D-09): both variables absent is the additive weakened fallback
(handled by the deploy entry, ``migrate.py``); ``PLUGIN_DB_USER`` without
``PLUGIN_DB_PASSWORD`` is a fatal misconfiguration; ``PLUGIN_DB_PASSWORD`` without
``PLUGIN_DB_USER`` is valid and uses the default name; a bootstrap/GRANT failure
with credentials configured is fatal.

The role's DCL is generated from the core metadata, never a hand-maintained list.
A catalog pre-flight (and post-check) refuses to run any DCL when the resolved
principal already owns a Core relation (table, the Core Alembic version table or
a Core sequence), holds a table- or column-level core ACL beyond
``SELECT``/``REFERENCES`` (or any ``WITH GRANT OPTION``), belongs to any role, or
can effectively write to a core table (direct, ``PUBLIC`` or membership-derived)
— so a configured but silently escalated boundary aborts the deploy before a
child can start (T-287.1-01/T-287.1-16).

The testable function takes an existing privileged connection (D-16); the CLI is
a thin wrapper that connects with the maintenance (``DB_USER``) URL — never the
runtime application connection. The optional ``core_tables`` parameter is a
test-only seam — the production CLI never passes it and always uses
:func:`core_table_names`.
"""

import asyncio
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Iterable

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_maintenance_database_url
from app.logging import configure_logging
from app.plugin_system.database_registry import (
    CORE_VERSION_TABLE,
    core_table_names,
)

logger = logging.getLogger(__name__)

# D-08: the published default plugin login identity when PLUGIN_DB_USER is unset.
DEFAULT_PLUGIN_DB_USER = "indramesh_plugin"

PLUGIN_DB_USER_ENV = "PLUGIN_DB_USER"
PLUGIN_DB_PASSWORD_ENV = "PLUGIN_DB_PASSWORD"

# T-287.1-04: the resolved name is interpolated as a quoted identifier, so its
# charset must be constrained. A conservative PostgreSQL identifier (letters,
# digits, underscore; at most 63 bytes) accepts any operator-chosen safe name and
# rejects whitespace, quoting, semicolons and path/number-leading values.
PLUGIN_ROLE_IDENTIFIER_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_PLUGIN_ROLE_IDENTIFIER_RE = re.compile(PLUGIN_ROLE_IDENTIFIER_PATTERN)

# Fixed, secret-free diagnostics. The boundary reasons are persisted/logged
# verbatim and never carry SQL, a password or a raw exception string.
BOUNDARY_OWNERSHIP = "Plugin database role owns a core object."
BOUNDARY_DIRECT_ACL = (
    "Plugin database role holds a core table privilege beyond SELECT and REFERENCES."
)
BOUNDARY_MEMBERSHIP = "Plugin database role is a member of another role."
BOUNDARY_EFFECTIVE_WRITE = "Plugin database role can write to a core table."

CONFIG_INVALID_CREDENTIALS = (
    "PLUGIN_DB_USER is set but PLUGIN_DB_PASSWORD is missing; configure both or neither."
)
CONFIG_INVALID_IDENTIFIER = (
    "PLUGIN_DB_USER must be a valid PostgreSQL identifier "
    f"({PLUGIN_ROLE_IDENTIFIER_PATTERN})."
)
BOOTSTRAP_FAILED = "Plugin database role bootstrap failed."
PLUGIN_ROLE_SKIPPED = (
    "Plugin database role credentials are not configured "
    "(PLUGIN_DB_USER / PLUGIN_DB_PASSWORD absent) — skipping role bootstrap."
)
# D-10: the ONE prominent weakened-state surface, emitted once at deploy-runner
# start when both credentials are absent. Log-only — no API/UI field.
PLUGIN_WEAKENED_WARNING = (
    "Plugin database role credentials are not configured "
    "(PLUGIN_DB_USER / PLUGIN_DB_PASSWORD absent) — running with the privileged "
    "application connection; core tables are not protected from plugin revisions."
)

# NOTE: the plugin password is never interpolated into a client-side SQL string.
# `ALTER ROLE ... PASSWORD` is a utility statement PostgreSQL does not accept
# bind parameters for, so the value is handed to the server as transaction-local
# GUCs and only ever composed into SQL inside a PL/pgSQL `format()` call.
_ROLE_NAME_GUC = "indramesh.plugin_role_name"
_ROLE_PASSWORD_GUC = "indramesh.plugin_role_password"


class PluginRoleConfigError(RuntimeError):
    """A fatal, fail-closed plugin-role credential misconfiguration (D-04/D-09)."""


class PluginRoleBoundaryError(RuntimeError):
    """Privilege drift found before any plugin revision ran (D-07/T-287.1-16)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


class PluginRoleBootstrapError(RuntimeError):
    """A fatal DCL/GRANT failure whose real cause must never be logged verbatim."""


@dataclass(frozen=True)
class PluginRoleConfig:
    """Resolved plugin-role credentials from the environment."""

    configured: bool
    role_name: str | None
    password: str | None

    @property
    def weakened(self) -> bool:
        """True only when *both* plugin credentials are absent (D-09)."""
        return not self.configured


def _env_value(environ, key: str) -> str | None:
    value = environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def validate_plugin_role_identifier(name: str) -> str:
    """Return ``name`` when it is a safe PostgreSQL identifier, else raise."""
    if not isinstance(name, str) or not _PLUGIN_ROLE_IDENTIFIER_RE.fullmatch(name):
        raise PluginRoleConfigError(CONFIG_INVALID_IDENTIFIER)
    return name


def resolve_plugin_role_config(environ=None) -> PluginRoleConfig:
    """Resolve the D-04/D-09 credential matrix into a :class:`PluginRoleConfig`.

    - both absent -> unconfigured (weakened fallback)
    - ``PLUGIN_DB_USER`` set without ``PLUGIN_DB_PASSWORD`` -> fatal
    - ``PLUGIN_DB_PASSWORD`` set without ``PLUGIN_DB_USER`` -> default role name
    - an invalid resolved identifier -> fatal
    """
    env = os.environ if environ is None else environ
    user = _env_value(env, PLUGIN_DB_USER_ENV)
    password = _env_value(env, PLUGIN_DB_PASSWORD_ENV)

    if user is None and password is None:
        return PluginRoleConfig(configured=False, role_name=None, password=None)

    if password is None:
        raise PluginRoleConfigError(CONFIG_INVALID_CREDENTIALS)

    role_name = DEFAULT_PLUGIN_DB_USER if user is None else user
    validate_plugin_role_identifier(role_name)
    return PluginRoleConfig(configured=True, role_name=role_name, password=password)


def _quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _sql_in_list(values: Iterable[str]) -> str:
    return ", ".join("'" + value.replace("'", "''") + "'" for value in values)


async def _role_oid(conn, role_name: str):
    return (
        await conn.execute(
            text("SELECT oid FROM pg_roles WHERE rolname = :name"),
            {"name": role_name},
        )
    ).scalar()


async def _assert_plugin_role_boundary(
    conn, role_name: str, core_tables: list[str]
) -> None:
    """Fail closed on any privilege drift of an EXISTING ``role_name``.

    A not-yet-created role has no drift and returns immediately. Otherwise the
    checks run against the live catalog: schema ownership and ownership of any
    Core relation (an ORM table, the Core Alembic version table, or a Core
    sequence), table- AND column-level core ACLs beyond ``SELECT``/``REFERENCES``
    (including any ``WITH GRANT OPTION``, which would let the role widen Core
    access), any role membership, and the effective ``INSERT``/``UPDATE``/
    ``DELETE``/``TRUNCATE``/``TRIGGER`` privilege (which ``has_table_privilege``
    also resolves via ``PUBLIC`` and nested membership). Column-level privileges
    live in ``pg_attribute.attacl``, not ``pg_class.relacl``, and
    ``has_table_privilege`` ignores them — hence the separate column check. Any
    membership at all is caught by the first hop, so the direct
    ``pg_auth_members`` check covers the transitive case too.
    """
    oid = await _role_oid(conn, role_name)
    if oid is None:
        return

    owned_schema = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_namespace "
                "WHERE nspname = 'public' AND nspowner = CAST(:oid AS oid) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if owned_schema:
        raise PluginRoleBoundaryError(BOUNDARY_OWNERSHIP)

    if not core_tables:
        return

    # WR-02: the Core Alembic version table is created by Alembic, not an ORM
    # model, so it is absent from ``core_table_names()``; add it explicitly.
    # ``relkind`` also covers sequences ('S'): any Core sequence backing an id
    # column is linked to its Core table, and PostgreSQL only permits transferring
    # a linked sequence's ownership together with the table's — so owning such a
    # sequence implies owning the Core table, which this same check already
    # refuses. The version table and any explicitly named Core sequence are
    # therefore both covered here.
    checked_relations = list(core_tables)
    if CORE_VERSION_TABLE not in checked_relations:
        checked_relations.append(CORE_VERSION_TABLE)
    in_list = _sql_in_list(checked_relations)

    owned_relation = (
        await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p', 'S') "
                "AND c.relowner = CAST(:oid AS oid) "
                f"AND c.relname IN ({in_list}) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if owned_relation is not None:
        raise PluginRoleBoundaryError(BOUNDARY_OWNERSHIP)

    extra_acl = (
        await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN LATERAL aclexplode(c.relacl) AS acl "
                "WHERE n.nspname = 'public' "
                f"AND c.relname IN ({in_list}) "
                "AND acl.grantee = CAST(:oid AS oid) "
                "AND (acl.privilege_type NOT IN ('SELECT', 'REFERENCES') "
                "OR acl.is_grantable) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if extra_acl is not None:
        raise PluginRoleBoundaryError(BOUNDARY_DIRECT_ACL)

    # WR-01: column-level privileges are stored in pg_attribute.attacl, not in
    # pg_class.relacl, and has_table_privilege() evaluates table-level grants
    # only — so a column-level GRANT UPDATE(col)/GRANT OPTION on a Core table
    # would otherwise pass both the table-ACL and effective-write checks.
    extra_column_acl = (
        await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid "
                "AND a.attnum > 0 AND NOT a.attisdropped "
                "CROSS JOIN LATERAL aclexplode(a.attacl) AS acl "
                "WHERE n.nspname = 'public' "
                f"AND c.relname IN ({in_list}) "
                "AND acl.grantee = CAST(:oid AS oid) "
                "AND (acl.privilege_type NOT IN ('SELECT', 'REFERENCES') "
                "OR acl.is_grantable) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if extra_column_acl is not None:
        raise PluginRoleBoundaryError(BOUNDARY_DIRECT_ACL)

    membership = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_auth_members "
                "WHERE member = CAST(:oid AS oid) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if membership:
        raise PluginRoleBoundaryError(BOUNDARY_MEMBERSHIP)

    effective_write = (
        await conn.execute(
            text(
                "SELECT c.relname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "CROSS JOIN (VALUES ('INSERT'), ('UPDATE'), ('DELETE'), "
                "('TRUNCATE'), ('TRIGGER')) AS p(priv) "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                f"AND c.relname IN ({in_list}) "
                "AND has_table_privilege(CAST(:oid AS oid), c.oid, p.priv) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if effective_write is not None:
        raise PluginRoleBoundaryError(BOUNDARY_EFFECTIVE_WRITE)


async def _reconcile_role(conn, role_name: str, password: str) -> None:
    """CREATE-if-missing + idempotent ``ALTER ROLE`` reconciliation (D-07)."""
    quoted = _quote_identifier(role_name)
    if await _role_oid(conn, role_name) is None:
        await conn.execute(text(f"CREATE ROLE {quoted}"))

    # The password travels as a transaction-local GUC and is composed into SQL
    # only inside the server-side format() call — never a client SQL string.
    await conn.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": _ROLE_NAME_GUC, "value": role_name},
    )
    await conn.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": _ROLE_PASSWORD_GUC, "value": password},
    )
    await conn.execute(
        text(
            "DO $indramesh_dcl$ "
            "DECLARE r text := current_setting('" + _ROLE_NAME_GUC + "'); "
            "p text := current_setting('" + _ROLE_PASSWORD_GUC + "'); "
            "BEGIN EXECUTE format("
            "'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOBYPASSRLS PASSWORD %L', r, p); "
            "END $indramesh_dcl$;"
        )
    )


async def _grant_core_privileges(conn, role_name: str, core_tables: list[str]) -> None:
    """Grant the exact D-05/D-06 privilege set — additive, never to PUBLIC."""
    quoted = _quote_identifier(role_name)
    if core_tables:
        table_list = ", ".join(_quote_identifier(name) for name in core_tables)
        await conn.execute(
            text(f"GRANT SELECT, REFERENCES ON TABLE {table_list} TO {quoted}")
        )
    await conn.execute(text(f"GRANT USAGE, CREATE ON SCHEMA public TO {quoted}"))


async def bootstrap_plugin_role(
    conn,
    *,
    role_name: str,
    password: str,
    core_tables: Iterable[str] | None = None,
) -> None:
    """Idempotently create/reconcile the plugin migration role on ``conn``.

    ``conn`` is an existing privileged ``AsyncConnection`` already inside a
    transaction (D-16). ``core_tables`` is a test-only seam; production always
    passes ``None`` so the set comes from :func:`core_table_names`.
    """
    identity = validate_plugin_role_identifier(role_name)
    tables = sorted(core_table_names() if core_tables is None else core_tables)

    # D-07/D-13: refuse an already-drifted principal BEFORE issuing any DCL.
    await _assert_plugin_role_boundary(conn, identity, tables)

    try:
        await _reconcile_role(conn, identity, password)
        await _grant_core_privileges(conn, identity, tables)
    except PluginRoleBoundaryError:
        raise
    except Exception:
        # D-04: never leak the DCL/GRANT cause — it may embed the password.
        raise PluginRoleBootstrapError(BOOTSTRAP_FAILED) from None

    # Postcondition: the grants we just issued must not have widened anything.
    await _assert_plugin_role_boundary(conn, identity, tables)


async def verify_plugin_role_boundary(
    conn,
    *,
    role_name: str,
    core_tables: Iterable[str] | None = None,
) -> None:
    """Fail closed on privilege drift of a configured role, WITHOUT any DCL (WR-03).

    The deploy's separate bootstrap step performs the full reconcile, but every
    path that can execute a revision must refuse a drifted role before a child
    starts — a direct ``python -m app.plugin_system.migrate`` or the installer
    helper never ran the bootstrap in the same process. A not-yet-created role
    has no drift and returns immediately (the child then fails closed on
    connect). ``core_tables`` is a test-only seam, exactly as in
    :func:`bootstrap_plugin_role`.
    """
    identity = validate_plugin_role_identifier(role_name)
    tables = sorted(core_table_names() if core_tables is None else core_tables)
    await _assert_plugin_role_boundary(conn, identity, tables)


async def _bootstrap_with_application_connection(config: PluginRoleConfig) -> int:
    # D-08/DEPL-03: CREATE ROLE / ALTER ROLE need CREATEROLE, which the runtime
    # identity does not hold. The CLI therefore connects with the maintenance
    # (DB_USER) identity; only the function name is kept for compatibility.
    engine = create_async_engine(
        get_maintenance_database_url(), poolclass=NullPool, hide_parameters=True
    )
    try:
        async with engine.begin() as conn:
            await bootstrap_plugin_role(
                conn, role_name=config.role_name, password=config.password
            )
    except PluginRoleBoundaryError as exc:
        logger.error("Plugin database role boundary refusal: %s", exc.reason)
        return 1
    except (PluginRoleBootstrapError, PluginRoleConfigError):
        logger.error(BOOTSTRAP_FAILED)
        return 1
    except Exception:
        logger.error(BOOTSTRAP_FAILED)
        return 1
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    """Deploy entry point: ``python -m app.plugin_system.bootstrap_role``."""
    configure_logging("bootstrap-role")
    try:
        config = resolve_plugin_role_config()
    except PluginRoleConfigError as exc:
        logger.error("Plugin database role configuration is invalid: %s", exc)
        return 1

    if not config.configured:
        logger.info(PLUGIN_ROLE_SKIPPED)
        return 0

    return asyncio.run(_bootstrap_with_application_connection(config))


if __name__ == "__main__":
    sys.exit(main())
