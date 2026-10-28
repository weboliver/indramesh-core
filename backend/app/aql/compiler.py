"""AQL query compiler and execution orchestrator.

The compiler ties together the parser, transformer, resolver, Elasticsearch,
and list_objects() into a single execute_aql() function.

Flow:
1. Parse AQL string -> Lark Tree (parser.py)
2. Transform Tree -> CompileOutput with unresolved names (transformer.py)
3. Resolve names -> AQLResolvedQuery with UUIDs + condition split (resolver.py)
4. Compose the having (reference-traversal) PK filter, if any
5. Call list_objects() with filter_tree + pk_filter + object_type_id
6. Return paginated results

`~` (contains) is compiled into the SQL filter_tree as `contains` and runs as
ILIKE, backed by the pg_trgm GIN indexes. It deliberately does NOT go through
OpenSearch: the analyzed-field pre-filter matched whole tokens only and turned
`~ "serv"` into a hard filter that dropped the substring hit "server"
(AQL-CONTAINS-01).
"""
import uuid

from lark import Tree
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.aql.parser import parse
from app.aql.transformer import (
    transform,
    _tree_to_dict,
    _flatten_tree,
    _collect_conditions,
    _ensure_filter_tree_shape,
    _extract_order_by,
)
from app.aql.resolver import AQLResolver
from app.aql.schemas import AQLResolvedQuery, AttrCondition, CompileOutput, ObjectTypeCondition
from app.models.object import Object
from app.models.object_type import ObjectType
from app.models.object_type_attribute import ObjectTypeAttribute
from app.services.object_service import list_objects
from app.services.filter_utils import evaluate_filter_tree
from app.services.value_visibility import resolve_principal_roles


def _tree_has_or(node: dict) -> bool:
    """Return True if `node` or any nested group uses OR combination.

    `pk_filter` is applied by list_objects() as a hard AND'ed
    `Object.id.in_(pk_filter)`, which is only correct when every ES condition
    is AND-combined with the rest of the query — an OR-combined ES condition
    would otherwise silently drop valid non-ES matches. The ES hybrid path is
    only used when the filter_tree is pure-AND; any OR anywhere skips ES for
    the whole request, falling back to the "contains" (ILIKE) conditions the
    transformer already put in filter_tree regardless of ES routing.
    """
    if node.get("op") == "OR" and (node.get("conditions") or node.get("groups")):
        return True
    return any(_tree_has_or(g) for g in node.get("groups", []))


async def _materialize_pk_set_filtered(db: AsyncSession, filter_tree: dict) -> set[uuid.UUID]:
    """Materialize a filter_tree with NO object_type scoping.

    Used for the D-01/D-02 global (objectType-less) candidate-fan-out
    filter_tree — an OR-of-AND-groups tree whose leaves are already
    attr_id-resolved, and every attr_id intrinsically belongs to exactly one
    ObjectType, so no additional `Object.object_type_id` constraint is needed
    (or even expressible — there is no single scoping type)."""
    query = select(Object.id)
    if filter_tree and (filter_tree.get("conditions") or filter_tree.get("groups")):
        clause = await evaluate_filter_tree(filter_tree, db)
        if clause is not None:
            query = query.where(clause)
    result = await db.execute(query)
    return set(result.scalars().all())


async def _resolve_and_materialize_branch(
    db: AsyncSession, resolver: AQLResolver, resolved: AQLResolvedQuery
) -> set[uuid.UUID]:
    """Resolve one isolated AND-scoped branch (already produced by the D-07
    recursive OR/AND branch-walker) to its materialized set of matching
    Object PKs.

    Mirrors execute_aql()'s Steps 4/5/5b (filter_tree build, ES hybrid/global
    fan-out, having_pk_filters composition) but returns a PK set instead of
    calling list_objects() — RBAC/sort/pagination are applied exactly once, at
    the walker's final composed pk_filter (Pitfall 6/7), not per branch.
    """
    filter_tree = (
        resolved.filter_tree
        if resolved.filter_tree.get("conditions") or resolved.filter_tree.get("groups")
        else None
    )

    is_global = resolved.candidate_object_type_ids is not None
    pk_filter: list[uuid.UUID] | None = None

    # pk_filter is composed only from the having (reference-traversal) anchor
    # sets below: `~` executes as SQL ILIKE inside filter_tree on both paths,
    # so there is no OpenSearch pre-filter to narrow with (AQL-CONTAINS-01).
    if resolved.having_pk_filters:
        all_pk_sets: list[set[uuid.UUID]] = []
        if pk_filter is not None:
            all_pk_sets.append(set(pk_filter))
        all_pk_sets.extend(resolved.having_pk_filters)

        if len(all_pk_sets) == 1:
            pk_filter = list(all_pk_sets[0])
        else:
            pk_filter = list(set.intersection(*all_pk_sets))

    if resolved.object_type_id is not None:
        base_pks = await resolver._materialize_pk_set(
            resolved.object_type_id, filter_tree or {}
        )
    elif resolved.object_type_ids is not None:
        base_pks = await resolver._materialize_pk_set_multi(resolved.object_type_ids)
    elif is_global:
        base_pks = await _materialize_pk_set_filtered(db, filter_tree or {})
    else:
        base_pks = set()

    if pk_filter is not None:
        base_pks &= set(pk_filter)

    return base_pks


async def _execute_leaf_branch(
    db: AsyncSession, node: dict, resolver: AQLResolver
) -> set[uuid.UUID]:
    """Run one isolated AND-scoped branch (no structural OR beneath it) through
    the EXISTING per-branch pipeline: `_collect_conditions` -> `CompileOutput`
    -> `AQLResolver.resolve()` (D-01/02/03 global/intersection resolution) ->
    ES hybrid/global fan-out -> having composition -> materialize to a PK set.

    This is the exact "transform -> resolve -> materialize -> compose" shape
    `resolver.py::_resolve_having` already uses for nested inR/outR conditions,
    generalized up to the top-level query (D-07 Option B, RESEARCH.md's
    Recommendation). `node` is already filter-tree-shaped (has "op") by the
    time it reaches here — `_resolve_branch` always wraps bare leaves via
    `_ensure_filter_tree_shape` before recursing.
    """
    object_type_name, object_type_names, object_type_and_children, attr_conditions, having_conditions = (
        _collect_conditions(node)
    )

    object_type = None
    if object_type_name:
        object_type = ObjectTypeCondition(name=object_type_name)
    elif object_type_names:
        object_type = ObjectTypeCondition(names=object_type_names)
    elif object_type_and_children:
        object_type = ObjectTypeCondition(and_children_name=object_type_and_children)

    leaf_output = CompileOutput(
        filter_tree=node,
        object_type=object_type,
        order_by=None,
        order_direction=None,
        conditions=attr_conditions,
        having_conditions=having_conditions,
    )

    resolved = await resolver.resolve(leaf_output)
    return await _resolve_and_materialize_branch(db, resolver, resolved)


async def _resolve_branch(
    db: AsyncSession, node: dict, resolver: AQLResolver
) -> set[uuid.UUID]:
    """Recursive D-07 OR/AND branch-walker (Option B — full recursion at ANY
    nesting depth, NOT a top-level-only special case).

    Dispatch is driven ONLY by `node["op"]`, with no "top level" gate:
      - `node["op"] == "OR"`: UNION of `_resolve_branch(child)` over every
        child — the OR node's own direct leaf conditions (each wrapped as its
        own one-leaf AND-branch) plus its nested groups. A child may itself be
        OR, AND, or a leaf, at any depth.
      - `node["op"] == "AND"` AND a structural OR exists somewhere beneath it
        (`_tree_has_or(node)`): INTERSECTION of `_resolve_branch(child)` over
        the same combined children list — a child may itself contain a
        further-nested OR.
      - otherwise (leaf: an AND-scoped branch, however deep, with NO
        structural OR anywhere beneath it): run the existing per-branch
        pipeline via `_execute_leaf_branch` — this is the "fast path" for the
        overwhelming majority of AND-only branches, preserving D-01/D-02's
        multi-attribute-name intersection semantics as ONE resolver.resolve()
        call rather than splitting into independently-resolved single-leaf
        branches.

    Because dispatch is on `node["op"]` alone, structural OR at ANY depth is
    handled by the same recursion — an OR nested inside an AND, or an AND
    nested inside an OR nested inside an AND, all resolve via the identical
    walk. This is precisely what distinguishes Option B (full recursion) from
    Option A (top-level-only special case) and is the locked D-07 requirement.
    """
    if node.get("op") == "OR":
        children = list(node.get("conditions", [])) + list(node.get("groups", []))
        pk_sets = [
            await _resolve_branch(db, _ensure_filter_tree_shape(child), resolver)
            for child in children
        ]
        return set().union(*pk_sets) if pk_sets else set()

    if node.get("op") == "AND" and _tree_has_or(node):
        children = list(node.get("conditions", [])) + list(node.get("groups", []))
        pk_sets = [
            await _resolve_branch(db, _ensure_filter_tree_shape(child), resolver)
            for child in children
        ]
        return set.intersection(*pk_sets) if pk_sets else set()

    return await _execute_leaf_branch(db, node, resolver)


async def execute_aql(
    db: AsyncSession,
    aql_string: str,
    skip: int = 0,
    limit: int = 100,
    user_role: str | None = None,
    user_id: str | None = None,
    is_superuser: bool = False,
    tree: Tree | None = None,
) -> tuple[list[Object], int]:
    """Parse, compile, and execute an AQL query.

    This is the main entry point for AQL query execution. Phase 191's API
    endpoint will call this function.

    Args:
        db: Database session
        aql_string: Raw AQL query text
        skip: Pagination offset
        limit: Page size
        user_role: User role for RBAC cache key
        user_id: User ID for record permission filtering
        is_superuser: Whether user is superuser
        tree: Optional pre-parsed query tree (Plan 204-02 Task 0). When
            provided, it is used instead of parsing ``aql_string`` again —
            lets the public AQL wrapper parse the query exactly once per
            request (once for orderWay derivation, once here would be
            double). Defaults to None: callers that omit it get today's
            behavior byte-for-byte (``aql_string`` parsed internally).

    Returns:
        Tuple of (list of Object items, total count)

    Raises:
        AQLParseError: On syntax errors
        AQLValidationError: On semantic errors (unknown names, missing objectType)
    """
    # Step 1: Parse
    tree = tree if tree is not None else parse(aql_string)

    # D-05: one principal-bound resolver per request. Names are resolved through
    # the same value-visibility predicate the REST exits compose, and the roles
    # are resolved exactly once here (pitfall P-08) — never per branch.
    role_ids = (
        await resolve_principal_roles(db, user_id, is_superuser)
        if user_id is not None
        else None
    )
    resolver = AQLResolver(
        db, user_id=user_id, is_superuser=is_superuser, role_ids=role_ids
    )

    # Step 1b (D-07): detect whether the tree contains any structural OR
    # ANYWHERE (top-level or nested) BEFORE ever calling transform()/
    # _collect_conditions() on the whole tree — once OR is combined with
    # objectType/having leaves, _collect_conditions's single-scope
    # assumptions (one object_type_name, one flat attr_conditions list) no
    # longer hold across branches, and must never see the merged whole tree.
    # No-OR queries take the byte-for-byte unchanged fast path below.
    probe_flat = _flatten_tree(_tree_to_dict(tree, [False]))
    if not _tree_has_or(probe_flat):
        return await _execute_aql_fast_path(
            db, tree, skip, limit, user_role, user_id, is_superuser, resolver
        )

    # D-07 Option B: full recursive OR/AND branch-walker. Each isolated
    # AND-scoped branch gets its own independent objectType/attribute/having
    # scope (D-01/02/03 global resolution + D-05 ES fan-out), materialized to
    # a PK set; PK sets union at OR boundaries and intersect at AND
    # boundaries, recursively, at any nesting depth.
    order_by, order_direction = _extract_order_by(tree)
    root_node = _ensure_filter_tree_shape(probe_flat)
    final_pks = await _resolve_branch(db, root_node, resolver)

    items, total = await list_objects(
        db,
        skip=skip,
        limit=limit,
        object_type_id=None,
        object_type_ids=None,
        filter_tree=None,
        pk_filter=list(final_pks),
        sort_by=order_by,
        sort_order=order_direction.lower() if order_direction else None,
        user_role=user_role,
        user_id=user_id,
        is_superuser=is_superuser,
    )
    return items, total


async def _execute_aql_fast_path(
    db: AsyncSession,
    tree,
    skip: int,
    limit: int,
    user_role: str | None,
    user_id: str | None,
    is_superuser: bool,
    resolver: AQLResolver,
) -> tuple[list[Object], int]:
    """The no-structural-OR-anywhere path: today's unchanged
    transform()->resolve()->ES loop->having composition->list_objects() body,
    preserved byte-for-byte (Plan 01-03 behavior + the ES-hybrid pk_filter
    optimization) so the overwhelming majority of queries see zero behavior
    change beyond the D-01/02/03 relaxations already shipped in Plans 01-03.
    """
    # Step 2: Transform (AST -> intermediate representation with names)
    output = transform(tree)

    # Step 3: Resolve names to UUIDs and split conditions (the resolver is the
    # request's principal-bound instance — see execute_aql())
    resolved = await resolver.resolve(output)

    # Step 4: Build filter_tree from SQL-routed conditions (all conditions,
    # including ~, are already present in resolved.filter_tree as "contains"
    # ops — ES routing below is an optional optimization, not a replacement).
    filter_tree = (
        resolved.filter_tree
        if resolved.filter_tree.get("conditions") or resolved.filter_tree.get("groups")
        else None
    )

    # Step 5: every `~` (contains) condition executes as SQL ILIKE. The
    # OpenSearch pre-filter that used to narrow the candidate set here ran
    # `simple_query_string` against the analyzed text field, i.e. it matched
    # whole tokens only, and its PKs were AND'ed in front of the SQL query —
    # so `Description ~ "serv"` returned nothing while a value "server"
    # existed. ILIKE is backed by the pg_trgm GIN indexes on
    # object_values.value_simple and objects.name (AQL-CONTAINS-01).
    pk_filter: list[uuid.UUID] | None = None

    # Step 5b: Compose having_pk_filters (reference-traversal anchor PK sets,
    # Plan 03) with any ES-derived pk_filter above into a single flat pk_filter
    # (Pattern 4 / D-05 open question resolution). having leaves are only ever
    # produced under a pure-AND tree — Plan 01's transformer rejects a having
    # leaf under OR at parse time, the same OR-safety invariant `_tree_has_or()`
    # relies on for the ES hybrid path above — so composing them here via plain
    # set intersection (AND semantics) is always safe, regardless of
    # `hybrid_safe`/`use_es` above. No list_objects() signature change: this
    # only narrows the single `pk_filter` list already passed to it.
    if resolved.having_pk_filters:
        all_pk_sets: list[set[uuid.UUID]] = []
        if pk_filter is not None:
            all_pk_sets.append(set(pk_filter))
        all_pk_sets.extend(resolved.having_pk_filters)

        if len(all_pk_sets) == 1:
            pk_filter = list(all_pk_sets[0])
        else:
            pk_filter = list(set.intersection(*all_pk_sets))

    # Step 6: Execute via list_objects
    items, total = await list_objects(
        db,
        skip=skip,
        limit=limit,
        object_type_id=resolved.object_type_id,
        object_type_ids=resolved.object_type_ids,
        filter_tree=filter_tree,
        pk_filter=pk_filter,
        sort_by=resolved.order_by,
        # list_objects() checks lowercase "desc" but the grammar's ORDER_DIRECTION
        # token is uppercase "ASC"/"DESC" — lowercase it here.
        sort_order=resolved.order_direction.lower() if resolved.order_direction else None,
        user_role=user_role,
        user_id=user_id,
        is_superuser=is_superuser,
    )

    return items, total
