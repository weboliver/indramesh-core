"""Object-comment orchestration (Phase 280 plan 02 tracer).

Mirrors object_file_service's shape for the parts that transfer directly
(display-name resolution via the User/UserProfile outerjoin, object-scoped
list, dual-filter lookup, hard delete) while diverging where comments
differ: created-asc default ordering (D-04 — the opposite of files'
hardcoded desc()), a post-trim 64,000-character boundary, and no storage
leg at all (D-14).
"""

import logging
import uuid
from typing import List

from sqlalchemy import delete as sa_delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ObjectComment, User, UserProfile
from app.schemas.object_comment import ObjectCommentResponse

logger = logging.getLogger(__name__)


class ConnectorCommentConflict(ValueError):
    """A connector write addressed a deterministic id owned by another row (D-13).

    The SDK derives the Core comment id from ``{source}:{object}:{external_id}``,
    so this only fires on a genuine hash collision or a bug; it is surfaced as a
    typed ``CoreDataConflict`` and never silently reparents an existing row.
    """


async def create_object_comment(
    db: AsyncSession,
    *,
    object_id: uuid.UUID,
    body: str,
    author_id: uuid.UUID,
) -> ObjectCommentResponse:
    """Persist one Object comment (COMMENT-01).

    author_id comes exclusively from the authenticated current_user
    (T-280-06 impersonation mitigation); the display name resolves on the
    subsequent list fetch, mirroring create_object_file's identical
    null-display-name-on-create pattern — the frontend's post-save query
    invalidation refetches the list for the real name.
    """
    comment = ObjectComment(
        object_id=object_id, author_id=author_id, body=body
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return _to_response(comment, None)


async def list_object_comments(
    db: AsyncSession, object_id: uuid.UUID, order: str = "asc"
) -> List[ObjectCommentResponse]:
    """Chronological comment list with author display names (COMMENT-02).

    Defaults to created ASC (D-04 — oldest first, the opposite of
    list_object_files' hardcoded desc()); `order="desc"` reverses it.
    Display resolution mirrors object_file_service's exact fallback chain
    (First Last -> First -> Last -> username -> None), never a raw UUID
    (D-06).
    """
    order_col = (
        ObjectComment.created_at.asc()
        if order == "asc"
        else ObjectComment.created_at.desc()
    )
    # IN-05 (280-REVIEW.md): a fixed secondary sort on the unique id makes the
    # relative order of same-created_at comments a deterministic function of
    # the data rather than of the query planner's arbitrary tie handling (a
    # latent flake for the D-04 ordering contract). Deliberately id.asc() in
    # both directions, so a tie resolves identically regardless of `order`.
    rows = (
        await db.execute(
            select(
                ObjectComment,
                UserProfile.first_name,
                UserProfile.last_name,
                User.username,
            )
            .outerjoin(User, ObjectComment.author_id == User.id)
            .outerjoin(UserProfile, User.id == UserProfile.user_id)
            .where(ObjectComment.object_id == object_id)
            .order_by(order_col, ObjectComment.id.asc())
        )
    ).all()
    items = []
    for row in rows:
        comment = row[0]
        if comment.managed_by is not None:
            # 290 D-12: managed_by alone decides external origin. A
            # connector-managed comment's author is the external display name
            # stored on the row: author_id is always NULL for these rows, so the
            # User/UserProfile chain below would resolve to None/empty — this
            # explicit override makes the external name authoritative and
            # prevents the frontend ever falling back to "System" (T-281-05).
            display_name = comment.external_author_name
            items.append(_to_response(comment, display_name))
            continue
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
        items.append(_to_response(comment, display_name))
    return items


async def get_object_comment(
    db: AsyncSession, object_id: uuid.UUID, comment_id: uuid.UUID
) -> ObjectComment | None:
    """Object-scoped ObjectComment lookup (T-280-04).

    Filters by BOTH parent object and comment id, so a comment UUID belonging
    to a different Object can never be addressed through a mismatched Object
    path — mirrors get_object_file's IDOR mitigation.
    """
    return (
        await db.execute(
            select(ObjectComment).where(
                ObjectComment.object_id == object_id,
                ObjectComment.id == comment_id,
            )
        )
    ).scalar_one_or_none()


async def delete_object_comment(
    db: AsyncSession, object_id: uuid.UUID, comment_id: uuid.UUID
) -> bool:
    """Hard-delete one Object comment (COMMENT-03/04).

    A plain SQL DELETE under the same dual filter — never an UPDATE with a
    soft-delete marker, no deleted_at/is_deleted column exists anywhere in
    the schema. Returns False when no row is addressable under the given
    parent Object (route turns it into 404). Idempotent by construction:
    the second concurrent DELETE's rowcount is 0, exactly one caller wins.

    290 D-12/T-290-04-01: a connector-managed comment (managed_by IS NOT
    NULL) is read-only and returns False (→ 404) BEFORE any delete logic runs
    — identical to delete_object_file's guard. The persisted provenance is
    the only authority (never a client-provided flag).
    A hand-crafted DELETE therefore cannot remove a row the connector owns.
    """
    comment = await get_object_comment(db, object_id, comment_id)
    if not comment:
        return False
    if comment.managed_by is not None:
        return False
    result = await db.execute(
        sa_delete(ObjectComment).where(
            ObjectComment.object_id == object_id,
            ObjectComment.id == comment_id,
        )
    )
    await db.commit()
    return result.rowcount > 0


async def upsert_connector_comment(
    db: AsyncSession,
    *,
    object_id: uuid.UUID,
    comment_id: uuid.UUID,
    body: str,
    external_author_name: str | None,
    managed_by: str,
) -> tuple[ObjectCommentResponse, bool]:
    """Read-or-create one connector-managed comment on its deterministic id (D-13).

    The Core row is created with ``author_id=NULL`` and the Core-owned
    ``managed_by`` marker, so the persisted provenance (not an external link
    row) is what makes it read-only through the native API. A replay on the
    same id returns the committed row (``created=False``) without a second
    insert; a genuinely changed body/author updates only this connector-managed
    row.
    """
    existing = await db.get(ObjectComment, comment_id)
    if existing is not None:
        if existing.object_id != object_id or existing.managed_by != managed_by:
            raise ConnectorCommentConflict(
                "The connector comment id is already bound to a different parent"
            )
        if (
            existing.body != body
            or existing.external_author_name != external_author_name
        ):
            existing.body = body
            existing.external_author_name = external_author_name
            await db.commit()
            await db.refresh(existing)
        return _to_response(existing, external_author_name), False

    comment = ObjectComment(
        id=comment_id,
        object_id=object_id,
        author_id=None,
        body=body,
        external_author_name=external_author_name,
        managed_by=managed_by,
    )
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return _to_response(comment, external_author_name), True


async def delete_connector_comment(
    db: AsyncSession,
    object_id: uuid.UUID,
    comment_id: uuid.UUID,
    managed_by: str,
) -> bool:
    """Delete a comment owned by THIS connector (D-13).

    The native ``delete_object_comment`` guard is untouched: it still refuses a
    connector-managed row. This connector-scoped path deletes only a row whose
    persisted ``managed_by`` matches the connector that owns it, so a remote
    reconciliation can remove its own surplus comments without ever weakening the
    native read-only contract.
    """
    comment = await get_object_comment(db, object_id, comment_id)
    if not comment or comment.managed_by != managed_by:
        return False
    result = await db.execute(
        sa_delete(ObjectComment).where(
            ObjectComment.object_id == object_id,
            ObjectComment.id == comment_id,
        )
    )
    await db.commit()
    return result.rowcount > 0


def _to_response(
    comment: ObjectComment, display_name: str | None
) -> ObjectCommentResponse:
    return ObjectCommentResponse(
        id=comment.id,
        body=comment.body,
        author_display_name=display_name,
        created_at=comment.created_at,
        # 290 D-12/D-14: managed_by is the single source of truth; is_external
        # is derived from it on the DTO.
        managed_by=comment.managed_by,
    )