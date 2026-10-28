"""Canonical AttributeType data_type -> ObjectValue typed-column mapping (D-08/D-09).

Before this module existed, the type-to-column mapping was duplicated across
the shared consumers (import_execution_service._typed_column_for_datatype,
api/object_values.py's local _get_value_column, filter_utils._get_value_column)
— each hand-written, each capable of silently drifting from the others (the
STATUS drift this module's introduction fixes: filter_utils routed STATUS to
value_simple while every other consumer already used value_struct). This module
is now the ONE place the mapping is defined; every caller delegates to it.

Two deliberate, tested exceptions — NOT bugs, do not "fix" by adding them
back to the map:

1. REFERENCE has no ObjectValue column at all — a reference attribute's value
   lives in the ObjectRef table (a link between two Object rows), not as a
   scalar ObjectValue column. Both `typed_column_for_data_type("reference")`
   and `typed_value_column("reference")` raise ValueError rather than
   silently falling back to value_simple. Production callers already branch
   away from "reference" before reaching this helper (filter_utils.
   build_filter_clause checks `dtype == "reference"` first;
   import_execution_service handles references via a separate code path,
   _write_object_refs), so this raise is unreachable in normal operation —
   it exists to guard the contract 245-04's matrix test asserts, and to make
   an unknown/typo'd data_type string distinguishable from "reference"
   (without the raise, both would silently resolve to value_simple).

2. The CSV import wire format (import_execution_service.py) writes STATUS and
   JSON values into value_simple, not value_struct/value_struct — the
   CsvImportConnector's `_coerce_value` and `_extract_typed_value` read that
   same wire column for identity-match/extraction. import_execution_service's
   own `_typed_column_for_datatype` keeps an explicit carve-out for status/
   json instead of delegating those two data types to this module.
"""

from app.models.attribute_type import AttributeTypeDataType

TYPED_COLUMN_MAP: dict[str, str] = {
    AttributeTypeDataType.INTEGER.value: "value_integer",
    AttributeTypeDataType.DOUBLE.value: "value_float",
    AttributeTypeDataType.BOOLEAN.value: "value_boolean",
    AttributeTypeDataType.DATETIME.value: "value_datetime",
    AttributeTypeDataType.JSON.value: "value_struct",
    AttributeTypeDataType.STATUS.value: "value_struct",
    AttributeTypeDataType.USER.value: "value_simple",
    AttributeTypeDataType.GROUP.value: "value_simple",
    AttributeTypeDataType.TEXT.value: "value_simple",
    AttributeTypeDataType.VERSION.value: "value_simple",
    AttributeTypeDataType.JIRA.value: "value_simple",
    AttributeTypeDataType.CONFLUENCE.value: "value_simple",
    AttributeTypeDataType.URL.value: "value_simple",
    AttributeTypeDataType.EMAIL.value: "value_simple",
    AttributeTypeDataType.SELECT.value: "value_simple",
    AttributeTypeDataType.TEXTAREA.value: "value_simple",
    # REFERENCE deliberately absent — see the module docstring and
    # typed_column_for_data_type()'s explicit raise below.
}


def typed_column_for_data_type(data_type: str) -> str:
    """Return the ObjectValue column NAME for a data_type string.

    Unknown/typo'd strings fall back to "value_simple" (matches every
    caller's pre-existing fallback behavior) — EXCEPT "reference", which
    raises ValueError (see module docstring exception 1).
    """
    if data_type == AttributeTypeDataType.REFERENCE.value:
        raise ValueError("REFERENCE has no ObjectValue column — use ObjectRef")
    return TYPED_COLUMN_MAP.get(data_type, "value_simple")


def typed_value_column(data_type: str):
    """Return the ObjectValue column OBJECT (for use in a SQLAlchemy query)
    for a data_type string. Same "reference" raise as
    typed_column_for_data_type (see module docstring exception 1)."""
    from app.models.object_value import ObjectValue

    return getattr(ObjectValue, typed_column_for_data_type(data_type))
