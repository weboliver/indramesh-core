"""CSV export/import connectors — formats Objects to/from CSV using field mappings.

Pure wire-protocol connectors — zero DB access (mirrors the JiraConnector pattern).
All data is resolved and bundled by the caller and passed in via plain dicts/lists.
This keeps the connectors free of async/ORM concerns and avoids N+1 queries across
potentially thousands of objects.
"""

import csv
import io
import logging
import uuid
from datetime import datetime

from app.models.attribute_type import AttributeTypeDataType
from app.models.object import Object
from app.models.object_value import ObjectValue
from app.models.object_ref import ObjectRef
from app.models.object_type_attribute import ObjectTypeAttribute
from app.services.select_option_service import resolve_display_value

logger = logging.getLogger(__name__)


_FORMULA_INJECTION_PREFIXES = ("=", "+", "-", "@", "\t", "\r")

_DELIMITER_CANDIDATES = (",", ";", "\t", "|")


def detect_delimiter(csv_content: str) -> str:
    """Pick the field delimiter from the header line: the candidate that occurs
    most often outside quotes, "," when none occurs (single-column file)."""
    header = csv_content.lstrip("﻿").splitlines()[0] if csv_content.strip() else ""
    counts = dict.fromkeys(_DELIMITER_CANDIDATES, 0)
    in_quotes = False
    for char in header:
        if char == '"':
            in_quotes = not in_quotes
        elif not in_quotes and char in counts:
            counts[char] += 1
    best = max(_DELIMITER_CANDIDATES, key=lambda c: counts[c])
    return best if counts[best] > 0 else ","


def _sanitize_csv_cell(value: str) -> str:
    """Neutralize CSV/formula injection (CWE-1236) by prefixing a leading
    apostrophe on cell values that start with a character spreadsheet software
    (Excel/Google Sheets) treats as a formula trigger.

    WR-10: user-controlled text (Object.name, free-text attribute values) is
    written as-is into export CSVs; without this, opening the export in a
    spreadsheet app can execute attacker-controlled formulas.
    """
    if value and value[0] in _FORMULA_INJECTION_PREFIXES:
        return "'" + value
    return value


class CsvExportResult:
    """Result of a CSV export operation."""
    csv_content: str
    row_count: int
    columns: list[str]
    columns_skipped: list[dict]  # [{attr_id, reason}, ...]
    file_path: str | None  # Resolved full path after writing

    def __init__(self, csv_content: str = "", row_count: int = 0,
                 columns: list[str] = None, columns_skipped: list[dict] = None,
                 file_path: str | None = None):
        self.csv_content = csv_content
        self.row_count = row_count
        self.columns = columns or []
        self.columns_skipped = columns_skipped or []
        self.file_path = file_path


class CsvExportConnector:
    """Formats Objects to CSV using an ExportDefinition's field mappings.

    Pure wire-protocol connector — zero DB access. Receives all data via
    constructor/invoke arguments. Follows JiraConnector pattern.
    """

    def __init__(self, delimiter: str = ",", encoding: str = "utf-8"):
        self.delimiter = delimiter
        self.encoding = encoding

    def build_csv(
        self,
        objects: list[Object],
        values_by_object: dict[uuid.UUID, list[ObjectValue]],
        refs_by_object: dict[uuid.UUID, list[ObjectRef]],
        field_mappings: list[dict],
        attr_cache: dict[uuid.UUID, ObjectTypeAttribute],
        type_cache: dict[uuid.UUID, str],  # attr_id -> data_type string (pre-extracted async, avoids MissingGreenletError)
        status_cache: dict[uuid.UUID, str],  # status_uuid -> status_name
        obj_name_cache: dict[uuid.UUID, str],  # object_id -> object.name
        select_cache: dict[uuid.UUID, dict[str, str]] | None = None,  # attr_id -> {option id -> base value} (D-12, base-language only)
        user_cache: dict[str, str] | None = None,  # str(user uuid) -> username (D-15)
        group_cache: dict[str, str] | None = None,  # str(group uuid) -> group name (D-15)
    ) -> CsvExportResult:
        """Build CSV content from resolved data — no I/O.

        Field mapping entry: {attr_id, csv_column, column_order}
        attr_cache: ObjectTypeAttribute.id -> ObjectTypeAttribute (metadata like name, cardinality)
        type_cache: attr_id -> data_type string (pre-extracted in async context to avoid MissingGreenletError)

        Returns CsvExportResult with csv_content string, row_count, columns list.
        """
        output = io.StringIO()
        writer = csv.writer(output, delimiter=self.delimiter)

        # Sort mappings by column_order
        sorted_mappings = sorted(field_mappings, key=lambda m: m["column_order"])

        # Build header row, tracking which mappings resolve
        headers: list[str] = []
        valid_mappings: list[dict] = []
        skipped: list[dict] = []
        for mapping in sorted_mappings:
            attr_id = uuid.UUID(mapping["attr_id"]) if isinstance(mapping["attr_id"], str) else mapping["attr_id"]
            attr = attr_cache.get(attr_id)
            if not attr:
                skipped.append({"attr_id": str(attr_id), "reason": "Attribute not found or deleted"})
                logger.warning("skipping attribute %s: not found in ObjectTypeAttribute", attr_id)
                continue
            valid_mappings.append(mapping)
            data_type = type_cache.get(attr_id)
            if data_type == AttributeTypeDataType.REFERENCE.value:
                headers.append(f'{mapping["csv_column"]}_name')
                headers.append(f'{mapping["csv_column"]}_uuid')
            else:
                headers.append(mapping["csv_column"])
        if not valid_mappings:
            output.close()
            return CsvExportResult(csv_content="", row_count=0, columns=[], columns_skipped=skipped)

        writer.writerow(headers)

        # Build rows
        for obj in objects:
            row = self._build_row(
                obj, values_by_object, refs_by_object,
                valid_mappings, attr_cache, type_cache, status_cache,
                select_cache, obj_name_cache, user_cache, group_cache,
            )
            writer.writerow(row)

        csv_string = output.getvalue()
        output.close()

        return CsvExportResult(
            csv_content=csv_string,
            row_count=len(objects),
            columns=headers,
            columns_skipped=skipped,
        )

    def _build_row(
        self,
        obj: Object,
        values_by_object: dict[uuid.UUID, list[ObjectValue]],
        refs_by_object: dict[uuid.UUID, list[ObjectRef]],
        valid_mappings: list[dict],
        attr_cache: dict[uuid.UUID, ObjectTypeAttribute],
        type_cache: dict[uuid.UUID, str],
        status_cache: dict[uuid.UUID, str],
        select_cache: dict[uuid.UUID, dict[str, str]] | None,
        obj_name_cache: dict[uuid.UUID, str],
        user_cache: dict[str, str] | None = None,
        group_cache: dict[str, str] | None = None,
    ) -> list[str]:
        sorted_mappings = sorted(valid_mappings, key=lambda m: m["column_order"])
        row: list[str] = []

        # Group values and refs by attribute
        values_by_attr: dict[uuid.UUID, list[ObjectValue]] = {}
        for v in values_by_object.get(obj.id, []):
            aid = v.object_type_attribute_id
            values_by_attr.setdefault(aid, []).append(v)

        refs_by_attr: dict[str, list[ObjectRef]] = {}
        for r in refs_by_object.get(obj.id, []):
            refs_by_attr.setdefault(r.from_attribute_name, []).append(r)

        for mapping in sorted_mappings:
            attr_id = uuid.UUID(mapping["attr_id"]) if isinstance(mapping["attr_id"], str) else mapping["attr_id"]
            attr = attr_cache.get(attr_id)
            if not attr:
                continue

            data_type = type_cache.get(attr_id)
            if data_type == AttributeTypeDataType.REFERENCE.value:
                refs = refs_by_attr.get(attr.name, [])
                names = []
                uuids = []
                for ref in refs:
                    name = obj_name_cache.get(ref.to_object_id, "")
                    names.append(name)
                    uuids.append(str(ref.to_object_id))
                row.append(_sanitize_csv_cell(";".join(names)))
                row.append(";".join(uuids))  # UUIDs are never attacker-influenced free text
            else:
                vals = values_by_attr.get(attr_id, [])
                cell = self._format_value(vals, attr, type_cache, status_cache,
                                          select_cache, attr_id, user_cache, group_cache)
                row.append(_sanitize_csv_cell(cell))

        return row

    def _format_value(
        self,
        values: list[ObjectValue],
        attr: ObjectTypeAttribute,
        type_cache: dict[uuid.UUID, str],
        status_cache: dict[uuid.UUID, str],
        select_cache: dict[uuid.UUID, dict[str, str]] | None,
        attr_id: uuid.UUID,
        user_cache: dict[str, str] | None = None,
        group_cache: dict[str, str] | None = None,
    ) -> str:
        is_multi = self._is_multi_valued(attr)

        if not values:
            return ""

        if not is_multi:
            val = values[0]
            return self._coerce_single(val, type_cache, status_cache, select_cache, attr_id,
                                       user_cache, group_cache)

        parts = []
        for val in values:
            parts.append(self._coerce_single(val, type_cache, status_cache, select_cache, attr_id,
                                              user_cache, group_cache))
        return ";".join(parts)

    def _is_multi_valued(self, attr: ObjectTypeAttribute) -> bool:
        """Detect multi-valued attribute per D-12."""
        mc = attr.maximum_cardinality
        if mc > 1:
            return True
        if mc == -1:
            # -1 means unlimited — definitely multi-valued
            return True
        return False

    def _coerce_single(
        self,
        val: ObjectValue,
        type_cache: dict[uuid.UUID, str],
        status_cache: dict[uuid.UUID, str],
        select_cache: dict[uuid.UUID, dict[str, str]] | None,
        attr_id: uuid.UUID,
        user_cache: dict[str, str] | None = None,
        group_cache: dict[str, str] | None = None,
    ) -> str:
        """Coerce a single ObjectValue to CSV string per D-11.

        Uses type_cache (pre-extracted in async context) instead of accessing
        attr.attribute_type.data_type directly to avoid MissingGreenletError
        in synchronous code with lazy-loaded SQLAlchemy relationships.
        """
        dtype = type_cache.get(attr_id, "")

        if dtype == AttributeTypeDataType.INTEGER.value:
            return str(val.value_integer) if val.value_integer is not None else ""
        elif dtype == AttributeTypeDataType.DOUBLE.value:
            return str(val.value_float) if val.value_float is not None else ""
        elif dtype == AttributeTypeDataType.BOOLEAN.value:
            if val.value_boolean is None:
                return ""
            return "true" if val.value_boolean else "false"
        elif dtype == AttributeTypeDataType.DATETIME.value:
            if val.value_datetime is None:
                return ""
            return val.value_datetime.isoformat()
        elif dtype == AttributeTypeDataType.STATUS.value:
            if not val.value_simple:
                return ""
            try:
                status_uuid = uuid.UUID(val.value_simple)
            except (ValueError, AttributeError):
                return val.value_simple
            return status_cache.get(status_uuid, val.value_simple)
        elif dtype == AttributeTypeDataType.SELECT.value:
            # D-12/D-15: post-migration value_simple holds the option id —
            # resolve to the base display value via the caller-built select_cache
            # (attr_id-scoped {id: value}); unknown/stale values stay raw. No
            # lang dimension — exports are base-language only.
            return resolve_display_value(
                val.value_simple, (select_cache or {}).get(attr_id, {})
            ) or ""
        elif dtype in (AttributeTypeDataType.USER.value, AttributeTypeDataType.GROUP.value):
            # D-15: emit the resolved user/group name; raw-fallback (D-07) for
            # unresolvable UUIDs or legacy non-UUID stored values — never
            # fabricated. Deliberately NOT in _STRING_TYPES (import side): the
            # export/import name<->id resolution needs this dedicated branch,
            # not the raw passthrough _STRING_TYPES gives every other type.
            if not val.value_simple:
                return ""
            cache = user_cache if dtype == AttributeTypeDataType.USER.value else group_cache
            try:
                key = str(uuid.UUID(val.value_simple))
            except (ValueError, TypeError):
                return val.value_simple
            return (cache or {}).get(key, val.value_simple)
        elif dtype in (
            AttributeTypeDataType.TEXT.value,
            AttributeTypeDataType.URL.value,
            AttributeTypeDataType.EMAIL.value,
            AttributeTypeDataType.JSON.value,
            AttributeTypeDataType.VERSION.value,
            AttributeTypeDataType.JIRA.value,
            AttributeTypeDataType.CONFLUENCE.value,
        ):
            return val.value_simple or ""
        else:
            return val.value_simple or ""


class CsvImportRowError:
    """A single row-level error during CSV import (D-12)."""

    def __init__(self, row_number: int, reason: str):
        self.row_number = row_number
        self.reason = reason


class CsvUnknownNameError(ValueError):
    """Raised by `_coerce_value` when a USER/GROUP cell's name doesn't match
    any cached user/group (D-16) — the caller in `parse_csv` catches this and
    records a row-level `CsvImportRowError` instead of silently writing null
    or fabricating a value. `str(exc)` is the raw, unresolved cell value."""


class CsvImportResult:
    """Result of a CSV import parse operation — structured data ready for persistence.

    Each row in `rows` is a dict keyed by attr_id (str) with value = the coerced
    typed value. The error report (per D-12 / D-13) is a JSON-serializable list
    of {row_number, reason} dicts that the caller writes to Import.error_message.
    """
    rows: list[dict[str, object]]
    row_count: int
    columns: list[str]
    errors: list[CsvImportRowError]
    field_mappings: list[dict]
    columns_skipped: list[dict]  # [{attr_id, reason}, ...] — mappings that couldn't be resolved against the CSV headers

    def __init__(self, rows: list[dict[str, object]] = None, row_count: int = 0,
                 columns: list[str] = None, errors: list[CsvImportRowError] = None,
                 field_mappings: list[dict] = None, columns_skipped: list[dict] = None):
        self.rows = rows or []
        self.row_count = row_count
        self.columns = columns or []
        self.errors = errors or []
        self.field_mappings = field_mappings or []
        self.columns_skipped = columns_skipped or []


class CsvImportConnector:
    """Parses CSV rows into typed import data using an ImportDefinition's field mappings.

    Pure wire-protocol connector — zero DB access (mirrors CsvExportConnector pattern).
    All data (attr_cache, type_cache, etc.) is resolved by the caller.

    Date parsing: uses date_format from the ImportDefinition when set; otherwise
    heuristic auto-detection via datetime.fromisoformat (Python 3.11+), falling
    back to dateutil.parser.parse if python-dateutil is available, then simple
    string-based heuristic.
    """

    # Mapping from the plan's type names to actual AttributeTypeDataType values
    # that store via value_simple (non-typed columns)
    # USER/GROUP are deliberately NOT here — they get an explicit name-to-id
    # resolution branch in _coerce_value (D-16) instead of the raw
    # passthrough every type in this set gets.
    _STRING_TYPES = frozenset({
        AttributeTypeDataType.TEXT.value,
        AttributeTypeDataType.URL.value,
        AttributeTypeDataType.EMAIL.value,
        AttributeTypeDataType.SELECT.value,
        AttributeTypeDataType.JSON.value,
        AttributeTypeDataType.VERSION.value,
        AttributeTypeDataType.JIRA.value,
        AttributeTypeDataType.CONFLUENCE.value,
    })

    def __init__(self, delimiter: str = ",", encoding: str = "utf-8",
                 value_separator: str = ";", date_format: str | None = None):
        """Initialize the import connector.

        Args:
            delimiter: CSV column field delimiter (default: ",")
            encoding: CSV file encoding (default: "utf-8")
            value_separator: Multi-value cell split separator (default: ";")
            date_format: Optional strptime format string; None = heuristic auto-detect
        """
        self.delimiter = delimiter
        self.encoding = encoding
        self.value_separator = value_separator
        self.date_format = date_format

    def parse_csv(
        self,
        csv_content: str,
        field_mappings: list[dict],
        attr_cache: dict[uuid.UUID, ObjectTypeAttribute],
        type_cache: dict[uuid.UUID, str],  # attr_id -> data_type string
        status_cache: dict[str, str] | None = None,  # status_name -> status_id
        select_cache: dict[str, dict[str, str]] | None = None,  # attr_id (str) -> {option value -> option id}
        obj_scope: dict[uuid.UUID, str] | None = None,  # object_id -> name (for ref lookup)
        user_cache: dict[str, str] | None = None,  # username -> str(user uuid) (D-16)
        group_cache: dict[str, str] | None = None,  # group name -> str(group uuid) (D-16)
    ) -> CsvImportResult:
        """Parse CSV content into structured typed import data.

        Args:
            csv_content: Raw CSV text content
            field_mappings: list of {attr_id, csv_column, column_order}
            attr_cache: attr_id -> ObjectTypeAttribute metadata
            type_cache: attr_id -> data_type string (pre-extracted in async context)
            status_cache: status_name -> status_id (inverted from export pattern)
            select_cache: attr_id (str) -> {option value -> option id} — built by
                the caller from each SELECT attribute's structure options; used to
                resolve display-string cells to canonical option ids (D-05), with
                unknown values stored raw (D-15, never fabricated)
            obj_scope: object_id -> name for reference UUID resolution lookup
            user_cache: username -> str(user uuid) — resolves USER cells to ids
                (D-16); a name absent from this cache raises a row-level error
                instead of writing null or a fabricated value.
            group_cache: group name -> str(group uuid) — same contract as
                user_cache, for GROUP cells.

        Returns:
            CsvImportResult with typed rows and error list
        """
        reader = csv.DictReader(io.StringIO(csv_content), delimiter=self.delimiter)
        headers = reader.fieldnames or []

        if not headers:
            return CsvImportResult(
                rows=[], row_count=0, columns=[], field_mappings=field_mappings,
                columns_skipped=[
                    {"attr_id": m["attr_id"], "reason": "CSV file has no header row"}
                    for m in field_mappings
                ],
            )

        # WR-12: a CSV whose field delimiter does not match self.delimiter
        # parses into a single giant column instead of raising a clear error.
        # Surface an explicit diagnostic instead of letting every mapping silently
        # fail to resolve (see CR-03) with no hint as to why.
        delimiter_mismatch_suspected = len(headers) == 1 and len(field_mappings) > 1

        # Check which field_mappings are usable
        valid_mappings: list[dict] = []
        skipped_mappings: list[dict] = []
        if delimiter_mismatch_suspected:
            skipped_mappings.append({
                "attr_id": None,
                "reason": f"Possible delimiter mismatch: CSV parsed to a single column "
                          f"('{headers[0]}') using delimiter '{self.delimiter}', but "
                          f"{len(field_mappings)} field mappings were configured. Check "
                          f"that the CSV's actual delimiter matches the import connector's.",
            })

        for mapping in field_mappings:
            csv_column = mapping["csv_column"]
            data_type = type_cache.get(uuid.UUID(mapping["attr_id"]) if isinstance(mapping["attr_id"], str) else mapping["attr_id"])
            if not data_type:
                skipped_mappings.append({"attr_id": mapping["attr_id"], "reason": "Attribute type not found in type_cache"})
                continue
            if data_type == AttributeTypeDataType.REFERENCE.value:
                # Check for two-column format or single name column
                has_uuid_col = f"{csv_column}_uuid" in headers
                has_name_col = f"{csv_column}_name" in headers
                if has_uuid_col or has_name_col:
                    valid_mappings.append(mapping)
                elif csv_column in headers:
                    # Single-column format (name only, no _uuid/_name suffix)
                    valid_mappings.append(mapping)
                else:
                    skipped_mappings.append({"attr_id": mapping["attr_id"], "reason": f"Column '{csv_column}' not found in CSV"})
            else:
                if csv_column in headers:
                    valid_mappings.append(mapping)
                else:
                    skipped_mappings.append({"attr_id": mapping["attr_id"], "reason": f"Column '{csv_column}' not found in CSV"})

        # Sort by column_order for consistent output
        valid_mappings.sort(key=lambda m: m["column_order"])

        rows: list[dict[str, object]] = []
        errors: list[CsvImportRowError] = []
        row_number = 0

        for csv_row in reader:
            row_number += 1
            row_data: dict[str, object] = {}

            for mapping in valid_mappings:
                attr_id_str = mapping["attr_id"]
                csv_column = mapping["csv_column"]
                attr_id = uuid.UUID(attr_id_str) if isinstance(attr_id_str, str) else attr_id_str
                data_type = type_cache.get(attr_id)
                attr = attr_cache.get(attr_id)

                if not data_type or not attr:
                    continue

                if data_type == AttributeTypeDataType.REFERENCE.value:
                    ref_result, invalid_ref_uuids = self._coerce_reference(
                        csv_row, csv_column, attr, attr_id_str, obj_scope
                    )
                    if ref_result is not None:
                        row_data[attr_id_str] = ref_result
                    if invalid_ref_uuids:
                        errors.append(CsvImportRowError(
                            row_number=row_number,
                            reason=f"Column '{csv_column}': unparseable reference UUID(s) "
                                   f"dropped: {', '.join(invalid_ref_uuids)}",
                        ))
                    continue

                raw_value = csv_row.get(csv_column, "")
                if raw_value is None or raw_value == "":
                    continue

                is_multi = self._is_multi_valued(attr)

                if is_multi:
                    values = raw_value.split(self.value_separator)
                    coerced_values = []
                    for v in values:
                        v = v.strip()
                        if v:
                            try:
                                coerced = self._coerce_value(
                                    v, data_type, status_cache, select_cache, attr_id_str,
                                    user_cache, group_cache,
                                )
                            except CsvUnknownNameError as exc:
                                # D-16: unknown USER/GROUP name — row-level error,
                                # never a silently-written null or fabricated id.
                                errors.append(CsvImportRowError(
                                    row_number=row_number,
                                    reason=f"Column '{csv_column}': unknown {data_type} "
                                           f"name '{exc}'",
                                ))
                                continue
                            coerced_values.append(coerced)
                    if coerced_values:
                        row_data[attr_id_str] = {
                            "attr_id": attr_id_str,
                            "values": coerced_values,
                        }
                else:
                    try:
                        coerced = self._coerce_value(
                            raw_value, data_type, status_cache, select_cache, attr_id_str,
                            user_cache, group_cache,
                        )
                    except CsvUnknownNameError as exc:
                        errors.append(CsvImportRowError(
                            row_number=row_number,
                            reason=f"Column '{csv_column}': unknown {data_type} name '{exc}'",
                        ))
                        continue
                    if coerced is not None:
                        coerced["attr_id"] = attr_id_str
                        row_data[attr_id_str] = coerced

            if row_data:
                rows.append(row_data)

        return CsvImportResult(
            rows=rows,
            row_count=len(rows),
            columns=headers,
            errors=errors,
            field_mappings=field_mappings,
            columns_skipped=skipped_mappings,
        )

    def _coerce_value(
        self,
        raw_value: str,
        data_type: str,
        status_cache: dict[str, str] | None,
        select_cache: dict[str, dict[str, str]] | None = None,
        attr_id_str: str | None = None,
        user_cache: dict[str, str] | None = None,
        group_cache: dict[str, str] | None = None,
    ) -> dict | None:
        """Coerce a single CSV cell string into a typed value dict.

        Returns a dict with the coerced typed column(s) set, or None if the value
        is empty. On coercion failure, stores raw string in value_simple per D-10.

        Raises CsvUnknownNameError for a USER/GROUP cell whose name isn't in
        the corresponding cache (D-16) — the caller converts this to a
        row-level CsvImportRowError rather than writing null/fabricating an id.
        """
        if not raw_value:
            return None

        raw_value = raw_value.strip()

        result: dict = {"value_simple": None, "value_integer": None,
                        "value_float": None, "value_boolean": None, "value_datetime": None}

        if data_type == AttributeTypeDataType.INTEGER.value:
            try:
                result["value_integer"] = int(raw_value)
            except (ValueError, TypeError):
                result["value_simple"] = raw_value

        elif data_type == AttributeTypeDataType.DOUBLE.value:
            try:
                result["value_float"] = float(raw_value)
            except (ValueError, TypeError):
                result["value_simple"] = raw_value

        elif data_type == AttributeTypeDataType.BOOLEAN.value:
            lower_val = raw_value.lower()
            if lower_val in ("true", "1", "yes"):
                result["value_boolean"] = True
            elif lower_val in ("false", "0", "no"):
                result["value_boolean"] = False
            else:
                result["value_simple"] = raw_value

        elif data_type == AttributeTypeDataType.DATETIME.value:
            parsed = self._parse_date(raw_value)
            if parsed is not None:
                result["value_datetime"] = parsed
            else:
                result["value_simple"] = raw_value

        elif data_type == AttributeTypeDataType.STATUS.value:
            if status_cache:
                resolved_id = status_cache.get(raw_value)
                if resolved_id:
                    result["value_simple"] = resolved_id
                else:
                    result["value_simple"] = raw_value
            else:
                result["value_simple"] = raw_value

        elif data_type == AttributeTypeDataType.SELECT.value:
            # D-05/D-15: known option display strings resolve to the canonical
            # option id via the per-attribute select_cache (attr_id-scoped — the
            # same option value can carry a DIFFERENT id in different
            # attributes, so a flat value->id map would fabricate wrong ids);
            # unknown values stay raw, never fabricated (T-223-09).
            if select_cache and attr_id_str:
                resolved_id = select_cache.get(attr_id_str, {}).get(raw_value)
                result["value_simple"] = resolved_id if resolved_id else raw_value
            else:
                result["value_simple"] = raw_value

        elif data_type in (AttributeTypeDataType.USER.value, AttributeTypeDataType.GROUP.value):
            # D-16: a known username/group name resolves to its UUID; an
            # unknown name raises so the caller records a row-level error
            # instead of writing null or fabricating a UUID from raw text.
            cache = user_cache if data_type == AttributeTypeDataType.USER.value else group_cache
            resolved_id = (cache or {}).get(raw_value)
            if resolved_id:
                result["value_simple"] = resolved_id
            else:
                raise CsvUnknownNameError(raw_value)

        elif data_type in self._STRING_TYPES:
            result["value_simple"] = raw_value

        else:
            # Unknown data type — store raw string
            result["value_simple"] = raw_value

        return result

    def _parse_date(self, value: str) -> datetime | None:
        """Parse a date string into a datetime instance, or None on failure.

        Returns an actual datetime object (not an ISO string) — the sole caller
        (_coerce_value's DATETIME branch) stores this straight into
        ObjectValue.value_datetime, a TIMESTAMP column; asyncpg requires a real
        datetime.datetime for that bind parameter and rejects a plain str with
        "invalid input ... expected a datetime.date or datetime.datetime
        instance, got 'str'" — every row of a DATETIME-mapped column failed
        import with exactly that DataError before this fix.
        """
        if self.date_format:
            try:
                return datetime.strptime(value, self.date_format)
            except (ValueError, TypeError):
                return None

        # Heuristic: try ISO 8601 via fromisoformat (Python 3.11+)
        try:
            cleaned = value.replace("Z", "+00:00").replace("T", " ")
            # Try with timezone info stripped first
            return datetime.fromisoformat(cleaned)
        except (ValueError, TypeError):
            pass

        # Heuristic: try common date formats
        for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%d-%m-%Y", "%d/%m/%Y",
                     "%m-%d-%Y", "%m/%d/%Y", "%Y-%m-%d %H:%M:%S",
                     "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
            try:
                return datetime.strptime(value, fmt)
            except (ValueError, TypeError):
                continue

        # Try dateutil if available
        try:
            from dateutil import parser as dateutil_parser
            return dateutil_parser.parse(value)
        except (ImportError, ValueError, TypeError):
            pass

        return None

    def _coerce_reference(
        self,
        csv_row: dict,
        csv_column: str,
        attr: ObjectTypeAttribute,
        attr_id_str: str,
        obj_scope: dict[uuid.UUID, str] | None,
    ) -> tuple[dict | None, list[str]]:
        """Resolve a reference attribute from a CSV row.

        Looks for {csv_column}_uuid first (preferred), then {csv_column}_name.
        Multi-value references (semicolon-separated within each cell) are resolved
        to lists of ref_object_ids.

        Returns a tuple of (result dict or None, list of unparseable raw _uuid
        values that were dropped rather than silently forwarded — WR-06). The
        caller records the second element as a row-level error so a dropped
        reference doesn't get reported as a fully-successful row.
        """
        uuid_col = f"{csv_column}_uuid"
        name_col = f"{csv_column}_name"
        is_multi = self._is_multi_valued(attr)

        # Prefer _uuid column, fall back to _name column
        ref_uuids: list[str] = []
        invalid_uuids: list[str] = []

        if uuid_col in csv_row and csv_row[uuid_col]:
            raw_uuids = csv_row[uuid_col]
            if is_multi:
                parts = raw_uuids.split(self.value_separator)
            else:
                parts = [raw_uuids]
            for p in parts:
                p = p.strip()
                if p:
                    try:
                        uuid.UUID(p)
                        ref_uuids.append(p)
                    except (ValueError, TypeError):
                        # WR-06: don't forward unparseable UUID garbage downstream —
                        # record it so the caller can surface a row-level error.
                        invalid_uuids.append(p)

        elif name_col in csv_row and csv_row[name_col]:
            raw_names = csv_row[name_col]
            if is_multi:
                parts = raw_names.split(self.value_separator)
            else:
                parts = [raw_names]
            for p in parts:
                p = p.strip()
                if p and obj_scope:
                    # Invert obj_scope: name -> id
                    for obj_id, obj_name in obj_scope.items():
                        if obj_name == p:
                            ref_uuids.append(str(obj_id))
                            break

        elif csv_column in csv_row and csv_row[csv_column]:
            # Single-column fallback: treat value as name
            raw_val = csv_row[csv_column]
            if is_multi:
                parts = raw_val.split(self.value_separator)
            else:
                parts = [raw_val]
            for p in parts:
                p = p.strip()
                if p and obj_scope:
                    for obj_id, obj_name in obj_scope.items():
                        if obj_name == p:
                            ref_uuids.append(str(obj_id))
                            break

        if not ref_uuids:
            return None, invalid_uuids

        return {"attr_id": attr_id_str, "ref_object_ids": ref_uuids}, invalid_uuids

    def _is_multi_valued(self, attr: ObjectTypeAttribute) -> bool:
        """Detect multi-valued attribute per D-16 (mirrors CsvExportConnector)."""
        mc = attr.maximum_cardinality
        if mc > 1:
            return True
        if mc == -1:
            return True
        return False
