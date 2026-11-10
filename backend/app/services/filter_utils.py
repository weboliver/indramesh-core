import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import select, and_, or_, not_
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.object_value import ObjectValue
from app.models.object import Object
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.attribute_type import AttributeType
from app.services.select_option_service import resolve_select_literal
from app.services.typed_value_mapping import typed_value_column


def _get_value_column(dtype: Any) -> Any:
    """Resolve the typed value column for a given AttributeType data_type
    string. Delegates to the canonical seam (D-08) — this heals the STATUS
    drift: STATUS now resolves to value_struct here too, matching every other
    consumer, instead of the old inline mapping's fallback-to-value_simple."""
    return typed_value_column(dtype)


async def resolve_user_group_literal(
    db: AsyncSession, dtype: str | None, literal: str
) -> str:
    """Idempotent USER/GROUP filter-literal -> UUID resolution (D-15).

    Mirrors resolve_select_literal's call-site pattern (evaluate_filter_tree +
    object_service.list_objects) but with D-07's raw pass-through instead of
    a ValueError: legacy GROUP values were stored as plain names before the
    groups table existed, so an unresolvable literal must still be usable as
    a raw match against value_simple — never rejected.

    Returns the literal unchanged when dtype is neither "user" nor "group",
    or when the literal already parses as a UUID (idempotent pass-through —
    an upstream resolver, e.g. AQL, may have already resolved it). Otherwise
    looks up the literal case-sensitively against users.username (user) /
    groups.name (group — lazy-imported, mirrors this module's existing
    local-import pattern for models introduced later in the same plan) and
    returns str(id) on a match; on no match returns the literal UNCHANGED
    (D-07), never raises.
    """
    if dtype not in ("user", "group"):
        return literal
    try:
        uuid.UUID(literal)
        return literal
    except (ValueError, TypeError, AttributeError):
        pass

    if dtype == "user":
        from app.models.user import User
        result = await db.execute(select(User.id).where(User.username == literal))
    else:
        from app.models.group import Group
        result = await db.execute(select(Group.id).where(Group.name == literal))

    matched = result.scalar_one_or_none()
    return str(matched) if matched else literal


def build_filter_clause(
    attr_id: uuid.UUID, filter_obj: dict | str, dtype: str, attr_name: str | None = None
) -> Any:
    """Return a SQLAlchemy filter clause (subquery) for the given attribute filter.

    If filter_obj is a string, treat as eq (backward-compatible flat format).
    If filter_obj is a dict, read {op, val[, val2]} and apply the typed operator.
    Returns a subquery: select(ObjectValue.object_id).where(...) — except for
    REFERENCE, which has no ObjectValue row and returns a subquery over ObjectRef
    instead (see _reference_filter), and the "Name" attribute, which the editor
    excludes from the value form in favor of Object.name (see _object_name_filter).
    attr_name is required for REFERENCE and for the "Name" special case.
    """
    if attr_name == "Name":
        # The "Name" ObjectTypeAttribute is a schema-level artifact synced from
        # Jira — EditObjectModal.jsx deliberately excludes it from the edit form
        # and updates Object.name directly instead, so any ObjectValue row under
        # it is stale/unmaintained. Match Object.name directly regardless of dtype.
        if not isinstance(filter_obj, dict):
            return _object_name_filter("eq", filter_obj)
        op = filter_obj.get("op")
        if op is None:
            raise ValueError(f"attr_filters: missing required 'op' field in filter for attribute {attr_id}")
        return _object_name_filter(op, filter_obj.get("val"))

    if not isinstance(filter_obj, dict):
        # Backward-compatible flat format: treat as eq with raw string value
        if dtype == "reference":
            return _reference_filter(attr_name, "eq", filter_obj)
        value_col = _get_value_column(dtype)
        return _eq_filter(attr_id, "eq", filter_obj, value_col, dtype)

    op = filter_obj.get("op")
    if op is None:
        raise ValueError(f"attr_filters: missing required 'op' field in filter for attribute {attr_id}")

    val = filter_obj.get("val")
    val2 = filter_obj.get("val2")

    value_col = _get_value_column(dtype)

    # Numeric types: INTEGER, DOUBLE
    if dtype in ("integer", "double"):
        return _numeric_filter(attr_id, op, val, val2, value_col, dtype)

    # Date type: DATETIME
    if dtype == "datetime":
        return _date_filter(attr_id, op, val, val2, value_col)

    # Boolean type
    if dtype == "boolean":
        return _boolean_filter(attr_id, op, val, value_col)

    # STATUS — the value lives in a JSON struct ({"id": ..., "name": ...}),
    # not a scalar column, so it needs its own branch (D-09 STATUS-drift fix):
    # value_col here is now ObjectValue.value_struct (a JSON column) —
    # _text_filter's ilike() would be a Postgres type error against it.
    if dtype == "status":
        return _status_filter(attr_id, op, val, value_col)

    # Text-based types: TEXT, URL, EMAIL, VERSION, GROUP, USER, JIRA, CONFLUENCE
    if dtype in ("text", "url", "email", "version", "group", "user", "jira", "confluence"):
        return _text_filter(attr_id, op, val, value_col, dtype)

    # REFERENCE — no scalar value of its own, matches against the referenced object's name
    if dtype == "reference":
        return _reference_filter(attr_name, op, val)

    # SELECT — eq only
    # D-05: SELECT vals arrive PRE-RESOLVED (display string -> option id) at
    # both call sites (evaluate_filter_tree + object_service.list_objects) —
    # this branch compares ids against value_simple, never display strings.
    if dtype == "select":
        return _eq_filter(attr_id, op, val, value_col, dtype)

    # JSON and unrecognized types are unsupported
    raise ValueError(
        f"Attribute type '{dtype}' does not support filtering."
    )


def _numeric_filter(
    attr_id: uuid.UUID, op: str, val: Any, val2: Any, value_col: Any, dtype: str
) -> Any:
    """Build filter clause for numeric types (INTEGER, DOUBLE)."""
    if dtype == "integer":
        try:
            val = int(val)
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid numeric value '{val}' for filter on attribute {attr_id}"
            )
    else:
        try:
            val = float(val)
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid numeric value '{val}' for filter on attribute {attr_id}"
            )

    if op == "eq":
        clause = value_col == val
    elif op == "gt":
        clause = value_col > val
    elif op == "lt":
        clause = value_col < val
    elif op == "gte":
        clause = value_col >= val
    elif op == "lte":
        clause = value_col <= val
    elif op == "between":
        if val2 is None:
            raise ValueError(
                f"Operator 'between' requires val2 for filter on attribute {attr_id}"
            )
        if dtype == "integer":
            try:
                val2 = int(val2)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Invalid numeric value '{val2}' for filter on attribute {attr_id}"
                )
        else:
            try:
                val2 = float(val2)
            except (ValueError, TypeError):
                raise ValueError(
                    f"Invalid numeric value '{val2}' for filter on attribute {attr_id}"
                )
        clause = value_col.between(val, val2)
    else:
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type '{dtype}'. "
            f"Valid operators: eq, gt, lt, gte, lte, between"
        )

    return (
        select(ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id == attr_id,
            clause,
        )
    )


def _date_filter(
    attr_id: uuid.UUID, op: str, val: Any, val2: Any, value_col: Any
) -> Any:
    """Build filter clause for DATETIME type."""
    try:
        d1 = datetime.fromisoformat(str(val))
    except (ValueError, TypeError):
        raise ValueError(
            f"Invalid date value '{val}' for filter on attribute {attr_id}"
        )

    # gt/lt are STRICT, gte/lte are INCLUSIVE — the ordinal operators AQL's
    # grammar already accepts (resolver maps >,<,>=,<= to these). before/after
    # stay today's inclusive aliases so no existing query's order changes.
    if op == "eq":
        clause = value_col == d1
    elif op == "gt":
        clause = value_col > d1
    elif op == "lt":
        clause = value_col < d1
    elif op == "gte":
        clause = value_col >= d1
    elif op == "lte":
        clause = value_col <= d1
    elif op == "before":
        clause = value_col <= d1
    elif op == "after":
        clause = value_col >= d1
    elif op == "between":
        if val2 is None:
            raise ValueError(
                f"Operator 'between' requires val2 for filter on attribute {attr_id}"
            )
        try:
            d2 = datetime.fromisoformat(str(val2))
        except (ValueError, TypeError):
            raise ValueError(
                f"Invalid date value '{val2}' for filter on attribute {attr_id}"
            )
        clause = value_col.between(d1, d2)
    else:
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type 'datetime'. "
            f"Valid operators: eq, gt, lt, gte, lte, before, after, between"
        )

    return (
        select(ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id == attr_id,
            clause,
        )
    )


def _boolean_filter(
    attr_id: uuid.UUID, op: str, val: Any, value_col: Any
) -> Any:
    """Build filter clause for BOOLEAN type."""
    if op != "eq":
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type 'boolean'. "
            f"Valid operators: eq"
        )

    coerced = str(val).lower() in ("true", "1", "yes")
    return (
        select(ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id == attr_id,
            value_col == coerced,
        )
    )


_ILIKE_ESCAPE = "\\"


def _escape_ilike(value: Any) -> str:
    """Escape LIKE/ILIKE wildcards so a filter value matches literally.

    `%` and `_` are pattern wildcards: before this, `Name = "50%"` matched every
    object and `~ "a_c"` matched "abc". AQL `~` is documented as a plain
    substring search, so the value is escaped and every pattern below declares
    `ESCAPE '\'`. The backslash itself is escaped first to keep that unambiguous.
    """
    text = str(value)
    return (
        text.replace("\\", "\\\\")
        .replace("%", "\\%")
        .replace("_", "\\_")
    )


def _text_filter(
    attr_id: uuid.UUID, op: str, val: Any, value_col: Any, dtype: str
) -> Any:
    """Build filter clause for text-based types (TEXT, URL, EMAIL, etc.)."""
    term = _escape_ilike(val)
    if op == "eq":
        clause = value_col.ilike(term, escape=_ILIKE_ESCAPE)
    elif op == "contains":
        clause = value_col.ilike(f"%{term}%", escape=_ILIKE_ESCAPE)
    elif op == "starts_with":
        clause = value_col.ilike(f"{term}%", escape=_ILIKE_ESCAPE)
    else:
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type '{dtype}'. "
            f"Valid operators: eq, contains, starts_with"
        )

    return (
        select(ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id == attr_id,
            clause,
        )
    )


def _status_filter(
    attr_id: uuid.UUID, op: str, val: Any, value_col: Any
) -> Any:
    """Build filter clause for STATUS type — matches the option id stored in
    the JSON struct's "id" key (mirrors the shape _validate_status_value
    writes: {"id": ..., "name": ...}). A status id has no meaningful
    substring match, so only 'eq' is supported (D-09 STATUS-drift fix)."""
    if op != "eq":
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type 'status'. "
            f"Valid operators: eq"
        )

    return (
        select(ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id == attr_id,
            value_col["id"].astext == str(val),
        )
    )


def _eq_filter(
    attr_id: uuid.UUID, op: str, val: Any, value_col: Any, dtype: str
) -> Any:
    """Build eq-only filter clause for keyword types (SELECT)."""
    if op != "eq":
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type '{dtype}'. "
            f"Valid operators: eq"
        )

    return (
        select(ObjectValue.object_id)
        .where(
            ObjectValue.object_type_attribute_id == attr_id,
            value_col == val,
        )
    )


def _object_name_filter(op: str, val: Any) -> Any:
    """Build filter clause for the "Name" attribute — matches Object.name directly
    instead of an ObjectValue row (see build_filter_clause's "Name" special case)."""
    term = _escape_ilike(val)
    if op == "eq":
        clause = Object.name.ilike(term, escape=_ILIKE_ESCAPE)
    elif op == "contains":
        clause = Object.name.ilike(f"%{term}%", escape=_ILIKE_ESCAPE)
    elif op == "starts_with":
        clause = Object.name.ilike(f"{term}%", escape=_ILIKE_ESCAPE)
    else:
        raise ValueError(
            f"Operator '{op}' is not valid for the Name attribute. "
            f"Valid operators: eq, contains, starts_with"
        )

    return select(Object.id).where(clause)


def _reference_filter(attr_name: str | None, op: str, val: Any) -> Any:
    """Build filter clause for REFERENCE type attributes.

    Reference values live in ObjectRef (a link between two Object rows), not
    ObjectValue — a reference attribute has no scalar value of its own, so
    filtering matches against the *referenced* object's name instead. ObjectRef
    disambiguates which attribute a link belongs to via from_attribute_name
    (the attribute's name, not its id).
    """
    from app.models.object_ref import ObjectRef

    term = _escape_ilike(val)
    if op == "eq":
        clause = Object.name.ilike(term, escape=_ILIKE_ESCAPE)
    elif op == "contains":
        clause = Object.name.ilike(f"%{term}%", escape=_ILIKE_ESCAPE)
    elif op == "starts_with":
        clause = Object.name.ilike(f"{term}%", escape=_ILIKE_ESCAPE)
    else:
        raise ValueError(
            f"Operator '{op}' is not valid for attribute type 'reference'. "
            f"Valid operators: eq, contains, starts_with"
        )

    return (
        select(ObjectRef.from_object_id)
        .join(Object, Object.id == ObjectRef.to_object_id)
        .where(
            ObjectRef.from_attribute_name == attr_name,
            clause,
        )
    )


MAX_TREE_DEPTH = 5
MAX_TOTAL_CONDITIONS = 50


def _collect_attr_ids(node: dict, attr_ids: set) -> None:
    """Recursively collect all unique attr_ids from filter tree."""
    for child in node.get("groups", []):
        _collect_attr_ids(child, attr_ids)
    for cond in node.get("conditions", []):
        try:
            attr_ids.add(uuid.UUID(cond['attr_id']))
        except (ValueError, KeyError):
            pass


async def evaluate_filter_tree(
    tree: dict,
    db: AsyncSession,
    *,
    user_id: str | None = None,
    is_superuser: bool = False,
) -> Any:
    """Recursively evaluate a filter tree. Returns a SQLAlchemy clause
    that can be applied directly as query.where(result) — the returned
    clause is already boolean-typed, NOT a subquery.

    Args:
        tree: A dict with 'op' (AND|OR), 'groups' (list), and 'conditions' (list).
            Individual conditions may carry an optional 'negate' bool.
        db: AsyncSession for database lookups
        user_id: The caller's id. When given and the caller is not a superuser,
            a condition on an attribute whose type carries a record DENY for
            that principal — or whose ``object_type_attributes`` row is hidden —
            is skipped exactly like a condition on an unknown ``attr_id``
            (D-18 / TRI317-68): identical result set, no error, no marker.
        is_superuser: The RLR-05 bypass — a superuser pays no predicate and no
            extra statement. The defaults keep the pre-existing unfiltered
            reading for callers with no principal (the AQL compiler/resolver,
            whose filter trees already carry only ids that passed
            ``visible_attribute_conditions``).

    Returns:
        A SQLAlchemy boolean expression (and_/or_ clause) or None if tree is empty

    Raises:
        ValueError: If tree exceeds max depth, max conditions, or has invalid operators
    """
    # Pre-fetch phase: collect all unique attr_ids and batch-load OTAs with AttributeTypes
    unique_attr_ids = set()
    _collect_attr_ids(tree, unique_attr_ids)

    # Batch-load all OTAs with their AttributeTypes in one query. The visibility
    # predicate rides this same statement (a join plus its clauses) when a
    # principal is present — one role-resolution in total, never one per
    # condition — so a denied or hidden id stops matching before the loop below
    # reaches it.
    attr_lookup = {}  # attr_id → (ObjectTypeAttribute, AttributeType)
    if unique_attr_ids:
        ota_statement = (
            select(ObjectTypeAttribute)
            .options(selectinload(ObjectTypeAttribute.attribute_type))
            .where(ObjectTypeAttribute.id.in_(unique_attr_ids))
        )
        if user_id is not None and not is_superuser:
            from app.services.value_visibility import visible_attribute_conditions

            ota_statement = ota_statement.join(
                AttributeType,
                ObjectTypeAttribute.attribute_type_id == AttributeType.id,
            ).where(*await visible_attribute_conditions(db, user_id, False))
        result = await db.execute(ota_statement)
        for attr in result.scalars():
            attr_lookup[attr.id] = (attr, attr.attribute_type)

    counter = [0]

    async def _evaluate(node: dict, depth: int) -> Any:
        if depth > MAX_TREE_DEPTH:
            raise ValueError("filter_tree: exceeds maximum nesting depth")
        op = node.get("op", "AND")
        groups = node.get("groups", [])
        conditions = node.get("conditions", [])

        clauses = []

        # Evaluate child groups recursively. IMPORTANT: _evaluate() already
        # returns a combined boolean clause (the result of and_/or_),
        # NOT a subquery — it must NOT be wrapped in Object.id.in_(...)
        # below, or SQLAlchemy raises ArgumentError ("IN expression list,
        # SELECT construct, or bound parameter object expected, got
        # <BooleanClauseList>"). Any tree with >=1 nested group crashes
        # if leaf-subquery wrapping and group composition share the same
        # Object.id.in_(c) step.
        for child in groups:
            child_clause = await _evaluate(child, depth + 1)
            if child_clause is not None:
                clauses.append(child_clause)

        # Evaluate leaf conditions. build_filter_clause() returns a
        # subquery (select(ObjectValue.object_id).where(...)) — THIS is
        # what needs the Object.id.in_(...) wrap, applied immediately here
        # rather than uniformly at combine-time.
        for cond in conditions:
            counter[0] += 1
            if counter[0] > MAX_TOTAL_CONDITIONS:
                raise ValueError("filter_tree: exceeds maximum condition count")
            attr_id = uuid.UUID(cond['attr_id'])
            op_name = cond['op']
            val = cond.get('val')
            val2 = cond.get('val2')

            filter_obj = {'op': op_name, 'val': val}
            if val2 is not None:
                filter_obj['val2'] = val2

            # Lookup attribute and type from pre-fetched dict (no new query)
            if attr_id not in attr_lookup:
                continue
            attr, at = attr_lookup[attr_id]
            dtype = at.data_type if at else None

            # D-05: SELECT condition literals are display strings (or already
            # resolved ids on the AQL path) — resolve to the canonical option
            # id BEFORE the SQL comparison so value_simple (ids) always
            # compares ids. Unknown literals raise ValueError naming
            # attribute + value (objects.py converts to 422; the AQL path
            # already resolved literals in the resolver, so it passes through
            # idempotently here).
            if dtype == "select" and val is not None:
                filter_obj["val"] = resolve_select_literal(
                    attr.structure, str(val), attr_name=attr.name
                )
            elif dtype in ("user", "group") and val is not None:
                # D-15: user/group filter literals resolve names to UUIDs at
                # the query boundary, with raw pass-through (D-07) for
                # legacy name-stored GROUP values.
                filter_obj["val"] = await resolve_user_group_literal(db, dtype, str(val))

            subq = build_filter_clause(attr_id, filter_obj, dtype, attr_name=attr.name)
            leaf_clause = Object.id.in_(subq)
            if cond.get("negate"):
                leaf_clause = not_(leaf_clause)
            clauses.append(leaf_clause)

        if not clauses:
            return None

        return or_(*clauses) if op == "OR" else and_(*clauses)

    return await _evaluate(tree, 0)
