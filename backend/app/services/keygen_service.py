"""246 OBJ-02 (D-10): per-schema Postgres SEQUENCE for IndraMesh-native keys.

The composite IndraMesh key is ``{keygen}-{N}`` where ``keygen`` comes from
``schemas.keygen`` (D-09) and N from a per-schema SEQUENCE named
``keygen_seq_<hex(schema_id)>`` (D-10, no hyphens — must match the 246
migration's ``_seq_name`` exactly so existing schemas' sequences are reused).

Atomic ``nextval`` — never a read-then-write counter (race-free). The sequence
is per-schema, monotonically increasing, never reused, robust against
deletions (D-10).

Lifecycle functions are idempotent so they compose with the migration's
already-created sequences and with rollback-truthful migration tests.
"""

import uuid

from sqlalchemy import text
from sqlalchemy.exc import ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.schema import Schema


class KeygenNotConfiguredError(RuntimeError):
    """A native key cannot be minted because the schema has no keygen setup.

    Raised by :func:`next_key` when either the ``{keygen}`` prefix or the
    per-schema SEQUENCE is missing — a clear domain error instead of a raw
    ``nextval`` ``ProgrammingError`` (or a nonsense ``"None-N"`` key) at the
    REST create boundary (WR-06).
    """


def sequence_name(schema_id: uuid.UUID | str) -> str:
    """Postgres SEQUENCE name for a schema: ``keygen_seq_<hex>`` (no hyphens).

    Accepts a str schema id too (API responses carry ids as strings), matching
    the migration's ``_seq_name`` exactly so existing sequences are reused.
    """
    if isinstance(schema_id, str):
        schema_id = uuid.UUID(schema_id)
    return f"keygen_seq_{schema_id.hex}"


async def create_schema_sequence(db: AsyncSession, schema_id: uuid.UUID) -> None:
    """Create the per-schema keygen sequence if missing (idempotent).

    The runtime role holds no ``CREATE`` on schema ``public`` (D-01), so the DDL
    runs inside the owner's ``SECURITY DEFINER`` function; the caller only needs
    ``EXECUTE``.
    """
    await db.execute(
        text("SELECT public.indramesh_keygen_sequence_create(:schema_id)"),
        {"schema_id": schema_id},
    )
    await db.flush()


async def drop_schema_sequence(db: AsyncSession, schema_id: uuid.UUID) -> None:
    """Drop the per-schema keygen sequence if present (idempotent).

    Mirrors :func:`create_schema_sequence`: the drop also goes through the
    owner's definer function, never a direct ``DROP`` from the runtime role.
    """
    await db.execute(
        text("SELECT public.indramesh_keygen_sequence_drop(:schema_id)"),
        {"schema_id": schema_id},
    )
    await db.flush()


async def next_key(db: AsyncSession, schema: Schema) -> str:
    """Return the next composite key ``{schema.keygen}-{N}`` from the schema's
    sequence. ``schema.keygen`` is read at call time so a keygen rename on the
    same Schema instance regenerates under the new prefix (D-12).

    WR-06: a schema without a keygen prefix or without its per-schema SEQUENCE
    (e.g. one created by a sync path that predates DECOUPLE-04/D1) raises
    ``KeygenNotConfiguredError`` instead of surfacing a raw ``nextval``
    ``ProgrammingError`` or minting a ``"None-N"`` key.
    """
    if not schema.keygen:
        raise KeygenNotConfiguredError(
            f"Schema {schema.id} has no keygen prefix; cannot mint a native key"
        )
    try:
        result = await db.execute(text(
            f"SELECT nextval('public.{sequence_name(schema.id)}')"
        ))
    except ProgrammingError as exc:
        # Only a genuinely absent sequence is a domain "not configured" error.
        # A missing grant is a ProgrammingError too and must stay visible
        # instead of masquerading as an unconfigured schema (D-11).
        if getattr(getattr(exc, "orig", None), "sqlstate", None) == "42P01":
            raise KeygenNotConfiguredError(
                f"Schema {schema.id} has no keygen sequence; cannot mint a native key"
            ) from exc
        raise
    number = result.scalar_one()
    return f"{schema.keygen}-{number}"