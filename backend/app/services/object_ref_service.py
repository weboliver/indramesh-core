import uuid
from typing import List

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, or_, select, text, tuple_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AttributeType,
    AttributeTypeDataType,
    Object,
    ObjectRef,
    ObjectType,
    Source,
)
from app.schemas.crud import ObjectRefCreate, ObjectRefUpdate
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.services.object_service import (
    _fire_object_hook,
    _resolve_origin,
    _touch_system_attribute,
)
from app.services.audit_service import audit_log, model_to_dict
from app.services.record_permission_service import (
    _level_denied_sql,
    build_record_permission_filter,
)
from app.services.value_lock_service import assert_binding_writable, resolve_attribute_by_name
from app.services.value_visibility import (
    assert_record_action_allowed,
    resolve_principal_roles,
)
from app.services import hook_origin

ENTITY_TYPE = "object_ref"


class InvalidRefAttributeError(ValueError):
    """A reference names an attribute that does not resolve on the from-object's
    effective chain (unknown, ambiguous, or not reference-typed).

    Routers translate this to **400** — unlike the other ``ValueError``s of this
    module, which mean "the referenced row does not exist" and answer 404.
    EXT-09-D1/D-12: a name-keyed row must not be written against a name no
    attribute carries, or it detaches on the next rename.
    """


async def _assert_reference_attribute(
    db: AsyncSession, object_type_id: uuid.UUID, attribute_name: str | None
) -> None:
    """Validate a supplied attribute name against the from-object's chain.

    ``None``/empty stays valid: the column is nullable and the plugin SDK and
    the Jira pull create nameless refs today. Only a supplied name is resolved —
    own type first, then the nearest inheriting ancestor (the same resolution
    the value-lock guard uses). Ambiguity and a non-reference data type are
    rejected, because a ref bound to either cannot be maintained by name.
    """
    if not attribute_name:
        return
    attribute, ambiguous = await resolve_attribute_by_name(
        db, object_type_id, attribute_name
    )
    if ambiguous:
        raise InvalidRefAttributeError(
            f"Ambiguous reference attribute name: {attribute_name}"
        )
    if attribute is None:
        raise InvalidRefAttributeError(
            f"Unknown reference attribute name: {attribute_name}"
        )
    data_type = (
        await db.execute(
            select(AttributeType.data_type).where(
                AttributeType.id == attribute.attribute_type_id
            )
        )
    ).scalar_one_or_none()
    if data_type != AttributeTypeDataType.REFERENCE:
        raise InvalidRefAttributeError(
            f"Attribute is not reference-typed: {attribute_name}"
        )


# One bounded map over the **effective** ancestor chain: for every object type
# that actually carries refs, the (type id, attribute name) pairs whose nearest
# resolution is a hidden reference attribute. A plain join on
# `object_type_id + from_attribute_name` would only match an own-type attribute
# and let an inherited hidden attribute through (SEC313-97 / pitfall P-09).
# The after-1.0 `from_attribute_id` column turns this into a plain join.
#
# `ref_types` bounds the walk to the distinct from-types present in
# `object_refs` — never a full `object_types` scan — and the ancestor step
# gates on the joined parent's `inherits_attributes`, the same parent-side
# semantics the existing ancestor-attribute resolution uses (D-06). The
# `is_hidden DESC` tiebreaker makes an ambiguous same-level name fail closed.
_HIDDEN_REF_ATTRIBUTES_SQL = text("""
    WITH RECURSIVE ref_types AS (
        SELECT DISTINCT object_type_id
        FROM object_refs
        WHERE object_type_id IS NOT NULL
    ),
    ot_ancestors AS (
        SELECT rt.object_type_id AS root_id, ot.id, ot.parent_id,
               ot.inherits_attributes, 0 AS depth
        FROM ref_types rt
        INNER JOIN object_types ot ON ot.id = rt.object_type_id

        UNION ALL

        SELECT a.root_id, ot.id, ot.parent_id, ot.inherits_attributes,
               a.depth + 1
        FROM ot_ancestors a
        INNER JOIN object_types ot ON ot.id = a.parent_id
        WHERE ot.inherits_attributes = true AND a.depth < 100
    )
    SELECT resolved.root_id, resolved.name
    FROM (
        SELECT DISTINCT ON (a.root_id, ota.name)
            a.root_id AS root_id,
            ota.name AS name,
            ota.is_hidden AS is_hidden
        FROM ot_ancestors a
        INNER JOIN object_type_attributes ota ON ota.object_type_id = a.id
        INNER JOIN attribute_types at ON at.id = ota.attribute_type_id
        WHERE at.data_type = :data_type
        ORDER BY a.root_id, ota.name, a.depth ASC, ota.is_hidden DESC
    ) resolved
    WHERE resolved.is_hidden = true
""")


async def hidden_ref_clause(db: AsyncSession):
    """A WHERE clause excluding refs of a hidden attribute, or ``None``.

    Exact per-type pairing (``NOT IN ((type, name), ...)``): a hidden name on
    one type must not hide the same name on a type where it is visible. A
    nameless ref belongs to no attribute and is never matched.
    """
    rows = (
        await db.execute(
            _HIDDEN_REF_ATTRIBUTES_SQL,
            {"data_type": AttributeTypeDataType.REFERENCE.value},
        )
    ).all()
    if not rows:
        return None
    pairs = [(row.root_id, row.name) for row in rows]
    return or_(
        ObjectRef.from_attribute_name.is_(None),
        tuple_(ObjectRef.object_type_id, ObjectRef.from_attribute_name).notin_(pairs),
    )


async def ref_hidden_by_attribute(db: AsyncSession, ref_id: uuid.UUID) -> bool:
    """Whether ``ref_id`` is stored under a hidden reference attribute.

    The caller has already established that the reference exists; a nameless
    reference is never hidden (the clause keeps ``from_attribute_name IS NULL``).
    """
    hidden_clause = await hidden_ref_clause(db)
    if hidden_clause is None:
        return False
    visible = (
        await db.execute(
            select(ObjectRef.id).where(ObjectRef.id == ref_id).where(hidden_clause)
        )
    ).scalar_one_or_none()
    return visible is None


# AUTHZ-08 / D-08: the same ancestor-name resolution as
# ``_HIDDEN_REF_ATTRIBUTES_SQL``, one level further. The inner select resolves
# the nearest same-named reference attribute per (root type, name) exactly as
# before; the attribute-type record DENY is applied in the **outer** WHERE on
# the resolved row's ``attribute_type_id``. That order is the point: filtering
# inside the ``DISTINCT ON`` subquery would run *before* the nearest-ancestor
# pick, so a root type whose nearest same-named attribute is denied would fall
# back to an ancestor's permitted attribute and expose exactly the refs the DENY
# must hide. The deny fragment must reference ``resolved.attribute_type_id`` —
# the ``at`` alias is out of scope outside the derived table.
#
# Values interpolated into this string come from the module-level action table
# only (``_level_denied_sql``); the request values (``:data_type``,
# ``:role_ids``) ride as bind parameters.
_DENIED_REF_ATTRIBUTES_SQL = """
    WITH RECURSIVE ref_types AS (
        SELECT DISTINCT object_type_id
        FROM object_refs
        WHERE object_type_id IS NOT NULL
    ),
    ot_ancestors AS (
        SELECT rt.object_type_id AS root_id, ot.id, ot.parent_id,
               ot.inherits_attributes, 0 AS depth
        FROM ref_types rt
        INNER JOIN object_types ot ON ot.id = rt.object_type_id

        UNION ALL

        SELECT a.root_id, ot.id, ot.parent_id, ot.inherits_attributes,
               a.depth + 1
        FROM ot_ancestors a
        INNER JOIN object_types ot ON ot.id = a.parent_id
        WHERE ot.inherits_attributes = true AND a.depth < 100
    )
    SELECT resolved.root_id, resolved.name
    FROM (
        SELECT DISTINCT ON (a.root_id, ota.name)
            a.root_id AS root_id,
            ota.name AS name,
            ota.attribute_type_id AS attribute_type_id
        FROM ot_ancestors a
        INNER JOIN object_type_attributes ota ON ota.object_type_id = a.id
        INNER JOIN attribute_types at ON at.id = ota.attribute_type_id
        WHERE at.data_type = :data_type
        ORDER BY a.root_id, ota.name, a.depth ASC
    ) resolved
    WHERE {level_denied}
"""


async def denied_ref_attribute_clause(
    db: AsyncSession,
    *,
    user_id: str | None = None,
    role_ids: list[uuid.UUID] | None = None,
    is_superuser: bool = False,
):
    """A WHERE clause excluding refs of an attribute type the caller may not read.

    RLR-05 bypass first, before any query: ``resolve_principal_roles`` answers
    ``None`` for a superuser, so a caller that only forwarded ``role_ids`` would
    otherwise resolve the administrator's real roles with ``is_superuser=False``
    and strip refs the administrator may see.

    ``role_ids`` lets a caller that already resolved the principal's roles pass
    the list once (pitfall P-08: one bound value for every ``:role_ids``
    fragment in the statement). No roles means the DENY-only model cannot match
    a ``record_permissions`` row at all, so the CTE is skipped entirely.
    """
    if is_superuser:
        return None
    if role_ids is None:
        if user_id is None:
            return None
        role_ids = await resolve_principal_roles(db, user_id, False)
    if not role_ids:
        return None
    statement = text(
        _DENIED_REF_ATTRIBUTES_SQL.format(
            level_denied=_level_denied_sql(
                "'attribute_type'", "resolved.attribute_type_id", "read"
            )
        )
    )
    rows = (
        await db.execute(
            statement,
            {
                "data_type": AttributeTypeDataType.REFERENCE.value,
                "role_ids": role_ids,
            },
        )
    ).all()
    if not rows:
        return None
    pairs = [(row.root_id, row.name) for row in rows]
    return or_(
        ObjectRef.from_attribute_name.is_(None),
        tuple_(ObjectRef.object_type_id, ObjectRef.from_attribute_name).notin_(pairs),
    )


async def ref_denied_by_attribute_type(
    db: AsyncSession,
    ref_id: uuid.UUID,
    *,
    user_id: str | None = None,
    role_ids: list[uuid.UUID] | None = None,
    is_superuser: bool = False,
) -> bool:
    """Whether ``ref_id`` is stored under an attribute type the caller may not read.

    Id-based on purpose: the by-id route may serve the reference from the entity
    cache as a plain dict, so the check must never read ``from_attribute_name``
    off that object (pitfall 4). Mirrors :func:`ref_hidden_by_attribute`.
    """
    if is_superuser:
        return False
    denied_clause = await denied_ref_attribute_clause(
        db, user_id=user_id, role_ids=role_ids, is_superuser=is_superuser
    )
    if denied_clause is None:
        return False
    visible = (
        await db.execute(
            select(ObjectRef.id).where(ObjectRef.id == ref_id).where(denied_clause)
        )
    ).scalar_one_or_none()
    return visible is None


async def _source_attribute_type_id(
    db: AsyncSession, object_type_id: uuid.UUID, attribute_name: str | None
) -> uuid.UUID | None:
    """The attribute type behind a reference's SOURCE attribute, or ``None``.

    Resolved through :func:`resolve_attribute_by_name` — the single-type
    resolver :func:`_assert_reference_attribute` already uses for this exact
    question (own type first, then the nearest inheriting ancestor). The
    ancestor-name CTEs above deliberately are **not** the base here: they walk
    every reference-carrying type in ``object_refs`` and answer the different
    read-side question ("which stored pairs are denied anywhere").

    A nameless reference belongs to no attribute (the plugin-SDK/Jira-pull
    shape) and stays writable; an unresolvable name has already answered its
    named 400 in :func:`_assert_reference_attribute` before this runs.
    """
    if not attribute_name:
        return None
    attribute, ambiguous = await resolve_attribute_by_name(
        db, object_type_id, attribute_name
    )
    if ambiguous or attribute is None:
        return None
    return attribute.attribute_type_id


async def _object_filter(db: AsyncSession, user_id: str | None, is_superuser: bool):
    if user_id is None:
        return None
    return await build_record_permission_filter(
        db, user_id, "object", is_superuser=is_superuser
    )


async def _ends_visible(db: AsyncSession, object_filter, from_id, to_id) -> bool:
    if object_filter is None:
        return True
    ends = {from_id, to_id}
    result = await db.execute(
        select(Object.id).where(Object.id.in_(list(ends))).where(object_filter)
    )
    return len(set(result.scalars().all())) == len(ends)


def visible_ref_conditions(object_filter) -> tuple:
    """Both-ends record visibility for ObjectRef list queries (quick 261012-cpp)."""
    visible_ids = select(Object.id).where(object_filter)
    return (
        ObjectRef.from_object_id.in_(visible_ids),
        ObjectRef.to_object_id.in_(visible_ids),
    )


async def object_ref_visible(
    db: AsyncSession, ref, user_id: str, *, is_superuser: bool
) -> bool:
    """Whether the caller may read both ends of ``ref`` (an ORM row or the
    cached dict). A ref with a DENY'd from/to object behaves like a nonexistent
    one (quick 261012-cpp); superusers cost zero queries."""
    if is_superuser:
        return True
    if isinstance(ref, dict):
        from_id = uuid.UUID(str(ref["from_object_id"]))
        to_id = uuid.UUID(str(ref["to_object_id"]))
    else:
        from_id, to_id = ref.from_object_id, ref.to_object_id
    object_filter = await _object_filter(db, user_id, False)
    return await _ends_visible(db, object_filter, from_id, to_id)


async def create_object_ref(
    db: AsyncSession,
    ref_data: ObjectRefCreate,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    user_id: str | None = None,
    is_superuser: bool = False,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
    attribute_type_checked: bool = False,
) -> ObjectRef:
    """``user_id`` None means a trusted internal caller with no principal at
    all (the plugin SDK) and stays unfiltered; with ``user_id`` set a DENY'd
    from/to object behaves like a nonexistent one (quick 261012-cpp) and a
    record ``write`` DENY on the SOURCE end answers 403 (AUTHZ-07 / D-02 as
    revised 2026-11-01). The target only has to be readable.

    AUTHZ-08 / D-09: the SOURCE **attribute type** carries the same ``write``
    decision as the source object; the target end and the target's attribute
    type never gate a write (a shared catalog object must stay referenceable).
    ``attribute_type_checked`` says the caller already enforced that decision
    for this reference's source attribute type — the composite public create
    decides every payload attribute type in one batched statement
    (``validate_object_payload`` step 8b), so the gate here would re-open the
    REV-03 finding per reference. It skips only that gate, never the read
    filter or the source-object ``write`` gate.

    ``commit``/``invalidate_cache`` are the D-15 composite-writer switches — the
    public create/replace routes own the single transaction and the one cache
    purge. ``emit_index_outbox`` is accepted for a uniform composite call site:
    a reference write never creates an outbox row of its own (only the owning
    object does), so it has no effect here.
    """
    object_filter = await _object_filter(db, user_id, is_superuser)
    from_query = select(Object).where(Object.id == ref_data.from_object_id)
    if object_filter is not None:
        from_query = from_query.where(object_filter)
    result = await db.execute(from_query)
    from_obj = result.scalar_one_or_none()
    if not from_obj:
        raise ValueError("From object not found")

    to_query = select(Object).where(Object.id == ref_data.to_object_id)
    if object_filter is not None:
        to_query = to_query.where(object_filter)
    result = await db.execute(to_query)
    to_obj = result.scalar_one_or_none()
    if not to_obj:
        raise ValueError("To object not found")

    # AUTHZ-07 / D-02 (as revised 2026-11-01): a reference changes only its
    # SOURCE, so the source needs a record `write` decision; the target only has
    # to be READABLE, which the object filter above already enforced. Requiring
    # `write` on the target would make a write-protected catalog object (a
    # location, a vendor) unlinkable. Ordered after the read filter on purpose:
    # an invisible end keeps its `ValueError` -> 404 above, so the invisible and
    # the non-writable cases never collapse into one status code (D-01). The
    # helper is the same 403 path every value write uses.
    # ``user_id`` None is a trusted internal caller with no principal at all
    # (the plugin SDK); the public composite create passes one (Task 3).
    if user_id is not None:
        await assert_record_action_allowed(
            user_id, is_superuser, object_id=from_obj.id, action="write"
        )

    # EXT-09-D1 / D-12: the row's type is the FROM object's type. A caller that
    # still passes a stale type id (SDK/plugin) is normalised instead of being
    # rejected — the previous behaviour persisted the mismatch unvalidated.
    effective_object_type_id = from_obj.object_type_id

    result = await db.execute(
        select(ObjectType).where(ObjectType.id == effective_object_type_id)
    )
    object_type = result.scalar_one_or_none()
    if not object_type:
        raise ValueError("Object type not found")

    result = await db.execute(
        select(Source).where(Source.id == ref_data.source_id)
    )
    source = result.scalar_one_or_none()
    if not source:
        raise ValueError("Source not found")

    # EXT-09-D1/D-12: a name that does not resolve on the chain is a 400
    # (InvalidRefAttributeError), not a 404: the endpoint exists, the supplied
    # attribute does not. Validated before the lock decision so an unknown or
    # ambiguous name answers its named 400 even on a locked type — the lock
    # check itself fails closed on ambiguity and would mask the real error.
    await _assert_reference_attribute(
        db, effective_object_type_id, ref_data.from_attribute_name
    )

    # AUTHZ-08 / D-09: a reference changes only its SOURCE, so the source's
    # ATTRIBUTE TYPE needs a record `write` decision beside the source object's
    # (319 D-02). Placed directly after the name validation — an unknown or
    # ambiguous name keeps its named 400 — and before the lock decision and the
    # insert, so a denied write adds no row. The target end stays untouched
    # here: requiring anything on it would make a write-protected catalog
    # object unlinkable, which D-09 rejects explicitly.
    if user_id is not None and not attribute_type_checked:
        source_attribute_type_id = await _source_attribute_type_id(
            db, effective_object_type_id, ref_data.from_attribute_name
        )
        if source_attribute_type_id is not None:
            await assert_record_action_allowed(
                user_id,
                is_superuser,
                attribute_type_id=source_attribute_type_id,
                action="write",
            )

    # 290 D-02/D-03/D-04: deny a real binding write while the source is
    # externally managed (own object type OR resolved bound attribute),
    # before the insert/audit/commit. Resolved against the SOURCE object's
    # type chain — never the reference target's type.
    await assert_binding_writable(
        db,
        object_type_id=from_obj.object_type_id,
        attribute_name=ref_data.from_attribute_name,
        origin=origin,
        object_id=ref_data.from_object_id,
    )

    db_ref = ObjectRef(
        from_object_id=ref_data.from_object_id,
        to_object_id=ref_data.to_object_id,
        object_type_id=effective_object_type_id,
        source_id=ref_data.source_id,
        from_attribute_name=ref_data.from_attribute_name,
    )
    db.add(db_ref)
    await db.flush()
    new_values = model_to_dict(db_ref)
    await audit_log(db, "object_refs", db_ref.id, "CREATE", actor_id,
                    old_values=None, new_values=new_values, object_id=ref_data.from_object_id)
    # CORESEAM-11/D-01/D-04: emit the ref-created hook in the SAME transaction as
    # the insert, after audit and BEFORE the first commit (best-effort D-05). The
    # primary object is the source endpoint; the payload carries both endpoint
    # ids in the snapshot, the trusted actor and origin. It never routes through
    # on_object_updated (D-03).
    effective_origin = _resolve_origin(origin, "object.ref.create", actor_id)
    await _fire_object_hook(
        "on_object_ref_created",
        db,
        object_id=ref_data.from_object_id,
        schema_id=from_obj.schema_id,
        object_type_id=from_obj.object_type_id,
        ref_id=db_ref.id,
        old_values=None,
        new_values=new_values,
        actor_id=actor_id,
        origin=effective_origin.as_payload(),
    )
    if not commit:
        # Composite writer (D-15): flush only; the caller owns the single
        # commit, the enqueue and the one purge (and touches "Updated" once).
        await db.flush()
        return db_ref
    await db.commit()
    await db.refresh(db_ref)
    await _touch_system_attribute(db, from_obj.id, from_obj.object_type_id, "Updated")
    result = await db.execute(
        select(ObjectRef)
        .where(ObjectRef.id == db_ref.id)
        .options(
            selectinload(ObjectRef.to_object),
            selectinload(ObjectRef.from_object),
            selectinload(ObjectRef.object_type),
        )
    )
    loaded = result.scalar_one_or_none()
    if invalidate_cache:
        await invalidate_entity_cache(ENTITY_TYPE)
        await invalidate_entity_cache("search")
    return loaded or db_ref


async def list_object_refs(
    db: AsyncSession, skip: int = 0, limit: int = 100,
    from_object_id: uuid.UUID | None = None,
    to_object_id: uuid.UUID | None = None,
    user_role: str | None = None,  # NEW per D-11
    *,
    user_id: str | None = None,
    is_superuser: bool = False,
) -> tuple[List[ObjectRef], int]:
    """``user_id`` None means a trusted internal caller and stays unfiltered;
    with ``user_id`` set refs with a DENY'd from/to object are omitted from the
    items and the total, the shared list cache is bypassed entirely
    (quick 261012-cpp), and refs of a hidden reference attribute are omitted
    (SEC313-97)."""
    from sqlalchemy.orm import selectinload
    query = select(ObjectRef).options(selectinload(ObjectRef.to_object), selectinload(ObjectRef.from_object))
    count_query = select(func.count()).select_from(ObjectRef)
    if from_object_id:
        query = query.where(ObjectRef.from_object_id == from_object_id)
        count_query = count_query.where(ObjectRef.from_object_id == from_object_id)
    if to_object_id:
        query = query.where(ObjectRef.to_object_id == to_object_id)
        count_query = count_query.where(ObjectRef.to_object_id == to_object_id)
    object_filter = await _object_filter(db, user_id, is_superuser)
    conditions: list = []
    if object_filter is not None:
        conditions.extend(visible_ref_conditions(object_filter))
    if user_id is not None and not is_superuser:
        # SEC313-97: hidden attribute names are metadata, not principal-scoped,
        # but the unfiltered shared cache must never serve a restricted caller
        # (the same rule the object DENY path follows). AUTHZ-08 / D-08 adds the
        # attribute-type record DENY beside it — that one *is* principal-scoped,
        # so its roles are resolved once here and handed to the single fragment.
        hidden_clause = await hidden_ref_clause(db)
        if hidden_clause is not None:
            conditions.append(hidden_clause)
        role_ids = await resolve_principal_roles(db, user_id, False)
        denied_clause = await denied_ref_attribute_clause(
            db, user_id=user_id, role_ids=role_ids, is_superuser=False
        )
        if denied_clause is not None:
            conditions.append(denied_clause)
    if conditions:
        query = query.where(*conditions)
        count_query = count_query.where(*conditions)
        total = (await db.execute(count_query)).scalar()
        items = (await db.execute(query.offset(skip).limit(limit))).scalars().all()
        return items, total
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit, from_object_id=from_object_id, to_object_id=to_object_id, user_role=user_role)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    serialized = []
    for ref in items:
        d = {c.key: getattr(ref, c.key) for c in ref.__table__.columns}
        d["to_object_name"] = ref.to_object.name if ref.to_object else None
        d["from_object_name"] = ref.from_object.name if ref.from_object else None
        serialized.append(d)
    await set_cached_with_tags(
        cache_key,
        {"items": serialized, "total": total},
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_object_ref(
    db: AsyncSession, ref_id: uuid.UUID
) -> ObjectRef | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(ref_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(ObjectRef)
        .where(ObjectRef.id == ref_id)
        .options(selectinload(ObjectRef.to_object), selectinload(ObjectRef.from_object))
    )
    ref = result.scalar_one_or_none()
    if ref:
        payload = {c.key: getattr(ref, c.key) for c in ref.__table__.columns}
        payload["to_object_name"] = ref.to_object_name
        payload["from_object_name"] = ref.from_object_name
        await set_cached_with_tags(
            cache_key,
            jsonable_encoder(payload),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return ref


async def update_object_ref(
    db: AsyncSession,
    ref_id: uuid.UUID,
    ref_update: ObjectRefUpdate,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    user_id: str | None = None,
    is_superuser: bool = False,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> ObjectRef | None:
    """``user_id`` None means a trusted internal caller (SDK) and stays
    unfiltered; with ``user_id`` set a ref with a DENY'd end and a DENY'd
    from/to object behave like nonexistent ones (quick 261012-cpp), and a
    record ``write`` DENY on the ref's source — current or newly proposed —
    answers 403 (AUTHZ-07 / D-02 as revised 2026-11-01). The same holds for the
    source's **attribute type** (AUTHZ-08 / D-09). A ref stored under a hidden
    or attribute-type-DENY'd reference attribute answers the route's 404 before
    the no-op and validation paths (SEC313-97 / WR-02, U-02; AUTHZ-08 / D-08).

    Composite switches as in :func:`create_object_ref`.
    """
    object_filter = await _object_filter(db, user_id, is_superuser)
    result = await db.execute(
        select(ObjectRef).where(ObjectRef.id == ref_id)
    )
    ref = result.scalar_one_or_none()
    if not ref:
        return None
    # quick 261012-cpp: before the D-05 no-op return, so a hidden ref never
    # answers 200 with its names.
    if not await _ends_visible(db, object_filter, ref.from_object_id, ref.to_object_id):
        return None
    # SEC313-97 / WR-02, U-02: a hidden reference answers exactly like a missing
    # one — before the D-05 no-op return (which would echo its names) and before
    # attribute validation (which would answer 400).
    if (
        user_id is not None
        and not is_superuser
        and await ref_hidden_by_attribute(db, ref.id)
    ):
        return None
    # AUTHZ-08 / D-08: the same refusal for a ref stored under an attribute
    # type the caller may not read. Id-based, so it never reads the name off a
    # cached dict (pitfall 4) and never echoes it.
    if (
        user_id is not None
        and not is_superuser
        and await ref_denied_by_attribute_type(
            db, ref.id, user_id=user_id, is_superuser=is_superuser
        )
    ):
        return None

    # AUTHZ-07 / D-02 (as revised 2026-11-01): the ref's SOURCE must be
    # writable — the persisted one, and a different one the update moves the
    # ref onto. A target (persisted or supplied) only has to be readable,
    # which the object filter below already enforces. Checked before the D-05
    # no-op return so a no-op patch on a write-DENIED ref cannot answer 200.
    # A proposed source the caller cannot read is deliberately skipped here:
    # it keeps answering the route's 404 when it is resolved below (D-01).
    if user_id is not None:
        await assert_record_action_allowed(
            user_id, is_superuser, object_id=ref.from_object_id, action="write"
        )
        # AUTHZ-08 / D-09 on the same seam: the source's ATTRIBUTE TYPE carries
        # its own `write` decision — persisted binding first, then a proposed
        # one. Both before the no-op return, so a no-op patch on a
        # write-DENIED attribute type cannot answer 200 either. The target end
        # and the target's attribute type never gate a write.
        persisted_attribute_type_id = await _source_attribute_type_id(
            db, ref.object_type_id, ref.from_attribute_name
        )
        if persisted_attribute_type_id is not None:
            await assert_record_action_allowed(
                user_id,
                is_superuser,
                attribute_type_id=persisted_attribute_type_id,
                action="write",
            )
        proposed_from_id = (
            ref_update.from_object_id
            if ref_update.from_object_id is not None
            else ref.from_object_id
        )
        proposed_name = (
            ref_update.from_attribute_name
            if ref_update.from_attribute_name is not None
            else ref.from_attribute_name
        )
        proposed_type_id = ref.object_type_id
        if proposed_from_id != ref.from_object_id:
            readable_new_source = select(Object.object_type_id).where(
                Object.id == proposed_from_id
            )
            if object_filter is not None:
                readable_new_source = readable_new_source.where(object_filter)
            proposed_type_id = (
                await db.execute(readable_new_source)
            ).scalar_one_or_none()
            if proposed_type_id is not None:
                await assert_record_action_allowed(
                    user_id,
                    is_superuser,
                    object_id=proposed_from_id,
                    action="write",
                )
        if proposed_type_id is not None and (
            proposed_from_id != ref.from_object_id
            or proposed_name != ref.from_attribute_name
        ):
            proposed_attribute_type_id = await _source_attribute_type_id(
                db, proposed_type_id, proposed_name
            )
            if proposed_attribute_type_id is not None:
                await assert_record_action_allowed(
                    user_id,
                    is_superuser,
                    attribute_type_id=proposed_attribute_type_id,
                    action="write",
                )

    # 290 D-05: an unchanged payload is a side-effect-free no-op (locked refs
    # included) — checked before any lock read or mutation.
    supplied = {
        field: getattr(ref_update, field)
        for field in ref_update.model_fields_set
        if getattr(ref_update, field) is not None
    }
    if not any(getattr(ref, field) != value for field, value in supplied.items()):
        return ref

    # 290 D-02/D-04: a real change is denied when either the persisted or the
    # proposed source binding is locked. The binding belongs to the SOURCE
    # object's type chain (never the reference target's type); an ambiguous
    # binding name fails closed (T-290-05-03).
    # D-02/D-14: the hook's primary object is the ORIGINAL source, so capture it
    # (id + schema/type) before the mutation may move `from_object_id`.
    original_from_id = ref.from_object_id
    original_from_name = ref.from_attribute_name
    original_result = await db.execute(
        select(Object).where(Object.id == original_from_id)
    )
    original_from_obj = original_result.scalar_one_or_none()

    proposed_from_id = (
        ref_update.from_object_id if ref_update.from_object_id is not None
        else ref.from_object_id
    )
    proposed_name = (
        ref_update.from_attribute_name if ref_update.from_attribute_name is not None
        else ref.from_attribute_name
    )
    proposed_query = select(Object).where(Object.id == proposed_from_id)
    if object_filter is not None:
        proposed_query = proposed_query.where(object_filter)
    proposed_result = await db.execute(proposed_query)
    proposed_from_obj = proposed_result.scalar_one_or_none()
    if proposed_from_obj is None:
        raise ValueError("From object not found")
    # EXT-09-D1/D-12: only a **supplied** name is validated — a retarget that
    # leaves the name alone must not be refused because a legacy row carries a
    # name that no longer resolves. Validated before the lock decision so an
    # unknown or ambiguous name answers its named 400 rather than the lock
    # check's fail-closed 409.
    await _assert_reference_attribute(
        db, proposed_from_obj.object_type_id, ref_update.from_attribute_name
    )
    await assert_binding_writable(
        db,
        object_type_id=proposed_from_obj.object_type_id,
        attribute_name=proposed_name,
        origin=origin,
        object_id=proposed_from_id,
    )
    if proposed_from_id != original_from_id or proposed_name != original_from_name:
        if original_from_obj is not None:
            await assert_binding_writable(
                db,
                object_type_id=original_from_obj.object_type_id,
                attribute_name=original_from_name,
                origin=origin,
                object_id=original_from_id,
            )

    old_values = model_to_dict(ref)
    if ref_update.from_object_id is not None:
        from_query = select(Object).where(Object.id == ref_update.from_object_id)
        if object_filter is not None:
            from_query = from_query.where(object_filter)
        result = await db.execute(from_query)
        if not result.scalar_one_or_none():
            raise ValueError("From object not found")
        ref.from_object_id = ref_update.from_object_id
    if ref_update.to_object_id is not None:
        to_query = select(Object).where(Object.id == ref_update.to_object_id)
        if object_filter is not None:
            to_query = to_query.where(object_filter)
        result = await db.execute(to_query)
        if not result.scalar_one_or_none():
            raise ValueError("To object not found")
        ref.to_object_id = ref_update.to_object_id
    # EXT-09-D1 / D-12: the stored type always follows the from object's type.
    # A supplied `object_type_id` is accepted for wire compatibility but
    # normalised rather than persisted as a mismatch.
    ref.object_type_id = proposed_from_obj.object_type_id
    if ref_update.source_id is not None:
        result = await db.execute(
            select(Source).where(Source.id == ref_update.source_id)
        )
        if not result.scalar_one_or_none():
            raise ValueError("Source not found")
        ref.source_id = ref_update.source_id
    if ref_update.from_attribute_name is not None:
        ref.from_attribute_name = ref_update.from_attribute_name

    new_values = model_to_dict(ref)
    await audit_log(db, "object_refs", ref_id, "UPDATE", actor_id,
                    old_values=old_values, new_values=new_values, object_id=ref.from_object_id)
    # CORESEAM-11/D-01/D-02/D-03/D-04: exactly ONE ref-update event with the full
    # two-ended snapshots, keyed to the original source object, emitted in the
    # same transaction after audit and before the first commit. A ref change
    # never produces a general on_object_updated.
    effective_origin = _resolve_origin(origin, "object.ref.update", actor_id)
    await _fire_object_hook(
        "on_object_ref_updated",
        db,
        object_id=original_from_id,
        schema_id=original_from_obj.schema_id if original_from_obj else None,
        object_type_id=(
            original_from_obj.object_type_id
            if original_from_obj
            else ref.object_type_id
        ),
        ref_id=ref_id,
        old_values=old_values,
        new_values=new_values,
        actor_id=actor_id,
        origin=effective_origin.as_payload(),
    )
    if not commit:
        await db.flush()
        return ref
    await db.commit()
    await db.refresh(ref)
    from_obj_result = await db.execute(select(Object).where(Object.id == ref.from_object_id))
    from_obj = from_obj_result.scalar_one_or_none()
    if from_obj:
        await _touch_system_attribute(db, from_obj.id, from_obj.object_type_id, "Updated")
    if invalidate_cache:
        await invalidate_entity_cache(ENTITY_TYPE)
        await invalidate_entity_cache("search")
    return ref


async def delete_object_ref(
    db: AsyncSession,
    ref_id: uuid.UUID,
    actor_id: uuid.UUID,
    *,
    origin: hook_origin.MutationOrigin | None = None,
    user_id: str | None = None,
    is_superuser: bool = False,
    commit: bool = True,
    emit_index_outbox: bool = True,
    invalidate_cache: bool = True,
) -> bool:
    """``user_id`` None means a trusted internal caller and stays unfiltered;
    with ``user_id`` set a ref with a DENY'd end behaves like a nonexistent one
    (quick 261012-cpp), checked before the lock guard. The same holds for a ref
    stored under a hidden reference attribute (SEC313-97 / WR-02, U-02) or one
    whose attribute type the caller may not read (AUTHZ-08 / D-08), and a
    record ``delete`` DENY on the ref's source answers 403 (AUTHZ-07).

    Composite switches as in :func:`create_object_ref`.
    """
    result = await db.execute(
        select(ObjectRef).where(ObjectRef.id == ref_id)
    )
    ref = result.scalar_one_or_none()
    if not ref:
        return False
    object_filter = await _object_filter(db, user_id, is_superuser)
    if not await _ends_visible(db, object_filter, ref.from_object_id, ref.to_object_id):
        return False
    # SEC313-97 / WR-02, U-02: hidden before the lock guard, so a restricted
    # caller can neither delete the row nor confirm it through a 409.
    if (
        user_id is not None
        and not is_superuser
        and await ref_hidden_by_attribute(db, ref.id)
    ):
        return False
    # AUTHZ-08 / D-08: an attribute-type DENY refuses the delete the same way —
    # id-based, so the row's names are never echoed and nothing is mutated.
    if (
        user_id is not None
        and not is_superuser
        and await ref_denied_by_attribute_type(
            db, ref.id, user_id=user_id, is_superuser=is_superuser
        )
    ):
        return False
    # AUTHZ-07: deleting a reference removes a row from its SOURCE container,
    # so the caller needs a record `delete` decision on that end — the same
    # action every other delete path in the tree resolves. `delete` is a
    # singleton action (record_permission_service._ACTION_ALIASES), so a stored
    # `write` DENY alone still does not block the delete. Checked after the
    # hidden guard (an invisible ref keeps answering 404, D-01) and before the
    # lock guard (a denied delete must not answer 409).
    if user_id is not None:
        await assert_record_action_allowed(
            user_id, is_superuser, object_id=ref.from_object_id, action="delete"
        )
    from_object_id = ref.from_object_id
    from_obj_result = await db.execute(select(Object).where(Object.id == from_object_id))
    from_obj = from_obj_result.scalar_one_or_none()
    # 290 D-02/D-04: deny a real delete of an externally managed binding before
    # the audit/delete/commit.
    if from_obj is not None:
        await assert_binding_writable(
            db,
            object_type_id=from_obj.object_type_id,
            attribute_name=ref.from_attribute_name,
            origin=origin,
            object_id=from_object_id,
        )
    old_values = model_to_dict(ref)
    await audit_log(db, "object_refs", ref_id, "DELETE", actor_id,
                    old_values=old_values, new_values=None, object_id=from_object_id)
    # CORESEAM-11/D-01/D-04/D-06: emit the ref-deleted hook after audit but
    # BEFORE db.delete(ref) (the analogue of on_object_deleted), with the
    # snapshot captured above and a null opposite side. Best-effort before the
    # first commit (D-05).
    effective_origin = _resolve_origin(origin, "object.ref.delete", actor_id)
    await _fire_object_hook(
        "on_object_ref_deleted",
        db,
        object_id=from_object_id,
        schema_id=from_obj.schema_id if from_obj else None,
        object_type_id=from_obj.object_type_id if from_obj else ref.object_type_id,
        ref_id=ref_id,
        old_values=old_values,
        new_values=None,
        actor_id=actor_id,
        origin=effective_origin.as_payload(),
    )
    await db.delete(ref)
    if not commit:
        await db.flush()
        return True
    await db.commit()
    if from_obj:
        await _touch_system_attribute(db, from_obj.id, from_obj.object_type_id, "Updated")
    if invalidate_cache:
        await invalidate_entity_cache(ENTITY_TYPE)
        await invalidate_entity_cache("search")
    return True
