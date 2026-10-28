"""Object-file storage orchestration (Phase 279 plan 01 tracer).

Owns the streaming upload relay (D-09), the blob-first metadata commit with
best-effort compensation (R-03 / D-23), and the newest-first metadata list
with uploader display-name resolution (D-11, FILE-02). StorageClient is the
only caller-facing storage seam (D-21); storage keys stay service-generated
UUIDs (D-22) and original filenames are metadata only.
"""

import codecs
import logging
import uuid
from typing import AsyncIterator, List

from fastapi import HTTPException, UploadFile
from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import storage_settings
from app.models import ObjectFile, User, UserProfile
from app.schemas.object_file import ObjectFileResponse
from app.services.settings_service import get_setting
from app.services.storage_client import get_storage_client

logger = logging.getLogger(__name__)

#: Admin setting key holding the integral per-file maximum in MB (D-06).
OBJECT_FILE_MAX_SIZE_MB_SETTING = "object_file_max_size_mb"

#: Default per-file maximum when the setting is absent (D-06).
DEFAULT_OBJECT_FILE_LIMIT_MB = 50

#: WR-03/CR-01: upper bound on the admin-configurable per-file maximum.
#: Enforced both at the settings write path (app/api/settings.py imports
#: this constant) and here, at the point the stored setting is actually
#: turned into the byte ceiling the upload relay counts against — a stored
#: value above this cap (e.g. written before the cap existed) falls back to
#: the default rather than being honored verbatim.
OBJECT_FILE_MAX_SIZE_MB_CAP = 2048

#: Fixed relay chunk size — the upload stream never buffers more than this
#: window in memory at any time (FILE-07, RESEARCH Pitfall 2).
OBJECT_FILE_CHUNK_SIZE = 1024 * 1024

#: Bounded head captured from the streamed chunks for server-side type
#: detection; the declared multipart content type is only a hint.
OBJECT_FILE_SNIFF_BYTES = 8192

OCTET_STREAM = "application/octet-stream"

#: canonical type -> alternative signatures; each signature is a tuple of
#: (offset, magic) pairs that must all match.
_FILE_SIGNATURES = (
    ("application/pdf", (((0, b"%PDF-"),),)),
    ("image/png", (((0, b"\x89PNG\r\n\x1a\n"),),)),
    ("image/jpeg", (((0, b"\xff\xd8\xff"),),)),
    ("image/gif", (((0, b"GIF87a"),), ((0, b"GIF89a"),))),
    ("image/webp", (((0, b"RIFF"), (8, b"WEBP")),)),
    ("image/tiff", (((0, b"II*\x00"),), ((0, b"MM\x00*"),))),
    ("application/zip", (((0, b"PK\x03\x04"),), ((0, b"PK\x05\x06"),))),
    ("application/gzip", (((0, b"\x1f\x8b\x08"),),)),
    ("application/x-7z-compressed", (((0, b"7z\xbc\xaf\x27\x1c"),),)),
    ("application/x-ole-storage", (((0, b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),),)),
)

_GENERIC_DECLARED_TYPES = frozenset({"application/octet-stream", "binary/octet-stream"})

_DECLARED_ALIASES = {
    "image/jpg": "image/jpeg",
    "image/pjpeg": "image/jpeg",
    "image/x-png": "image/png",
    "application/x-pdf": "application/pdf",
    "application/x-zip-compressed": "application/zip",
    "application/x-zip": "application/zip",
    "application/x-gzip": "application/gzip",
}

#: detected container type -> declared types it may be refined to.
_CONTAINER_FAMILIES = {
    "application/zip": frozenset({
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.oasis.opendocument.spreadsheet",
        "application/vnd.oasis.opendocument.presentation",
        "application/epub+zip",
    }),
    "application/x-ole-storage": frozenset({
        "application/msword",
        "application/vnd.ms-excel",
        "application/vnd.ms-powerpoint",
        "application/vnd.ms-outlook",
    }),
}

_TEXT_DECLARED_TYPES = frozenset({"text/plain", "text/csv", "application/json"})

_ACTIVE_CONTENT_MARKERS = (
    b"<!doctype",
    b"<html",
    b"<head",
    b"<body",
    b"<script",
    b"<svg",
    b"<iframe",
    b"<?xml",
)

#: Every value detect_content_type can return (T-ipy-01 invariant).
DETECTABLE_CONTENT_TYPES = frozenset(
    {content_type for content_type, _ in _FILE_SIGNATURES}
    | {member for family in _CONTAINER_FAMILIES.values() for member in family}
    | _TEXT_DECLARED_TYPES
    | {OCTET_STREAM}
)


def _normalize_declared(declared: str | None) -> str | None:
    if not declared:
        return None
    value = declared.split(";", 1)[0].strip().lower()
    if not value or value in _GENERIC_DECLARED_TYPES:
        return None
    return _DECLARED_ALIASES.get(value, value)


def _match_signature(head: bytes) -> str | None:
    for content_type, signatures in _FILE_SIGNATURES:
        for signature in signatures:
            if all(
                head[offset:offset + len(magic)] == magic for offset, magic in signature
            ):
                return content_type
    return None


def _is_plain_text(head: bytes) -> bool:
    if b"\x00" in head:
        return False
    try:
        codecs.getincrementaldecoder("utf-8")().decode(head, final=False)
    except UnicodeDecodeError:
        return False
    lowered = head.lower()
    return not any(marker in lowered for marker in _ACTIVE_CONTENT_MARKERS)


def detect_content_type(head: bytes, declared: str | None) -> str:
    """Server-owned content type for a native upload.

    The result is derived from the file's leading bytes; the client-declared
    type is only a hint. Anything unrecognized or conflicting with the
    detected signature becomes application/octet-stream.
    """
    claimed = _normalize_declared(declared)
    detected = _match_signature(head)
    if detected is not None:
        if claimed is None or claimed == detected:
            return detected
        if claimed in _CONTAINER_FAMILIES.get(detected, ()):
            return claimed
        return OCTET_STREAM
    if claimed in _TEXT_DECLARED_TYPES and _is_plain_text(head):
        return claimed
    return OCTET_STREAM


async def get_object_file_limit_bytes(db: AsyncSession) -> int:
    """Resolve the active per-file byte limit: the stored integral MB setting
    with the 50 MB default fallback (D-06), converted to exact bytes (D-09).

    Clamps against OBJECT_FILE_MAX_SIZE_MB_CAP (CR-01) so a stored value
    above the cap — written before the cap existed, or via any path other
    than the PUT /settings/object_file_max_size_mb route — is never honored
    verbatim; it falls back to the default, matching the existing < 1
    fallback pattern.
    """
    raw = await get_setting(db, OBJECT_FILE_MAX_SIZE_MB_SETTING)
    try:
        mb = int(raw) if raw else DEFAULT_OBJECT_FILE_LIMIT_MB
    except (TypeError, ValueError):
        mb = DEFAULT_OBJECT_FILE_LIMIT_MB
    if mb < 1 or mb > OBJECT_FILE_MAX_SIZE_MB_CAP:
        mb = DEFAULT_OBJECT_FILE_LIMIT_MB
    return mb * 1024 * 1024


async def create_object_file(
    db: AsyncSession,
    *,
    object_id: uuid.UUID,
    upload: UploadFile,
    uploader_id: uuid.UUID | None,
) -> ObjectFileResponse:
    """Stream one upload into storage and commit its ObjectFile metadata.

    Ordering (R-03 / D-23): the counted async chunk relay feeds
    StorageClient.store() directly — never materialized (FILE-07) — and the
    ObjectFile row is committed only after storage succeeds. A metadata-commit
    failure triggers best-effort blob compensation (deleted key on storage
    failure is logged, never raised). The raised 413 (T-279-01) fires from
    inside the relay before any metadata exists.
    """
    max_bytes = await get_object_file_limit_bytes(db)
    storage_key = str(uuid.uuid4())
    total = 0
    head = b""

    async def counted_chunks():
        nonlocal total, head
        while True:
            chunk = await upload.read(OBJECT_FILE_CHUNK_SIZE)
            if not chunk:
                return
            total += len(chunk)
            if len(head) < OBJECT_FILE_SNIFF_BYTES:
                head += chunk[:OBJECT_FILE_SNIFF_BYTES - len(head)]
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds maximum size of {max_bytes} bytes",
                )
            yield chunk

    try:
        await get_storage_client().store(
            storage_settings.storage_bucket, storage_key, counted_chunks()
        )
    except Exception:
        try:
            await get_storage_client().delete(
                storage_settings.storage_bucket, storage_key
            )
        except Exception:
            logger.error(
                "orphan blob after failed store: %s/%s",
                storage_settings.storage_bucket,
                storage_key,
            )
        raise

    filename = (upload.filename or "upload")[:500]
    content_type = detect_content_type(head, upload.content_type)

    file = ObjectFile(
        object_id=object_id,
        storage_key=storage_key,
        filename=filename,
        content_type=content_type,
        size_bytes=total,
        uploader_id=uploader_id,
    )
    db.add(file)
    try:
        await db.commit()
    except Exception:
        try:
            await get_storage_client().delete(
                storage_settings.storage_bucket, storage_key
            )
        except Exception:
            logger.error(
                "orphan blob after failed metadata commit: %s/%s",
                storage_settings.storage_bucket,
                storage_key,
            )
        raise
    await db.refresh(file)
    return _to_response(file, None)


async def list_object_files(
    db: AsyncSession, object_id: uuid.UUID
) -> List[ObjectFileResponse]:
    """Newest-first ObjectFile metadata with uploader display names (D-11).

    Display resolution mirrors audit_logs.py's actor naming: prefer
    \"First Last\", fall back to username (R-06 — never a raw UUID).
    """
    rows = (
        await db.execute(
            select(
                ObjectFile, UserProfile.first_name, UserProfile.last_name, User.username
            )
            .outerjoin(User, ObjectFile.uploader_id == User.id)
            .outerjoin(UserProfile, User.id == UserProfile.user_id)
            .where(ObjectFile.object_id == object_id)
            .order_by(ObjectFile.created_at.desc())
        )
    ).all()
    items = []
    for row in rows:
        file = row[0]
        first_name = row[1] or ""
        last_name = row[2] or ""
        username = row[3] or ""
        if first_name and last_name:
            display_name = f"{first_name} {last_name}".strip()
        elif first_name:
            display_name = first_name
        elif last_name:
            display_name = last_name
        else:
            display_name = username or None
        items.append(_to_response(file, display_name))
    return items


async def get_object_file(
    db: AsyncSession, object_id: uuid.UUID, file_id: uuid.UUID
) -> ObjectFile | None:
    """Object-scoped ObjectFile lookup (T-279-05/T-279-06).

    Filters by BOTH parent object and file id, so a file UUID belonging to a
    different Object can never be addressed through a mismatched Object path.
    """
    return (
        await db.execute(
            select(ObjectFile).where(
                ObjectFile.object_id == object_id, ObjectFile.id == file_id
            )
        )
    ).scalar_one_or_none()


async def stream_object_file(file: ObjectFile) -> AsyncIterator[bytes]:
    """Forward the stored blob as bounded chunks (FILE-07 download leg).

    Feeds StorageClient.fetch() straight into StreamingResponse — the backend
    never buffers the complete payload (R-04 / D-13).
    """
    async for chunk in get_storage_client().fetch(
        storage_settings.storage_bucket, file.storage_key
    ):
        yield chunk


async def delete_object_file(
    db: AsyncSession, object_id: uuid.UUID, file_id: uuid.UUID
) -> bool:
    """DB-first hard delete (D-23, mirrors object_service.delete_object).

    Commits the ObjectFile metadata deletion BEFORE the idempotent storage
    cleanup; a storage-cleanup exception is logged as an orphan and never
    resurrects the committed row. Returns False when no row is addressable
    under the given parent Object (route turns it into 404).

    290 D-12/T-290-04-01: a connector-managed row (managed_by IS NOT NULL)
    is pull-only and must never be deletable through this service — the guard
    sits before any DELETE/storage-cleanup logic and returns the same False as
    a genuinely missing row, so no existence leaks through the 404 and a
    hand-crafted API call is refused exactly like the UI omits the control.
    The persisted provenance is the only authority (never a client-provided
    flag).
    """
    file = await get_object_file(db, object_id, file_id)
    if not file:
        return False
    if file.managed_by is not None:
        return False
    storage_key = file.storage_key
    result = await db.execute(
        sa_delete(ObjectFile).where(
            ObjectFile.object_id == object_id, ObjectFile.id == file_id
        )
    )
    await db.commit()
    if result.rowcount == 0:
        return False
    try:
        await get_storage_client().delete(storage_settings.storage_bucket, storage_key)
    except Exception:
        logger.error(
            "orphan blob after failed file delete: %s/%s",
            storage_settings.storage_bucket,
            storage_key,
        )
    return True


class ConnectorFileConflict(ValueError):
    """A connector write addressed a deterministic id owned by another row (D-13).

    The SDK derives the Core file id from ``{source}:{object}:{external_id}``, so
    this only fires on a genuine hash collision or a bug; it is surfaced as a
    typed ``CoreDataConflict`` and never silently reparents an existing row.
    """


async def _store_connector_blob(
    db: AsyncSession, stream
) -> tuple[str, int, bytes]:
    """Stream a connector upload into a FRESH service-generated storage key.

    Reuses the native relay contract: the size limit is resolved from the Core
    setting, the bytes are pulled chunk-by-chunk (never buffered wholesale) and a
    storage failure deletes the partial blob before re-raising. The raw storage
    key is Core-generated and can never be influenced by connector input.
    """
    max_bytes = await get_object_file_limit_bytes(db)
    storage_key = str(uuid.uuid4())
    total = 0
    head = b""

    async def counted_chunks():
        nonlocal total, head
        while True:
            chunk = await stream.read(OBJECT_FILE_CHUNK_SIZE)
            if not chunk:
                return
            total += len(chunk)
            if len(head) < OBJECT_FILE_SNIFF_BYTES:
                head += chunk[:OBJECT_FILE_SNIFF_BYTES - len(head)]
            if total > max_bytes:
                raise HTTPException(
                    status_code=413,
                    detail=f"File exceeds maximum size of {max_bytes} bytes",
                )
            yield chunk

    try:
        await get_storage_client().store(
            storage_settings.storage_bucket, storage_key, counted_chunks()
        )
    except Exception:
        try:
            await get_storage_client().delete(
                storage_settings.storage_bucket, storage_key
            )
        except Exception:
            logger.error(
                "orphan blob after failed connector store: %s/%s",
                storage_settings.storage_bucket,
                storage_key,
            )
        raise
    return storage_key, total, head


async def _best_effort_delete_blob(storage_key: str) -> None:
    try:
        await get_storage_client().delete(
            storage_settings.storage_bucket, storage_key
        )
    except Exception:
        logger.error(
            "orphan blob after connector replace/delete: %s/%s",
            storage_settings.storage_bucket,
            storage_key,
        )


async def upsert_connector_file(
    db: AsyncSession,
    *,
    object_id: uuid.UUID,
    file_id: uuid.UUID,
    stream,
    filename: str,
    content_type: str | None,
    external_author_name: str | None,
    managed_by: str,
    content_length: int | None = None,
) -> tuple[ObjectFileResponse, bool]:
    """Read-or-create/replace one connector-managed attachment on its deterministic id.

    A replay (same filename/author and, when supplied, the same declared length)
    short-circuits BEFORE the stream is touched, so a retry after a Core commit
    never stores a second blob. A changed payload streams to a fresh key and
    swaps the row metadata, compensating the new blob if the metadata commit
    fails and deleting the old blob only after the new row is committed.

    The row is externally owned (``managed_by``), has no local uploader and
    carries the bounded external author text — never a fabricated local User.
    """
    filename = (filename or "upload")[:500]
    existing = await db.get(ObjectFile, file_id)
    if existing is not None:
        if existing.object_id != object_id or existing.managed_by != managed_by:
            raise ConnectorFileConflict(
                "The connector attachment id is already bound to a different parent"
            )
        if (
            existing.filename == filename
            and existing.external_author_name == external_author_name
            and (content_length is None or existing.size_bytes == content_length)
        ):
            return _to_response(existing, None), False

        old_key = existing.storage_key
        storage_key, total, head = await _store_connector_blob(db, stream)
        existing.storage_key = storage_key
        existing.filename = filename
        existing.content_type = detect_content_type(head, content_type)
        existing.size_bytes = total
        existing.external_author_name = external_author_name
        try:
            await db.commit()
        except Exception:
            await _best_effort_delete_blob(storage_key)
            raise
        await _best_effort_delete_blob(old_key)
        await db.refresh(existing)
        return _to_response(existing, None), False

    storage_key, total, head = await _store_connector_blob(db, stream)
    file = ObjectFile(
        id=file_id,
        object_id=object_id,
        storage_key=storage_key,
        filename=filename,
        content_type=detect_content_type(head, content_type),
        size_bytes=total,
        uploader_id=None,
        managed_by=managed_by,
        external_author_name=external_author_name,
    )
    db.add(file)
    try:
        await db.commit()
    except Exception:
        await _best_effort_delete_blob(storage_key)
        raise
    await db.refresh(file)
    return _to_response(file, None), True


async def delete_connector_file(
    db: AsyncSession,
    object_id: uuid.UUID,
    file_id: uuid.UUID,
    managed_by: str,
) -> bool:
    """Delete a file owned by THIS connector, DB-first with blob cleanup (D-13).

    The native ``delete_object_file`` guard is untouched; this connector-scoped
    path deletes only a row whose persisted ``managed_by`` matches, commits the
    metadata delete first and then performs a best-effort storage cleanup.
    """
    file = await get_object_file(db, object_id, file_id)
    if not file or file.managed_by != managed_by:
        return False
    storage_key = file.storage_key
    result = await db.execute(
        sa_delete(ObjectFile).where(
            ObjectFile.object_id == object_id, ObjectFile.id == file_id
        )
    )
    await db.commit()
    if result.rowcount == 0:
        return False
    await _best_effort_delete_blob(storage_key)
    return True


def _to_response(file: ObjectFile, display_name: str | None) -> ObjectFileResponse:
    # 290 D-13: a connector-managed row renders the captured attachment author
    # instead of a resolved local uploader — no local User is ever fabricated
    # for an external author. Native rows keep the resolved local name.
    if file.managed_by is not None:
        display_name = file.external_author_name
    return ObjectFileResponse(
        id=file.id,
        filename=file.filename,
        content_type=file.content_type,
        size_bytes=file.size_bytes,
        uploader_display_name=display_name,
        created_at=file.created_at,
        # 290 D-12/D-14: managed_by is the single source of truth; is_external
        # is derived from it on the DTO.
        managed_by=file.managed_by,
    )