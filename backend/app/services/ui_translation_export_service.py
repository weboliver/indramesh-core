"""Phase 235 / EXP-01 — development-only filesystem export of the current
resolved `systemui` dictionaries.

Reuses `resolve_ui_dictionary()` verbatim for every active `Language`
(including English) so exported content is always the database-resolved
requested -> English -> code-default chain, never sparse stored rows or a
locale-file read (D-01..D-04, D-08). Writes are staged in the target
directory and published only after every active language resolved and
staged successfully (D-06/D-07); any resolve/stage/publish/audit/commit
failure restores every pre-existing target byte-for-byte and leaves no
temporary/backup artifact behind. A successful export writes exactly one
`ui_translation_exports` audit entry containing language codes and relative
paths only (D-17) — never dictionary text.

`_stage_target`/`_publish_target` are intentionally separate, patchable
module-level functions (not inlined) so failure-injection tests can
monkeypatch a specific stage of the pipeline without faking the filesystem.
"""
import json
import os
import shutil
import uuid
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Language
from app.services.audit_service import audit_log
from app.services.ui_translation_service import resolve_ui_dictionary

# D-05: fixed server export root, never client-supplied. Matches the
# docker-compose backend bind mount target (./frontend/locales ->
# /workspace/frontend-locales); a distinct name from
# export_execution_service.py's unrelated EXPORT_ROOT (CSV target exports)
# per 235-PLAN-REVIEW.md Finding 4.
UI_TRANSLATION_EXPORT_ROOT = os.path.realpath(
    os.getenv("UI_TRANSLATION_EXPORT_ROOT", "/workspace/frontend-locales")
)


class UiTranslationExportError(Exception):
    """Raised when the export cannot complete. Every pre-existing locale
    target is guaranteed unchanged when this (or any other exception)
    propagates out of export_ui_translation_dictionaries()."""


def _chmod_best_effort(path: str, mode: int) -> None:
    """Widen a path's mode bits so both the container's appuser and the host
    user (different UID, not in the same group — the whole reason this bind
    mount needs help) can read/write it. Best-effort like
    docker-entrypoint.sh's own boot-time sweep: a pre-existing path owned by
    a third UID (e.g. a stale export from a prior container UID) can't be
    chmod'd by a non-owner non-root process, and that must never fail an
    otherwise-successful export over a convenience permission widening."""
    try:
        os.chmod(path, mode)
    except OSError:
        pass


def _stage_target(target_path: str, payload_bytes: bytes) -> str:
    """Write payload_bytes to a same-directory temp file (flushed + fsynced)
    and return its path. Creates the target's directory if missing (D-05).
    Never touches target_path itself — publication is a separate step.

    docker-entrypoint.sh's chmod-on-boot sweep only covers paths that exist
    at container start; a language directory created here mid-run (or a
    file replaced later) would otherwise stay owned/moded by the container's
    UID with no boot-time sweep having ever seen it, locking the host
    developer out of hand-editing the export without sudo. So this function
    widens what it creates directly, every time, mode bits only (never
    touches ownership — see docker-entrypoint.sh for why chown is wrong
    here). Sets world (not just group) bits, matching the entrypoint's
    `a+rwX`: the host user is neither the file's owner nor in its group.
    """
    target_dir = os.path.dirname(target_path)
    os.makedirs(target_dir, exist_ok=True)
    _chmod_best_effort(target_dir, 0o777)
    temp_path = f"{target_path}.tmp-{uuid.uuid4().hex}"
    with open(temp_path, "wb") as f:
        f.write(payload_bytes)
        f.flush()
        os.fsync(f.fileno())
    _chmod_best_effort(temp_path, 0o666)
    return temp_path


def _publish_target(temp_path: str, target_path: str) -> tuple[bool, str | None]:
    """Atomically replace target_path with temp_path's contents (D-08).

    If target_path already exists, it is first copied to a same-filesystem
    backup (returned) so the caller can restore it on a later failure. The
    replacement itself is a single os.replace — a malformed pre-existing
    target is simply overwritten, never read/parsed.

    Returns (had_existing, backup_path).
    """
    had_existing = os.path.exists(target_path)
    backup_path = None
    if had_existing:
        backup_path = f"{target_path}.bak-{uuid.uuid4().hex}"
        shutil.copy2(target_path, backup_path)
    os.replace(temp_path, target_path)
    return had_existing, backup_path


def _serialize_dictionary(strings: dict) -> bytes:
    """Deterministic flat dotted-key JSON (D-04/D-08): UTF-8, no ensure_ascii
    escaping, pretty-printed, key-sorted, terminating newline — stable across
    two identical exports."""
    return (json.dumps(strings, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


async def export_ui_translation_dictionaries(
    db: AsyncSession, actor_id: uuid.UUID | None
) -> List[str]:
    """Resolve every active system UI language's dictionary and publish it
    to UI_TRANSLATION_EXPORT_ROOT/<language.code>/systemui.json.

    Returns the ordered list of relative paths written (D-04's paths-only
    contract), in the same deterministic code-ascending order the languages
    were resolved in.

    Raises UiTranslationExportError (empty active-language set, or a target
    resolving outside the export root) or propagates any resolver/filesystem/
    audit exception — in every failure case, every pre-existing target under
    the export root is left byte-identical and no temp/backup file remains
    (D-06/D-07).
    """
    active_result = await db.execute(
        select(Language).where(Language.is_active.is_(True)).order_by(Language.code)
    )
    active_languages = list(active_result.scalars().all())
    if not active_languages:
        raise UiTranslationExportError("No active system UI languages to export")

    # Resolve + serialize every dictionary BEFORE any filesystem mutation
    # (D-06) — a resolver failure on any language must leave the filesystem
    # completely untouched, so this loop only ever reads.
    targets: list[dict] = []
    for language in active_languages:
        dictionary = await resolve_ui_dictionary(db, language.code)
        payload_bytes = _serialize_dictionary(dictionary["strings"])

        relative_path = f"{language.code}/systemui.json"
        target_path = os.path.realpath(
            os.path.join(UI_TRANSLATION_EXPORT_ROOT, language.code, "systemui.json")
        )
        if target_path != UI_TRANSLATION_EXPORT_ROOT and not target_path.startswith(
            UI_TRANSLATION_EXPORT_ROOT + os.sep
        ):
            raise UiTranslationExportError(
                f"Resolved export target for language '{language.code}' escapes "
                f"the permitted export root ({UI_TRANSLATION_EXPORT_ROOT})"
            )
        targets.append(
            {
                "code": language.code,
                "relative_path": relative_path,
                "target_path": target_path,
                "payload_bytes": payload_bytes,
            }
        )

    staged: list[dict] = []
    try:
        for entry in targets:
            entry["temp_path"] = _stage_target(entry["target_path"], entry["payload_bytes"])
            staged.append(entry)

        # Publish only after every target staged successfully.
        for entry in staged:
            had_existing, backup_path = _publish_target(entry["temp_path"], entry["target_path"])
            entry["had_existing"] = had_existing
            entry["backup_path"] = backup_path
            entry["published"] = True

        # D-17: success-only audit — language codes and relative paths only,
        # never resolved dictionary text, temp paths, or the lock key.
        await audit_log(
            db,
            "ui_translation_exports",
            uuid.uuid4(),
            "EXPORT",
            actor_id,
            old_values=None,
            new_values={
                "languages": [entry["code"] for entry in staged],
                "paths": [entry["relative_path"] for entry in staged],
            },
        )
        await db.commit()
    except Exception:
        await db.rollback()
        # Restore every published target's pre-existing bytes (or remove it
        # if it didn't exist before), and drop any un-published temp file —
        # every pre-existing target ends up byte-identical (D-07).
        for entry in staged:
            if entry.get("published"):
                if entry.get("had_existing"):
                    shutil.copy2(entry["backup_path"], entry["target_path"])
                elif os.path.exists(entry["target_path"]):
                    os.remove(entry["target_path"])
            elif entry.get("temp_path") and os.path.exists(entry["temp_path"]):
                os.remove(entry["temp_path"])
        raise
    finally:
        # Backups are retained only until the success audit transaction
        # commits (or a failure has already used them to restore); temp
        # files that were consumed by os.replace no longer exist here.
        for entry in staged:
            for key in ("backup_path", "temp_path"):
                path = entry.get(key)
                if path and os.path.exists(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

    return [entry["relative_path"] for entry in staged]
