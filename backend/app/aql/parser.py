import pathlib

from lark import Lark, Tree, Token
from lark.exceptions import LarkError, UnexpectedCharacters, UnexpectedToken

from app.aql.errors import AQLParseError, AQLValidationError

_MAX_QUERY_LENGTH = 2048
# Empirically measured (190-01, Pitfall 4/D-02): a 2-hop
# `object HAVING inR(object HAVING inR(objectType = "X")))` chain consumes
# exactly depth 10 (right at the limit, still parses since _check_depth only
# rejects depth > max_depth); a 3-hop chain measures depth 13 and is rejected.
# Left at 10 for this plan — 2-hop headroom is sufficient for REF-01/REF-02;
# raising the limit for deeper chains is deferred to a later plan if needed.
_MAX_DEPTH = 10
_MAX_CONDITIONS = 50

_grammar_path = pathlib.Path(__file__).parent / "grammar.lark"
_lark_grammar = _grammar_path.read_text()

_parser = Lark(
    _lark_grammar,
    parser="lalr",
    start="query",
    propagate_positions=True,
    maybe_placeholders=False,
)


def _check_depth(tree: Tree, max_depth: int = _MAX_DEPTH) -> None:
    """Recursively check that tree depth does not exceed max_depth."""

    def _walk(node: Tree | Token, depth: int) -> None:
        if depth > max_depth:
            raise AQLValidationError(
                f"Query exceeds maximum nesting depth of {max_depth}"
            )
        if isinstance(node, Tree):
            for child in node.children:
                _walk(child, depth + 1)

    _walk(tree, 0)


def _count_conditions(tree: Tree, max_conditions: int = _MAX_CONDITIONS) -> int:
    """Count total leaf conditions in the tree. Raises if exceeds limit."""
    count = [0]

    def _walk(node: Tree | Token) -> None:
        if isinstance(node, Tree):
            if node.data in ("attr_condition", "objecttype_condition", "having_condition"):
                count[0] += 1
                if count[0] > max_conditions:
                    raise AQLValidationError(
                        f"Query exceeds maximum of {max_conditions} conditions"
                    )
            for child in node.children:
                _walk(child)

    _walk(tree)
    return count[0]


def parse(aql_string: str) -> Tree:
    """Parse an AQL query string into a Lark Tree.

    Raises:
        AQLValidationError: If the query violates safety limits
        AQLParseError: If the query has syntax errors, with line/column info
    """
    if len(aql_string) > _MAX_QUERY_LENGTH:
        raise AQLValidationError(
            f"Query exceeds maximum length of {_MAX_QUERY_LENGTH} characters "
            f"(got {len(aql_string)})"
        )

    try:
        tree = _parser.parse(aql_string)
    except UnexpectedCharacters as e:
        raise AQLParseError(
            f"Unexpected character '{e.char}' at position {e.pos_in_stream}",
            line=e.line,
            column=e.column,
        ) from e
    except UnexpectedToken as e:
        raise AQLParseError(
            f"Unexpected token '{e.token}' at position {e.pos_in_stream}",
            line=e.line,
            column=e.column,
        ) from e
    except LarkError as e:
        raise AQLParseError(str(e)) from e

    _check_depth(tree)
    _count_conditions(tree)

    return tree
