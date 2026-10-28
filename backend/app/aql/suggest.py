"""AQL autocomplete suggestion core.

Given a partial AQL query string and a cursor position, computes the set of
syntactically valid next-token suggestions via Lark's interactive parser
(`parser.parse_interactive()` + `.accepts()`) against the real
`grammar.lark` — never a hand-rolled re-implementation of the grammar
(D-193-01).

This module is a parallel, read-only path. It MUST NEVER import or call the
AQL compiler's query-execution entrypoint (`app.aql.compiler`) — that
pipeline is for actually running a complete, valid AQL query, not for
advisory suggestions.

Per D-03, a structurally-invalid pre-cursor prefix returns an empty
suggestion list — never an exception, never a structured parse-error object.
This module raises nothing; every code path returns a list (possibly empty).

Plan 02 layers DB-backed semantic resolution on top of Plan 01's syntactic
core: objectType-scope detection (nearest enclosing unclosed, respecting
paren depth — 193-RESEARCH.md Open Question 3), ObjectType-scoped attribute
fields including inherited ones (AC-02), field-type-aware operators (AC-03),
objectType value suggestions (AC-04), and exact-casing/quote-exclusion
filtering (AC-05).
"""
import re
from typing import Optional

from lark.exceptions import LarkError, UnexpectedCharacters, UnexpectedToken
from sqlalchemy import select

from app.aql.parser import _MAX_QUERY_LENGTH, _parser
from app.models import AttributeType
from app.services.object_type_attribute_service import (
    list_object_type_attributes,
    list_visible_attribute_name_data_types,
)
from app.services.object_type_service import list_object_types

# Terminal -> (kind, insertText) for fixed syntax-keyword suggestions.
# Pattern 2, 193-RESEARCH.md.
_SYNTAX_KEYWORDS: dict[str, tuple[str, str]] = {
    "OBJECTTYPE_KW": ("syntax-keyword", "objectType"),
    "OBJECTTYPE_AND_CHILDREN_KW": ("syntax-keyword", "objectTypeAndChildren("),
    "OBJECT": ("syntax-keyword", "object HAVING"),
    "NOT_OP": ("syntax-keyword", "NOT"),
    "LPAR": ("syntax-keyword", "("),
}

# AND/OR/IN are boolean-keyword suggestions with a fixed insertText matching
# the terminal's own literal spelling.
_BOOLEAN_KEYWORDS: dict[str, tuple[str, str]] = {
    "AND": ("boolean-keyword", "AND"),
    "OR": ("boolean-keyword", "OR"),
    "IN": ("boolean-keyword", "IN"),
}

# Baseline (unfiltered) CMP_OP set — used only as a fallback when the
# in-scope attribute's data_type cannot be resolved (e.g. no objectType
# scope, or the field is one of the fixed generic fields). Sourced from
# grammar.lark's CMP_OP terminal alternation.
_OPERATORS = ["=", "!=", ">", "<", ">=", "<=", "~"]

# Verified field-type -> AQL operator table (AC-03), copied verbatim from
# 193-RESEARCH.md Pattern 3 (sourced from filter_utils.py's actual per-dtype
# accepted-ops logic, cross-referenced with resolver.py's operator mapping).
# Do not re-derive from filter_utils.py — this table already encodes that.
_DTYPE_OPERATORS: dict[str, list[str]] = {
    "integer": ["=", "!=", ">", "<", ">=", "<="],
    "double": ["=", "!=", ">", "<", ">=", "<="],
    "boolean": ["=", "!="],
    "select": ["=", "!="],
    "text": ["=", "!=", "~"],
    "url": ["=", "!=", "~"],
    "email": ["=", "!=", "~"],
    "status": ["=", "!=", "~"],
    "version": ["=", "!=", "~"],
    "group": ["=", "!=", "~"],
    "jira": ["=", "!=", "~"],
    "confluence": ["=", "!=", "~"],
    "reference": ["=", "!=", "~"],
    # datetime: the ordinal CMP_OPs execute (317-04 Task 4) — resolver.py's
    # _map_operator maps >,<,>=,<= to gt/lt/gte/lte and filter_utils.py's
    # _date_filter implements exactly those four, so the suggestion table and
    # the executor agree. before/after/between are filter-layer aliases with
    # no AQL spelling and are deliberately not suggested.
    "datetime": ["=", "!=", ">", "<", ">=", "<="],
    # json is intentionally absent — filter_utils.py raises ValueError for
    # json entirely, so json-typed attributes are excluded from `field`
    # suggestions before they ever reach operator resolution (see
    # _scoped_field_suggestions below).
}

# objectType's verified execution-time-safe operator set (193-RESEARCH.md
# Open Question 1 / Pattern 3): the grammar syntactically permits `!=` and
# `NOT`-negation for objectType, but transformer.py::_collect_conditions
# always raises AQLValidationError for those — only plain `=` survives.
# `IN (...)` is offered separately via the IN boolean-keyword terminal, not
# as a CMP_OP. This is a deliberate, documented divergence from AC-03's
# literal wording ("=/IN/NOT IN") toward the verified-safe set.
_OBJECTTYPE_OPERATORS = ["="]

# REF_FN's fixed 2-item enum, sourced from grammar.lark's REF_FN terminal
# alternation (not a DB lookup).
_REF_FNS = ["inR(", "outR("]

# Fixed generic-field list used before any `objectType = "X"` filter is
# recognized (AC-02 generic half). insertText is quoted per the grammar's
# `attr_condition: STRING CMP_OP value` — field names are STRING tokens.
_GENERIC_FIELDS = ["Name", "objectTypeId"]

# Terminals explicitly excluded from suggestion output this plan (deferred /
# out-of-scope kinds per Pattern 2's table).
_EXCLUDED_TERMINALS = {"$END", "BOOL", "NUMBER", "ORDER", "BY", "NAME", "ORDER_DIRECTION", "REFTYPE"}

# STRING co-accepted with any of these terminals marks a "start of atom"
# field-name position (Pattern 2, 193-RESEARCH.md — verified empirically).
_FIELD_POSITION_MARKERS = {
    "OBJECTTYPE_KW", "OBJECTTYPE_AND_CHILDREN_KW", "OBJECT", "NOT_OP", "LPAR",
}

# "nearest enclosing unclosed objectType, respecting paren depth" scan
# (193-RESEARCH.md Open Question 3 / Pitfall 4).
_OBJECTTYPE_SCOPE_RE = re.compile(r'objectType\s*=\s*"([^"]*)"', re.IGNORECASE)


def _field_value(item, name: str):
    """`list_object_types`/`list_object_type_attributes` may return either
    ORM instances (cache miss) or plain dicts (cache hit / CTE rows) —
    normalize attribute access across both shapes."""
    if isinstance(item, dict):
        return item.get(name)
    return getattr(item, name, None)


def _has_quote(value: Optional[str]) -> bool:
    return bool(value) and '"' in value


def _find_enclosing_object_type_name(text: str) -> Optional[str]:
    """Scan the pre-cursor text for the nearest enclosing, still-unclosed
    `objectType = "X"` occurrence, respecting parenthesis nesting depth.

    Walks forward char-by-char, skipping over quoted string literals (so a
    `"` inside an unrelated STRING value never confuses depth tracking),
    pushing a new scope slot on `(` and popping on `)`. Each recognized
    `objectType = "X"` overwrites the innermost currently-open slot. At the
    end, the nearest (innermost) non-empty slot wins — this matches "nearest
    enclosing unclosed objectType" for nested `object HAVING inR(...)`
    blocks (193-RESEARCH.md Open Question 3).
    """
    stack: list[Optional[str]] = [None]
    i = 0
    n = len(text)
    while i < n:
        match = _OBJECTTYPE_SCOPE_RE.match(text, i)
        if match:
            stack[-1] = match.group(1)
            i = match.end()
            continue
        ch = text[i]
        if ch == '"':
            j = text.find('"', i + 1)
            i = n if j == -1 else j + 1
            continue
        if ch == "(":
            stack.append(None)
            i += 1
            continue
        if ch == ")":
            if len(stack) > 1:
                stack.pop()
            i += 1
            continue
        i += 1
    for name in reversed(stack):
        if name is not None:
            return name
    return None


async def _resolve_scope_object_type_id(
    db, text: str, user_id: Optional[str], is_superuser: bool
) -> Optional[str]:
    """Resolve the nearest-enclosing `objectType = "X"` name (if any) to an
    ObjectType id via the SAME permission-filtered service AC-04 uses — an
    exact-name match among the ilike-substring results. Returns None if no
    scope is recognized, or if the name doesn't resolve (e.g. permission-
    filtered out) — callers fall back to the generic field list."""
    name = _find_enclosing_object_type_name(text)
    if name is None:
        return None
    items, _total = await list_object_types(
        db, name=name, user_id=user_id, is_superuser=is_superuser
    )
    for item in items:
        if _field_value(item, "name") == name:
            return _field_value(item, "id")
    return None


async def _lookup_attribute_data_type(db, attribute_type_id) -> Optional[str]:
    """AttributeType.data_type is not eager-loaded by
    list_object_type_attributes's CTE — resolve it separately per
    attribute_type_id."""
    result = await db.execute(
        select(AttributeType.data_type).where(AttributeType.id == attribute_type_id)
    )
    row = result.first()
    if row is None:
        return None
    value = row[0]
    return value.value if hasattr(value, "value") else value


async def _scoped_field_suggestions(db, object_type_id, user_id, is_superuser) -> list[dict]:
    """ObjectType-scoped attribute field suggestions (incl. inherited),
    calling the SAME service function so Phase 194's PERM-01 permission fix
    propagates automatically (T-193-07). json-typed attributes are excluded
    entirely (AC-03); names containing `"` are excluded (AC-05)."""
    items, _total = await list_object_type_attributes(
        db, object_type_id=object_type_id, limit=500,
        user_id=user_id, is_superuser=is_superuser,
    )
    suggestions: list[dict] = []
    dtype_cache: dict = {}
    for attr in items:
        name = _field_value(attr, "name")
        if not name or _has_quote(name):
            continue
        attribute_type_id = _field_value(attr, "attribute_type_id")
        data_type = None
        if attribute_type_id is not None:
            if attribute_type_id not in dtype_cache:
                dtype_cache[attribute_type_id] = await _lookup_attribute_data_type(db, attribute_type_id)
            data_type = dtype_cache[attribute_type_id]
        if data_type == "json":
            continue
        suggestions.append({"kind": "field", "value": name, "insertText": f'"{name}"'})
    return suggestions


async def _global_attribute_name_dtypes(db, user_id, is_superuser) -> dict[str, set]:
    rows = await list_visible_attribute_name_data_types(
        db,
        user_id=user_id,
        is_superuser=is_superuser,
    )
    name_dtypes: dict[str, set] = {}
    for row in rows:
        name = row.name
        if not name:
            continue
        data_type = row.data_type
        data_type = data_type.value if hasattr(data_type, "value") else data_type
        name_dtypes.setdefault(name, set()).add(data_type)
    return name_dtypes


async def _global_field_suggestions(db, user_id, is_superuser) -> list[dict]:
    name_dtypes = await _global_attribute_name_dtypes(db, user_id, is_superuser)
    suggestions: list[dict] = []
    for name, data_types in name_dtypes.items():
        if _has_quote(name):
            continue
        if data_types and all(data_type == "json" for data_type in data_types):
            continue
        suggestions.append({"kind": "field", "value": name, "insertText": f'"{name}"'})
    return suggestions


async def _resolve_global_attribute_operators(
    db, attribute_name: str, user_id, is_superuser
) -> Optional[list[str]]:
    """Union-of-operators lookup for an unscoped attribute name (D-198-03).

    Reuses `_global_attribute_name_dtypes` (the same permission-filtered
    shared query the no-scope field-name branch uses, 198-01) to enumerate
    every ObjectType owning `attribute_name` and collect each one's
    dtype-specific operator set from `_DTYPE_OPERATORS`. Permissive by design
    — never hides an operator that's valid for at least one matching
    candidate.

    Returns `None` (NOT `[]`) when the name resolves to zero ObjectTypes so the
    caller's existing `if ops is None: ops = _OPERATORS` baseline fallback
    fires unchanged (D-198-03's explicit safety net — operators must look
    identical to today's behavior for unknown names).
    """
    name_dtypes = await _global_attribute_name_dtypes(db, user_id, is_superuser)
    data_types = name_dtypes.get(attribute_name)
    if not data_types:
        return None
    unioned: set = set()
    for data_type in data_types:
        for op in _DTYPE_OPERATORS.get(data_type, []):
            unioned.add(op)
    return list(unioned)


async def _resolve_attribute_operators(
    db, object_type_id, attribute_name: str, user_id, is_superuser
) -> Optional[list[str]]:
    """Look up the in-scope attribute's data_type and return the
    field-type-aware operator set (AC-03), or None if the attribute can't be
    resolved (caller falls back to the baseline unfiltered set)."""
    if object_type_id is None:
        return None
    items, _total = await list_object_type_attributes(
        db, object_type_id=object_type_id, limit=500,
        user_id=user_id, is_superuser=is_superuser,
    )
    match = next((a for a in items if _field_value(a, "name") == attribute_name), None)
    if match is None:
        return None
    attribute_type_id = _field_value(match, "attribute_type_id")
    if attribute_type_id is None:
        return None
    data_type = await _lookup_attribute_data_type(db, attribute_type_id)
    return _DTYPE_OPERATORS.get(data_type)


async def _objecttype_value_suggestions(
    db, name_prefix: str, user_id: Optional[str], is_superuser: bool
) -> list[dict]:
    """objectType `=`/`IN (`/`objectTypeAndChildren(` value suggestions
    (AC-04) — sourced SOLELY from list_object_types, the SAME service
    GET /object-types/?name= uses (permission-filtered, cached). No parallel
    data path."""
    items, _total = await list_object_types(
        db, name=name_prefix, user_id=user_id, is_superuser=is_superuser
    )
    suggestions: list[dict] = []
    for item in items:
        name = _field_value(item, "name")
        if not name or _has_quote(name):
            continue
        suggestions.append({"kind": "objectType-value", "value": name, "insertText": f'"{name}"'})
    return suggestions


def _lex_feed(text: str) -> tuple[str, str, set[str], list]:
    """Manual lex-and-feed loop against the shared `_parser` singleton.

    Reproduces 193-RESEARCH.md Pattern 1 verbatim. Returns
    (status, prefix, accepted_terminals, tokens_fed) where status is one of
    "partial" (cursor mid-token), "invalid" (structurally-invalid prefix,
    D-03), or "boundary" (cursor sits exactly on a token boundary).

    tokens_fed is the list of tokens successfully consumed before the loop
    stopped — used for objectType-scope detection and CMP_OP context
    (is the cursor right after OBJECTTYPE_KW, or after an attribute-name
    STRING?).
    """
    interactive = _parser.parse_interactive(text, "query")
    lex_stream = interactive.lexer_thread.lex(interactive.parser_state)
    tokens_fed: list = []
    try:
        while True:
            try:
                token = next(lex_stream)
            except StopIteration:
                break
            interactive.feed_token(token)
            tokens_fed.append(token)
    except UnexpectedCharacters as e:
        prefix = text[e.pos_in_stream:]
        return "partial", prefix, set(interactive.accepts()), tokens_fed
    except UnexpectedToken:
        return "invalid", None, set(), tokens_fed
    return "boundary", "", set(interactive.accepts()), tokens_fed


def _string_position_kind(raw_accepted: set[str]) -> Optional[str]:
    """Disambiguate what a co-accepted STRING terminal means at this
    position (Pattern 2, 193-RESEARCH.md — verified empirically):
    - "field": STRING co-accepted with a start-of-atom keyword marker
      (OBJECTTYPE_KW/OBJECTTYPE_AND_CHILDREN_KW/OBJECT/NOT_OP/LPAR).
    - "objectType-value": STRING is the ONLY accepted terminal (fires after
      OBJECTTYPE_KW+CMP_OP, and inside `IN (`/`objectTypeAndChildren(`).
    - "attr-value": STRING co-accepted with BOOL/NUMBER only (attribute
      VALUE position, e.g. `"Name" = `) — AC-12/AC-14, deferred, not built.
    Must be computed from the RAW (pre-exclusion) accepted set — BOOL/NUMBER
    are stripped by `_EXCLUDED_TERMINALS` before classification, which would
    otherwise make "attr-value" indistinguishable from "objectType-value".
    """
    if "STRING" not in raw_accepted:
        return None
    if raw_accepted & _FIELD_POSITION_MARKERS:
        return "field"
    if raw_accepted - {"STRING"}:
        return "attr-value"
    return "objectType-value"


def _classify(accepted: set[str], include_generic_fields: bool) -> list[dict]:
    """Map a raw (already-exclusion-filtered) Lark terminal-name set to
    classified suggestion dicts. Hand-authored terminal -> kind table
    (Pattern 2, 193-RESEARCH.md). `include_generic_fields` is False when an
    ObjectType scope is in effect — the caller injects
    `_scoped_field_suggestions` instead (AC-02)."""
    suggestions: list[dict] = []

    for terminal, (kind, insert_text) in _SYNTAX_KEYWORDS.items():
        if terminal in accepted:
            suggestions.append({"kind": kind, "value": insert_text, "insertText": insert_text})

    for terminal, (kind, insert_text) in _BOOLEAN_KEYWORDS.items():
        if terminal in accepted:
            suggestions.append({"kind": kind, "value": insert_text, "insertText": insert_text})

    if include_generic_fields and "STRING" in accepted:
        for field in _GENERIC_FIELDS:
            insert_text = f'"{field}"'
            suggestions.append({"kind": "field", "value": field, "insertText": insert_text})

    if "REF_FN" in accepted:
        for fn in _REF_FNS:
            suggestions.append({"kind": "ref-fn", "value": fn, "insertText": fn})

    return suggestions


async def get_suggestions(
    db,
    ql_query: str,
    cursor_pos: int,
    user_id: Optional[str],
    is_superuser: bool,
) -> list[dict]:
    """Compute classified autocomplete suggestions for a partial AQL query.

    Pure, return-based — never raises. Any Lark exception, out-of-range
    cursor_pos, or overlong pre-cursor text collapses to an empty list per
    D-03/T-193-01/T-193-03.

    Plan 02 additions on top of Plan 01's syntactic core: nearest-enclosing
    objectType scope detection (AC-02), ObjectType-scoped attribute fields
    incl. inherited (AC-02), field-type-aware operators incl. the objectType
    `=`-only safe set and the datetime ordinal exclusion (AC-03), objectType
    value suggestions via `list_object_types` (AC-04), and exact-casing /
    quote-exclusion filtering applied across all name-bearing suggestions
    (AC-05).
    """
    if cursor_pos < 0 or cursor_pos > len(ql_query):
        return []

    text = ql_query[:cursor_pos]

    if len(text) > _MAX_QUERY_LENGTH:
        return []

    try:
        status, prefix, raw_accepted, tokens_fed = _lex_feed(text)
    except LarkError:
        return []

    if status == "invalid":
        return []

    string_kind = _string_position_kind(raw_accepted)

    accepted = raw_accepted - _EXCLUDED_TERMINALS

    object_type_id: Optional[str] = None
    if string_kind == "field":
        object_type_id = await _resolve_scope_object_type_id(db, text, user_id, is_superuser)

    # Generic fields only make sense at a field-name position without an
    # ObjectType scope (AC-02 generic half) — never at the objectType-value /
    # attr-value position, where "STRING" is co-accepted but the user is
    # typing a VALUE, not a field name.
    suggestions = _classify(
        accepted,
        include_generic_fields=(string_kind == "field" and object_type_id is None),
    )

    if string_kind == "field" and object_type_id is not None:
        suggestions.extend(await _scoped_field_suggestions(db, object_type_id, user_id, is_superuser))

    if string_kind == "field" and object_type_id is None:
        suggestions.extend(await _global_field_suggestions(db, user_id, is_superuser))

    if string_kind == "objectType-value":
        raw_prefix = prefix if status == "partial" else ""
        name_prefix = raw_prefix[1:] if raw_prefix.startswith('"') else raw_prefix
        suggestions.extend(
            await _objecttype_value_suggestions(db, name_prefix, user_id, is_superuser)
        )

    if "CMP_OP" in accepted:
        last_token = tokens_fed[-1] if tokens_fed else None
        if last_token is not None and last_token.type == "OBJECTTYPE_KW":
            ops = _OBJECTTYPE_OPERATORS
        elif last_token is not None and last_token.type == "STRING":
            attribute_name = str(last_token)[1:-1] if len(str(last_token)) >= 2 else ""
            scope_object_type_id = await _resolve_scope_object_type_id(db, text, user_id, is_superuser)
            if scope_object_type_id is None:
                ops = await _resolve_global_attribute_operators(db, attribute_name, user_id, is_superuser)
            else:
                ops = await _resolve_attribute_operators(db, scope_object_type_id, attribute_name, user_id, is_superuser)
            if ops is None:
                ops = _OPERATORS
        else:
            ops = _OPERATORS
        for op in ops:
            suggestions.append({"kind": "operator", "value": op, "insertText": op})

    # AC-05: drop any name-bearing suggestion whose value contains a literal
    # `"` (unrepresentable in the STRING terminal); never transform casing —
    # applied uniformly across all suggestion kinds.
    suggestions = [s for s in suggestions if not _has_quote(s.get("value"))]

    return suggestions
