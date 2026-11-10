"""GraphQL cost & execution limits — the contract layer (Phase 312).

Single home of the whole limit layer: the contract-aware validation rules for
the document-level axes (tokens/aliases/depth/cost), the shared error factory
that builds the ``QUERY_TOO_COMPLEX`` envelope (D-02/D-03), the ONE cost
algorithm (``CostAccumulator``, shared by the enforcing ``CostLimitRule`` and
the standalone ``estimate_cost`` entry point), the ``SchemaExtension`` that
normalises the token parse abort, publishes the D-04 cost headers and rewrites
a Postgres statement-timeout cancel, and the ``timeout_result`` builder of the
synthesized wall-clock envelope (GQLC-04).

Every limit value is read from ``app.config.settings`` AT CALL TIME, never
captured into a module constant: the settings object is instantiated once at
import, so an env change needs a backend restart while a test monkeypatches the
attribute (312-RESEARCH.md §Common Pitfalls — Pitfall 11).

Import direction (hard rule, gated by the ``LIMITS_IMPORT_OK`` check): this
module must NEVER import ``app.graphql.schema`` at module level.
``app/graphql/schema.py`` imports the rule classes from here above its
``schema = strawberry.Schema(...)`` assignment, so a module-level schema import
raises ``ImportError: cannot import name 'schema' from partially initialized
module 'app.graphql.schema'`` at app startup, before any limit can run. The
rules read the schema from ``self.context.schema`` (a ``ValidationContext``)
instead.
"""

from __future__ import annotations

import hashlib
import logging
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from functools import partial

from graphql import (
    DocumentNode,
    FieldNode,
    FragmentDefinitionNode,
    FragmentSpreadNode,
    GraphQLError,
    GraphQLSchema,
    InlineFragmentNode,
    IntValueNode,
    OperationDefinitionNode,
    SKIP,
    SelectionSetNode,
    TypeInfo,
    TypeInfoVisitor,
    Undefined,
    ValidationContext,
    ValidationRule,
    is_list_type,
    is_non_null_type,
    parse,
    type_from_ast,
    validate,
    visit,
)
from graphql.language.parser import Parser
from strawberry.extensions import SchemaExtension
from strawberry.types import ExecutionResult

from app.api.pagination import MAX_LIMIT
from app.config import settings

logger = logging.getLogger(__name__)

CODE_QUERY_TOO_COMPLEX = "QUERY_TOO_COMPLEX"
"""The single stable rejection code for every limit axis (D-02)."""

CODE_QUERY_TIMEOUT = "QUERY_TIMEOUT"
"""The stable code for a cancelled operation — never a 5xx (D-17/D-18).

Unlike ``QUERY_TOO_COMPLEX`` (a pre-execution rejection naming the breached
``limit`` axis), a timeout carries ``extensions.source`` so a client can tell
which bound fired: Postgres cancelling a statement (``statement_timeout``) or
the Python wall-clock backstop (``wall_clock``). Both are HTTP 200 with
``data: null`` and no partial data (D-20).
"""

TIMEOUT_SOURCE_STATEMENT = "statement_timeout"
"""``extensions.source`` when Postgres cancelled a statement (D-18)."""

TIMEOUT_SOURCE_WALL_CLOCK = "wall_clock"
"""``extensions.source`` when the wall-clock backstop fired (D-17/D-18)."""

NESTED_LIST_WEIGHT = 5
"""What one nested list field is worth in the cost budget (D-10/D-12).

A code constant, deliberately NOT an eighth env var: D-09 enumerates the env
set, and an operator must not be able to widen the estimator arbitrarily. The
value is calibrated against this repo's tracked query corpus — the largest
value that keeps every legitimate tracked document under ``GRAPHQL_MAX_COST``
(312-RESEARCH.md §Limit Calibration). The per-parent hard cap in
``GRAPHQL_NESTED_LIST_LIMIT`` answers a different question ("how many rows may
one parent return at all").
"""


def make_limit_error(message: str, limit: str, actual: int, allowed: int) -> GraphQLError:
    """Build one ``QUERY_TOO_COMPLEX`` rejection with the contract extensions.

    D-03: every limit rejection carries ``{code, limit, actual, allowed}`` so a
    client can shrink a query deterministically. ``limit`` names the breached
    axis — ``tokens`` / ``aliases`` / ``depth`` / ``cost`` — and is what makes
    the four axes distinguishable by machine.
    """
    return GraphQLError(
        message,
        extensions={
            "code": CODE_QUERY_TOO_COMPLEX,
            "limit": limit,
            "actual": int(actual),
            "allowed": int(allowed),
        },
    )


def timeout_result() -> ExecutionResult:
    """Build the synthesized wall-clock timeout envelope (D-17/D-18/D-20).

    The ONE builder for "an operation was cut off": used by
    ``LimitedGraphQLRouter.execute_operation`` (both HTTP paths) when
    ``asyncio.timeout`` cancels the operation, and (from 312-06) by the
    saved-query wrapper, which runs ``schema.execute`` directly and never
    reaches a router. ``data`` is ``None`` — a cancelled operation must never
    ship partial or stale results (D-20). The statement-timeout variant is NOT
    built here: it rewrites the error graphql-core already produced (see
    ``LimitsExtension.on_execute``), so both sources end in the same envelope.
    """
    return ExecutionResult(
        data=None,
        errors=[
            GraphQLError(
                "Query execution exceeded the wall-clock limit",
                extensions={
                    "code": CODE_QUERY_TIMEOUT,
                    "source": TIMEOUT_SOURCE_WALL_CLOCK,
                },
            )
        ],
    )


def _is_statement_timeout(exc: object) -> bool:
    """Whether an execution error is a Postgres statement-timeout cancel.

    The exception SQLAlchemy surfaces is an OUTER ``DBAPIError`` whose
    ``sqlstate`` is ``None`` and which is not an ``OperationalError``: the
    ``57014`` marker lives on its ``__cause__`` chain (``DBAPIError`` →
    ``asyncpg`` error with ``sqlstate='57014'`` → ``QueryCanceledError``;
    measured, 312-RESEARCH.md §Verified mechanism (C)). Matching on the outer
    object therefore never fires — walk the chain instead, with a cycle guard,
    because ``__cause__``/``__context__`` graphs can contain loops.
    """
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if getattr(exc, "sqlstate", None) == "57014":
            return True
        if type(exc).__name__ == "QueryCanceledError":
            return True
        exc = getattr(exc, "__cause__", None) or getattr(exc, "__context__", None)
    return False


@dataclass(frozen=True)
class FragmentGraph:
    """One document's fragments, prepared once for every limit axis.

    ``fragments`` maps a fragment name to its definition, ``spreads`` to the
    fragment names it spreads (inline-fragment nesting collected, unknown names
    kept verbatim), ``order`` is a dependency order — every spread target before
    its user, cycle members excluded — and ``cyclic`` is the EXACT set of
    fragments that participate in a spread cycle. ``document`` is the parsed
    document the graph was built from: the cost fold needs it to build the
    per-fragment ``ValidationContext``.

    The graph carries no module-level state: it is built per document and
    discarded with it, so one request's fragment values are never observable by
    another (TRI317-66).
    """

    fragments: Mapping[str, FragmentDefinitionNode]
    spreads: Mapping[str, tuple[str, ...]]
    order: tuple[str, ...]
    cyclic: frozenset[str]
    document: DocumentNode


def prepare_fragment_graph(document_ast: DocumentNode) -> FragmentGraph:
    """Build the shared dependency-order fragment graph in one pass (TRI317-64).

    Both halves are iterative with explicit stacks, never recursive: the whole
    point is that a long chain of single spreads costs one pass and cannot
    exhaust the interpreter stack. ``cyclic`` is computed from the strongly
    connected components (size > 1, plus every self-spreading fragment) — NOT
    as "left unplaced by Kahn", because Kahn also leaves every fragment that
    merely *depends* on a cycle unplaced, and those must still be measured.
    """
    fragments: dict[str, FragmentDefinitionNode] = {
        definition.name.value: definition
        for definition in document_ast.definitions
        if isinstance(definition, FragmentDefinitionNode)
    }
    spreads: dict[str, tuple[str, ...]] = {
        name: _spread_names(fragment.selection_set)
        for name, fragment in fragments.items()
    }
    # Unknown spread targets are not nodes: they contribute 0, exactly as the
    # old recursive walks did when a definition could not be found.
    dependencies: dict[str, tuple[str, ...]] = {
        name: tuple(target for target in targets if target in fragments)
        for name, targets in spreads.items()
    }
    cyclic = _cyclic_fragments(fragments, dependencies)
    return FragmentGraph(
        fragments=fragments,
        spreads=spreads,
        order=_dependency_order(fragments, dependencies, cyclic),
        cyclic=cyclic,
        document=document_ast,
    )


def evaluate_fragments(
    graph: FragmentGraph,
    fold: Callable[[str, Mapping[str, int]], int],
) -> dict[str, int]:
    """Fold every fragment of ``graph`` exactly once, in dependency order.

    Cycle members are written into the result as ``0`` BEFORE the first fold
    call, so no fold ever needs a back-edge lookup and the order of spreads
    cannot matter (TRI317-65). ``fold`` receives the fragment name and the
    values known so far; every fragment it spreads is already present — a
    dependency (order guarantee) or a cycle member (seeded above).
    """
    values: dict[str, int] = {name: 0 for name in graph.cyclic}
    for name in graph.order:
        values[name] = fold(name, values)
    return values


def _spread_names(selection_set: SelectionSetNode | None) -> tuple[str, ...]:
    """Every fragment name spread inside a selection set, in first-seen order."""
    names: list[str] = []
    seen: set[str] = set()
    stack: list[SelectionSetNode] = [] if selection_set is None else [selection_set]
    while stack:
        current = stack.pop()
        for selection in current.selections:
            if isinstance(selection, FragmentSpreadNode):
                name = selection.name.value
                if name not in seen:
                    seen.add(name)
                    names.append(name)
            elif isinstance(selection, (FieldNode, InlineFragmentNode)):
                if selection.selection_set is not None:
                    stack.append(selection.selection_set)
    return tuple(names)


def _cyclic_fragments(
    fragments: Mapping[str, FragmentDefinitionNode],
    dependencies: Mapping[str, tuple[str, ...]],
) -> frozenset[str]:
    """Members of every spread cycle — iterative Tarjan, no recursion.

    A component of one member is a cycle only when that fragment spreads
    itself. A fragment that merely spreads a cycle is a component of its own
    and is NOT a member (plan review 2026-11-02): it keeps its own
    contribution instead of having its whole selection zeroed out.
    """
    index: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    cyclic: set[str] = set()
    counter = 0

    for root in fragments:
        if root in index:
            continue
        work: list[tuple[str, int]] = [(root, 0)]
        while work:
            name, position = work.pop()
            if position == 0:
                index[name] = counter
                lowlink[name] = counter
                counter += 1
                stack.append(name)
                on_stack.add(name)
            successors = dependencies[name]
            descended = False
            while position < len(successors):
                successor = successors[position]
                position += 1
                if successor not in index:
                    work.append((name, position))
                    work.append((successor, 0))
                    descended = True
                    break
                if successor in on_stack:
                    lowlink[name] = min(lowlink[name], index[successor])
            if descended:
                continue
            if lowlink[name] == index[name]:
                members: list[str] = []
                while True:
                    member = stack.pop()
                    on_stack.discard(member)
                    members.append(member)
                    if member == name:
                        break
                if len(members) > 1 or name in dependencies[name]:
                    cyclic.update(members)
            if work:
                parent = work[-1][0]
                lowlink[parent] = min(lowlink[parent], lowlink[name])
    return frozenset(cyclic)


def _dependency_order(
    fragments: Mapping[str, FragmentDefinitionNode],
    dependencies: Mapping[str, tuple[str, ...]],
    cyclic: frozenset[str],
) -> tuple[str, ...]:
    """Kahn peeling with the cycle members treated as already placed.

    A fragment is ready when every fragment it spreads is placed; because the
    cyclic members start out placed, every non-member lands in the order —
    which is exactly what an "unplaced means cyclic" reading would get wrong.
    The queue is seeded in definition order, so the result is deterministic.
    """
    pending = {
        name: sum(1 for target in dependencies[name] if target not in cyclic)
        for name in fragments
        if name not in cyclic
    }
    dependents: dict[str, list[str]] = {name: [] for name in fragments}
    for name, targets in dependencies.items():
        if name in cyclic:
            continue
        for target in targets:
            if target not in cyclic:
                dependents[target].append(name)

    ready = deque(
        name for name in fragments if name not in cyclic and pending[name] == 0
    )
    order: list[str] = []
    while ready:
        name = ready.popleft()
        order.append(name)
        for dependent in dependents[name]:
            pending[dependent] -= 1
            if pending[dependent] == 0:
                ready.append(dependent)
    return tuple(order)


def count_aliases(document_ast: DocumentNode) -> int:
    """Count aliased fields across every operation of the document.

    Mirrors the semantics of Strawberry's ``count_fields_with_alias``: the
    count is a DOCUMENT total (every operation contributes, never one selected
    operation's share), a fragment spread is expanded at its use site — a
    definition is never counted once as a root selection.

    TRI317-56/TRI317-65: every fragment is measured ONCE, in dependency order,
    by :func:`prepare_fragment_graph` + :func:`evaluate_fragments` (the one
    helper the cost axis shares). For an acyclic document the expanded totals
    are unchanged; a fragment that participates in a spread cycle contributes
    ``0`` deterministically, in any declaration order.
    """
    graph = prepare_fragment_graph(document_ast)
    values = evaluate_fragments(graph, partial(_fold_aliases, graph=graph))
    return sum(
        _count_selection_aliases(definition.selection_set, values)
        for definition in document_ast.definitions
        if isinstance(definition, OperationDefinitionNode)
    )


def measure_depth(document_ast: DocumentNode) -> int:
    """Maximum operation depth of the document (Strawberry's semantics).

    Mirrors ``QueryDepthLimiter``'s ``determine_depth``: a field with a
    selection set adds one level, a leaf adds zero, ``__``-prefixed fields (and
    their WHOLE subtree) are ignored entirely — so a pure introspection
    document measures 0 and the graphql-studio docs explorer keeps working
    (D-11) — and a fragment spread is expanded at its use site, never counted
    once from its definition. The document value is the maximum across every
    operation.

    TRI317-56/TRI317-65: a fragment's depth is relative to its spread site, so
    it is measured once in dependency order (the same helper as
    :func:`count_aliases`). A fragment that participates in a spread cycle
    contributes ``0``, deterministically in any declaration order.
    """
    graph = prepare_fragment_graph(document_ast)
    values = evaluate_fragments(graph, partial(_fold_depth, graph=graph))
    return max(
        (
            _depth_of_selection_set(definition.selection_set, values)
            for definition in document_ast.definitions
            if isinstance(definition, OperationDefinitionNode)
        ),
        default=0,
    )


def _count_selection_aliases(
    selection_set: SelectionSetNode | None,
    values: Mapping[str, int],
) -> int:
    """Aliases of one selection set, fragments read from the prepared values."""
    if selection_set is None:
        return 0

    total = 0
    for selection in selection_set.selections:
        if isinstance(selection, FieldNode):
            if selection.alias:
                total += 1
            total += _count_selection_aliases(selection.selection_set, values)
        elif isinstance(selection, InlineFragmentNode):
            total += _count_selection_aliases(selection.selection_set, values)
        elif isinstance(selection, FragmentSpreadNode):
            # A prepared value (dependency order) or a cycle member seeded to 0;
            # an unknown fragment name contributes 0 exactly as before.
            total += values.get(selection.name.value, 0)
    return total


def _depth_of_selection_set(
    selection_set: SelectionSetNode,
    values: Mapping[str, int],
) -> int:
    """Depth of one selection set, fragments read from the prepared values."""
    return max(
        (
            _depth_of_single_selection(selection, values)
            for selection in selection_set.selections
        ),
        default=0,
    )


def _depth_of_single_selection(selection: object, values: Mapping[str, int]) -> int:
    if isinstance(selection, FieldNode):
        if selection.name.value.startswith("__") or selection.selection_set is None:
            return 0
        return 1 + _depth_of_selection_set(selection.selection_set, values)
    if isinstance(selection, FragmentSpreadNode):
        return values.get(selection.name.value, 0)
    if isinstance(selection, InlineFragmentNode):
        return _depth_of_selection_set(selection.selection_set, values)
    return 0


def _fold_aliases(
    name: str, values: Mapping[str, int], *, graph: FragmentGraph
) -> int:
    """The alias axis's per-fragment fold: ``values`` is a plain lookup."""
    return _count_selection_aliases(graph.fragments[name].selection_set, values)


def _fold_depth(
    name: str, values: Mapping[str, int], *, graph: FragmentGraph
) -> int:
    """The depth axis's per-fragment fold: relative to the spread site."""
    return _depth_of_selection_set(graph.fragments[name].selection_set, values)


class AliasLimitRule(ValidationRule):
    """Document-level alias limit reporting the contract payload (GQLC-02).

    Measured in ``enter_document`` so the unit is the whole submitted document
    — every operation, fragment spreads expanded at their use sites — with a
    strict comparison, so a document carrying exactly ``GRAPHQL_MAX_ALIASES``
    aliases is accepted.
    """

    def enter_document(self, node: DocumentNode, *_args: object) -> None:
        allowed = settings.graphql_max_aliases
        actual = count_aliases(node)
        if actual > allowed:
            self.report_error(
                make_limit_error(
                    f"Query uses {actual} aliases; the allowed maximum is {allowed}",
                    "aliases",
                    actual,
                    allowed,
                )
            )


class DepthLimitRule(ValidationRule):
    """Document-level depth limit reporting the contract payload (GQLC-02).

    Strawberry's built-in ``QueryDepthLimiter`` reports a bare ``GraphQLError``
    with no ``extensions``, which would have shadowed this contract for
    over-depth documents — it was therefore REMOVED from ``_extensions`` (see
    ``app/graphql/schema.py``) and this rule is the depth axis on every path.
    ``__``-prefixed fields do not contribute depth, so a pure introspection
    document measures 0 (D-11). The comparison is strict: a document at exactly
    ``GRAPHQL_MAX_DEPTH`` is accepted.
    """

    def enter_document(self, node: DocumentNode, *_args: object) -> None:
        allowed = settings.graphql_max_depth
        actual = measure_depth(node)
        if actual > allowed:
            self.report_error(
                make_limit_error(
                    f"Operation depth {actual} exceeds the allowed maximum of {allowed}",
                    "depth",
                    actual,
                    allowed,
                )
            )


# ── The cost axis (GQLC-03): one algorithm, one enforcement point, one
#    measurement entry point. ──


def _clamp_limit(value: int) -> int:
    """Clamp a ``limit`` argument into the cost model's range.

    The upper bound is the resolvers' own ``min(limit, MAX_LIMIT)`` (imported
    from ``app.api.pagination``, never redefined here), so the estimator can
    never drift from the real clamp. The lower bound is 1 — never 0: the
    literal enters the arithmetic multiplicatively
    (``multiplier * (1 + child_sum)``), so a zero multiplier prices the WHOLE
    subtree at zero, and a single non-positive literal would undercut the
    budget for any resolver that reads it as "no limit". Measured before the
    floor: ``{ objects(limit: -1) { values { attribute { id } } } }`` priced its
    entire subtree at 0, below even the ``limit: 1`` shape. The internal
    resolvers clamp only the upper bound (``min(limit, MAX_LIMIT)``), so a
    negative literal reaches Postgres as-is; the public API's ``max(0, …)``
    clamp turns it into zero rows. Neither reading is free work.
    """
    return max(1, min(value, MAX_LIMIT))


def _noop_error_sink(_error: GraphQLError) -> None:
    """Swallow errors for measurement-only walks (nothing here reports)."""


def _fold_cost(
    name: str,
    values: Mapping[str, int],
    *,
    schema: GraphQLSchema,
    graph: FragmentGraph,
) -> int:
    """The cost axis's per-fragment fold, in dependency order.

    One ``TypeInfo`` seeded from the fragment's type condition (its parent type
    is fixed by the definition, never by the spread site) and one accumulator
    over a no-op error sink — the same construction graphql-core's
    ``validate`` uses internally. Every spread met inside the fragment is a
    plain dictionary lookup into ``values``: a dependency is already measured
    and a cycle member is seeded to 0, so no fold ever re-enters another.
    """
    fragment = graph.fragments[name]
    fragment_type = type_from_ast(schema, fragment.type_condition)
    if fragment_type is None:
        return 0
    type_info = TypeInfo(schema, initial_type=fragment_type)
    context = ValidationContext(schema, graph.document, type_info, _noop_error_sink)
    nested = CostAccumulator(context, fragment_values=values)
    visit(fragment.selection_set, TypeInfoVisitor(type_info, nested))
    return nested.measured_cost


class CostAccumulator(ValidationRule):
    """The ONE cost algorithm — shared by enforcement and measurement.

    A document-level, type-aware multiplicative estimate, accumulated during an
    ordinary validation walk with a stack of frames (one frame per entered
    field, holding its multiplier and the sum of its children's values):

    * ``enter_field`` pushes the field's multiplier, ``leave_field`` pops it,
      computes ``multiplier * (1 + child_sum)`` and folds the result into the
      enclosing frame — so the document's measured cost is the root total,
      exposed as ``measured_cost``.
    * The multiplier comes from the field's UNWRAPPED GraphQL type via
      ``self.context.get_field_def()`` (TypeInfo-backed), never from the field
      name: ``search.objects`` is a nested result group, and a name-based
      classification gets it wrong.
    * A list field that defines a ``limit`` argument costs the argument's
      clamped literal — floored at one unit of work by ``_clamp_limit``, so a
      non-positive literal can never price a subtree at zero; a VARIABLE (or any
      non-literal) costs ``MAX_LIMIT`` because a validation rule cannot see
      runtime values; an omitted argument costs its schema default; a list field
      without a ``limit`` argument costs ``NESTED_LIST_WEIGHT``; anything else
      costs 1.
    * ``__``-prefixed fields and their WHOLE subtree cost 0 (introspection
      exemption, D-11): the frame is pushed with multiplier 0, so every
      descendant folds into a zero product.
    * Fragment definitions are SKIPped — their selections are counted at the
      spread sites, which read the fragment's subtotal from ONE prepared map
      (``prepare_fragment_graph`` + ``evaluate_fragments``, the same dependency
      order the alias and depth axes use). A fragment's subtotal is independent
      of the spread site (its type condition fixes the parent type), which is
      what makes the shared map sound; a spread cycle (invalid GraphQL) costs 0
      because its members are seeded to 0 before the first fold.
    """

    def __init__(
        self,
        context: ValidationContext,
        *,
        fragment_costs: Mapping[str, int] | None = None,
        fragment_values: Mapping[str, int] | None = None,
    ) -> None:
        super().__init__(context)
        self._frames: list[list[int]] = []
        self.measured_cost = 0
        # ``fragment_costs`` is the historical name for the same thing: a
        # prepared name -> cost map. Either one is used as-is, so a caller that
        # measured the document already (``estimate_cost``) pays nothing here;
        # with neither, the map is prepared lazily from this context's document
        # — one graph per accumulator instance, never a module-level cache.
        prepared = fragment_values if fragment_values is not None else fragment_costs
        self._fragment_values: dict[str, int] | None = (
            None if prepared is None else dict(prepared)
        )

    # ── the walk ──

    def enter_field(self, node: FieldNode, *_args: object) -> None:
        multiplier = (
            0 if node.name.value.startswith("__") else self._field_multiplier(node)
        )
        self._frames.append([multiplier, 0])

    def leave_field(self, _node: FieldNode, *_args: object) -> None:
        multiplier, child_sum = self._frames.pop()
        self._fold(multiplier * (1 + child_sum))

    def enter_fragment_spread(self, node: FragmentSpreadNode, *_args: object) -> None:
        self._fold(self._fragment_cost(node.name.value))

    def enter_fragment_definition(self, *_args: object) -> object:
        # Counted at the spread sites only — never once from the definition.
        return SKIP

    def _fold(self, value: int) -> None:
        if self._frames:
            self._frames[-1][1] += value
        else:
            self.measured_cost += value

    # ── the multiplier (Review correction: rules 3 and 4 close the
    #    `objects(limit: $n)` bypass and the omitted-`limit` undercount) ──

    def _field_multiplier(self, node: FieldNode) -> int:
        field_def = self.context.get_field_def()
        if field_def is None:
            return 1
        field_type = field_def.type
        while is_non_null_type(field_type):
            field_type = field_type.of_type
        if not is_list_type(field_type):
            return 1
        limit_argument = field_def.args.get("limit")
        if limit_argument is None:
            return NESTED_LIST_WEIGHT
        passed = next(
            (
                argument
                for argument in node.arguments or ()
                if argument.name.value == "limit"
            ),
            None,
        )
        if passed is None:
            default = limit_argument.default_value
            if default is Undefined or default is None:
                return MAX_LIMIT
            return _clamp_limit(int(default))
        if isinstance(passed.value, IntValueNode):
            return _clamp_limit(int(passed.value.value))
        return MAX_LIMIT

    # ── fragment expansion (one prepared map, dependency order) ──

    def _fragment_cost(self, name: str) -> int:
        """The fragment's prepared subtotal — never a nested walk.

        A name absent from the map contributes 0, exactly as before: an unknown
        spread, or a fragment whose type condition does not resolve.
        """
        return self._prepared_values().get(name, 0)

    def _prepared_values(self) -> Mapping[str, int]:
        if self._fragment_values is None:
            graph = prepare_fragment_graph(self.context.document)
            self._fragment_values = evaluate_fragments(
                graph,
                partial(_fold_cost, schema=self.context.schema, graph=graph),
            )
        return self._fragment_values


class CostLimitRule(CostAccumulator):
    """Enforces the cost budget with the contract payload (GQLC-03).

    Reports once per document in ``leave_document`` (the whole document is the
    unit, so the walk is complete and every frame is closed) and only when the
    measured cost is strictly greater than ``GRAPHQL_MAX_COST`` — a document at
    exactly the budget is accepted.
    """

    def leave_document(self, _node: DocumentNode, *_args: object) -> None:
        allowed = settings.graphql_max_cost
        actual = self.measured_cost
        if actual > allowed:
            self.report_error(
                make_limit_error(
                    f"Query cost {actual} exceeds the allowed maximum of {allowed}",
                    "cost",
                    actual,
                    allowed,
                )
            )


def estimate_cost(graphql_schema: GraphQLSchema, document_ast: DocumentNode) -> int:
    """Measure one document with the ONE cost algorithm — the only entry point.

    The D-04 ``X-GraphQL-Cost`` header, the ``actual`` of a cost rejection and
    (from 312-05) the revalidation CLI's ``cost`` column all read this function,
    which builds the same ``TypeInfo``/``ValidationContext``/``visit``
    construction ``graphql.validation.validate`` uses internally (with a no-op
    error sink — nothing is reported here) and returns the accumulator's
    ``measured_cost``.

    TRI317-64/TRI317-67: the fragment subtotals are prepared ONCE, in
    dependency order, and handed to the accumulator — so every fragment is
    folded exactly once no matter how often it is spread, and the numbers are
    the same ones the alias and depth axes read.
    """
    graph = prepare_fragment_graph(document_ast)
    values = evaluate_fragments(
        graph, partial(_fold_cost, schema=graphql_schema, graph=graph)
    )
    type_info = TypeInfo(graphql_schema)
    context = ValidationContext(
        graphql_schema, document_ast, type_info, _noop_error_sink
    )
    accumulator = CostAccumulator(context, fragment_values=values)
    visit(document_ast, TypeInfoVisitor(type_info, accumulator))
    return accumulator.measured_cost


def count_tokens(document_text: str) -> int:
    """Return graphql-core's own token count for ``document_text``.

    The count includes the comment tokens the lexer skips over between focused
    tokens — exactly the counter the parser's ``max_tokens`` limit uses, so a
    comment-padded document cannot slip under the limit while still allocating
    one token per comment.
    """
    return Parser(document_text).parse_document().token_count


@dataclass
class DocumentAnalysis:
    """The shared analyzer's result: the contract errors plus the measured cost.

    ``cost`` is 312-02's single cost measurement (``estimate_cost``) — the same
    number the enforced ``CostLimitRule`` compares and the ``X-GraphQL-Cost``
    header reports — never a second computation. It is ``None`` only when there
    is no parsed document to measure at all, which a caller reporting an
    unparseable stored document hits (the revalidation tool); ``analyze_document``
    itself always returns a measured number.
    """

    errors: list[GraphQLError]
    cost: int | None


def _recursion_limit_error() -> GraphQLError:
    """The contract-shaped answer when a document is too deep to analyse.

    ``actual`` is a lower bound, never 0: the contract's reading is
    ``actual > allowed``, and a client shrinking against ``actual: 0`` would be
    told the document is fine. ``settings.graphql_max_depth + 1`` is the
    smallest value the breach proves.
    """
    allowed = settings.graphql_max_depth
    return make_limit_error(
        "Query nests too deeply to analyse; the actual depth is at least "
        f"{allowed + 1} and exceeds the allowed maximum of {allowed}",
        "depth",
        allowed + 1,
        allowed,
    )


def analyze_document(
    graphql_schema: GraphQLSchema,
    document_ast: DocumentNode,
    document_text: str,
) -> DocumentAnalysis:
    """Run the ONE limit contract against a document (GQLC-05/D-07).

    The write-side twin of the execution path: the token comparison plus the
    SAME rule classes the shared ``_extensions`` register (``AliasLimitRule``,
    ``DepthLimitRule``, ``CostLimitRule``), so a rule change can never apply to
    only one of save and execute. The returned ``cost`` is the single
    ``estimate_cost`` measurement — the number the CLI's ``cost`` column prints
    and the ``X-GraphQL-Cost`` header reports.

    The graphql-core schema is a PARAMETER, never an import: this module must not
    import ``app.graphql.schema`` at module level (see the module docstring), and
    taking the schema as an argument is what lets every caller resolve it the way
    its own context requires.

    TRI317-64: the whole analysis sits inside a ``RecursionError`` guard. The
    fragment walkers are iterative, but the parser, ``count_tokens``' own parse
    and the field/inline-fragment nesting inside a single fragment stay
    recursive; without this arm a deep document escapes as an unhandled 500 on
    saved-query save and as a generic error (dropping the D-04 cost headers) on
    execute. The guard never re-raises: both doors consume contract errors.
    """
    errors: list[GraphQLError] = []
    try:
        allowed_tokens = settings.graphql_max_tokens
        actual_tokens = count_tokens(document_text)
        if actual_tokens > allowed_tokens:
            errors.append(
                make_limit_error(
                    f"Query uses {actual_tokens} tokens; the allowed maximum is {allowed_tokens}",
                    "tokens",
                    actual_tokens,
                    allowed_tokens,
                )
            )
        errors.extend(
            validate(
                graphql_schema,
                document_ast,
                [AliasLimitRule, DepthLimitRule, CostLimitRule],
            )
        )
        cost = estimate_cost(graphql_schema, document_ast)
    except RecursionError:
        errors.append(_recursion_limit_error())
        return DocumentAnalysis(errors=errors, cost=0)
    return DocumentAnalysis(errors=errors, cost=cost)


def validate_document_limits(query_text: str) -> list[GraphQLError]:
    """The save-time convenience wrapper: the limit errors for ``query_text``.

    Resolves the strawberry schema through the function-local import below and
    parses the text, so the save-time call sites keep consuming a plain error
    list while the schema never becomes a module-level dependency of this module.
    An unparseable document returns no limit errors: the save path's syntax check
    (``validate_saved_query_document``) runs first and already reports it —
    including the recursive-parser ``RecursionError`` a deeply nested document
    raises (caught here so a caller that reaches this function first cannot leak
    an unhandled 500 either).
    """
    # Lazy import avoids an import cycle: schema.py imports the rule classes from this module at
    # module level (mirrors saved_query_service.execute_saved_query).
    from app.graphql.schema import schema as strawberry_schema

    try:
        document_ast = parse(query_text)
    except (GraphQLError, RecursionError):
        return []
    return analyze_document(strawberry_schema._schema, document_ast, query_text).errors


def _token_abort_actual(document_text: str | None) -> int:
    """The measured token count of a document the parser already aborted on.

    The abort proves the count is above the configured maximum. The measurement
    parse is itself BOUNDED (four times the configured limit) — never an
    uncapped re-parse of the very document the capped parse just aborted on:
    that would hand a request the CPU and memory the token limit exists to
    deny, and would re-walk a document the parser already refused (measured
    before the bound: a 1 MiB token-dense body cost multiple seconds and
    ~140 MiB on the rejection path alone).

    ``RecursionError`` is caught next to ``GraphQLError`` because graphql-core's
    parser is recursive: a document whose padding aborts the capped parse can
    still push the bounded re-parse past the interpreter's recursion limit
    (measured: ~250 nesting levels), and that must degrade to the bounded
    floor — a contract-shaped ``QUERY_TOO_COMPLEX``/``tokens`` rejection —
    instead of escaping as a generic non-contract error that would also strip
    the D-04 cost headers from the response.

    Below the bound the count is exact (``Parser.token_count`` includes the
    comment tokens the lexer skips, exactly like the capped parse's counter).
    Above it, the count is a lower bound clamped to ``max_tokens + 1``: being
    over the limit is all the contract needs to report.
    """
    text = document_text or ""
    bound = settings.graphql_max_tokens
    parser = Parser(text, max_tokens=bound * 4)
    try:
        return parser.parse_document().token_count
    except (GraphQLError, RecursionError):
        return max(parser.token_count, bound + 1)


def _request_path(context: object) -> str | None:
    request = getattr(context, "request", None)
    url = getattr(request, "url", None)
    path = getattr(url, "path", None)
    return str(path) if path is not None else None


def _caller_id(context: object) -> str | None:
    """The calling user's / service account's UUID — never a name (D-06)."""
    user = getattr(context, "user", None)
    user_id = getattr(user, "id", None)
    return str(user_id) if user_id is not None else None


def _log_limit_rejection(
    code: str,
    limit: str | None = None,
    actual: int | None = None,
    allowed: int | None = None,
    *,
    source: str | None = None,
    path: str | None = None,
    user: str | None = None,
    query: str | None = None,
) -> None:
    """Emit ONE structured rejection record — never any query text (D-06).

    The query appears only as two derived values: a SHA-256 hash and its
    length. Same reasoning as turning GET off — the document text must not
    reach logs. The caller identity is a UUID only (never the username or the
    e-mail); PII has no place in a rejection log.

    A ``QUERY_TOO_COMPLEX`` rejection always carries all three of
    ``limit``/``actual``/``allowed``; a ``QUERY_TIMEOUT`` has no breached axis,
    so those fields are ``None`` there and ``source`` (D-18) is what
    distinguishes ``statement_timeout`` from ``wall_clock``.
    """
    query_hash: str | None = None
    query_length: int | None = None
    if query is not None:
        query_hash = hashlib.sha256(query.encode("utf-8")).hexdigest()
        query_length = len(query)

    fields: dict[str, object] = {
        "code": code,
        "limit": limit,
        "actual": int(actual) if actual is not None else None,
        "allowed": int(allowed) if allowed is not None else None,
        "path": path,
        "user": user,
        "query_hash": query_hash,
        "query_length": query_length,
    }
    if source is not None:
        fields["source"] = source

    logger.warning(
        "GraphQL limit rejection code=%s limit=%s actual=%s allowed=%s "
        "path=%s user=%s query_hash=%s query_length=%s",
        code,
        limit,
        actual,
        allowed,
        path,
        user,
        query_hash,
        query_length,
        extra=fields,
    )


class LimitsExtension(SchemaExtension):
    """Arms the token limit, normalises its parse abort and logs rejections.

    * ``on_operation`` pre-yield sets ``parse_options["max_tokens"]`` from
      ``settings.graphql_max_tokens``, read at request time (never captured at
      import).
    * ``on_parse`` post-yield rewrites the parser's token abort IN PLACE.
      Strawberry raises the abort inside its own ``parsing()`` context manager,
      so no ``try/except`` around a parse call ever sees it; the same
      ``GraphQLError`` instance is referenced by both
      ``execution_context.pre_execution_errors`` and the returned
      ``PreExecutionError``, so mutating its ``message``/``extensions`` is the
      only place the rejection can be given the contract shape (D-02/D-03).
    * ``on_validate`` post-yield logs the rejections raised by the validation
      rules (aliases/depth/cost): a rule sees neither the request nor the
      calling user, so the extension — the one place both are available —
      emits the structured D-06 record. Exactly one record per error; the
      token case is logged in ``on_parse`` and never again here (validation
      does not run after a parse abort, and the id guard makes it idempotent
      regardless).
    * ``on_operation`` post-yield publishes the D-04 cost headers
      (``X-GraphQL-Cost`` / ``X-GraphQL-Cost-Limit``) on every response that
      had a parsed document. It is ``on_operation`` and not ``on_execute``
      because ``on_execute``'s post-yield does not run for pre-execution
      rejections (Pitfall 2) — which is exactly the response that must still
      show the measured cost.
    * ``on_execute`` post-yield normalises a Postgres statement-timeout cancel
      (GQLC-04/D-18): a resolver error whose exception chain carries ``57014``
      is rewritten to the ``QUERY_TIMEOUT``/``statement_timeout`` contract and
      the result's ``data`` is forced to ``None`` (D-20 — no partial data).
      No further database work happens in that request: the cancel aborts the
      transaction, so any follow-up statement would raise
      ``InFailedSQLTransactionError`` (Pitfall 5). Note that a WALL-CLOCK
      cancel never reaches this hook — the execution is cancelled before the
      hook's post-yield; ``LimitedGraphQLRouter`` synthesizes that envelope
      itself.
    """

    def __init__(self, *, execution_context: object | None = None) -> None:
        super().__init__(execution_context=execution_context)
        self._logged_error_ids: set[int] = set()

    def on_operation(self):
        self.execution_context.parse_options["max_tokens"] = (
            settings.graphql_max_tokens
        )
        yield
        self._publish_cost_headers()

    def _publish_cost_headers(self) -> None:
        """Write the two D-04 cost headers onto the outgoing response.

        The schema comes from the execution context
        (``execution_context.schema._schema``), never from an import of
        ``app.graphql.schema`` (import-direction hard rule above). The cost is
        the SAME ``estimate_cost`` the enforcing rule compares against the
        budget — one estimator, not two. ``graphql_document`` is None when
        nothing was parsed (empty body, syntax error, token parse abort), and
        only then are the headers skipped.
        """
        document = self.execution_context.graphql_document
        if document is None:
            return
        response = getattr(self.execution_context.context, "response", None)
        headers = getattr(response, "headers", None)
        if headers is None:
            return
        cost = estimate_cost(self.execution_context.schema._schema, document)
        headers["X-GraphQL-Cost"] = str(cost)
        headers["X-GraphQL-Cost-Limit"] = str(settings.graphql_max_cost)

    def on_parse(self):
        yield
        allowed = settings.graphql_max_tokens
        abort_marker = f"Document contains more than {allowed} tokens"
        for error in self.execution_context.pre_execution_errors or []:
            if abort_marker not in (error.message or ""):
                continue
            actual = _token_abort_actual(self.execution_context.query)
            error.message = f"Query exceeds the allowed maximum of {allowed} tokens"
            error.extensions = {
                "code": CODE_QUERY_TOO_COMPLEX,
                "limit": "tokens",
                "actual": actual,
                "allowed": allowed,
            }
            # D-06: no query text in ANY log. Strawberry's execution logger
            # prints `str(error)`, and a GraphQLError carrying a `source` prints
            # an excerpt of the document around the abort position — dropping
            # the source (the line/column `locations` survive) keeps the
            # document text out of the standard execution log too.
            error.source = None
            self._log_rejection(error)

    def on_validate(self):
        yield
        for error in self.execution_context.pre_execution_errors or []:
            extensions = error.extensions or {}
            if extensions.get("code") == CODE_QUERY_TOO_COMPLEX:
                self._log_rejection(error)

    def on_execute(self):
        yield
        result = self.execution_context.result
        if result is None:
            return
        rewritten = False
        for error in result.errors or []:
            if not _is_statement_timeout(getattr(error, "original_error", None)):
                continue
            error.message = "Query execution exceeded the statement timeout"
            error.extensions = {
                "code": CODE_QUERY_TIMEOUT,
                "source": TIMEOUT_SOURCE_STATEMENT,
            }
            rewritten = True
            self._log_rejection(error)
        if rewritten:
            # D-20: a cancelled operation never ships partial data — and the
            # transaction is aborted, so no further DB work may happen here.
            result.data = None

    def _log_rejection(self, error: GraphQLError) -> None:
        if id(error) in self._logged_error_ids:
            return
        self._logged_error_ids.add(id(error))
        extensions = error.extensions or {}
        _log_limit_rejection(
            extensions.get("code") or CODE_QUERY_TOO_COMPLEX,
            extensions.get("limit"),
            extensions.get("actual"),
            extensions.get("allowed"),
            source=extensions.get("source"),
            path=_request_path(self.execution_context.context),
            user=_caller_id(self.execution_context.context),
            query=self.execution_context.query,
        )

