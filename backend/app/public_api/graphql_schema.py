"""Public GraphQL schema module (Plan 205-01) — D-05 reduced Query + D-07 typed filters.

This module owns the PUBLIC GraphQL surface served by Plan 02's
``GraphQLRouter`` at ``/public/api/v1/graphql``:

- ``FilterOperator`` / ``LogicalOperator`` / ``AttributeFilterInput`` /
  ``FilterGroupInput`` — typed, structured filter arguments (D-07).  No
  opaque JSON scalar like the internal ``resolve_objects``.
- ``_to_attr_filters`` / ``_to_filter_tree`` — translation into the
  EXACT ``attr_filters`` / ``filter_tree`` dict shapes that
  ``app.services.object_service.list_objects`` already consumes.  The flat
  ``_to_attr_filters`` path validates (rejects) negate and duplicate
  ``attribute_id`` entries rather than silently dropping them (WR-01/WR-02).
  No new filtering engine — unsupported ops fail in ``build_filter_clause`` at
  query time.
- ``PublicQuery`` + ``public_schema`` — the six D-05 fields only
  (object/objects/objectType/objectTypes/schema/schemas).  Source,
  health and search are structurally absent.

A distinct public Query/schema is REQUIRED (D-05 + D-07): the shared
``app.graphql.schema.schema`` cannot drop source/health/search fields or
swap the JSON-scalar ``objects`` args without editing ``app/graphql/``
(out of scope).  Both schemas share the same read-only type graph —
RESEARCH.md confirmed a second Schema over it is safe.
"""

from __future__ import annotations

import uuid
from enum import Enum
from typing import List, Optional

import strawberry
from strawberry.types import Info

import graphql

from app.api.pagination import MAX_LIMIT, OBJECTS_DEFAULT_LIMIT
from app.graphql.resolvers import root_resolvers
from app.graphql.resolvers.mappers import object_from_orm
from app.graphql.schema import _extensions
from app.graphql.types import Object as GQLObject
from app.graphql.types import ObjectType as GQLObjectType
from app.graphql.types import Schema as GQLSchema
from app.services.object_service import list_objects


@strawberry.enum
class FilterOperator(Enum):
    """Typed filter operators (D-07).

    ``.value`` strings EXACTLY mirror ``build_filter_clause``'s op
    vocabulary (backend/app/services/filter_utils.py) — the runtime
    supports nothing else, and an unsupported op raises ValueError at
    query time.  Deliberately NO neq/in/ends_with members.
    """

    EQ = "eq"
    GT = "gt"
    LT = "lt"
    GTE = "gte"
    LTE = "lte"
    BETWEEN = "between"
    BEFORE = "before"
    AFTER = "after"
    CONTAINS = "contains"
    STARTS_WITH = "starts_with"


@strawberry.enum
class LogicalOperator(Enum):
    """Boolean combinators for nested FilterGroupInput trees."""

    AND = "AND"
    OR = "OR"


@strawberry.input
class AttributeFilterInput:
    """One leaf condition: ``attribute_id`` + operator + optional values.

    Maps onto one entry of the flat ``attr_filters`` dict or one
    ``conditions`` entry of a ``filter_tree`` node.
    """

    attribute_id: uuid.UUID
    op: FilterOperator
    value: Optional[str] = None
    value2: Optional[str] = None
    negate: bool = False


@strawberry.input
class FilterGroupInput:
    """Nested AND/OR group of attribute conditions (mirrors filter_tree).

    ``groups`` is self-referential — strawberry resolves the forward ref
    lazily at schema-build time.  Depth/condition-count guards are already
    enforced by ``evaluate_filter_tree`` (MAX_TREE_DEPTH /
    MAX_TOTAL_CONDITIONS) plus the schema's QueryDepthLimiter.
    """

    operator: LogicalOperator
    conditions: Optional[List[AttributeFilterInput]] = None
    groups: Optional[List["FilterGroupInput"]] = None


def _to_attr_filters(filters: list[AttributeFilterInput]) -> dict:
    """Translate typed leaf conditions into the flat attr_filters shape.

    Output: ``{ "<attribute_id_str>": {"op": ..., "val": ..., "val2": ...} }``
    — implicit AND across attributes, consumed by ``list_objects`` via
    ``build_filter_clause``.  Field-name mapping is exact:
    ``attribute_id`` -> str key, ``op`` -> ``op.value``, ``value`` -> ``val``,
    ``value2`` -> ``val2``.

    The flat path CANNOT represent negation or duplicate conditions on the
    same attribute — both are rejected with a clear ``GraphQLError``
    (WR-01/WR-02).  Consumers needing negate or same-attribute multi-conditions
    must use the nested ``filter`` (FilterGroupInput) argument instead.
    """
    seen_keys: set[str] = set()
    for f in filters:
        if f.negate:
            raise graphql.GraphQLError(
                "The flat `filters` argument cannot express negation. "
                "Use the nested `filter` (FilterGroupInput) argument instead."
            )
        key = str(f.attribute_id)
        if key in seen_keys:
            raise graphql.GraphQLError(
                f"Duplicate attribute_id in flat `filters`: {key}. "
                "Combine conditions on the same attribute via the nested "
                "`filter` (FilterGroupInput) argument instead."
            )
        seen_keys.add(key)
    return {
        str(f.attribute_id): {
            "op": f.op.value,
            "val": f.value,
            "val2": f.value2,
        }
        for f in filters
    }


def _to_filter_tree(group: FilterGroupInput) -> dict:
    """Translate a typed FilterGroupInput into the nested filter_tree shape.

    Output: ``{"op": "AND"|"OR", "conditions": [...], "groups": [...]}`` —
    recursively preserves nesting, consumed by ``evaluate_filter_tree``.
    ``negate`` is always emitted (bool) so the runtime never misreads a
    missing key.
    """
    return {
        "op": group.operator.value,
        "conditions": [
            {
                "attr_id": str(c.attribute_id),
                "op": c.op.value,
                "val": c.value,
                "val2": c.value2,
                "negate": c.negate,
            }
            for c in (group.conditions or [])
        ],
        "groups": [_to_filter_tree(g) for g in (group.groups or [])],
    }


async def resolve_public_objects(
    info: Info,
    object_type_id: Optional[uuid.UUID] = None,
    schema_id: Optional[uuid.UUID] = None,
    limit: int = OBJECTS_DEFAULT_LIMIT,
    offset: int = 0,
    filters: Optional[List[AttributeFilterInput]] = None,
    filter: Optional[FilterGroupInput] = None,
) -> list[GQLObject]:
    """Public ``objects(...)`` resolver with typed filter arguments (D-07).

    ``filter`` (a nested FilterGroupInput) wins over ``filters`` (flat leaf
    conditions) — mirroring list_objects' own ``if filter_tree: ... elif
    attr_filters:`` precedence.  D-12: user_id/is_superuser flow from the
    public context (service-user identity) so record-level DENY filtering
    applies exactly as it does for AQL (GQL-02 criterion 3).
    """
    effective_limit = max(0, min(limit, MAX_LIMIT))
    safe_offset = max(0, offset)
    if filter is not None:
        items, _ = await list_objects(
            info.context.session,
            skip=safe_offset,
            limit=effective_limit,
            object_type_id=object_type_id,
            schema_id=schema_id,
            filter_tree=_to_filter_tree(filter),
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    elif filters:
        items, _ = await list_objects(
            info.context.session,
            skip=safe_offset,
            limit=effective_limit,
            object_type_id=object_type_id,
            schema_id=schema_id,
            attr_filters=_to_attr_filters(filters),
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    else:
        items, _ = await list_objects(
            info.context.session,
            skip=safe_offset,
            limit=effective_limit,
            object_type_id=object_type_id,
            schema_id=schema_id,
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    return [object_from_orm(o) for o in items]


@strawberry.type
class PublicQuery:
    """Reduced public Query — D-05: hierarchy reads + objects, nothing else.

    Exactly six fields: object, objects, objectType, objectTypes, schema,
    schemas.  source/sources/health/search are structurally absent (they
    are not in the public REST API scope).

    The by-id fields ``object``, ``object_type`` and ``schema`` all delegate
    to ``root_resolvers``, which return null for a record-level DENY on the
    record or an ancestor (the CR-01 semantics now live there, quick
    261011-tgu).  The list fields keep their current resolvers.
    """

    object: Optional[GQLObject] = strawberry.field(
        resolver=root_resolvers.resolve_object)
    objects: List[GQLObject] = strawberry.field(resolver=resolve_public_objects)
    object_type: Optional[GQLObjectType] = strawberry.field(
        resolver=root_resolvers.resolve_object_type
    )
    object_types: List[GQLObjectType] = strawberry.field(
        resolver=root_resolvers.resolve_object_types
    )
    schema: Optional[GQLSchema] = strawberry.field(
        resolver=root_resolvers.resolve_schema
    )
    schemas: List[GQLSchema] = strawberry.field(
        resolver=root_resolvers.resolve_schemas
    )


public_schema = strawberry.Schema(
    query=PublicQuery,
    extensions=_extensions,
)
