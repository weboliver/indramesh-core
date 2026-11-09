"""Phase 322 (DEPL-02): the idempotent least-privilege deploy step.

``python -m app.db.roles`` runs as the maintenance identity (``DB_USER``) inside
the existing fatal deploy chain, after ``bootstrap_role`` and before the plugin
runner (D-09). On every run it reconciles:

* the DML-only application role (create only when missing, then attributes and
  password) — skipped entirely when ``DB_APP_USER`` is empty (D-04),
* the two ``SECURITY DEFINER`` keygen functions owned by ``DB_USER`` (D-01),
* ``apscheduler_jobs`` from APScheduler's own metadata, ``checkfirst=True``
  (D-07 — hand-written table DDL is forbidden),
* the blanket DML sweep plus the read-only migration-state exception (D-05,
  D-17) and the default privileges that cover objects created later (D-16),
* orphaned per-schema keygen sequences that no ``schemas`` row accounts for —
  best-effort housekeeping: a fixed warning, never a failed start,
* translations whose owner row is gone, for a fixed list of codes — best-effort
  housekeeping in the same way,
* a fail-closed post-check that refuses any privilege drift.

With ``DB_APP_USER`` empty the documented fallback applies: the role, its grants
and its default privileges are skipped, but the keygen functions and
``apscheduler_jobs`` are still installed — ``keygen_service`` no longer issues
any direct DDL, so a fallback install must still be able to create a schema.

No Alembic revision is created for any of this (D-10). Every statement is
``CREATE OR REPLACE`` / ``GRANT`` / ``REVOKE`` / ``ALTER DEFAULT PRIVILEGES``, so
a re-run converges; the only application data the step deletes is orphaned
translation rows, and it never rotates ``DB_USER``/``DB_PASSWORD``.

Diagnostics are fixed, secret-free strings: no SQL, no password and no raw
exception text ever reaches the log (T-322-04). The password travels as a
transaction-local GUC and is composed into SQL only inside a server-side
``format('%L')`` call — never a client-side string, never argv.
"""

import asyncio
import json
import logging
import os
import sys
import time
import uuid
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from app.db import get_maintenance_database_url
from app.logging import configure_logging
from app.plugin_system.bootstrap_role import (
    DEFAULT_PLUGIN_DB_USER,
    PLUGIN_DB_USER_ENV,
    PLUGIN_ROLE_IDENTIFIER_PATTERN,
    validate_plugin_role_identifier,
)
from app.services.select_option_service import option_ids_from_structure

logger = logging.getLogger(__name__)

# D-04: DB_APP_USER is not a secret and follows the DB_* convention; the
# password is a secret and is fail-closed on absence/empty/placeholder.
APP_ROLE_USER_ENV = "DB_APP_USER"
APP_ROLE_PASSWORD_ENV = "DB_APP_PASSWORD"
DB_USER_ENV = "DB_USER"

# The installer placeholder contract (scripts/lib/secrets.sh). A value that
# survived the fill must abort the deploy, never become a live credential.
_SECRET_PLACEHOLDER = "CHANGE_ME"

# Fixed, secret-free diagnostics (T-322-04). None carries SQL, a password or a
# raw exception string.
APP_ROLE_SKIPPED = (
    "Application database role is not configured (DB_APP_USER absent) — "
    "skipping role creation, grants and default privileges."
)
APP_ROLE_FALLBACK_WARNING = (
    "Application database role is not configured (DB_APP_USER absent) — the "
    "runtime process keeps the maintenance DB_USER identity; least-privilege "
    "database roles are not in effect."
)
APP_ROLE_FAILED = "Application database role deploy step failed."
APP_ROLE_CONFIG_INVALID_CREDENTIALS = (
    "DB_APP_USER is set but DB_APP_PASSWORD is missing, empty or still the "
    "installer placeholder; configure both."
)
APP_ROLE_CONFIG_INVALID_IDENTIFIER = (
    "DB_APP_USER must be a valid PostgreSQL identifier "
    f"({PLUGIN_ROLE_IDENTIFIER_PATTERN})."
)
APP_ROLE_CONFIG_IDENTITY_COLLISION = (
    "DB_APP_USER must differ from DB_USER and from the plugin database role."
)
APP_ROLE_PRIVILEGE_DRIFT = "Application database role privilege drift detected."

# 322.1: housekeeping for per-schema keygen sequences. A sequence is tied to its
# schema by name only. The product's delete paths drop their sequences themselves;
# manual DELETEs and the test harness's SQL cleanup leave sequences behind. The
# cleanup is best-effort: unlike the role step it never fails the deploy (D-04
# discipline of Phase 322.1).
ORPHANED_SEQUENCE_BATCH_SIZE = 500
ORPHANED_SEQUENCE_NAME_PATTERN = r"^keygen_seq_[0-9a-f]{32}$"
# Transaction-local lock timeout for one page: a DROP SEQUENCE waiting on a lock
# held by another session must not stall the fatal deploy chain indefinitely.
ORPHANED_SEQUENCE_LOCK_TIMEOUT = "5s"
# Safety valve for the paging loop: the loop ends on an empty page, and this cap
# only exists so a page query that stops making progress cannot spin forever.
ORPHANED_SEQUENCE_MAX_PAGES = 10000
ORPHANED_SEQUENCE_CLEANUP_FAILED = (
    "Orphaned keygen sequence cleanup failed; continuing the deploy step."
)
ORPHANED_SEQUENCE_CLEANUP_DROPPED = "Dropped %d orphaned keygen sequence(s)."

# Housekeeping for translations. `translations.entity_id` is polymorphic and has
# no foreign key, so a row can outlive its owner. Only the codes in this mapping
# are ever visited: `systemui` and any unknown code are absent on purpose.
# select_option has no owner table and takes its own path below.
ORPHANED_TRANSLATION_OWNER_TABLES = {
    "classification_label": "classification_labels",
    "object_type": "object_types",
    "object_type_attribute": "object_type_attributes",
    "object_value": "object_values",
    "reference_type": "reference_types",
    "status": "statuses",
}
ORPHANED_TRANSLATION_BATCH_SIZE = 500
# Transaction-local, per page: a lock or a slow statement ends the sweep instead
# of stalling the fatal deploy chain.
ORPHANED_TRANSLATION_LOCK_TIMEOUT = "5s"
ORPHANED_TRANSLATION_STATEMENT_TIMEOUT = "30s"
# Well below the k3s Job's 300 s deadline; the next deploy step continues.
ORPHANED_TRANSLATION_BUDGET_SECONDS = 60
ORPHANED_TRANSLATION_CLEANUP_FAILED = (
    "Orphaned translation cleanup failed; continuing the deploy step."
)
ORPHANED_TRANSLATION_CLEANUP_DELETED = "Deleted %d orphaned translation(s)."
ORPHANED_OPTION_TRANSLATIONS_SKIPPED = (
    "Attribute definitions could not be read completely; skipping select option "
    "translations in this run."
)
ORPHANED_TRANSLATION_BUDGET_REACHED = (
    "Orphaned translation cleanup reached its time budget; the next deploy step "
    "continues."
)

# One statement per page: a translation commits with or after its owner, so one
# snapshot that sees the translation also sees the owner. No OFFSET, because
# deleted rows leave the result set.
_DELETE_ORPHANED_TRANSLATIONS_SQL = """
DELETE FROM public.translations
WHERE id IN (
    SELECT t.id
    FROM public.translations t
    JOIN public.translation_types tt ON tt.id = t.translation_type_id
    WHERE tt.code = :code
      AND NOT EXISTS (
          SELECT 1 FROM public.{owner_table} owner_row WHERE owner_row.id = t.entity_id
      )
    LIMIT :batch_size
)
RETURNING id
"""

_SELECT_OPTION_CANDIDATES_SQL = """
SELECT t.id, t.entity_id
FROM public.translations t
JOIN public.translation_types tt ON tt.id = t.translation_type_id
WHERE tt.code = 'select_option'
"""

# `structure` is a json column, and a bare text() query hands it back as a str that
# option_ids_from_structure would silently read as "no options". Every option
# translation would then look orphaned, so the read is decoded explicitly.
_SELECT_ATTRIBUTE_STRUCTURES_SQL = """
SELECT structure::text
FROM public.object_type_attributes
WHERE structure IS NOT NULL
"""

_DELETE_TRANSLATIONS_BY_ID_SQL = """
DELETE FROM public.translations
WHERE id = ANY(CAST(:ids AS uuid[]))
RETURNING id
"""

# One statement finds a page of orphans. The single statement is the correctness
# argument: `create_schema` commits a schema row and its sequence in the same
# transaction, so one snapshot sees both or neither and a schema created while
# the cleanup runs is never reported as an orphan. `ORDER BY` keeps the paging
# deterministic and there is deliberately no OFFSET — dropped rows leave the
# result set, so every page starts at the top again.
_SELECT_ORPHANED_SEQUENCES_SQL = """
SELECT c.relname
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE n.nspname = 'public'
  AND c.relkind = 'S'
  AND c.relname ~ :pattern
  AND NOT EXISTS (
    SELECT 1 FROM public.schemas s
    WHERE c.relname = 'keygen_seq_' || replace(s.id::text, '-', '')
  )
ORDER BY c.relname
LIMIT :batch_size
"""

# The password is never interpolated into a client-side SQL string. `ALTER ROLE
# ... PASSWORD` accepts no bind parameters, so both values are handed to the
# server as transaction-local GUCs and composed only inside PL/pgSQL `format()`.
_APP_ROLE_GUC = "indramesh.app_role"
_APP_PASSWORD_GUC = "indramesh.app_password"

_KEYGEN_CREATE_SIGNATURE = "public.indramesh_keygen_sequence_create(uuid)"
_KEYGEN_DROP_SIGNATURE = "public.indramesh_keygen_sequence_drop(uuid)"

# D-01: `CREATE OR REPLACE` on every run. The parameter is a uuid (never free
# text) because the function runs with the owner's rights; the sequence name is
# always qualified with `public.` because `SET search_path = pg_catalog, public`
# would otherwise land an unqualified name in pg_catalog and fail.
_KEYGEN_CREATE_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.indramesh_keygen_sequence_create(schema_uuid uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $indramesh_keygen_create$
BEGIN
  EXECUTE format(
    'CREATE SEQUENCE IF NOT EXISTS public.%I',
    'keygen_seq_' || replace(schema_uuid::text, '-', '')
  );
END
$indramesh_keygen_create$
"""

_KEYGEN_DROP_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.indramesh_keygen_sequence_drop(schema_uuid uuid)
RETURNS void
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, public
AS $indramesh_keygen_drop$
BEGIN
  EXECUTE format(
    'DROP SEQUENCE IF EXISTS public.%I',
    'keygen_seq_' || replace(schema_uuid::text, '-', '')
  );
END
$indramesh_keygen_drop$
"""


class AppRoleConfigError(RuntimeError):
    """A fatal, fail-closed application-role credential misconfiguration."""


class AppRolePrivilegeError(RuntimeError):
    """Privilege drift found after the deploy statements ran (D-06)."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class AppRoleConfig:
    """Resolved application-role credentials from the environment."""

    configured: bool
    role_name: str | None
    password: str | None


def _env_value(environ, key: str) -> str | None:
    value = environ.get(key)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _is_placeholder(value: str) -> bool:
    """Recognise the installer placeholder, including quoted/CRLF variants."""
    return value.strip().strip('"').strip("'").strip() == _SECRET_PLACEHOLDER


def validate_app_role_identifier(name: str) -> str:
    """Return ``name`` when it is a safe PostgreSQL identifier, else raise.

    Reuses the plugin-role identifier contract rather than inventing a second
    one: the resolved name is interpolated as a quoted identifier.
    """
    try:
        return validate_plugin_role_identifier(name)
    except Exception:
        raise AppRoleConfigError(APP_ROLE_CONFIG_INVALID_IDENTIFIER) from None


def _resolved_plugin_role_name(environ=None) -> str:
    env = os.environ if environ is None else environ
    return _env_value(env, PLUGIN_DB_USER_ENV) or DEFAULT_PLUGIN_DB_USER


def resolve_app_role_config(environ=None) -> AppRoleConfig:
    """Resolve the D-04 credential matrix into an :class:`AppRoleConfig`.

    - ``DB_APP_USER`` absent/empty -> unconfigured (the documented fallback)
    - ``DB_APP_USER`` set without a usable password -> fatal
    - an invalid identifier -> fatal
    - ``DB_APP_USER`` equal to ``DB_USER`` or to the plugin role -> fatal

    The last rule matters: without it the reconciliation below would run
    ``ALTER ROLE ... NOSUPERUSER ... PASSWORD ...`` on the administrator's own
    role or on the plugin role.
    """
    env = os.environ if environ is None else environ
    user = _env_value(env, APP_ROLE_USER_ENV)
    password = _env_value(env, APP_ROLE_PASSWORD_ENV)

    if user is None:
        return AppRoleConfig(configured=False, role_name=None, password=None)

    validate_app_role_identifier(user)

    if password is None or _is_placeholder(password):
        raise AppRoleConfigError(APP_ROLE_CONFIG_INVALID_CREDENTIALS)

    maintenance_user = _env_value(env, DB_USER_ENV) or "postgres"
    plugin_user = _resolved_plugin_role_name(env)
    if user in (maintenance_user, plugin_user):
        raise AppRoleConfigError(APP_ROLE_CONFIG_IDENTITY_COLLISION)

    return AppRoleConfig(configured=True, role_name=user, password=password)


def _quote_identifier(value: str) -> str:
    """Quote an identifier exactly like ``bootstrap_role`` does."""
    return '"' + value.replace('"', '""') + '"'


async def _role_oid(conn, role_name: str):
    return (
        await conn.execute(
            text("SELECT oid FROM pg_roles WHERE rolname = :name"),
            {"name": role_name},
        )
    ).scalar()


async def install_keygen_functions(conn) -> None:
    """Create-or-replace both keygen definer functions and revoke PUBLIC (D-01).

    Runs on every deploy, configured or not: the fallback install must be able
    to create a schema, and ``keygen_service`` holds no direct DDL any more.
    """
    await conn.execute(text(_KEYGEN_CREATE_FUNCTION_SQL))
    await conn.execute(text(_KEYGEN_DROP_FUNCTION_SQL))
    await conn.execute(
        text(f"REVOKE ALL ON FUNCTION {_KEYGEN_CREATE_SIGNATURE} FROM PUBLIC")
    )
    await conn.execute(
        text(f"REVOKE ALL ON FUNCTION {_KEYGEN_DROP_SIGNATURE} FROM PUBLIC")
    )


async def _reconcile_role(conn, role_name: str, password: str) -> None:
    """CREATE-if-missing, then reconcile attributes and password every run."""
    quoted = _quote_identifier(role_name)
    if await _role_oid(conn, role_name) is None:
        await conn.execute(text(f"CREATE ROLE {quoted}"))

    await conn.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": _APP_ROLE_GUC, "value": role_name},
    )
    await conn.execute(
        text("SELECT set_config(:key, :value, true)"),
        {"key": _APP_PASSWORD_GUC, "value": password},
    )
    await conn.execute(
        text(
            "DO $indramesh_app_role$ "
            "DECLARE r text := current_setting('" + _APP_ROLE_GUC + "'); "
            "p text := current_setting('" + _APP_PASSWORD_GUC + "'); "
            "BEGIN EXECUTE format("
            "'ALTER ROLE %I WITH LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
            "NOREPLICATION NOBYPASSRLS PASSWORD %L', r, p); "
            "END $indramesh_app_role$;"
        )
    )


async def _grant_dml_surface(conn, role_name: str) -> None:
    """Grant exactly the D-05/D-06 surface — nothing more, never to PUBLIC."""
    quoted = _quote_identifier(role_name)
    await conn.execute(text(f"GRANT USAGE ON SCHEMA public TO {quoted}"))
    await conn.execute(
        text(
            "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
            f"TO {quoted}"
        )
    )
    await conn.execute(
        text(f"GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {quoted}")
    )
    await conn.execute(
        text(f"GRANT EXECUTE ON FUNCTION {_KEYGEN_CREATE_SIGNATURE} TO {quoted}")
    )
    await conn.execute(
        text(f"GRANT EXECUTE ON FUNCTION {_KEYGEN_DROP_SIGNATURE} TO {quoted}")
    )


async def _set_default_privileges(conn) -> None:
    """Cover objects the maintenance identity creates after this run (D-16).

    The table default covers the weakened plugin path: without a plugin role the
    plugin runner migrates in-process as ``DB_USER`` right after the sweep and
    ``_grant_runtime_dml`` never runs.
    """
    await conn.execute(
        text(
            "DO $indramesh_default_privileges$ "
            "BEGIN "
            "EXECUTE format("
            "'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public "
            "GRANT USAGE, SELECT ON SEQUENCES TO %I', "
            "current_user, current_setting('" + _APP_ROLE_GUC + "')); "
            "EXECUTE format("
            "'ALTER DEFAULT PRIVILEGES FOR ROLE %I IN SCHEMA public "
            "GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO %I', "
            "current_user, current_setting('" + _APP_ROLE_GUC + "')); "
            "END $indramesh_default_privileges$;"
        )
    )


async def _revoke_version_table_writes(conn) -> None:
    """Keep the migration state read-only for the application role (D-17).

    Without this a SQL injection could falsify the Core or a plugin migration
    state. The plugin-role default privileges would otherwise re-grant DML on a
    version table created after the sweep, so this runs after the sweep on every
    deploy.
    """
    await conn.execute(
        text(
            "DO $indramesh_version_tables$ "
            "DECLARE t record; r text := current_setting('" + _APP_ROLE_GUC + "'); "
            "BEGIN "
            "FOR t IN SELECT c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
            "AND c.relname LIKE 'alembic\\_version%' "
            "LOOP EXECUTE format("
            "'REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON public.%I FROM %I', "
            "t.relname, r); END LOOP; "
            "END $indramesh_version_tables$;"
        )
    )


async def _set_plugin_default_privileges(conn, role_name: str) -> None:
    """Cover plugin tables created later by the plugin role (D-05).

    Skipped when no role named by ``PLUGIN_DB_USER`` exists: the fallback
    install has no plugin role and must not fail on that. The name comes from
    configuration, never from a hard-wired literal.
    """
    plugin_role = _resolved_plugin_role_name()
    exists = (
        await conn.execute(
            text("SELECT 1 FROM pg_roles WHERE rolname = :name"),
            {"name": plugin_role},
        )
    ).scalar()
    if not exists:
        return

    plugin_quoted = _quote_identifier(plugin_role)
    app_quoted = _quote_identifier(role_name)
    await conn.execute(
        text(
            "ALTER DEFAULT PRIVILEGES FOR ROLE "
            f"{plugin_quoted} IN SCHEMA public "
            f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {app_quoted}"
        )
    )
    await conn.execute(
        text(
            "ALTER DEFAULT PRIVILEGES FOR ROLE "
            f"{plugin_quoted} IN SCHEMA public "
            f"GRANT USAGE, SELECT ON SEQUENCES TO {app_quoted}"
        )
    )


async def apply_app_role(conn, *, role_name: str, password: str) -> None:
    """Idempotently reconcile the application role on a privileged connection.

    ``conn`` is an existing ``AsyncConnection`` already inside a transaction.
    """
    identity = validate_app_role_identifier(role_name)

    await _reconcile_role(conn, identity, password)
    await install_keygen_functions(conn)
    await _grant_dml_surface(conn, identity)
    await _set_default_privileges(conn)
    await _revoke_version_table_writes(conn)
    await _set_plugin_default_privileges(conn, identity)


async def verify_app_role(conn, *, role_name: str) -> None:
    """Fail closed on any privilege drift of the application role (D-06/D-17).

    Checks: the role exists, all five escalation attributes are false, it is a
    member of no role, owns no relation in ``public``, lacks ``CREATE`` on
    ``public``, holds ``SELECT`` on every table, holds DML on every non-version
    table, holds no write privilege on any version table, and the two keygen
    functions are ``SECURITY DEFINER``, owned by the maintenance identity, with
    ``EXECUTE`` for the application role and nothing for ``PUBLIC``.
    """
    identity = validate_app_role_identifier(role_name)
    oid = await _role_oid(conn, identity)
    if oid is None:
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    attributes = (
        await conn.execute(
            text(
                "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, "
                "rolbypassrls FROM pg_roles WHERE rolname = :name"
            ),
            {"name": identity},
        )
    ).one()
    if any(attributes):
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

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
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    owned_relation = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' "
                "AND c.relowner = CAST(:oid AS oid) LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if owned_relation:
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    can_create = (
        await conn.execute(
            text(
                "SELECT has_schema_privilege(CAST(:oid AS oid), 'public', 'CREATE')"
            ),
            {"oid": oid},
        )
    ).scalar()
    if can_create:
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    missing_select = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND NOT has_table_privilege(CAST(:oid AS oid), c.oid, 'SELECT') "
                "LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if missing_select:
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    missing_write = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND c.relname NOT LIKE 'alembic\\_version%' "
                "AND NOT (has_table_privilege(CAST(:oid AS oid), c.oid, 'INSERT') "
                "AND has_table_privilege(CAST(:oid AS oid), c.oid, 'UPDATE') "
                "AND has_table_privilege(CAST(:oid AS oid), c.oid, 'DELETE')) "
                "LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if missing_write:
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    version_write = (
        await conn.execute(
            text(
                "SELECT 1 FROM pg_class c "
                "JOIN pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = 'public' AND c.relkind IN ('r', 'p') "
                "AND c.relname LIKE 'alembic\\_version%' "
                "AND (has_table_privilege(CAST(:oid AS oid), c.oid, 'INSERT') "
                "OR has_table_privilege(CAST(:oid AS oid), c.oid, 'UPDATE') "
                "OR has_table_privilege(CAST(:oid AS oid), c.oid, 'DELETE') "
                "OR has_table_privilege(CAST(:oid AS oid), c.oid, 'TRUNCATE')) "
                "LIMIT 1"
            ),
            {"oid": oid},
        )
    ).scalar()
    if version_write:
        raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

    current_identity = (await conn.execute(text("SELECT current_user"))).scalar()
    for signature in (_KEYGEN_CREATE_SIGNATURE, _KEYGEN_DROP_SIGNATURE):
        function_row = (
            await conn.execute(
                text(
                    "SELECT p.prosecdef, pg_get_userbyid(p.proowner) "
                    "FROM pg_proc p WHERE p.oid = CAST(:sig AS regprocedure)"
                ),
                {"sig": signature},
            )
        ).one_or_none()
        if (
            function_row is None
            or not function_row[0]
            or function_row[1] != current_identity
        ):
            raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

        can_execute = (
            await conn.execute(
                text(
                    "SELECT has_function_privilege(CAST(:oid AS oid), :sig, 'EXECUTE')"
                ),
                {"oid": oid, "sig": signature},
            )
        ).scalar()
        if not can_execute:
            raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)

        public_acl = (
            await conn.execute(
                text(
                    "SELECT 1 FROM pg_proc p "
                    "CROSS JOIN LATERAL aclexplode(p.proacl) AS acl "
                    "WHERE p.oid = CAST(:sig AS regprocedure) "
                    "AND acl.grantee = 0 LIMIT 1"
                ),
                {"sig": signature},
            )
        ).scalar()
        if public_acl:
            raise AppRolePrivilegeError(APP_ROLE_PRIVILEGE_DRIFT)


async def _drop_orphaned_sequences_page(engine) -> list[str]:
    """Drop one page of orphaned keygen sequences and commit it.

    One ``engine.begin()`` per page is the transaction boundary: it keeps each
    page far below the shared lock table's capacity, which a single transaction
    over the whole backlog would exhaust. The transaction-local ``lock_timeout``
    covers exactly this page, so a lock held elsewhere aborts it instead of
    stalling the deploy chain. Names come from the catalog and are quoted; the
    page size is read from the module global at call time.
    """
    async with engine.begin() as conn:
        await conn.execute(
            text("SELECT set_config('lock_timeout', :timeout, true)"),
            {"timeout": ORPHANED_SEQUENCE_LOCK_TIMEOUT},
        )
        result = await conn.execute(
            text(_SELECT_ORPHANED_SEQUENCES_SQL),
            {
                "pattern": ORPHANED_SEQUENCE_NAME_PATTERN,
                "batch_size": ORPHANED_SEQUENCE_BATCH_SIZE,
            },
        )
        names = [row[0] for row in result.all()]
        for name in names:
            await conn.execute(
                text(f"DROP SEQUENCE IF EXISTS public.{_quote_identifier(name)}")
            )
    return names


async def drop_orphaned_keygen_sequences() -> int:
    """Drop every orphaned keygen sequence, page by page, and never raise.

    Housekeeping, not a security step: an absent ``schemas`` table, a sequence
    owned by another role or any other error logs the fixed warning and stops
    the loop (stopping is mandatory — otherwise the loop would fetch the same
    rows forever). The dropped count is always logged, on success and on
    failure.

    The engine is built INSIDE the try and disposed defensively: a failure to
    connect must produce the fixed warning and a normal return, never an
    exception that would abort the deploy chain under ``set -e``.
    """
    engine = None
    dropped = 0
    pages = 0
    try:
        engine = create_async_engine(
            get_maintenance_database_url(), poolclass=NullPool, hide_parameters=True
        )
        while pages < ORPHANED_SEQUENCE_MAX_PAGES:
            pages += 1
            try:
                names = await _drop_orphaned_sequences_page(engine)
            except Exception:
                logger.warning(ORPHANED_SEQUENCE_CLEANUP_FAILED)
                break
            if not names:
                break
            dropped += len(names)
        else:
            # Safety valve: the loop is expected to end on an empty page. A cap
            # that is hit means the page query no longer makes progress; stop
            # with the same fixed warning rather than spin forever.
            logger.warning(ORPHANED_SEQUENCE_CLEANUP_FAILED)
    except Exception:
        logger.warning(ORPHANED_SEQUENCE_CLEANUP_FAILED)
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                logger.warning(ORPHANED_SEQUENCE_CLEANUP_FAILED)
    logger.info(ORPHANED_SEQUENCE_CLEANUP_DROPPED, dropped)
    return dropped


class _TranslationSweepBudgetReached(Exception):
    pass


def _check_translation_sweep_budget(deadline: float) -> None:
    if time.monotonic() >= deadline:
        raise _TranslationSweepBudgetReached


async def _set_translation_sweep_timeouts(conn) -> None:
    await conn.execute(
        text("SELECT set_config('lock_timeout', :timeout, true)"),
        {"timeout": ORPHANED_TRANSLATION_LOCK_TIMEOUT},
    )
    await conn.execute(
        text("SELECT set_config('statement_timeout', :timeout, true)"),
        {"timeout": ORPHANED_TRANSLATION_STATEMENT_TIMEOUT},
    )


async def _delete_orphaned_translations_page(
    engine, code: str, owner_table: str
) -> list[uuid.UUID]:
    """Delete one page of one code's orphans and commit it.

    One ``engine.begin()`` per page is the transaction boundary. The owner table
    comes only from ``ORPHANED_TRANSLATION_OWNER_TABLES``.
    """
    statement = _DELETE_ORPHANED_TRANSLATIONS_SQL.format(
        owner_table=_quote_identifier(owner_table)
    )
    async with engine.begin() as conn:
        await _set_translation_sweep_timeouts(conn)
        result = await conn.execute(
            text(statement),
            {"code": code, "batch_size": ORPHANED_TRANSLATION_BATCH_SIZE},
        )
        return [row[0] for row in result.all()]


async def _read_select_option_candidates(engine) -> list[tuple[uuid.UUID, uuid.UUID]]:
    """``(id, entity_id)`` of every select_option translation, in its own transaction."""
    async with engine.begin() as conn:
        await _set_translation_sweep_timeouts(conn)
        result = await conn.execute(text(_SELECT_OPTION_CANDIDATES_SQL))
        return [(row[0], row[1]) for row in result.all()]


def _parse_live_option_ids(structures) -> set[uuid.UUID]:
    """Option ids of the given JSON texts; anything unexpected raises.

    A partial answer would make live options look orphaned, so the caller skips
    the whole path on any exception.
    """
    live_ids: set[uuid.UUID] = set()
    for raw in structures:
        structure = json.loads(raw)
        if structure is None:
            continue
        if not isinstance(structure, dict):
            raise ValueError("attribute structure is not an object")
        options = structure.get("options")
        if options is not None and not isinstance(options, list):
            raise ValueError("attribute options are not a list")
        live_ids.update(option_ids_from_structure(structure))
    return live_ids


async def _read_live_option_ids(engine) -> set[uuid.UUID]:
    """Every option id in a stored attribute structure, in its own transaction."""
    async with engine.begin() as conn:
        await _set_translation_sweep_timeouts(conn)
        result = await conn.execute(text(_SELECT_ATTRIBUTE_STRUCTURES_SQL))
        return _parse_live_option_ids(row[0] for row in result.all())


async def _delete_translations_page(engine, ids: list[uuid.UUID]) -> list[uuid.UUID]:
    """Delete translations by primary key, one transaction per page."""
    async with engine.begin() as conn:
        await _set_translation_sweep_timeouts(conn)
        result = await conn.execute(text(_DELETE_TRANSLATIONS_BY_ID_SQL), {"ids": ids})
        return [row[0] for row in result.all()]


async def delete_orphaned_translations() -> int:
    """Delete translations whose owner row is gone, page by page, and never raise.

    select_option has no owner table, so the order carries the correctness
    argument. A translation can only be written once its option is committed, so
    every candidate's option is visible to the later live read unless it was
    really removed. A translation committed after the candidate read is not in the
    list, and the delete goes by translation id, so it cannot be hit. Any doubt
    about the live read skips the path.

    The first failed page ends the sweep. The deleted count is always logged. The
    engine is built inside the try, as in ``drop_orphaned_keygen_sequences``.
    """
    engine = None
    deleted = 0
    deadline = time.monotonic() + ORPHANED_TRANSLATION_BUDGET_SECONDS
    try:
        engine = create_async_engine(
            get_maintenance_database_url(), poolclass=NullPool, hide_parameters=True
        )
        for code, owner_table in ORPHANED_TRANSLATION_OWNER_TABLES.items():
            while True:
                _check_translation_sweep_budget(deadline)
                ids = await _delete_orphaned_translations_page(engine, code, owner_table)
                if not ids:
                    break
                deleted += len(ids)

        try:
            candidates = await _read_select_option_candidates(engine)
            live_ids = await _read_live_option_ids(engine)
        except Exception:
            logger.warning(ORPHANED_OPTION_TRANSLATIONS_SKIPPED)
        else:
            orphaned = [
                translation_id
                for translation_id, entity_id in candidates
                if entity_id not in live_ids
            ]
            batch_size = ORPHANED_TRANSLATION_BATCH_SIZE
            for start in range(0, len(orphaned), batch_size):
                _check_translation_sweep_budget(deadline)
                deleted += len(
                    await _delete_translations_page(
                        engine, orphaned[start:start + batch_size]
                    )
                )
    except _TranslationSweepBudgetReached:
        logger.warning(ORPHANED_TRANSLATION_BUDGET_REACHED)
    except Exception:
        logger.warning(ORPHANED_TRANSLATION_CLEANUP_FAILED)
    finally:
        if engine is not None:
            try:
                await engine.dispose()
            except Exception:
                logger.warning(ORPHANED_TRANSLATION_CLEANUP_FAILED)
    logger.info(ORPHANED_TRANSLATION_CLEANUP_DELETED, deleted)
    return deleted


def ensure_scheduler_table(sync_engine) -> None:
    """Create ``apscheduler_jobs`` from APScheduler's own metadata (D-07).

    Hand-written table DDL is forbidden: the library's schema is the contract.
    After this run the worker's own ``checkfirst`` create is a no-op.
    """
    from apscheduler.jobstores.sqlalchemy import SQLAlchemyJobStore

    jobstore = SQLAlchemyJobStore(engine=sync_engine)
    jobstore.jobs_t.create(sync_engine, checkfirst=True)


def _create_sync_maintenance_engine():
    from sqlalchemy import create_engine

    sync_url = get_maintenance_database_url().replace("+asyncpg", "")
    return create_engine(sync_url, poolclass=NullPool, hide_parameters=True)


async def _run_deploy_step(config: AppRoleConfig) -> int:
    engine = create_async_engine(
        get_maintenance_database_url(), poolclass=NullPool, hide_parameters=True
    )
    try:
        async with engine.begin() as conn:
            # Always installed: the fallback install still needs schema creation.
            await install_keygen_functions(conn)
            if not config.configured:
                logger.info(APP_ROLE_SKIPPED)
                logger.warning(APP_ROLE_FALLBACK_WARNING)
                return 0
            await apply_app_role(
                conn, role_name=config.role_name, password=config.password
            )
            await verify_app_role(conn, role_name=config.role_name)
    except AppRolePrivilegeError:
        logger.error(APP_ROLE_PRIVILEGE_DRIFT)
        return 1
    except AppRoleConfigError as exc:
        logger.error("%s", exc)
        return 1
    except Exception:
        # D-04/T-322-04: never leak the DCL/GRANT cause — it may embed the password.
        logger.error(APP_ROLE_FAILED)
        return 1
    finally:
        await engine.dispose()
    return 0


def main() -> int:
    """Deploy entry point: ``python -m app.db.roles``."""
    configure_logging("db-roles")
    try:
        config = resolve_app_role_config()
    except AppRoleConfigError as exc:
        logger.error("%s", exc)
        return 1

    try:
        sync_engine = _create_sync_maintenance_engine()
        try:
            ensure_scheduler_table(sync_engine)
        finally:
            sync_engine.dispose()
    except Exception:
        logger.error(APP_ROLE_FAILED)
        return 1

    # 322.1: housekeeping before the role work. Its own per-page transactions
    # (the role step holds one), and before the grant sweep so no grant lands on
    # a sequence that is about to go. A failure is a warning, never a non-zero
    # exit — the coroutine already guarantees that, and this guard is the
    # structural backstop at the call site (a patch, an import-time surprise or a
    # future edit must not be able to abort the deploy chain under `set -e`).
    try:
        asyncio.run(drop_orphaned_keygen_sequences())
    except Exception:
        logger.warning(ORPHANED_SEQUENCE_CLEANUP_FAILED)

    # Same guard, same reason: the sweep never fails the deploy chain.
    try:
        asyncio.run(delete_orphaned_translations())
    except Exception:
        logger.warning(ORPHANED_TRANSLATION_CLEANUP_FAILED)

    return asyncio.run(_run_deploy_step(config))


if __name__ == "__main__":
    sys.exit(main())
