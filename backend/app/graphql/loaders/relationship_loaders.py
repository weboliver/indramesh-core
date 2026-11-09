"""Relationship DataLoaders for GraphQL entities.

Batch-loads ObjectValues by object_id and ObjectRefs by from/to object_id,
returning grouped results as lists per input key.  Each factory function
takes an ``AsyncSession`` and returns a ``strawberry.dataloader.DataLoader``.

The ``session`` is an explicit parameter of the inner ``_load_fn``, bound
via ``functools.partial`` at factory call time — see the bug note in
Plan 181-02 for why lexical closure over ``session`` is wrong.
"""

import asyncio
import uuid
from collections import defaultdict
from functools import partial

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from strawberry.dataloader import DataLoader

from app.models.object_ref import ObjectRef
from app.models.object_type import ObjectType
from app.models.object_value import ObjectValue
from app.services.classification_service import load_classifications
from app.services.object_ref_service import (
    denied_ref_attribute_clause,
    hidden_ref_clause,
)
from app.services.value_visibility import (
    apply_value_visibility,
    resolve_principal_roles,
    value_visibility_joins,
    visible_value_conditions,
)

__all__ = [
    "object_values_by_object_id_loader",
    "object_refs_by_from_object_id_loader",
    "object_refs_by_to_object_id_loader",
    "object_types_by_schema_id_loader",
    "object_types_by_parent_id_loader",
    "inherited_attributes_by_object_type_id_loader",
    "classification_labels_by_attribute_id_loader",
    "classification_labels_by_object_id_loader",
]


def object_values_by_object_id_loader(
    session: AsyncSession,
    user_id=None,
    is_superuser: bool = False,
    lock: asyncio.Lock | None = None,
    *,
    trusted: bool = False,
) -> DataLoader:
    """Return a DataLoader that loads the *visible* ``ObjectValue`` rows per object.

    Groups results by ``object_id`` and returns ``list[ObjectValue]`` per key
    (empty list for objects with no values). ``lock`` serializes access to
    the shared per-request ``session`` against other resolvers/loaders
    concurrently dispatched by graphql-core's asyncio.gather.

    D-05 (317-04): the principal is bound at construction time, exactly like
    ``inherited_attributes_by_object_type_id_loader`` below, and both
    predicates (object-level and attribute-type-level record DENY) plus
    ``is_hidden`` are applied **inside** ``_load_fn``. This is the seam the
    GraphQL value exit depends on: a filter in ``resolve_object_values`` would
    run after the DataLoader has cached the unfiltered list, which is the
    bypass pattern the security ledger cites (SEC313-11 / EXT-04-M2) — a
    second resolver, or a second field on the same object, could then read the
    cached rows without passing the check again.

    The three joins exist only for the predicates: ``object_type_attributes``
    (is_hidden), ``attribute_types`` (the attribute-type record DENY) and
    ``objects`` (the object-level record DENY, which cascades over the
    object's type/schema/source chain). Each joined table is 1:1 on its key,
    so the row set is unchanged — no duplicate values. They are taken from
    :func:`value_visibility_joins` (all three pairs), not from
    :func:`attribute_visibility_joins`: the composed
    ``visible_value_conditions`` includes the object-level clause, so this
    selection must join ``objects`` too. The attribute-only helper belongs to
    the CSV value selection, which scopes objects through an ``IN (subquery)``
    instead of a join.

    ``user_id=None`` means "no principal bound" and **raises** unless the
    caller passes the keyword-only ``trusted=True`` opt-in (WR-06): a missing
    principal must fail when the context is built, not silently widen the
    view to every value. Only tests and internal storage-shape callers may
    pass ``trusted=True``; production contexts always bind the request
    principal (``build_graphql_context``) and never opt in. The trusted path
    issues no permission query and returns the unfiltered storage shape,
    exactly like a superuser view.
    """

    if user_id is None and not trusted:
        raise RuntimeError(
            "object_values_by_object_id_loader requires a principal "
            "(user_id) or trusted=True"
        )

    lock = lock or asyncio.Lock()

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        user_id,
        is_superuser: bool,
        lock: asyncio.Lock,
    ) -> list[list[ObjectValue]]:
        stmt = select(ObjectValue).where(ObjectValue.object_id.in_(keys))
        async with lock:
            if user_id is not None and not is_superuser:
                # Resolve the principal's roles once and hand the list to both
                # fragments (317-03 P-08): the two composed clauses bind the
                # same ``:role_ids`` name, so they must receive one value.
                role_ids = await resolve_principal_roles(
                    session, str(user_id), False
                )
                # The joins are added together with the clauses — the
                # unfiltered path keeps the exact statement shape it had
                # before (no needless joins).
                stmt = apply_value_visibility(
                    stmt,
                    await visible_value_conditions(
                        session, str(user_id), False, role_ids=role_ids
                    ),
                    joins=value_visibility_joins(),
                )
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        grouped: dict[uuid.UUID, list[ObjectValue]] = defaultdict(list)
        for row in rows:
            grouped[row.object_id].append(row)
        return [grouped.get(key, []) for key in keys]

    return DataLoader(
        load_fn=partial(
            _load_fn,
            session=session,
            user_id=user_id,
            is_superuser=is_superuser,
            lock=lock,
        )
    )


async def _memoized_hidden_ref_clause(session: AsyncSession, memo: dict):
    """Evaluate ``hidden_ref_clause`` once per loader instance (WR-02).

    ``_HIDDEN_REF_ATTRIBUTES_SQL`` is principal-independent, so one request
    needs at most one evaluation per loader. The ``memo`` dict is bound
    through ``functools.partial`` — never a lexical closure over ``session``
    (see the module docstring). The caller must already hold the request lock:
    this executes on the shared ``AsyncSession``. A stored ``None`` counts as
    computed too, hence the membership test rather than a truthiness check.
    """
    if "hidden_ref_clause" not in memo:
        memo["hidden_ref_clause"] = await hidden_ref_clause(session)
    return memo["hidden_ref_clause"]


async def _memoized_denied_ref_clause(session: AsyncSession, memo: dict, user_id):
    """Evaluate ``denied_ref_attribute_clause`` once per loader instance.

    Unlike the hidden clause this one *is* principal-scoped, so it is memoized
    per loader and only ever computed for a non-superuser (the callers apply
    their own ``is_superuser`` guard). Same contract as
    :func:`_memoized_hidden_ref_clause`: ``memo`` is bound through
    ``functools.partial``, the caller already holds the request lock, and a
    stored ``None`` counts as computed (membership test, not truthiness).
    """
    if "denied_ref_clause" not in memo:
        memo["denied_ref_clause"] = await denied_ref_attribute_clause(
            session, user_id=str(user_id)
        )
    return memo["denied_ref_clause"]


def object_refs_by_from_object_id_loader(
    session: AsyncSession,
    lock: asyncio.Lock | None = None,
    *,
    is_superuser: bool = False,
    user_id: str | None = None,
) -> DataLoader:
    """Return a DataLoader that loads all outgoing ``ObjectRef`` rows per ``from_object_id``.

    Groups results by ``from_object_id`` and returns ``list[ObjectRef]`` per key
    (empty list for objects with no outgoing refs).

    WR-02: for a non-superuser, refs stored under an ``is_hidden`` reference
    attribute — declared on the ref's own ObjectType or inherited by it
    (``object_ref_service.hidden_ref_clause``) — are excluded **inside**
    ``_load_fn``. That is the SEC313-11 seam: a filter in the resolver would
    run after this DataLoader cached the unfiltered rows, and a second field
    or alias on the same object could then read them without the check
    running again. ``is_superuser=True`` is the unfiltered admin view; the
    clause is memoized per loader instance, so one request pays one bounded
    CTE (T-317.2-08).

    AUTHZ-08 / D-08: the attribute-type record DENY is applied in the same
    place, from ``object_ref_service.denied_ref_attribute_clause`` bound to
    the request principal. Both clauses are memoized in this loader's own
    ``memo`` dict, never in the resolver.
    """

    lock = lock or asyncio.Lock()
    memo: dict = {}

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        lock: asyncio.Lock,
        is_superuser: bool,
        memo: dict,
        user_id,
    ) -> list[list[ObjectRef]]:
        stmt = select(ObjectRef).where(ObjectRef.from_object_id.in_(keys))
        async with lock:
            if not is_superuser:
                clause = await _memoized_hidden_ref_clause(session, memo)
                if clause is not None:
                    stmt = stmt.where(clause)
            # The same guard the value loader above uses: ``context.py`` binds
            # a user_id for every request, so a user_id-only test would run the
            # principal-scoped clause for an administrator and strip refs the
            # admin may see (RLR-05).
            if user_id is not None and not is_superuser:
                denied_clause = await _memoized_denied_ref_clause(
                    session, memo, user_id
                )
                if denied_clause is not None:
                    stmt = stmt.where(denied_clause)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        grouped: dict[uuid.UUID, list[ObjectRef]] = defaultdict(list)
        for row in rows:
            grouped[row.from_object_id].append(row)
        return [grouped.get(key, []) for key in keys]

    return DataLoader(
        load_fn=partial(
            _load_fn,
            session=session,
            lock=lock,
            is_superuser=is_superuser,
            memo=memo,
            user_id=user_id,
        )
    )


def object_refs_by_to_object_id_loader(
    session: AsyncSession,
    lock: asyncio.Lock | None = None,
    *,
    is_superuser: bool = False,
    user_id: str | None = None,
) -> DataLoader:
    """Return a DataLoader that loads all incoming ``ObjectRef`` rows per ``to_object_id``.

    Groups results by ``to_object_id`` and returns ``list[ObjectRef]`` per key
    (empty list for objects with no incoming refs).

    WR-02: same in-``_load_fn`` hidden-attribute filter as the outgoing
    loader — non-superusers never receive a ref stored under an ``is_hidden``
    reference attribute (own or inherited), and the cached list is already
    filtered (SEC313-11). AUTHZ-08 / D-08: the attribute-type record DENY is
    applied in the same in-``_load_fn`` position, and only for a bound
    non-superuser principal.
    """

    lock = lock or asyncio.Lock()
    memo: dict = {}

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        lock: asyncio.Lock,
        is_superuser: bool,
        memo: dict,
        user_id,
    ) -> list[list[ObjectRef]]:
        stmt = select(ObjectRef).where(ObjectRef.to_object_id.in_(keys))
        async with lock:
            if not is_superuser:
                clause = await _memoized_hidden_ref_clause(session, memo)
                if clause is not None:
                    stmt = stmt.where(clause)
            if user_id is not None and not is_superuser:
                denied_clause = await _memoized_denied_ref_clause(
                    session, memo, user_id
                )
                if denied_clause is not None:
                    stmt = stmt.where(denied_clause)
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        grouped: dict[uuid.UUID, list[ObjectRef]] = defaultdict(list)
        for row in rows:
            grouped[row.to_object_id].append(row)
        return [grouped.get(key, []) for key in keys]

    return DataLoader(
        load_fn=partial(
            _load_fn,
            session=session,
            lock=lock,
            is_superuser=is_superuser,
            memo=memo,
            user_id=user_id,
        )
    )


def object_types_by_schema_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads all ``ObjectType`` rows per ``schema_id``.

    Groups results by ``schema_id`` and returns ``list[ObjectType]`` per key
    (empty list for schemas with no object types).
    """

    lock = lock or asyncio.Lock()

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        lock: asyncio.Lock,
    ) -> list[list[ObjectType]]:
        stmt = select(ObjectType).where(ObjectType.schema_id.in_(keys))
        async with lock:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        grouped: dict[uuid.UUID, list[ObjectType]] = defaultdict(list)
        for row in rows:
            grouped[row.schema_id].append(row)
        return [grouped.get(key, []) for key in keys]

    return DataLoader(load_fn=partial(_load_fn, session=session, lock=lock))


def object_types_by_parent_id_loader(session: AsyncSession, lock: asyncio.Lock | None = None) -> DataLoader:
    """Return a DataLoader that loads all child ``ObjectType`` rows per ``parent_id``.

    Groups results by ``parent_id`` and returns ``list[ObjectType]`` per key
    (empty list for object types with no children).
    """

    lock = lock or asyncio.Lock()

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        lock: asyncio.Lock,
    ) -> list[list[ObjectType]]:
        stmt = select(ObjectType).where(ObjectType.parent_id.in_(keys))
        async with lock:
            result = await session.execute(stmt)
            rows = list(result.scalars().all())
        grouped: dict[uuid.UUID, list[ObjectType]] = defaultdict(list)
        for row in rows:
            grouped[row.parent_id].append(row)
        return [grouped.get(key, []) for key in keys]

    return DataLoader(load_fn=partial(_load_fn, session=session, lock=lock))


def inherited_attributes_by_object_type_id_loader(
    session: AsyncSession,
    user_id,
    is_superuser: bool,
    lock: asyncio.Lock | None = None,
) -> DataLoader:
    """Return a DataLoader that loads inherited ObjectTypeAttribute rows per root
    ObjectType id (WORK-02, D-05/D-06/D-07/D-08/D-10).

    Batches ``list_object_type_attributes_multi`` (the cache-free multi-root
    CTE sibling) across every requested root ObjectType id in ONE query per
    dispatch — never one CTE call per root. Groups the flat row-dict result
    by ``"root_id"`` and returns ``list[dict]`` per key (empty list for roots
    with no attributes), preserving input-key order (the DataLoader contract).

    ``user_id``/``is_superuser`` are bound once at loader-creation time
    (request-scoped, from the authenticated GraphQLContext.user) — every
    batch dispatch for this loader instance reuses the same permission
    context, matching the per-request record-permission scope the REST path
    also resolves once per request.

    ``lock`` serializes the service call against the shared per-request
    ``AsyncSession`` — the SAME lock every other DataLoader/resolver in this
    request acquires (D-10). The caller (``resolve_inherited_attributes``)
    must NOT also hold this lock: ``asyncio.Lock`` is not reentrant, and a
    nested acquisition here would deadlock the request.
    """
    from app.services.object_type_attribute_service import list_object_type_attributes_multi

    lock = lock or asyncio.Lock()

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        lock: asyncio.Lock,
    ) -> list[list[dict]]:
        async with lock:
            rows = await list_object_type_attributes_multi(
                session, root_ids=keys, user_id=user_id, is_superuser=is_superuser,
            )
        grouped: dict[uuid.UUID, list[dict]] = defaultdict(list)
        for row in rows:
            grouped[row["root_id"]].append(row)
        return [grouped.get(key, []) for key in keys]

    return DataLoader(load_fn=partial(_load_fn, session=session, lock=lock))


def _classification_labels_loader(
    session: AsyncSession, target_kind: str, lock: asyncio.Lock | None = None
) -> DataLoader:
    """Shared factory for the two per-target-kind classification label loaders.

    ``load_classifications`` answers EVERY requested key (empty list for an
    unclassified target, D-06), so the loader returns the assignment lists in
    input-key order. ``lock`` is the shared per-request lock: the service call
    runs on the one AsyncSession every resolver and loader of the request share,
    so it must not overlap another dispatch (T-317.1-16). The resolver bodies stay
    lock-free — ``asyncio.Lock`` is not reentrant.
    """
    lock = lock or asyncio.Lock()

    async def _load_fn(
        keys: list[uuid.UUID],
        session: AsyncSession,
        lock: asyncio.Lock,
        target_kind: str,
    ) -> list[list[dict]]:
        async with lock:
            result = await load_classifications(session, target_kind, list(keys))
        return [result.get(key, []) for key in keys]

    return DataLoader(
        load_fn=partial(_load_fn, session=session, lock=lock, target_kind=target_kind)
    )


def classification_labels_by_attribute_id_loader(
    session: AsyncSession, lock: asyncio.Lock | None = None
) -> DataLoader:
    """Return a DataLoader that loads assigned label refs per attribute id (D-07).

    One dispatch covers every attribute id requested in the current batch — the
    seam that keeps ``classificationLabels``/``confidentiality``/``personalData``/
    ``exportControl`` at one statement for N parents (T-317.1-15).
    """
    return _classification_labels_loader(session, "attribute", lock)


def classification_labels_by_object_id_loader(
    session: AsyncSession, lock: asyncio.Lock | None = None
) -> DataLoader:
    """Return a DataLoader that loads assigned label refs per object id (CLASS-05).

    Labels carry no visibility of their own: the loader only ever runs for a
    parent the resolver already returned, so a record the caller cannot see is
    never a key (T-317.1-14).
    """
    return _classification_labels_loader(session, "object", lock)
