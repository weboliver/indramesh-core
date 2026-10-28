"""Core Alembic autogenerate filters for plugin-owned tables (PLUGDB-06).

The core migration chain must never treat plugin-owned tables as unmanaged
reflected tables. Alembic excludes only the *configured* version table by exact
name (``alembic/autogenerate/compare/tables.py`` — it subtracts the pair
``(version_table_schema, version_table)``), so without an explicit filter a core
``alembic revision --autogenerate`` would emit ``op.drop_table`` for every
``plugin_<name>_*`` table and every per-plugin ``alembic_version_<name>`` table.

The prefix tuple is the phase's shared vocabulary: the plugin system's table
ownership contract (D-01/D-02) fixes ``plugin_`` as the plugin-owned table
namespace, and per-plugin version tables are named ``alembic_version_<name>``.

The predicate lives here — not inline in ``backend/alembic/env.py`` — because
``env.py`` is executed by Alembic's ``load_python_file`` and its module-level
``run_migrations_online()`` runs on import, so an inline definition cannot be
unit-tested. ``env.py`` imports this module (its ``prepend_sys_path = .`` puts the
backend package root on ``sys.path``).
"""

# Tables created at runtime by a component that is not the core schema.
# ``apscheduler_jobs`` is created by APScheduler itself on first start (see
# ``app/scheduler.py``); the core baseline does not create it and must not
# propose to drop it when autogenerate runs against a live database.
RUNTIME_OWNED_TABLES = ("apscheduler_jobs",)

# Prefixes owned by plugins rather than the core schema.
PLUGIN_OWNED_TABLE_PREFIXES = (
    # Plugin-owned data tables — the reference plugin creates
    # ``plugin_example_notes``.
    "plugin_",
    # Per-plugin Alembic version tables (``alembic_version_<normalized name>``).
    # The trailing underscore is load-bearing: the core's own version table is
    # named exactly ``alembic_version``, so
    # ``"alembic_version".startswith("alembic_version_")`` is ``False`` and the
    # core version table stays visible to autogenerate (RESEARCH Pitfall 9).
    "alembic_version_",
)


def include_object(obj, name, type_, reflected, compare_to):
    """Alembic ``IncludeObjectFn``: hide plugin-owned tables from core autogenerate.

    Returns ``False`` if and only if the compared object is a table whose name
    starts with one of :data:`PLUGIN_OWNED_TABLE_PREFIXES`; ``True`` for every
    other object — including the exact core ``alembic_version`` table and every
    non-table schema object (column, index, constraint, schema). Alembic composes
    object filters as an AND-subtraction (``AutogenContext.run_object_filters``
    returns ``False`` if ANY filter returns ``False``), so returning ``True``
    here cannot re-enable anything Alembic already excluded.

    This is deliberately NOT the Alembic cookbook's blanket
    ``reflected and compare_to is None`` variant, which would hide every
    unmanaged table, not just plugin-owned ones.
    """
    if type_ == "table" and isinstance(name, str):
        if name in RUNTIME_OWNED_TABLES:
            return False
        if name.startswith(PLUGIN_OWNED_TABLE_PREFIXES):
            return False
    return True
