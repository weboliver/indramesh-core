"""Trusted mutation-origin envelope for Object hook payloads (CORESEAM-11 / D-14).

Every Object/Ref/index hook payload carries an ``origin`` describing WHY the
mutation happened: the four allowed kinds are ``user``, ``plugin``, ``import``
and ``system`` (CONTEXT/CORESEAM-11). The envelope is built only at trusted
entry points — an authenticated route, a Core-owned plugin context, the CSV
import task or a pure system index run — and copied out as a plain dict via
``as_payload()``. Identity is never taken from a client DTO.

This module is the single attribution vocabulary for the whole phase: the Object
services, the SDK adapters, the CSV importer and (in later plans) the index
outbox all build and propagate :class:`MutationOrigin` rather than a loose dict.
It imports nothing from the app so it can be used at every layer without adding
an import cycle.
"""

import dataclasses
import uuid
from typing import Any

# The four allowed origin kinds (CORESEAM-11 / D-14). Additive-only: a new kind
# is a product decision, so an unknown value fails loud rather than silently
# producing an unattributable payload.
ALLOWED_KINDS: frozenset[str] = frozenset({"user", "plugin", "import", "system"})

# The stable technical name for a pure system index run with no initiating actor
# or task (CONTEXT D-13) — never a synthetic user identity.
SYSTEM_ORIGIN_NAME = "core-index"


@dataclasses.dataclass(frozen=True)
class MutationOrigin:
    """An immutable, trusted description of what caused one mutation.

    ``kind`` is one of :data:`ALLOWED_KINDS`; ``name`` is the stable, meaningful
    caller label (an operation name for a user write, the plugin identity for an
    SDK write, the import-definition name for CSV, ``core-index`` for a pure
    system run); ``task_id`` is the persisted Task that caused the write, or
    ``None`` when there is none.
    """

    kind: str
    name: str
    task_id: uuid.UUID | None = None

    def __post_init__(self) -> None:
        if self.kind not in ALLOWED_KINDS:
            raise ValueError(
                f"Unknown mutation origin kind {self.kind!r} — allowed: "
                f"{sorted(ALLOWED_KINDS)}"
            )
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("Mutation origin name must be a non-empty string")
        if self.task_id is not None and not isinstance(self.task_id, uuid.UUID):
            raise ValueError("Mutation origin task_id must be a UUID or None")

    def as_payload(self) -> dict[str, Any]:
        """Copy out the plain ``{kind, name, task_id}`` dict for hook kwargs.

        ``task_id`` stays a :class:`uuid.UUID` (or ``None``), matching the
        existing hook kwargs — serialization into the index outbox is a later
        concern handled by its own producer.
        """
        return {"kind": self.kind, "name": self.name, "task_id": self.task_id}


def user_origin(name: str, task_id: uuid.UUID | None = None) -> MutationOrigin:
    """A write initiated by an authenticated user (or a persisted task creator)."""
    return MutationOrigin(kind="user", name=name, task_id=task_id)


def plugin_origin(name: str, task_id: uuid.UUID | None = None) -> MutationOrigin:
    """A write performed by a plugin through the Core Data SDK."""
    return MutationOrigin(kind="plugin", name=name, task_id=task_id)


def import_origin(name: str, task_id: uuid.UUID | None = None) -> MutationOrigin:
    """A write performed by a CSV import inside its per-row transaction."""
    return MutationOrigin(kind="import", name=name, task_id=task_id)


def system_origin(
    name: str = SYSTEM_ORIGIN_NAME, task_id: uuid.UUID | None = None
) -> MutationOrigin:
    """A write with no initiating user, plugin or import (pure system run)."""
    return MutationOrigin(kind="system", name=name, task_id=task_id)
