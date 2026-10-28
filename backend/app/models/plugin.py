"""Core ``plugins`` table model (PLUGCORE-10).

Lives in ``app/models/`` (one ORM model file per entity, this codebase's
convention) — NOT inside ``app/plugin_system/``. The ``app.models`` barrel
imports this module, which is what makes ``Base.metadata`` see the table for
Alembic autogenerate and for the app's own ORM usage.
"""

import enum
import uuid
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy import DateTime, String, Text
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class PluginState(str, enum.Enum):
    """Lifecycle states of an installed plugin (PLUGCORE-03)."""

    LOADED = "loaded"      # constructed, not yet activated
    ACTIVE = "active"      # on_load() succeeded, available for dispatch
    ERROR = "error"        # discovery/import/on_load/mismatch failure — isolated
    DISABLED = "disabled"  # operator kill-switch — survives restart (PLUGCORE-10)


class Plugin(Base):
    __tablename__ = "plugins"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    name: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    state: Mapped[PluginState] = mapped_column(String(20), default=PluginState.LOADED)
    # Per-plugin config namespace; secret fields are Fernet-encrypted inline
    # (D-12). Owned by the config API (Plan 282-07) — the loader only ever
    # creates it as {} for a brand-new row and never overwrites it.
    config: Mapped[dict] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    installed_at: Mapped[datetime] = mapped_column(
        DateTime, default=lambda: datetime.now(timezone.utc).replace(tzinfo=None)
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        onupdate=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
    )
    # PLUGWORKER-02/D-01: worker-exclusive columns — only the worker process
    # (PluginRegistry(is_worker=True)) ever writes these, on a genuine
    # LOADED -> ACTIVE transition. The backend process never reads or writes
    # them (D-01/D-04). Deliberately no default/onupdate — they are NOT
    # auto-stamped; a kill-switch must preserve the last-loaded snapshot (D-02).
    # They are the data basis for Phase 285's admin badge, which compares
    # worker_loaded_version against `version` (D-03).
    worker_loaded_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    worker_loaded_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Phase 287 (PLUGDB-01/D-11/D-12/D-13): the plugin's declared database
    # contract plus the sanitized migration diagnostic. The first two are written
    # by the registry only on the success path of an at-head activation, so a
    # recorded contract always implies the plugin's tables exist; they are then
    # immutable for that installed plugin identity (D-12). ``database_error``/
    # ``database_error_revision`` carry a core-authored, sanitized short reason
    # (D-13/D-14) — never raw exception text (plan 287-03 owns persisting it).
    # The API deliberately exposes only ``database_version_table`` and the two
    # diagnostic fields (plan 287-03); ``database_migrations_dir`` stays internal.
    database_migrations_dir: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    database_version_table: Mapped[str | None] = mapped_column(
        String(63), nullable=True
    )
    database_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    database_error_revision: Mapped[str | None] = mapped_column(
        String(64), nullable=True
    )
