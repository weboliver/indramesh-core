"""Per-process connector-type registry (CORESEAM-01, v20 Phase 289).

``Source.type`` and ``Target.connector_type`` are registry-validated lowercase
strings, not a closed enum. Core and active plugins declare connector
identities here as immutable descriptors; the Source/Target **write** paths
normalize and resolve against this registry, while every **read** path
(list/get/cache/GraphQL) serializes the persisted string verbatim — a row whose
connector type is unknown, or belongs to a plugin that is not active, must stay
readable and listable.

Deliberately a leaf module: it imports no other ``app.*`` module, so
``app.schemas.crud`` can depend on it without creating an import cycle through
the ``app.plugin_system`` package initializer (which imports the lifecycle
registry, ``PluginContext`` and — transitively — ``app.schemas.crud``). The
declaration/entry/registry shape mirrors ``task_registry.py``: immutable
declarations, atomic per-owner registration, collision on a foreign owner.

Connector identifiers are NOT plugin-qualified (unlike task types), so a
persisted ``jira`` row stays valid while the Jira plugin owns that identifier
(CLEAN-01/D-03). Built-ins are owned by ``core``; a plugin may not shadow one
(its registration collides and the plugin is isolated).
"""

import re
from dataclasses import dataclass
from typing import Any, Iterable

from pydantic import BaseModel, ValidationError

# Write identity grammar (CORESEAM-01): the exact Python string after
# ``strip().lower()`` must match this lowercase ASCII identifier form. Reads
# never re-validate a persisted string against it.
CONNECTOR_IDENTITY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,49}$")

# The entity kinds every descriptor declares applicability for.
CONNECTOR_ENTITY_KINDS = frozenset({"source", "target"})

# The owner reserved for Core's own built-in connector descriptors.
CORE_OWNER = "core"


class ConnectorTypeCollisionError(ValueError):
    """A declaration's identifier is already owned by another owner."""

    def __init__(self, name: str, owner: str) -> None:
        self.name = name
        self.owner = owner
        super().__init__(
            f"connector type {name!r} is already declared by owner {owner!r}"
        )


class ConnectorTypeValidationError(ValueError):
    """A connector declaration or a write-supplied connector identity is invalid."""


class CsvConnectorConfig(BaseModel):
    """The CSV connector's config contract.

    ``file_path`` moved to ``Target.export_folder`` (a dedicated, DB-unique
    column) — it stays accepted-but-ignored here so old clients sending it do
    not get a validation error during the transition.
    """

    delimiter: str = ","
    encoding: str = "utf-8"


def _config_error_fields(exc: ValidationError) -> str:
    """Name only the offending config fields — never their values (T-289-01-03)."""
    names = []
    for error in exc.errors():
        location = ".".join(str(part) for part in error.get("loc", ()))
        names.append(location or "<root>")
    return ", ".join(names)


@dataclass(frozen=True)
class ConnectorTypeDeclaration:
    """One immutable connector identity (CORESEAM-01).

    ``name`` is the exact persisted lowercase identifier. ``applies_to`` is the
    subset of ``{"source", "target"}`` this connector may be written for. The
    ``requires_*`` fields are each the set of entity kinds for which that core
    field is mandatory (e.g. ``jira`` requires a URL for ``target`` writes).
    ``config_model`` is the connector-owned config contract; when
    ``normalize_config`` is set, the validated defaults are written back (CSV's
    delimiter/encoding), otherwise the submitted config is persisted unchanged.

    ``label``
        Optional non-blank display label (CORESEAM-01), returned verbatim by any
        catalog/serializer that presents the descriptor. ``None`` falls back to
        the exact identifier — the same semantics as ``TaskTypeDeclaration.label``.
    """

    name: str
    applies_to: frozenset[str]
    label: str | None = None
    requires_url: frozenset[str] = frozenset()
    requires_schema_id: frozenset[str] = frozenset()
    requires_export_folder: frozenset[str] = frozenset()
    config_model: type[BaseModel] | None = None
    normalize_config: bool = False

    def validate_config(self, config: dict | None) -> dict | None:
        """Validate (and optionally normalize) a connector config payload.

        Raises ``ConnectorTypeValidationError`` naming only the offending config
        fields — never the values. Returns the config to persist.
        """
        if self.config_model is None:
            return config
        payload = {
            key: value for key, value in (config or {}).items() if key != "file_path"
        }
        try:
            validated = self.config_model(**payload)
        except ValidationError as exc:
            raise ConnectorTypeValidationError(
                f"invalid {self.name} connector config: {_config_error_fields(exc)}"
            ) from exc
        if self.normalize_config:
            return validated.model_dump()
        return config

    def missing_requirements(
        self,
        entity_kind: str,
        *,
        url: str | None = None,
        schema_id: Any = None,
        export_folder: str | None = None,
    ) -> tuple[str, ...]:
        """Names of the declarative fields this write must carry but does not."""
        missing: list[str] = []
        if entity_kind in self.requires_url and not (url or "").strip():
            missing.append("source_url" if entity_kind == "source" else "target_url")
        if entity_kind in self.requires_schema_id and schema_id is None:
            missing.append("schema_id")
        if entity_kind in self.requires_export_folder and not (
            export_folder or ""
        ).strip():
            missing.append("export_folder")
        return tuple(missing)


@dataclass(frozen=True)
class ConnectorTypeEntry:
    """A resolved declaration plus the owner that declared it (``core`` or a plugin)."""

    declaration: ConnectorTypeDeclaration
    owner: str


def validate_declaration(declaration: ConnectorTypeDeclaration) -> None:
    """Fail closed on a descriptor Core cannot safely persist or route (CORESEAM-01)."""
    if not isinstance(declaration.name, str) or not CONNECTOR_IDENTITY_RE.match(
        declaration.name
    ):
        raise ConnectorTypeValidationError(
            f"connector type name {declaration.name!r} must be a lowercase "
            "identifier matching '^[a-z][a-z0-9_-]{0,49}$'"
        )
    if (
        not declaration.applies_to
        or not declaration.applies_to <= CONNECTOR_ENTITY_KINDS
    ):
        raise ConnectorTypeValidationError(
            f"connector type {declaration.name!r} must declare applies_to as a "
            "non-empty subset of {'source', 'target'}"
        )
    # CORESEAM-01 encoding: a supplied label is a non-blank Unicode string
    # returned verbatim; None means "fall back to the exact identifier". Mirrors
    # TaskTypeDeclaration's label rule.
    if declaration.label is not None and (
        not isinstance(declaration.label, str) or not declaration.label.strip()
    ):
        raise ConnectorTypeValidationError(
            f"connector type {declaration.name!r} declares a blank label"
        )


class ConnectorTypeRegistry:
    """An in-memory, per-process map of exact identifier -> owning declaration."""

    def __init__(self) -> None:
        self._entries: dict[str, ConnectorTypeEntry] = {}

    def add(
        self, owner: str, declarations: Iterable[ConnectorTypeDeclaration]
    ) -> None:
        """Validate and register every declaration owned by ``owner``.

        Validation and collision checks complete for ALL declarations before any
        entry is written, so an owner with one bad or colliding declaration is
        rejected atomically — it never becomes a partially-registered owner.
        Re-adding the same owner replaces its previous entries.
        """
        prepared = list(declarations)
        if not prepared:
            raise ConnectorTypeValidationError(
                f"connector owner {owner!r} declares zero connector types"
            )
        seen: set[str] = set()
        for declaration in prepared:
            validate_declaration(declaration)
            if declaration.name in seen:
                raise ConnectorTypeValidationError(
                    f"connector owner {owner!r} declares connector type "
                    f"{declaration.name!r} more than once"
                )
            seen.add(declaration.name)
            existing = self._entries.get(declaration.name)
            if existing is not None and existing.owner != owner:
                raise ConnectorTypeCollisionError(declaration.name, existing.owner)

        self.remove_owner(owner)
        for declaration in prepared:
            self._entries[declaration.name] = ConnectorTypeEntry(
                declaration=declaration, owner=owner
            )

    def remove_owner(self, owner: str) -> None:
        """Drop every entry owned by ``owner`` (rollback on activation failure)."""
        for name in [
            name
            for name, entry in self._entries.items()
            if entry.owner == owner
        ]:
            del self._entries[name]

    def resolve(self, literal: str) -> ConnectorTypeEntry | None:
        """Return the entry for the exact normalized identifier, or ``None``."""
        return self._entries.get(literal)

    def names(self) -> frozenset[str]:
        """The exact declared identifiers in this snapshot."""
        return frozenset(self._entries)


# Core built-ins: `internal`/`api` are the frontend's Source defaults and `csv`
# stays a Core-owned Target connector. The `jira` connector is owned solely by
# the Jira plugin through the public SDK (CLEAN-01/D-03) — Core seeds nothing
# for it here, so the built-in set is exactly these three identities.
BUILTIN_CONNECTOR_DECLARATIONS: tuple[ConnectorTypeDeclaration, ...] = (
    ConnectorTypeDeclaration(
        name="internal", applies_to=frozenset({"source"}), label="Internal"
    ),
    ConnectorTypeDeclaration(
        name="api", applies_to=frozenset({"source"}), label="API"
    ),
    ConnectorTypeDeclaration(
        name="csv",
        applies_to=frozenset({"target"}),
        label="CSV",
        requires_export_folder=frozenset({"target"}),
        config_model=CsvConnectorConfig,
        normalize_config=True,
    ),
)

_registry: ConnectorTypeRegistry | None = None


def build_connector_type_registry() -> ConnectorTypeRegistry:
    """A fresh connector registry seeded with the Core-owned built-ins.

    The single construction path for a built-in-bearing snapshot, shared by the
    lazy process-local accessor below and by ``PluginRegistry``'s own connector
    snapshot, so the Core built-in set can never drift between the two.
    """
    registry = ConnectorTypeRegistry()
    registry.add(CORE_OWNER, BUILTIN_CONNECTOR_DECLARATIONS)
    return registry


def get_connector_type_registry() -> ConnectorTypeRegistry:
    """The process-local connector snapshot (lazily created with the built-ins)."""
    global _registry
    if _registry is None:
        _registry = build_connector_type_registry()
    return _registry


def resolve_connector_type(value: str | None, entity_kind: str) -> ConnectorTypeEntry:
    """Normalize and resolve a write-supplied connector identifier.

    The ONLY place a write turns a connector string into a descriptor: the
    identifier is ``strip().lower()``-normalized, checked against the ASCII
    identity grammar, resolved in the registry, and checked for applicability to
    ``entity_kind``. Raises ``ConnectorTypeValidationError`` (a ``ValueError``)
    for every failure mode; read paths must never call this.

    Empty input is rejected separately from the grammar so a blank/whitespace
    value gets the "required" message rather than a grammar complaint.
    """
    normalized = value.strip().lower() if isinstance(value, str) else ""
    if not normalized:
        raise ConnectorTypeValidationError(
            f"{entity_kind} connector type is required"
        )
    if not CONNECTOR_IDENTITY_RE.match(normalized):
        raise ConnectorTypeValidationError(
            f"connector type {normalized!r} must be a lowercase identifier "
            "matching '^[a-z][a-z0-9_-]{0,49}$'"
        )
    entry = get_connector_type_registry().resolve(normalized)
    if entry is None:
        raise ConnectorTypeValidationError(
            f"unknown {entity_kind} connector type {normalized!r}"
        )
    if entity_kind not in entry.declaration.applies_to:
        raise ConnectorTypeValidationError(
            f"connector type {normalized!r} does not apply to {entity_kind} records"
        )
    return entry