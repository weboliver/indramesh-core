from lark import Tree, Token
from lark.visitors import Transformer
from app.aql.schemas import CompileOutput, AttrCondition, ObjectTypeCondition, HavingCondition
from app.aql.errors import AQLValidationError


_OPERATOR_MAP = {
    "=": "eq",
    ">": "gt",
    "<": "lt",
    ">=": "gte",
    "<=": "lte",
    "~": "contains",
}

# != is handled as eq + negate at the condition level


def _tree_to_dict(tree: Tree, negate_stack: list[bool]) -> dict:
    """Recursively convert a Lark expression tree to a filter_tree dict.

    Args:
        tree: Lark Tree for an expression
        negate_stack: Stack of active NOT flags from parent context

    Returns:
        filter_tree-compatible dict with string placeholders for names
    """
    current_negate = negate_stack[-1] if negate_stack else False

    if tree.data == "query":
        # Top-level node from parse() — grammar rule is `query: expression order_by?`
        # and is NOT `?`-prefixed, so the root Tree.data is always "query", never
        # "expression". Recurse into the expression child only; order_by (if present)
        # is handled separately by transform(), not by this boolean-tree walk.
        return _tree_to_dict(tree.children[0], negate_stack)

    if tree.data == "expression":
        # Unwrap single expression → delegate to child
        return _tree_to_dict(tree.children[0], negate_stack)

    if tree.data == "or_expr":
        children = [c for c in tree.children if isinstance(c, Tree)]
        if len(children) == 1:
            return _tree_to_dict(children[0], negate_stack)
        return {
            "op": "OR",
            "groups": [],
            "conditions": [],
            "_children": [_tree_to_dict(c, negate_stack) for c in children],
        }

    if tree.data == "and_expr":
        children = [c for c in tree.children if isinstance(c, Tree)]
        if len(children) == 1:
            return _tree_to_dict(children[0], negate_stack)
        return {
            "op": "AND",
            "groups": [],
            "conditions": [],
            "_children": [_tree_to_dict(c, negate_stack) for c in children],
        }

    if tree.data == "negate":
        # NOT_OP is a named terminal and stays in the tree, so children is
        # [Token('NOT_OP', 'NOT'), <not_expr subtree>] — use children[-1] for the
        # wrapped sub-expression regardless of terminal-filtering behavior.
        negate_stack.append(not current_negate)
        result = _tree_to_dict(tree.children[-1], negate_stack)
        negate_stack.pop()
        # De Morgan: NOT wrapping a group flips the group's own combinator, on top
        # of the leaf-level negate flags already applied via negate_stack above.
        # NOT(A OR B) == (NOT A) AND (NOT B); NOT(A AND B) == (NOT A) OR (NOT B).
        if "_children" in result and result.get("op") in ("AND", "OR"):
            result["op"] = "OR" if result["op"] == "AND" else "AND"
        return result

    if tree.data == "atom":
        return _tree_to_dict(tree.children[0], negate_stack)

    if tree.data == "condition":
        return _tree_to_dict(tree.children[0], negate_stack)

    if tree.data == "attr_condition":
        attr_token = tree.children[0]
        op_token = tree.children[1]
        val_token = tree.children[2]

        attr_name = _extract_string(attr_token)
        op_str = str(op_token)
        value = _extract_value(val_token)

        state = {
            "type": "attribute",
            "attr_name": attr_name,
            "op": op_str,
            "value": value,
            "negate": current_negate,
        }
        return state

    if tree.data == "objecttype_condition":
        # OBJECTTYPE_KW/OBJECTTYPE_AND_CHILDREN_KW are named terminals and stay
        # in the tree. Three shapes:
        # equality:       [Token('OBJECTTYPE_KW', ...), Token('CMP_OP', ...), Token('STRING', ...)]
        # IN(...):        [Token('OBJECTTYPE_KW', ...), Token('STRING', ...), Token('STRING', ...), ...]
        # AndChildren(X): [Token('OBJECTTYPE_AND_CHILDREN_KW', ...), Token('STRING', ...)]
        # (the "IN"/"("/")"/"," anonymous literals are filtered out by Lark),
        # disambiguated first on children[0].type, then (for OBJECTTYPE_KW)
        # on whether children[1] is a CMP_OP token.
        first = tree.children[0]
        if isinstance(first, Token) and first.type == "OBJECTTYPE_AND_CHILDREN_KW":
            value = _extract_string(tree.children[1])
            return {
                "type": "objecttype",
                "op": "AND_CHILDREN",
                "value": value,
                "negate": current_negate,
            }

        second = tree.children[1]
        if isinstance(second, Token) and second.type == "CMP_OP":
            op_token = second
            val_token = tree.children[2]

            op_str = str(op_token)
            value = _extract_string(val_token)

            state = {
                "type": "objecttype",
                "op": op_str,
                "value": value,
                "negate": current_negate,
            }
            return state

        values = [_extract_string(t) for t in tree.children[1:]]
        state = {
            "type": "objecttype",
            "op": "IN",
            "value": values,
            "negate": current_negate,
        }
        return state

    if tree.data == "having_condition":
        # children: [Token('REF_FN', 'inR'|'outR'), <expression Tree>, STRING*]
        # REF_FN is a named terminal and stays in the tree (only anonymous
        # string literals "object"/"HAVING"/"refType"/"IN"/"("/")"/"," are
        # filtered) — index past it like objecttype_condition indexes past
        # OBJECTTYPE_KW. The nested expression subtree is captured RAW (not
        # resolved/flattened here) so the resolver can recursively re-run it.
        ref_fn_token = tree.children[0]
        nested_tree = tree.children[1]
        reftype_tokens = tree.children[2:]

        # REF_FN is now case-insensitive at the grammar level ("inR"i|"outR"i),
        # so the matched text can be any casing (e.g. "INR") — compare
        # lowercased rather than the exact literal "inR".
        direction = "in" if str(ref_fn_token).lower() == "inr" else "out"
        reftype_names = [_extract_string(t) for t in reftype_tokens] or None

        return {
            "type": "having",
            "direction": direction,
            "reftype_names": reftype_names,
            "nested_tree": nested_tree,
            "negate": current_negate,
        }

    raise AQLValidationError(f"Unknown tree node: {tree.data}")


def _extract_string(token: Token | Tree) -> str:
    """Extract a clean string value from a STRING token (strip surrounding quotes)."""
    raw = str(token)
    if raw.startswith('"') and raw.endswith('"'):
        return raw[1:-1]
    return raw


def _extract_value(token: Token | Tree) -> str | int | float | bool:
    """Extract a typed Python value from a value node."""
    if isinstance(token, Tree):
        # `value: STRING | NUMBER | BOOL` is not `?`-prefixed, so the parser always
        # wraps the actual token in a `Tree('value', [Token(...)])` — unwrap it first,
        # otherwise str(token) stringifies to Lark's Tree repr, not the real value.
        token = token.children[0]
    raw = str(token)
    # STRING token (quoted)
    if raw.startswith('"'):
        return raw[1:-1]
    # BOOL token
    if raw.lower() in ("true", "false"):
        return raw.lower() == "true"
    # NUMBER token
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        return raw


def _flatten_tree(node: dict) -> dict:
    """Flatten intermediate _children into filter_tree groups and conditions.

    The intermediate tree uses _children to preserve tree structure.
    This pass converts AND/OR nodes with _children into proper filter_tree format
    by splitting children into scalar conditions vs nested groups.
    """
    if "_children" not in node:
        return node

    op = node["op"]
    children = node.pop("_children")

    conditions = []
    groups = []

    for child in children:
        flat = _flatten_tree(child)
        if flat.get("type") in ("attribute", "objecttype", "having"):
            conditions.append(flat)
        elif "op" in flat and flat["op"] in ("AND", "OR"):
            groups.append(flat)
        else:
            conditions.append(flat)

    node["groups"] = groups
    node["conditions"] = conditions
    return node


def _extract_order_by(tree: Tree) -> tuple[str | None, str | None]:
    """Extract ORDER BY name/direction from the top-level `query` Tree.

    `query: expression order_by?` — the second child, if present, is the
    `order_by` Tree (`"ORDER" "BY" NAME ORDER_DIRECTION?`). Grammar literals
    ("ORDER", "BY") are filtered from children by Lark (anonymous string-literal
    terminals are dropped from the tree by default), so `order_by.children` is
    `[NAME]` or `[NAME, ORDER_DIRECTION]`.
    """
    if tree.data != "query" or len(tree.children) < 2:
        return None, None
    order_by_node = tree.children[1]
    if not isinstance(order_by_node, Tree) or order_by_node.data != "order_by":
        return None, None
    # NAME is a free-form regex terminal, not a keyword literal — lowercase it
    # so `ORDER BY Name`/`ORDER BY NAME` are accepted the same as `ORDER BY name`,
    # consistent with every other keyword now being case-insensitive.
    name = str(order_by_node.children[0]).lower()
    direction = str(order_by_node.children[1]).upper() if len(order_by_node.children) > 1 else "ASC"
    return name, direction


def _collect_conditions(
    flat: dict,
) -> tuple[str | None, list[str] | None, str | None, list[AttrCondition], list[HavingCondition]]:
    """Walk a flattened filter_tree dict, extracting the objectType name (if
    any), attribute conditions, and having (inR/outR) conditions.

    Shared by transform() and _transform_expression() — both need identical
    leaf-collection + OR-safety-rejection behavior, the only difference being
    what happens to the tree's order_by (transform() extracts it separately;
    a nested `expression` subtree never has its own order_by).
    """
    object_type_name = None
    object_type_names: list[str] | None = None
    object_type_and_children: str | None = None
    attr_conditions: list[AttrCondition] = []
    having_conditions: list[HavingCondition] = []

    def _collect_leaf(cond: dict) -> None:
        if cond.get("type") == "objecttype":
            nonlocal object_type_name, object_type_names, object_type_and_children
            if cond.get("op") in ("IN", "AND_CHILDREN"):
                # objectType IN (...) / objectTypeAndChildren(...) are also
                # plain, non-negated only — NOT has no defined semantics here.
                if cond.get("negate"):
                    kw = "IN (...)" if cond["op"] == "IN" else "AndChildren(...)"
                    raise AQLValidationError(
                        f"NOT is not supported on objectType {kw} conditions"
                    )
                if (
                    object_type_name is not None
                    or object_type_names is not None
                    or object_type_and_children is not None
                ):
                    raise AQLValidationError(
                        "Multiple different objectType conditions are not supported"
                    )
                if cond["op"] == "IN":
                    object_type_names = cond["value"]
                else:
                    object_type_and_children = cond["value"]
                return
            # objectType only supports plain, non-negated equality — the grammar
            # accepts any CMP_OP and NOT wrapping, but resolution only ever reads
            # cond["value"], so !=, >, or NOT objectType = "X" would otherwise be
            # silently treated as plain equality instead of erroring.
            if cond.get("op") != "=" or cond.get("negate"):
                raise AQLValidationError(
                    'objectType only supports plain equality: objectType = "Name"'
                )
            if object_type_names is not None or object_type_and_children is not None:
                raise AQLValidationError(
                    "Multiple different objectType conditions are not supported"
                )
            if object_type_name is not None and object_type_name != cond["value"]:
                raise AQLValidationError(
                    "Multiple different objectType conditions are not supported"
                )
            object_type_name = cond["value"]
        elif cond.get("type") == "attribute":
            at = AttrCondition(
                attr_name=cond["attr_name"],
                operator=cond["op"],
                value=cond["value"],
                is_text_search=(cond["op"] == "~"),
                negate=cond.get("negate", False),
            )
            attr_conditions.append(at)
        elif cond.get("type") == "having":
            # having (object HAVING inR/outR) leaves are resolved into a hard PK-set
            # intersection (D-05), never a negatable filter clause — true negation
            # would require materializing "all objects minus the resolved set",
            # which is not implemented. Reject NOT explicitly instead of silently
            # ignoring it, mirroring the objectType NOT-rejection precedent above.
            if cond.get("negate"):
                raise AQLValidationError(
                    "NOT is not supported on object HAVING inR/outR(...) conditions"
                )
            hc = HavingCondition(
                direction=cond["direction"],
                reftype_names=cond["reftype_names"],
                nested_tree=cond["nested_tree"],
            )
            having_conditions.append(hc)

    def _collect(node: dict) -> None:
        # A bare single-condition query (e.g. `objectType = "Server"`, no AND/OR)
        # never goes through _flatten_tree's group-wrapping branch — `flat` IS the
        # leaf condition dict itself, not a {"conditions": [...], "groups": [...]}
        # wrapper. Handle the leaf case first.
        if node.get("type") in ("attribute", "objecttype", "having"):
            _collect_leaf(node)
            return
        # objectType and having (object HAVING inR/outR) leaves under an OR node
        # are handled by compiler.py's recursive OR/AND branch-walker (D-07):
        # the walker intercepts OR nodes BEFORE they ever reach _collect_conditions,
        # isolating each OR branch into its own independent CompileOutput (via
        # _transform_expression) and resolving/materializing each one separately,
        # unioning the resulting PK sets at the OR boundary. By the time _collect()
        # walks a node here, any OR structure has already been peeled off one
        # layer up — this function only ever sees already-isolated AND-branches,
        # so no rejection is needed here.
        for cond in node.get("conditions", []):
            _collect_leaf(cond)
        for group in node.get("groups", []):
            _collect(group)

    _collect(flat)

    # AQL-09: objectType IN (...) / objectTypeAndChildren(...) both select
    # multiple types with no single attribute schema in common, so neither can
    # be combined with attribute or inR/outR conditions the way a single
    # objectType = "X" can (D-01/D-03 both resolve attribute/refType names
    # against exactly one ObjectType).
    if (object_type_names is not None or object_type_and_children is not None) and (
        attr_conditions or having_conditions
    ):
        raise AQLValidationError(
            "objectType IN (...) / objectTypeAndChildren(...) cannot be combined "
            'with attribute or inR/outR conditions — use a single objectType = "Name" '
            "filter to add attribute/reference conditions."
        )

    return object_type_name, object_type_names, object_type_and_children, attr_conditions, having_conditions


def _ensure_filter_tree_shape(flat: dict) -> dict:
    """Normalize a bare single-leaf node into a proper filter_tree wrapper.

    A query with no AND/OR combination at all (e.g. `"CPU" > 4` alone, or a
    nested inR/outR condition captured as a single bare leaf) never goes
    through _flatten_tree's group-wrapping branch — `flat` IS the leaf dict
    itself (`{"type": "attribute"/"objecttype"/"having", ...}`), not a
    `{"op": "AND"/"OR", "conditions": [...], "groups": [...]}` wrapper.

    Only call this ONCE, at the top level (transform()/_transform_expression()) —
    NOT inside _flatten_tree's own recursive loop, where a wrapped leaf would be
    misclassified as a nested AND/OR group instead of a condition.

    Without this, the leaf's own "op" (e.g. "=" for an attribute/objecttype
    condition) would silently overwrite the intended AND/OR combinator in
    consumers reading filter_tree["op"], and the leaf itself would be missing
    from filter_tree["conditions"] entirely (previously harmless only because
    every existing top-level query AND-combines objectType with at least one
    other leaf; a bare inR/outR nested condition with a single attribute leaf
    and no explicit objectType — Plan 03 — exposed this).
    """
    if flat.get("type") in ("attribute", "objecttype", "having"):
        return {"op": "AND", "conditions": [flat], "groups": []}
    return flat


def transform(tree: Tree) -> CompileOutput:
    """Convert a parsed Lark Tree into CompileOutput with unresolved names."""
    negate_stack = [False]
    order_by, order_direction = _extract_order_by(tree)
    intermediate = _tree_to_dict(tree, negate_stack)
    flat = _flatten_tree(intermediate)

    object_type_name, object_type_names, object_type_and_children, attr_conditions, having_conditions = (
        _collect_conditions(flat)
    )

    # `flat` is already filter_tree-shaped (op/groups/conditions); objecttype-typed
    # entries are stripped downstream by AQLResolver._resolve_filter_tree.
    resolved_ft = _ensure_filter_tree_shape(flat)

    object_type = None
    if object_type_name:
        object_type = ObjectTypeCondition(name=object_type_name)
    elif object_type_names:
        object_type = ObjectTypeCondition(names=object_type_names)
    elif object_type_and_children:
        object_type = ObjectTypeCondition(and_children_name=object_type_and_children)

    return CompileOutput(
        filter_tree=resolved_ft,
        object_type=object_type,
        order_by=order_by,
        order_direction=order_direction,
        conditions=attr_conditions,
        having_conditions=having_conditions,
    )


def _transform_expression(nested_tree: Tree) -> CompileOutput:
    """Run a nested `expression` Lark Tree back through the same transform
    machinery as transform(), minus the top-level `query`/order_by unwrap.

    Used by the resolver (Plan 03) to recursively resolve the condition
    argument captured inside an `object HAVING inR/outR(...)` leaf — the
    nested_tree on a HavingCondition is exactly this `expression` subtree,
    still part of the single parsed tree (and therefore still bounded by the
    AQL-07 max-depth limit checked once at parse() time).
    """
    negate_stack = [False]
    intermediate = _tree_to_dict(nested_tree, negate_stack)
    flat = _flatten_tree(intermediate)

    object_type_name, object_type_names, object_type_and_children, attr_conditions, having_conditions = (
        _collect_conditions(flat)
    )

    object_type = None
    if object_type_name:
        object_type = ObjectTypeCondition(name=object_type_name)
    elif object_type_names:
        object_type = ObjectTypeCondition(names=object_type_names)
    elif object_type_and_children:
        object_type = ObjectTypeCondition(and_children_name=object_type_and_children)

    return CompileOutput(
        filter_tree=_ensure_filter_tree_shape(flat),
        object_type=object_type,
        order_by=None,
        order_direction=None,
        conditions=attr_conditions,
        having_conditions=having_conditions,
    )


class AQLTransformer(Transformer):
    """Lark Transformer that converts a parse Tree into CompileOutput.

    Usage:
        tree = parse(aql_string)
        output = AQLTransformer().transform(tree)
    """

    def transform(self, tree: Tree) -> CompileOutput:
        return transform(tree)
