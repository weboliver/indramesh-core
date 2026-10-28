"""Plugin-owned declarative metadata for the reference plugin (Phase 287).

This module declares the plugin's OWN ``DeclarativeBase``/``MetaData`` — it
never imports ``app.db.base.Base``. ``ExamplePlugin.database_metadata`` exposes
``Base.metadata`` to the core runner, which passes it to the shared Alembic env
as ``target_metadata`` (PLUGDB-01).

D-01/D-02: a plugin may create foreign keys from its own ``plugin_<name>_*``
tables to existing Core tables, and every such FK states its ``ON DELETE``
behaviour explicitly (D-03) — there is no Core-wide default.

Known limitation (A-287-04, review 2026-09-22): a ``ForeignKey("objects.id")``
inside a PRIVATE MetaData is resolved lazily. Inserts/selects through this
metadata work, but any plugin-side ``autogenerate``/``compare_metadata`` over it
raises ``NoReferencedTableError`` because Alembic's ``_fk_spec`` dereferences
``element.column``. Revisions are therefore hand-written in this phase, and the
shared env's plugin-table allowlist is a guard-rail only.
"""

import uuid

from sqlalchemy import ForeignKey, String
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class ExampleNote(Base):
    __tablename__ = "plugin_example_notes"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    body: Mapped[str] = mapped_column(String(500), nullable=False)
    # D-01/D-03: a plugin-owned table may reference the Core; the ON DELETE
    # behaviour is explicit per FK, never defaulted.
    object_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("objects.id", ondelete="SET NULL"),
        nullable=True,
    )
