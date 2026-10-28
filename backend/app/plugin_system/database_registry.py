"""Plugin table ownership inventory (PLUGDB-02, D-01/D-02).

The per-process analogue of ``task_registry.TaskTypeRegistry``: an exact-name map
from a declared plugin table (``plugin_<normalized-name>_*``) to the one plugin
that owns it, plus a parallel namespace for the plugin's declared Alembic
``version_table``. It never imports a concrete plugin and never touches the core
schema — the core table set is read from the ``app.models`` barrel's own
``Base.metadata``.

Validation is atomic and first-owner-wins: every declaration of one plugin is
checked before any entry is written, so a plugin with one mis-named, core-
colliding or cross-plugin-colliding declaration is rejected without becoming a
partially-registered owner. Re-adding the same owner is idempotent (reactivation
re-registers a plugin that was disabled at process startup); ``remove_owner()``
rolls a losing/failed owner back out of both namespaces.
"""

from dataclasses import dataclass

from app.plugin_system.base import IndraMeshPlugin
from app.plugin_system.manifest import plugin_table_prefix

# The core's own version table is never a plugin table name; the manifest
# grammar already rejects a plugin named ``alembic_version`` but the inventory
# additionally refuses any exact core-table collision via ``core_table_names()``.
CORE_VERSION_TABLE = "alembic_version"


class TableOwnershipError(ValueError):
    """A declared table/version table is already owned by another plugin (D-02)."""

    def __init__(self, name: str, owner: str) -> None:
        self.name = name
        self.owner = owner
        super().__init__(
            f"table {name!r} is already owned by plugin {owner!r}"
        )


@dataclass(frozen=True)
class TableOwnershipEntry:
    """A registered plugin table plus the name of the plugin that owns it."""

    table_name: str
    plugin_name: str


def core_table_names() -> set[str]:
    """The core schema's table names.

    ``app.models/__init__.py`` is the single source of truth for the core table
    set: importing the barrel registers every ORM model on ``Base.metadata``, so
    this inventory can never drift from a hand-maintained list. Imported
    function-locally to keep the plugin-system import graph free of a model-layer
    import at module load.
    """
    import app.models  # noqa: F401  (barrel import registers every core model)
    from app.db.base import Base

    return set(Base.metadata.tables)


def _owner_name(plugin: IndraMeshPlugin) -> str:
    return (
        plugin.manifest.name
        if plugin.manifest is not None
        else type(plugin).__name__
    )


class DatabaseTableRegistry:
    """An in-memory, per-process map of table name -> owning plugin."""

    def __init__(self) -> None:
        self._tables: dict[str, str] = {}
        self._version_tables: dict[str, str] = {}

    def add(self, plugin: IndraMeshPlugin) -> None:
        """Validate and register every declared table owned by ``plugin``.

        A plugin that declares no ``[database]`` section and exposes no metadata
        is an ordinary plugin and contributes nothing (a no-op, not an error).
        Metadata present without a manifest section — or the inverse — is
        refused, as is a plugin that declares zero tables or a table outside its
        own ``plugin_<normalized-name>_`` namespace.

        The plugin's declared ``version_table`` is claimed in a parallel
        namespace in the same atomic pass, so PLUGDB-01's "unique version table"
        is enforced rather than merely conventional. All checks complete before
        any entry is written; a losing owner keeps none of its entries.
        """
        owner = _owner_name(plugin)
        manifest = plugin.manifest
        metadata = getattr(plugin, "database_metadata", None)
        self.add_declaration(owner, manifest, metadata)

    def add_declaration(self, owner: str, manifest, metadata) -> None:
        """Validate and register a raw ``(owner, manifest, metadata)`` declaration.

        The seam shared by :meth:`add` (activation) and the deploy migration
        runner's pre-flight: the runner validates a discovered plugin's declared
        tables BEFORE any revision executes, so a plugin whose declaration
        violates the ownership/naming contract is refused without its migration
        ever running (CR-01).
        """
        declares_database = manifest is not None and manifest.database is not None

        if declares_database and metadata is None:
            raise ValueError(
                f"plugin {owner!r} declares a [database] section but exposes no "
                "database_metadata"
            )
        if not declares_database and metadata is not None:
            raise ValueError(
                f"plugin {owner!r} exposes database_metadata but declares no "
                "[database] section"
            )
        if not declares_database:
            return

        declared_tables = list(metadata.tables)
        if not declared_tables:
            raise ValueError(
                f"plugin {owner!r} declares a [database] section but zero tables"
            )

        prefix = plugin_table_prefix(owner)
        core_tables = core_table_names()
        version_table = manifest.database.version_table

        prepared: list[str] = []
        seen: set[str] = set()
        for table_name in declared_tables:
            if not isinstance(table_name, str) or not table_name:
                raise ValueError(
                    f"plugin {owner!r} declares an invalid table name {table_name!r}"
                )
            if not table_name.startswith(prefix):
                raise ValueError(
                    f"plugin {owner!r} declares table {table_name!r} outside its "
                    f"namespace (must start with {prefix!r}, PLUGDB-02)"
                )
            if table_name in core_tables:
                raise ValueError(
                    f"plugin {owner!r} declares table {table_name!r} which already "
                    "exists in the core schema (D-02)"
                )
            if table_name in seen:
                raise ValueError(
                    f"plugin {owner!r} declares table {table_name!r} more than once"
                )
            seen.add(table_name)
            existing = self._tables.get(table_name)
            if existing is not None and existing != owner:
                raise TableOwnershipError(table_name, existing)
            prepared.append(table_name)

        existing_version_owner = self._version_tables.get(version_table)
        if existing_version_owner is not None and existing_version_owner != owner:
            raise TableOwnershipError(version_table, existing_version_owner)

        self.remove_owner(owner)
        for table_name in prepared:
            self._tables[table_name] = owner
        self._version_tables[version_table] = owner

    def remove_owner(self, plugin_name: str) -> None:
        """Drop every table entry and version-table claim owned by ``plugin_name``."""
        for table_name in [
            name for name, owner in self._tables.items() if owner == plugin_name
        ]:
            del self._tables[table_name]
        for version_table in [
            name
            for name, owner in self._version_tables.items()
            if owner == plugin_name
        ]:
            del self._version_tables[version_table]

    def owners(self) -> dict[str, str]:
        """A copy of the current table name -> owning plugin map."""
        return dict(self._tables)

    def version_table_owners(self) -> dict[str, str]:
        """A copy of the current version table -> owning plugin map."""
        return dict(self._version_tables)
