"""The one classification assignment core every surface builds on (CLASS-01, D-07).

``set_labels`` writes through the assignment tables; ``load_classifications`` and
``attach_classifications`` are the batch read helpers REST, GraphQL, the plugin SDK
projection and the public shape share. Cardinality, family consistency, RESTRICT
and CASCADE are enforced by PostgreSQL (D-03, D-15); this module validates label
ids against the catalogue and keeps the writes de-duplicated.

Only attribute and object targets have a service path in 1.0.0 — the Schema and
ObjectType tables exist without an endpoint (D-09).
"""

import uuid
from collections.abc import Mapping
from typing import Any

from sqlalchemy import delete as sa_delete
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.redis import get_redis
from app.models import (
    ClassificationFamily,
    ClassificationLabel,
    Language,
    Object,
    ObjectClassificationLabel,
    ObjectTypeAttribute,
    ObjectTypeAttributeClassificationLabel,
    ObjectTypeClassificationLabel,
    SchemaClassificationLabel,
    Translation,
    TranslationType,
)
from app.services.audit_service import audit_log, model_to_dict
from app.services.cache_service import invalidate_entity_cache
from app.services.translation_service import delete_translations_for

FAMILY_CONFIDENTIALITY = "confidentiality"
FAMILY_PERSONAL_DATA = "personal_data"
FAMILY_EXPORT_CONTROL = "export_control"
PERSONAL_DATA_LABEL_KEY = "personal_data"


class _UnsetType:
    """Sentinel: the caller did not touch this family."""

    __slots__ = ()

    def __repr__(self) -> str:
        return "UNSET"


UNSET = _UnsetType()

# target_kind -> (assignment model, target column name)
_TARGETS: dict[str, tuple[type, str]] = {
    "attribute": (ObjectTypeAttributeClassificationLabel, "object_type_attribute_id"),
    "object": (ObjectClassificationLabel, "object_id"),
}

# target_kind -> the target table whose row serialises concurrent label writes
_TARGET_ROWS: dict[str, type] = {
    "attribute": ObjectTypeAttribute,
    "object": Object,
}


def _target_model(target_kind: str) -> tuple[type, str]:
    try:
        return _TARGETS[target_kind]
    except KeyError:
        raise ValueError(f"Unknown classification target kind: {target_kind}") from None


def _as_uuid(value: Any) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


async def load_classifications(
    db: AsyncSession, target_kind: str, target_ids: list[uuid.UUID]
) -> dict[uuid.UUID, list[dict]]:
    """Return label refs per target id, ordered family ``sort``, label ``position``, label ``key``.

    Every requested id is present in the result; a target without assignments
    maps to an empty list (D-06).
    """
    model, target_column = _target_model(target_kind)
    target_col = getattr(model, target_column)

    normalized = [_as_uuid(target_id) for target_id in target_ids]
    result: dict[uuid.UUID, list[dict]] = {target_id: [] for target_id in normalized}
    if not normalized:
        return result

    rows = await db.execute(
        select(
            target_col,
            ClassificationLabel.id,
            ClassificationLabel.key,
            ClassificationFamily.key,
            ClassificationLabel.name,
            ClassificationLabel.position,
        )
        .join(ClassificationLabel, model.label_id == ClassificationLabel.id)
        .join(ClassificationFamily, ClassificationLabel.family_id == ClassificationFamily.id)
        .where(target_col.in_(normalized))
        .order_by(
            ClassificationFamily.sort,
            ClassificationLabel.position,
            ClassificationLabel.key,
        )
    )
    for target_id, label_id, key, family, name, position in rows.all():
        result[target_id].append(
            {
                "label_id": label_id,
                "key": key,
                "family": family,
                "name": name,
                "position": position,
            }
        )
    return result


async def attach_classifications(
    db: AsyncSession, target_kind: str, items: list
) -> list:
    """Attach ``classification_labels`` to ORM instances or dicts in one query.

    Items are keyed by ``id`` (attribute or dict key); string and UUID ids are
    both accepted.
    """
    if not items:
        return items

    def _item_id(item) -> uuid.UUID:
        if isinstance(item, Mapping):
            return _as_uuid(item["id"])
        return _as_uuid(item.id)

    by_id = await load_classifications(db, target_kind, [_item_id(item) for item in items])
    for item in items:
        labels = by_id.get(_item_id(item), [])
        if isinstance(item, dict):
            item["classification_labels"] = labels
        else:
            item.classification_labels = labels
    return items


async def _family_rows(db: AsyncSession, family_keys: list[str]) -> dict[str, ClassificationFamily]:
    rows = await db.execute(
        select(ClassificationFamily).where(ClassificationFamily.key.in_(family_keys))
    )
    return {family.key: family for family in rows.scalars().all()}


async def _personal_data_label(db: AsyncSession) -> ClassificationLabel:
    result = await db.execute(
        select(ClassificationLabel)
        .join(ClassificationFamily, ClassificationLabel.family_id == ClassificationFamily.id)
        .where(
            ClassificationFamily.key == FAMILY_PERSONAL_DATA,
            ClassificationLabel.key == PERSONAL_DATA_LABEL_KEY,
            ClassificationLabel.is_system.is_(True),
        )
    )
    label = result.scalars().first()
    if label is None:
        raise ValueError("The personal_data system label is missing from the catalogue")
    return label


async def get_label_id_by_keys(
    db: AsyncSession, family_key: str, label_key: str
) -> uuid.UUID | None:
    """Resolve a label by its stable ``family key + label key`` pair (D-08)."""
    return await db.scalar(
        select(ClassificationLabel.id)
        .join(ClassificationFamily, ClassificationLabel.family_id == ClassificationFamily.id)
        .where(
            ClassificationFamily.key == family_key,
            ClassificationLabel.key == label_key,
        )
    )


class UnknownClassificationLabel(ValueError):
    """A template/tree block names a label key this instance does not know (D-18).

    Template and tree imports fail closed: the missing key is reported before
    anything is written and no label is created on the fly. REST maps it to a
    400, the strict tree path to its own 422 tree error.
    """

    def __init__(self, family: str, key: str) -> None:
        self.family = family
        self.key = key
        super().__init__(f"Unknown classification label '{family}/{key}'")


def template_classification(refs: list[dict] | None) -> dict | None:
    """The template block for one target's labels, keyed by stable label keys (D-08).

    ``refs`` is one target's ``load_classifications`` result, which is already in
    display order — ``export_control`` therefore keeps the catalogue's position
    order. ``None`` means "not classified" (an unclassified attribute's block);
    an empty dict would read as "classified with the defaults" instead.
    """
    if not refs:
        return None

    block: dict = {"confidentiality": None, "personal_data": False, "export_control": []}
    for ref in refs:
        family = ref.get("family")
        if family == FAMILY_CONFIDENTIALITY:
            block["confidentiality"] = ref["key"]
        elif family == FAMILY_PERSONAL_DATA:
            block["personal_data"] = True
        elif family == FAMILY_EXPORT_CONTROL:
            block["export_control"].append(ref["key"])
    return block


async def load_label_key_map(db: AsyncSession) -> dict[tuple[str, str], uuid.UUID]:
    """Every catalogue label as ``(family key, label key) -> label id`` (D-08, one query).

    Imports resolve their whole payload against this map before the first write,
    so an unknown key is caught while nothing exists yet (D-18).
    """
    rows = await db.execute(
        select(
            ClassificationFamily.key,
            ClassificationLabel.key,
            ClassificationLabel.id,
        ).join(ClassificationLabel, ClassificationLabel.family_id == ClassificationFamily.id)
    )
    return {(family_key, label_key): label_id for family_key, label_key, label_id in rows.all()}


def _classification_block_fields(spec) -> tuple[str | None, bool, list[str]]:
    """Read a template/tree block uniformly from a DTO or a plain mapping."""
    if isinstance(spec, Mapping):
        return (
            spec.get("confidentiality"),
            bool(spec.get("personal_data", False)),
            list(spec.get("export_control") or []),
        )
    return (
        getattr(spec, "confidentiality", None),
        bool(getattr(spec, "personal_data", False)),
        list(getattr(spec, "export_control", None) or []),
    )


def resolve_template_classification(
    spec, key_map: dict[tuple[str, str], uuid.UUID]
) -> dict:
    """Turn a template/tree block into ``set_labels`` kwargs carrying real label ids.

    Accepts a ``TemplateClassification``, the strict ``SchemaTreeClassification``
    or a plain mapping. Raises :class:`UnknownClassificationLabel` for the first
    key the catalogue does not know — callers resolve every block before the
    first write (D-18).
    """
    confidentiality_key, personal_data, export_control_keys = _classification_block_fields(spec)

    def _label_id(family_key: str, label_key: str) -> uuid.UUID:
        label_id = key_map.get((family_key, label_key))
        if label_id is None:
            raise UnknownClassificationLabel(family_key, label_key)
        return label_id

    return {
        "confidentiality": (
            None
            if confidentiality_key is None
            else _label_id(FAMILY_CONFIDENTIALITY, confidentiality_key)
        ),
        "personal_data": personal_data,
        "export_control": [
            _label_id(FAMILY_EXPORT_CONTROL, key) for key in export_control_keys
        ],
    }


async def set_labels(
    db: AsyncSession,
    target_kind: str,
    target_id: uuid.UUID,
    *,
    confidentiality=UNSET,
    personal_data=UNSET,
    export_control=UNSET,
) -> tuple[list[dict], list[dict]]:
    """Replace the given families' labels on one target; flush only, never commit.

    A family that is not passed (or is ``UNSET``) is left untouched. ``None``,
    ``[]`` and ``False`` clear a family. Returns ``(old_refs, new_refs)`` for the
    audit diff.

    The target row is locked ``FOR UPDATE`` first: locking the assignment rows
    would not serialise anything on the first write (a ``DELETE`` that matches
    no row takes no row lock), so two editors could both clear and both insert
    into a single-valued family and the loser would violate the partial unique
    index. A residual constraint failure is reported as ``ValueError`` — the
    same 400 contract the label validation already uses — instead of surfacing
    as an unmapped 500.
    """
    target_uuid = _as_uuid(target_id)
    model, target_column = _target_model(target_kind)
    target_col = getattr(model, target_column)
    target_row = _TARGET_ROWS[target_kind]

    locked_target = await db.scalar(
        select(target_row.id).where(target_row.id == target_uuid).with_for_update()
    )
    if locked_target is None:
        raise ValueError(f"Unknown classification target: {target_kind} {target_uuid}")

    old_refs = (await load_classifications(db, target_kind, [target_uuid]))[target_uuid]

    requested: dict[str, list[uuid.UUID]] = {}
    if confidentiality is not UNSET:
        requested[FAMILY_CONFIDENTIALITY] = (
            [] if confidentiality is None else [_as_uuid(confidentiality)]
        )
    if export_control is not UNSET:
        requested[FAMILY_EXPORT_CONTROL] = (
            []
            if export_control is None
            else list(dict.fromkeys(_as_uuid(label_id) for label_id in export_control))
        )
    if personal_data is not UNSET:
        if personal_data:
            requested[FAMILY_PERSONAL_DATA] = [(await _personal_data_label(db)).id]
        else:
            requested[FAMILY_PERSONAL_DATA] = []

    if not requested:
        return old_refs, old_refs

    all_label_ids = [label_id for ids in requested.values() for label_id in ids]
    labels_by_id: dict[uuid.UUID, ClassificationLabel] = {}
    actual_family: dict[uuid.UUID, str] = {}
    if all_label_ids:
        rows = await db.execute(
            select(ClassificationLabel, ClassificationFamily.key)
            .join(ClassificationFamily, ClassificationLabel.family_id == ClassificationFamily.id)
            .where(ClassificationLabel.id.in_(all_label_ids))
        )
        for label, family_key in rows.all():
            labels_by_id[label.id] = label
            actual_family[label.id] = family_key

    for family_key, ids in requested.items():
        for label_id in ids:
            if label_id not in labels_by_id:
                raise ValueError(f"Unknown classification label: {label_id}")
            if actual_family[label_id] != family_key:
                raise ValueError(
                    f"Classification label {label_id} is not part of family '{family_key}'"
                )

    families = await _family_rows(db, list(requested.keys()))

    for family_key, desired_ids in requested.items():
        family = families.get(family_key)
        if family is None:
            # Nothing can be assigned to a family that does not exist; a clear is a no-op.
            continue

        delete_stmt = sa_delete(model).where(
            target_col == target_uuid,
            model.family_id == family.id,
        )
        if desired_ids:
            delete_stmt = delete_stmt.where(model.label_id.notin_(desired_ids))
        await db.execute(delete_stmt)

        if desired_ids:
            existing_ids = set(
                (
                    await db.execute(
                        select(model.label_id).where(
                            target_col == target_uuid,
                            model.family_id == family.id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            for label_id in desired_ids:
                if label_id in existing_ids:
                    continue
                label = labels_by_id[label_id]
                db.add(
                    model(
                        **{
                            target_column: target_uuid,
                            "label_id": label_id,
                            "family_id": label.family_id,
                            "family_multi_valued": label.family_multi_valued,
                        }
                    )
                )

    try:
        await db.flush()
    except IntegrityError:
        # The partial unique index on (target_id, family_id) is the real guard
        # for single-valued families (D-03). The row lock above should make this
        # unreachable; if it is not, abort the request cleanly instead of
        # letting the constraint surface as a 500.
        await db.rollback()
        raise ValueError(
            "The classification was changed concurrently; reload and retry"
        ) from None
    new_refs = (await load_classifications(db, target_kind, [target_uuid]))[target_uuid]
    return old_refs, new_refs


def classification_kwargs(data, *, partial: bool) -> dict:
    """Translate a create/update DTO into ``set_labels`` kwargs (D-07).

    Create carries all three values. Update carries only the fields the client
    actually set: ``confidentiality: null`` clears, ``export_control: null``/``[]``
    clears, ``personal_data: null`` leaves the family unchanged.
    """
    if not partial:
        return {
            "confidentiality": getattr(data, "confidentiality", None),
            "personal_data": getattr(data, "personal_data", False),
            "export_control": list(getattr(data, "export_control", None) or []),
        }

    fields_set = set(getattr(data, "model_fields_set", set()))
    kwargs: dict = {}
    if "confidentiality" in fields_set:
        kwargs["confidentiality"] = data.confidentiality
    if "export_control" in fields_set:
        kwargs["export_control"] = list(data.export_control or [])
    if "personal_data" in fields_set and data.personal_data is not None:
        kwargs["personal_data"] = data.personal_data
    return kwargs


def classification_audit_keys(refs: list[dict]) -> list[str]:
    """Stable ``family/key`` keys for the audit diff."""
    return sorted(f"{ref['family']}/{ref['key']}" for ref in refs)


async def list_families(db: AsyncSession, lang: str | None = None) -> list[dict]:
    """The instance catalogue, optionally localised, in display order (D-12, D-13).

    Two ordered reads (families by ``sort``, labels by ``position``) plus — when
    ``lang`` is given — one direct translation lookup for the label names. The
    schema-scoped ``resolve_translations`` pipeline is deliberately not used:
    the catalogue is global and has no owning schema (RESEARCH Pitfall 5).

    A label without a translation in ``lang`` keeps its base name and reports
    ``resolved_language: None``, so a caller never mistakes the base name for a
    translated one.
    """
    families = (
        await db.execute(
            select(ClassificationFamily).order_by(
                ClassificationFamily.sort, ClassificationFamily.key
            )
        )
    ).scalars().all()

    family_key_by_id = {family.id: family.key for family in families}
    label_rows = (
        await db.execute(
            select(ClassificationLabel).order_by(
                ClassificationLabel.position, ClassificationLabel.key
            )
        )
    ).scalars().all()

    translated_names: dict[uuid.UUID, str] = {}
    if lang:
        rows = await db.execute(
            select(Translation.entity_id, Translation.value)
            .join(
                TranslationType,
                Translation.translation_type_id == TranslationType.id,
            )
            .join(Language, Translation.language_id == Language.id)
            .where(
                TranslationType.code == "classification_label",
                Translation.field == "name",
                Language.code == lang,
            )
        )
        translated_names = dict(rows.all())

    labels_by_family: dict[str, list[dict]] = {}
    for label in label_rows:
        family_key = family_key_by_id.get(label.family_id)
        if family_key is None:
            # Cannot happen while the FK holds; skip rather than invent a family.
            continue
        translated = translated_names.get(label.id)
        labels_by_family.setdefault(family_key, []).append(
            {
                "id": label.id,
                "family": family_key,
                "key": label.key,
                "name": translated if translated is not None else label.name,
                "position": label.position,
                "is_system": label.is_system,
                "resolved_language": lang if translated is not None else None,
                "created_at": label.created_at,
                "updated_at": label.updated_at,
            }
        )

    return [
        {
            "id": family.id,
            "key": family.key,
            "name": family.name,
            "is_multi_valued": family.is_multi_valued,
            "is_ranked": family.is_ranked,
            "is_system": family.is_system,
            "allows_operator_labels": family.allows_operator_labels,
            "sort": family.sort,
            "labels": labels_by_family.get(family.key, []),
        }
        for family in families
    ]


class ClassificationNotFound(Exception):
    """The addressed label does not exist (404 at the API boundary)."""


class ClassificationConflict(Exception):
    """The catalogue refuses the write: system row, assigned label, duplicate key (409)."""


# The four assignment tables the RESTRICT FK on ``classification_labels`` guards
# (D-01, D-15). Checked explicitly so the API answers 409 with a readable message
# instead of surfacing a raw foreign-key error.
_ASSIGNMENT_MODELS: tuple[type, ...] = (
    SchemaClassificationLabel,
    ObjectTypeClassificationLabel,
    ObjectTypeAttributeClassificationLabel,
    ObjectClassificationLabel,
)

# Fail-open public cache namespaces a catalogue change can make stale. The
# token-lookup namespace is a preserved prefix in ``maintenance/cutover.py`` and
# is deliberately absent here: catalogue writes never touch authentication
# payloads.
_PUBLIC_CACHE_PATTERNS: tuple[str, ...] = (
    "cache:public:object:*",
    "cache:public:objecttype:*",
    "cache:public:objecttype-schema:*",
    "cache:public:schema:*",
    "cache:public:schemas:*",
)
# SCAN batch size: the Redis default of 10 makes a sweep over a large shared
# database thousands of round trips.
_SCAN_BATCH = 1000


async def purge_classification_dependent_caches() -> None:
    """Drop every cache that renders label names or the assignment structure.

    The tag-based invalidation covers the internal attribute/object payload
    caches; the public ``cache:public:*`` keys are written with ``set_cached``
    (no tags), so only a SCAN can reach them — mirroring
    ``purge_public_object_cache`` (fail-open: a Redis error never fails the
    catalogue write, the TTL bounds the staleness).
    """
    await invalidate_entity_cache("object_type_attribute")
    await invalidate_entity_cache("object")
    try:
        redis = await get_redis()
        for pattern in _PUBLIC_CACHE_PATTERNS:
            # Batched SCAN: the default COUNT of 10 turns a 40k-key namespace
            # (arq results live in the same database) into thousands of round
            # trips, which made a label rename take seconds.
            batch: list[str] = []
            async for key in redis.scan_iter(pattern, count=_SCAN_BATCH):
                batch.append(key)
                if len(batch) >= _SCAN_BATCH:
                    await redis.delete(*batch)
                    batch.clear()
            if batch:
                await redis.delete(*batch)
    except Exception:
        # Fail-open: a catalogue write must not fail because the cache is down.
        pass


async def _get_label(db: AsyncSession, label_id: uuid.UUID) -> ClassificationLabel:
    label = await db.scalar(
        select(ClassificationLabel).where(ClassificationLabel.id == _as_uuid(label_id))
    )
    if label is None:
        raise ClassificationNotFound(f"Classification label {label_id} does not exist")
    return label


def _label_payload(label: ClassificationLabel, family_key: str) -> dict:
    """The catalogue shape of one label: ``family`` is the family key, not its id."""
    return {
        "id": label.id,
        "family": family_key,
        "key": label.key,
        "name": label.name,
        "position": label.position,
        "is_system": label.is_system,
        # A just-written label carries no translation in the requested language.
        "resolved_language": None,
        "created_at": label.created_at,
        "updated_at": label.updated_at,
    }


async def _next_position(db: AsyncSession, family_id: uuid.UUID) -> int:
    current_max = await db.scalar(
        select(func.max(ClassificationLabel.position)).where(
            ClassificationLabel.family_id == family_id
        )
    )
    return 0 if current_max is None else current_max + 1


async def create_label(db: AsyncSession, data, actor_id: uuid.UUID | None) -> dict:
    """Add one operator label to a family that accepts them (D-14).

    Raises ``ValueError`` for an unknown family key (400) and
    ``ClassificationConflict`` for a family without operator labels or a
    duplicate key inside the same family (409).
    """
    family = await db.scalar(
        select(ClassificationFamily).where(ClassificationFamily.key == data.family)
    )
    if family is None:
        raise ValueError(f"Unknown classification family: {data.family}")
    if not family.allows_operator_labels:
        raise ClassificationConflict(
            f"Family '{family.key}' does not accept operator-created labels"
        )

    duplicate = await db.scalar(
        select(ClassificationLabel.id).where(
            ClassificationLabel.family_id == family.id,
            ClassificationLabel.key == data.key,
        )
    )
    if duplicate is not None:
        raise ClassificationConflict(
            f"A label with key '{data.key}' already exists in family '{family.key}'"
        )

    position = data.position
    if position is None:
        position = await _next_position(db, family.id)

    label = ClassificationLabel(
        family_id=family.id,
        family_multi_valued=family.is_multi_valued,
        key=data.key,
        name=data.name,
        position=position,
        is_system=False,
    )
    db.add(label)
    try:
        await db.flush()
    except IntegrityError:
        # Lost a race against a concurrent create of the same key.
        await db.rollback()
        raise ClassificationConflict(
            f"A label with key '{data.key}' already exists in family '{family.key}'"
        ) from None

    await audit_log(
        db,
        "settings",
        label.id,
        "CREATE",
        actor_id,
        old_values=None,
        new_values=model_to_dict(label),
    )
    await db.commit()
    await db.refresh(label)
    return _label_payload(label, family.key)


async def update_label(
    db: AsyncSession, label_id: uuid.UUID, data, actor_id: uuid.UUID | None
) -> dict:
    """Rename and/or reorder one label; ``key``/``family`` are immutable (D-14).

    ``ClassificationNotFound`` for a missing label, ``ClassificationConflict``
    for a system label. Only the fields the client actually sent are applied.
    """
    label = await _get_label(db, label_id)
    if label.is_system:
        raise ClassificationConflict(
            f"Classification label '{label.key}' is a system label and cannot be modified"
        )

    old_values = model_to_dict(label)
    fields_set = set(getattr(data, "model_fields_set", set()))
    if "name" in fields_set and data.name is not None:
        label.name = data.name
    if "position" in fields_set and data.position is not None:
        label.position = data.position

    await db.flush()
    await audit_log(
        db,
        "settings",
        label.id,
        "UPDATE",
        actor_id,
        old_values=old_values,
        new_values=model_to_dict(label),
    )
    await db.commit()
    await db.refresh(label)

    # Label names and ranks are rendered inside attribute and object payloads.
    await purge_classification_dependent_caches()
    family_key = await db.scalar(
        select(ClassificationFamily.key).where(ClassificationFamily.id == label.family_id)
    )
    return _label_payload(label, family_key)


async def delete_label(db: AsyncSession, label_id: uuid.UUID, actor_id: uuid.UUID | None) -> None:
    """Delete one unassigned operator label together with its translations (D-15).

    A system label or a label still assigned at any level is refused with
    ``ClassificationConflict``; the RESTRICT foreign key is the backstop for a
    row assigned between the pre-check and the commit.
    """
    label = await _get_label(db, label_id)
    if label.is_system:
        raise ClassificationConflict(
            f"Classification label '{label.key}' is a system label and cannot be deleted"
        )

    for model in _ASSIGNMENT_MODELS:
        assigned = await db.scalar(
            select(model.label_id).where(model.label_id == label.id).limit(1)
        )
        if assigned is not None:
            raise ClassificationConflict(
                f"Classification label '{label.key}' is still assigned and cannot be deleted"
            )

    old_values = model_to_dict(label)
    # Translations have no FK to their entity — remove them in the same transaction.
    await delete_translations_for(db, "classification_label", [label.id])
    await audit_log(
        db,
        "settings",
        label.id,
        "DELETE",
        actor_id,
        old_values=old_values,
        new_values=None,
    )
    await db.delete(label)
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        raise ClassificationConflict(
            f"Classification label '{old_values.get('key')}' is still assigned and cannot be deleted"
        ) from None
