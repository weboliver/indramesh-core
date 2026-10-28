"""Connector-neutral effective write lock for core value writes (290 D-02..D-05).

A value is effectively locked when the object's OWN ObjectType carries
``externally_readonly`` OR the resolved attribute row does (D-02). The
ObjectType that matters is the requested/owning object's type — never the
defining ancestor of an inherited attribute; an inherited attribute's own row
still carries its marker across the inheritance.

Authority is always a fresh persisted read taken with ``SELECT ... FOR SHARE``
(never the Redis metadata cache), so a stale cached projection cannot bypass
the server-side lock and a concurrent marker change cannot slip between check
and write. ``managed_by`` alone never locks anything — only the boolean
markers do (D-01/D-03).

``ValueLockConflict`` is deliberately NOT a ``ValueError``: callers must map it
explicitly (REST -> 409, public API -> 409 with its ``errors`` list) instead of
falling into the pre-existing 400/404 validation mapping.

290 D-15 extends the same authority to destructive deletes: an
``externally_readonly`` ObjectType blocks deleting its Objects, the type itself
(including every cascade descendant) and its Schema via ``assert_types_deletable``.
"""

import uuid
from datetime import datetime, timezone
from typing import Iterable, Optional

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Object, ObjectType, ObjectTypeAttribute, ObjectWriteProtectionRelease


class ValueLockConflict(Exception):
    """A real write/delete targets an externally read-only ObjectType/attribute."""

    def __init__(
        self,
        *,
        attribute_name: Optional[str] = None,
        object_type_name: Optional[str] = None,
        managed_by: Optional[str] = None,
        object_type_names: Optional[list[str]] = None,
    ) -> None:
        self.attribute_name = attribute_name
        self.object_type_name = object_type_name
        self.object_type_names = list(object_type_names) if object_type_names else None
        self.managed_by = managed_by
        scope = f" (managed by '{managed_by}')" if managed_by else ""
        if self.object_type_names:
            names = ", ".join(f"'{name}'" for name in self.object_type_names)
            label = "Object type" if len(self.object_type_names) == 1 else "Object types"
            verb = "is" if len(self.object_type_names) == 1 else "are"
            super().__init__(
                f"{label} {names} {verb} externally read-only{scope}; "
                "detach before deleting."
            )
            return
        target = f" for attribute '{attribute_name}'" if attribute_name else ""
        super().__init__(f"Value is externally read-only{scope}{target}.")


def _locked(
    object_type: Optional[ObjectType],
    attribute: Optional[ObjectTypeAttribute],
) -> bool:
    return bool(object_type is not None and object_type.externally_readonly) or bool(
        attribute is not None and attribute.externally_readonly
    )


def _owner_exempt(
    origin,
    object_type: Optional[ObjectType],
    attribute: Optional[ObjectTypeAttribute],
) -> bool:
    """Whether ``origin`` may write through a marker it owns (D-08).

    Only an attribute-level marker owned by the exact plugin that set it is
    exempted. An ``externally_readonly`` ObjectType is never exempt, whoever
    owns it: D-08 writes no ObjectType markers, and a directly set one must
    keep blocking every writer. The origin is Core-built (``plugin_origin``);
    a user/import/None origin is never exempt, so no request payload can reach
    the exception.
    """
    if object_type is not None and object_type.externally_readonly:
        return False
    if origin is None or getattr(origin, "kind", None) != "plugin":
        return False
    if attribute is None or not attribute.externally_readonly:
        return False
    if not attribute.managed_by:
        return False
    return attribute.managed_by == origin.name


async def _released(db: AsyncSession, object_id: uuid.UUID, owner: str) -> bool:
    """Fresh persisted per-object release read under ``FOR SHARE`` (D-11).

    Never the cache: a stale projection must not unlock an object and a
    concurrent release/relock cannot slip between check and write.
    """
    result = await db.execute(
        select(ObjectWriteProtectionRelease.object_id)
        .where(
            ObjectWriteProtectionRelease.object_id == object_id,
            ObjectWriteProtectionRelease.managed_by == owner,
        )
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none() is not None


async def _object_release_exempt(
    db: AsyncSession,
    object_id: Optional[uuid.UUID],
    object_type: Optional[ObjectType],
    attribute: Optional[ObjectTypeAttribute],
) -> bool:
    """Whether a per-object release lifts *this* attribute marker (D-11).

    Honoured only when the object is named, the ObjectType itself is not
    ``externally_readonly`` (a type-level marker is never released), the
    attribute carries a non-NULL ``managed_by`` and that exact owner holds a
    release row for the object.
    """
    if object_id is None:
        return False
    if object_type is not None and object_type.externally_readonly:
        return False
    if attribute is None or not attribute.externally_readonly:
        return False
    if not attribute.managed_by:
        return False
    return await _released(db, object_id, attribute.managed_by)


async def _lock_object_type(
    db: AsyncSession, object_type_id: uuid.UUID
) -> Optional[ObjectType]:
    """Fresh persisted ObjectType read under ``FOR SHARE`` (never the cache)."""
    result = await db.execute(
        select(ObjectType)
        .where(ObjectType.id == object_type_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


async def _lock_attribute(
    db: AsyncSession, attribute_id: uuid.UUID
) -> Optional[ObjectTypeAttribute]:
    """Fresh persisted ObjectTypeAttribute read under ``FOR SHARE``."""
    result = await db.execute(
        select(ObjectTypeAttribute)
        .where(ObjectTypeAttribute.id == attribute_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    return result.scalar_one_or_none()


_ANCESTOR_ATTRIBUTES_BY_NAME_SQL = text("""
    WITH RECURSIVE ot_ancestors AS (
        SELECT id, parent_id, inherits_attributes, 0 AS depth
        FROM object_types
        WHERE id = :object_type_id
        UNION ALL
        SELECT ot.id, ot.parent_id, ot.inherits_attributes, a.depth + 1
        FROM object_types ot
        INNER JOIN ot_ancestors a ON ot.id = a.parent_id
        WHERE ot.inherits_attributes = true AND a.depth < 100
    )
    SELECT ota.id AS id, a.depth AS depth
    FROM object_type_attributes ota
    INNER JOIN ot_ancestors a ON ota.object_type_id = a.id
    WHERE ota.name = :attribute_name AND a.depth > 0
    ORDER BY a.depth ASC, ota.is_system DESC, ota.id ASC
""")


async def resolve_attribute_by_name(
    db: AsyncSession, object_type_id: uuid.UUID, attribute_name: Optional[str]
) -> tuple[Optional[ObjectTypeAttribute], bool]:
    """Resolve an attribute by name on the type's effective chain.

    Own type first (system attributes preferred), then the nearest ancestor
    that inherits attributes — the same resolution order the system-attribute
    mirror uses. Returns ``(attribute, ambiguous)``; ``ambiguous`` is True when
    more than one candidate exists at the winning level, in which case the
    caller must fail closed instead of guessing which binding was meant.
    """
    if not attribute_name:
        return None, False

    own_result = await db.execute(
        select(ObjectTypeAttribute)
        .where(
            ObjectTypeAttribute.object_type_id == object_type_id,
            ObjectTypeAttribute.name == attribute_name,
        )
        .order_by(ObjectTypeAttribute.is_system.desc(), ObjectTypeAttribute.id.asc())
    )
    own = own_result.scalars().all()
    if len(own) > 1:
        return None, True
    if own:
        return own[0], False

    rows = (
        await db.execute(
            _ANCESTOR_ATTRIBUTES_BY_NAME_SQL,
            {"object_type_id": str(object_type_id), "attribute_name": attribute_name},
        )
    ).all()
    if not rows:
        return None, False
    nearest_depth = rows[0].depth
    nearest = [row for row in rows if row.depth == nearest_depth]
    if len(nearest) > 1:
        return None, True
    return await db.get(ObjectTypeAttribute, nearest[0].id), False


async def resolve_name_attribute(
    db: AsyncSession, object_type_id: uuid.UUID, *, system_only: bool = False
) -> Optional[ObjectTypeAttribute]:
    """Own-first/inherited Name attribute — the binding the name mirror writes.

    Mirrors the existing mirror resolutions: ``system_only=True`` matches
    ``_resolve_system_ota(..., "Name")`` (creation path), ``system_only=False``
    matches ``update_object()``'s rename mirror (any own Name, system
    preferred; inherited parent Name as fallback).
    """
    query = select(ObjectTypeAttribute).where(
        ObjectTypeAttribute.object_type_id == object_type_id,
        ObjectTypeAttribute.name == "Name",
    )
    if system_only:
        query = query.where(ObjectTypeAttribute.is_system.is_(True))
    query = (
        query.order_by(ObjectTypeAttribute.is_system.desc(), ObjectTypeAttribute.id.asc())
        .limit(1)
    )
    own = (await db.execute(query)).scalar_one_or_none()
    if own is not None:
        return own

    from app.services.object_type_service import resolve_ancestor_ota_by_name

    inherited_id = await resolve_ancestor_ota_by_name(db, "Name", object_type_id)
    if inherited_id is None:
        return None
    return await db.get(ObjectTypeAttribute, inherited_id)


async def assert_value_writable(
    db: AsyncSession,
    *,
    object_type_id: uuid.UUID,
    attribute_id: uuid.UUID,
    origin=None,
    object_id: Optional[uuid.UUID] = None,
) -> None:
    """Raise ``ValueLockConflict`` when the effective persisted lock applies.

    An attribute marker owned by the calling plugin (``origin``) does not block
    that plugin's own write (D-08). A concrete ``object_id`` whose attribute
    marker is released for the marker's owner (D-11) is writable for every
    caller. The ``origin`` must be the raw caller origin — never a synthesized
    one — so a user/import/None caller stays blocked.
    """
    object_type = await _lock_object_type(db, object_type_id)
    attribute = await _lock_attribute(db, attribute_id)
    if not _locked(object_type, attribute):
        return
    if _owner_exempt(origin, object_type, attribute):
        return
    if await _object_release_exempt(db, object_id, object_type, attribute):
        return
    managed_by = None
    if attribute is not None and attribute.externally_readonly:
        managed_by = attribute.managed_by
    elif object_type is not None and object_type.externally_readonly:
        managed_by = object_type.managed_by
    raise ValueLockConflict(
        attribute_name=attribute.name if attribute is not None else None,
        object_type_name=object_type.name if object_type is not None else None,
        managed_by=managed_by,
    )


# OTA write-protection changes are audited against the owning ObjectType — the
# same auditable entity type ``object_type_attribute_service`` uses, so the
# ObjectType's history shows the marker toggle.
_MARKER_AUDIT_ENTITY_TYPE = "object_types"


async def set_attribute_write_protection(
    db: AsyncSession,
    attribute_id: uuid.UUID,
    *,
    readonly: bool,
    owner: str,
    actor_id: Optional[uuid.UUID] = None,
) -> bool:
    """Set or lift one plugin-owned attribute write-protection marker (D-08).

    ``owner`` is Core-set (the calling plugin's ``_plugin_name``) and is never
    taken from a request payload. The attribute row is locked ``FOR UPDATE``
    (never the cache), so the call serializes against the ``FOR SHARE`` reads
    the enforcement performs and a concurrent value write cannot interleave.

    Returns ``True`` when the marker changed in the requested direction and
    ``False`` for an already-in-that-state no-op. Setting is refused with
    ``ValueLockConflict`` when another (or no) owner holds the marker;
    lifting is refused unless this owner holds it.
    """
    result = await db.execute(
        select(ObjectTypeAttribute)
        .where(ObjectTypeAttribute.id == attribute_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attribute = result.scalar_one_or_none()
    if attribute is None:
        raise ValueError("Object type attribute not found")

    if readonly:
        if attribute.externally_readonly:
            if attribute.managed_by == owner:
                return False
            raise ValueLockConflict(
                attribute_name=attribute.name, managed_by=attribute.managed_by
            )
    else:
        if not attribute.externally_readonly:
            return False
        if attribute.managed_by != owner:
            raise ValueLockConflict(
                attribute_name=attribute.name, managed_by=attribute.managed_by
            )

    from app.services.audit_service import audit_log, model_to_dict
    from app.services.cache_service import invalidate_entity_cache

    old_values = model_to_dict(attribute)
    if readonly:
        attribute.externally_readonly = True
        attribute.managed_by = owner
    else:
        attribute.externally_readonly = False
        attribute.managed_by = None
    new_values = model_to_dict(attribute)

    if actor_id is not None:
        await audit_log(
            db,
            _MARKER_AUDIT_ENTITY_TYPE,
            attribute.object_type_id,
            "UPDATE",
            actor_id,
            old_values=old_values,
            new_values=new_values,
        )

    await db.commit()
    await invalidate_entity_cache("object_type_attribute")
    await invalidate_entity_cache("object_type")
    from app.services.object_type_attribute_service import (
        _invalidate_descendant_resolved_caches,
    )

    await _invalidate_descendant_resolved_caches(db, attribute.object_type_id)
    return True


async def set_object_write_protection_release(
    db: AsyncSession,
    object_id: uuid.UUID,
    *,
    released: bool,
    owner: str,
    actor_id: Optional[uuid.UUID] = None,
) -> bool:
    """Set or lift this plugin's per-object write-protection release (D-11).

    ``owner`` is Core-set (the calling plugin's ``_plugin_name``) and is never
    taken from a request payload. The call only ever touches the ``(object_id,
    owner)`` row, so a plugin can neither create nor remove another node's
    release. Returns ``True`` when a row was inserted/deleted and ``False`` for
    an already-in-that-state no-op; an unknown object raises ``ValueError``.
    """
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from app.services.audit_service import audit_log, model_to_dict
    from app.services.cache_service import invalidate_entity_cache

    result = await db.execute(
        select(Object)
        .where(Object.id == object_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    obj = result.scalar_one_or_none()
    if obj is None:
        raise ValueError("Object not found")

    if released:
        insert_result = await db.execute(
            pg_insert(ObjectWriteProtectionRelease)
            .values(object_id=object_id, managed_by=owner)
            .on_conflict_do_nothing(index_elements=["object_id", "managed_by"])
        )
        changed = insert_result.rowcount > 0
    else:
        delete_result = await db.execute(
            delete(ObjectWriteProtectionRelease).where(
                ObjectWriteProtectionRelease.object_id == object_id,
                ObjectWriteProtectionRelease.managed_by == owner,
            )
        )
        changed = delete_result.rowcount > 0

    if changed and actor_id is not None:
        old_values = model_to_dict(obj)
        old_values["write_protection_release_owner"] = None if released else owner
        new_values = dict(old_values)
        new_values["write_protection_release_owner"] = owner if released else None
        await audit_log(
            db,
            "objects",
            object_id,
            "UPDATE",
            actor_id,
            old_values=old_values,
            new_values=new_values,
            object_id=object_id,
        )

    await db.commit()
    await invalidate_entity_cache("object")
    return changed


async def released_attribute_ids(db: AsyncSession, object_id: uuid.UUID) -> list[uuid.UUID]:
    """Attribute ids whose marker a per-object release lifts for this object (D-11).

    The read-side projection of ``_object_release_exempt``: an attribute-level
    marker whose owner holds a release row for the object, over the object's
    ObjectType and its ancestors. A type-level marker is never released, so an
    ``externally_readonly`` ObjectType yields nothing.
    """
    obj = (
        await db.execute(select(Object.object_type_id).where(Object.id == object_id))
    ).scalar_one_or_none()
    if obj is None:
        return []
    owners = (
        await db.execute(
            select(ObjectWriteProtectionRelease.managed_by).where(
                ObjectWriteProtectionRelease.object_id == object_id
            )
        )
    ).scalars().all()
    if not owners:
        return []

    type_ids: list[uuid.UUID] = []
    type_id: Optional[uuid.UUID] = obj
    while type_id is not None and type_id not in type_ids and len(type_ids) < 100:
        row = (
            await db.execute(
                select(ObjectType.parent_id, ObjectType.externally_readonly).where(
                    ObjectType.id == type_id
                )
            )
        ).one_or_none()
        if row is None:
            break
        if not type_ids and row.externally_readonly:
            return []
        type_ids.append(type_id)
        type_id = row.parent_id

    result = await db.execute(
        select(ObjectTypeAttribute.id).where(
            ObjectTypeAttribute.object_type_id.in_(type_ids),
            ObjectTypeAttribute.externally_readonly.is_(True),
            ObjectTypeAttribute.managed_by.in_(owners),
        )
    )
    return list(result.scalars().all())


async def assert_types_deletable(
    db: AsyncSession, type_ids: Iterable[uuid.UUID | str]
) -> None:
    """Raise ``ValueLockConflict`` when a listed ObjectType is locked (D-15).

    The delete-side twin of ``assert_value_writable``: a fresh persisted read
    of every type in the cascade set under ``FOR SHARE`` (never the cache).
    ``managed_by`` alone never blocks, and an empty collection is a no-op — a
    Schema without ObjectTypes deletes normally.
    """
    normalized = [
        type_id if isinstance(type_id, uuid.UUID) else uuid.UUID(str(type_id))
        for type_id in type_ids
    ]
    if not normalized:
        return
    result = await db.execute(
        select(ObjectType)
        .where(ObjectType.id.in_(normalized))
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    locked = [row for row in result.scalars().all() if row.externally_readonly]
    if not locked:
        return
    locked.sort(key=lambda row: row.name or "")
    raise ValueLockConflict(
        object_type_names=[row.name for row in locked if row.name],
        managed_by=next((row.managed_by for row in locked if row.managed_by), None),
    )


async def assert_binding_writable(
    db: AsyncSession,
    *,
    object_type_id: uuid.UUID,
    attribute_name: Optional[str],
    origin=None,
    object_id: Optional[uuid.UUID] = None,
) -> None:
    """Raise ``ValueLockConflict`` for a locked reference binding (fail closed).

    An attribute marker owned by the calling plugin (``origin``) does not block
    that plugin's own binding write (D-08). A concrete ``object_id`` whose
    attribute marker is released for the marker's owner (D-11) is writable. An
    ambiguous attribute name still fails closed for every writer, including the
    owner and a released object.
    """
    object_type = await _lock_object_type(db, object_type_id)
    if object_type is not None and object_type.externally_readonly:
        raise ValueLockConflict(
            object_type_name=object_type.name, managed_by=object_type.managed_by
        )
    if not attribute_name:
        return
    attribute, ambiguous = await resolve_attribute_by_name(
        db, object_type_id, attribute_name
    )
    if ambiguous:
        raise ValueLockConflict(attribute_name=attribute_name)
    if attribute is None:
        return
    locked = await _lock_attribute(db, attribute.id)
    if locked is not None and locked.externally_readonly:
        if _owner_exempt(origin, object_type, locked):
            return
        if await _object_release_exempt(db, object_id, object_type, locked):
            return
        raise ValueLockConflict(
            attribute_name=locked.name, managed_by=locked.managed_by
        )