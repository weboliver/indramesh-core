import uuid
from dataclasses import dataclass, field
from typing import Any

from lark import Tree


@dataclass
class AttrCondition:
    """An intermediate representation of an attribute condition before resolution."""
    attr_name: str                 # bare attribute name from AST (e.g., "CPU")
    operator: str                  # AQL operator string ("=", "!=", ">", etc.)
    value: Any                     # coerced Python value (str, int, float, bool)
    is_text_search: bool = False   # True if operator is "~" (contains/search)
    negate: bool = False           # True if wrapped in NOT


@dataclass
class ObjectTypeCondition:
    """Intermediate representation of an objectType condition.

    Exactly one of `name` (plain equality) / `names` (AQL-09 IN (...)
    multi-type filter) / `and_children_name` (objectTypeAndChildren(...)
    hierarchy-expansion filter) is set — never more than one, enforced by
    the transformer.
    """
    name: str | None = None                # ObjectType name from `objectType = "X"`
    names: list[str] | None = None         # ObjectType names from `objectType IN (...)`
    and_children_name: str | None = None   # ObjectType name from `objectTypeAndChildren("X")`


@dataclass
class HavingCondition:
    """Intermediate representation of an `object HAVING inR/outR(...)` leaf.

    The nested condition argument is captured as a raw, unresolved Lark Tree —
    resolution (recursive transform + PK-set traversal) happens downstream in
    the resolver (Plan 03), not here.
    """
    direction: str                      # "in" (inR) or "out" (outR)
    reftype_names: list[str] | None     # refType IN (...) names, or None if omitted
    nested_tree: Tree                   # raw `expression` subtree, not yet transformed


@dataclass
class CompileOutput:
    """Structured output from the AQL Transformer — names NOT yet resolved to UUIDs.

    Attributes:
        filter_tree: The boolean-filter dict (AND/OR groups + unresolved conditions).
                     Names are stored as string placeholders.
        object_type: ObjectTypeCondition if present, else None.
        order_by: Sort field name (currently only "name").
        order_direction: "ASC", "DESC", or None.
        conditions: Raw list of AttrCondition for ES vs SQL routing decisions.
        having_conditions: Raw list of HavingCondition (inR/outR leaves) for
                           recursive resolution downstream (Plan 03/04).
    """
    filter_tree: dict              # filter_tree with name placeholders
    object_type: ObjectTypeCondition | None = None
    order_by: str | None = None
    order_direction: str | None = None
    conditions: list[AttrCondition] = field(default_factory=list)
    having_conditions: list[HavingCondition] = field(default_factory=list)


@dataclass
class AQLResolvedQuery:
    """Fully resolved AQL query — all names resolved to UUIDs, ready for execution.

    Attributes:
        filter_tree: Full filter_tree dict with attr_id UUIDs (NOT name strings).
        object_type_id: Resolved ObjectType UUID (or None if no objectType filter,
                         or if object_type_ids is set instead — AQL-09 IN (...)).
        order_by: Sort field name.
        order_direction: "ASC" or "DESC" or None.
        es_conditions: AttrConditions that should be executed via ES (~ operations).
        sql_conditions: AttrConditions that should be executed via SQL filter_tree.
        having_pk_filters: One set of anchor object PKs per resolved having leaf
                           (Plan 03/04 compose these via set.intersection() with
                           any ES pk_filter before calling list_objects()).
        object_type_ids: Resolved ObjectType UUIDs from `objectType IN (...)`
                          (AQL-09) — mutually exclusive with object_type_id.
        candidate_object_type_ids: The flat list of ObjectTypes owning ALL of a
                                    global (objectType-less) branch's attribute
                                    names (D-01/D-02) — consumed by compiler.py's
                                    D-05 ES fan-out loop. Mutually distinct from
                                    object_type_ids (which is a hard type filter
                                    passed to list_objects). Both object_type_id
                                    and object_type_ids are left None when this
                                    is set.
    """
    filter_tree: dict
    object_type_id: uuid.UUID | None
    order_by: str | None = None
    order_direction: str | None = None
    es_conditions: list[AttrCondition] = field(default_factory=list)
    sql_conditions: list[AttrCondition] = field(default_factory=list)
    having_pk_filters: list[set[uuid.UUID]] = field(default_factory=list)
    object_type_ids: list[uuid.UUID] | None = None  # AQL-09: objectType IN (...)
    candidate_object_type_ids: list[uuid.UUID] | None = None
