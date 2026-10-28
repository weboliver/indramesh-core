"""Per-request batched record-level read visibility for GraphQL resolvers.

List-filter semantics via build_record_permission_filter: a read DENY on the
record or on its object type, schema or source hides it; no entry means the
permissive default; superusers bypass (quick 261011-tgu/261011-tv1).

``attribute_type`` is a single-level record model (no ancestor cascade), so it
is answerable here exactly like the hierarchy entities — D-05 (317-04) needs
the attribute-type level to be a first-class question, not only a clause
inside ``value_visibility``.
"""

import uuid
from collections import defaultdict
from functools import partial

from sqlalchemy import select
from strawberry.dataloader import DataLoader
from strawberry.types import Info

from app.models.attribute_type import AttributeType
from app.models.object import Object
from app.models.object_type import ObjectType
from app.models.schema import Schema
from app.models.source import Source
from app.services.record_permission_service import build_record_permission_filter

_VISIBILITY_MODELS = {
    "object": Object,
    "object_type": ObjectType,
    "schema": Schema,
    "source": Source,
    "attribute_type": AttributeType,
}


def record_visibility_loader(session, user_id, lock) -> DataLoader:
    """Return a DataLoader keyed by ``(entity_type, record_id)`` yielding bool."""

    async def _load_fn(keys, session, user_id, lock) -> list[bool]:
        keys = [(entity_type, uuid.UUID(str(record_id))) for entity_type, record_id in keys]
        ids_by_type: dict[str, list[uuid.UUID]] = defaultdict(list)
        for entity_type, record_id in keys:
            if entity_type not in _VISIBILITY_MODELS:
                raise ValueError(f"Unsupported entity_type: {entity_type}")
            ids_by_type[entity_type].append(record_id)

        visible: set[tuple[str, uuid.UUID]] = set()
        for entity_type, ids in ids_by_type.items():
            model = _VISIBILITY_MODELS[entity_type]
            async with lock:
                clause = await build_record_permission_filter(
                    session, str(user_id), entity_type, is_superuser=False
                )
                if clause is None:
                    visible.update((entity_type, i) for i in ids)
                    continue
                result = await session.execute(
                    select(model.id).where(model.id.in_(ids)).where(clause)
                )
                visible.update((entity_type, row) for row in result.scalars().all())
        return [(entity_type, record_id) in visible for entity_type, record_id in keys]

    return DataLoader(
        load_fn=partial(_load_fn, session=session, user_id=user_id, lock=lock)
    )


def _visibility_loader(info: Info) -> DataLoader:
    loader = getattr(info.context, "record_visibility", None)
    if loader is None:
        loader = record_visibility_loader(
            info.context.session, info.context.user.id, info.context.lock
        )
        info.context.record_visibility = loader
    return loader


async def record_visible(info: Info, entity_type: str, record_id: uuid.UUID) -> bool:
    """True unless the record (or an ancestor) carries a read DENY for the user.

    Callers must never hold info.context.lock while awaiting this (not reentrant).
    """
    if info.context.user.is_superuser:
        return True
    return await _visibility_loader(info).load((entity_type, record_id))


async def visible_ids(info: Info, entity_type: str, record_ids) -> set[uuid.UUID]:
    """Return the subset of record_ids visible to the user (same rules as record_visible)."""
    record_ids = list(record_ids)
    if not record_ids:
        return set()
    if info.context.user.is_superuser:
        return set(record_ids)
    flags = await _visibility_loader(info).load_many(
        [(entity_type, record_id) for record_id in record_ids]
    )
    return {record_id for record_id, ok in zip(record_ids, flags) if ok}
