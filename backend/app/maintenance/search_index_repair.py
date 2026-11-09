"""Repair partially mapped ``objects_<schema_id>`` search indices (REV-01 / D-01..D-04).

One Schema owns one ``objects_<schema_id>`` index shared by all of its
ObjectTypes, but an index created before a type existed carries only the field
mappings of the type whose document created it. The remaining types then fall
back to OpenSearch's dynamic mapping and range, sort and exact-match queries
answer wrongly for them.

This module compares, for every Schema, the mapping the persisted definitions
imply (:func:`build_schema_index_mappings` — the same builder the create path
uses, so detection and repair cannot disagree) with the mapping the index
actually carries, and reports every diverging property. It is a **dry run by
default**: only ``--apply`` deletes and recreates the indices of diverging
Schemas and reindexes their documents. A Schema whose index does not diverge is
never touched, and a Schema without an index is reported as unmapped — nothing
is created for it.

The comparison lives in one pure function (:func:`detect_divergences`), so it is
unit-testable without a search backend.

Known gap (D-04): a rebuild deletes the old index first, so **search returns no
hits for the affected Schema while it runs** — a 1.0.1 patch cannot switch
indices through an alias without an alias model in every search path. The report
and the deployment documentation name the gap; the operator chooses the window.
Stale properties — an ``attr_*`` field whose attribute definition no longer
exists — are listed but never trigger a rebuild: OpenSearch cannot drop a field
without one, and the field changes no result.

Exit codes: ``0`` clean, ``1`` divergence found in a dry run (or a rebuild
completed with warnings), ``2`` the run itself failed.
"""

from __future__ import annotations

import json
import logging
import uuid

from sqlalchemy import select

from app.models.object_type import ObjectType
from app.models.schema import Schema
from app.services.search_service import (
    build_schema_index_mappings,
    delete_index,
    ensure_index,
    get_opensearch,
    reindex_schema,
)

logger = logging.getLogger(__name__)

EXIT_CLEAN = 0
"""Everything is mapped (or only stale fields and unmapped schemas were listed)."""

EXIT_DIVERGENCE = 1
"""A dry run found at least one divergence, or a rebuild completed with warnings."""

EXIT_FAILED = 2
"""The run itself failed; nothing is reported as clean."""

REASON_MISSING = "missing_property"
"""The definition declares a property the index does not map."""

REASON_WRONG_TYPE = "wrong_type"
"""The index maps the property with a different field type than the definition."""

REASON_STALE = "stale_property"
"""The index maps an ``attr_*`` property whose attribute definition is gone."""

DIVERGING_REASONS = (REASON_MISSING, REASON_WRONG_TYPE)
"""Only these make a Schema diverging; a stale property never does."""

ATTR_PREFIX = "attr_"

NO_HITS_GAP = "search returns no hits for a schema while its index is rebuilt"
"""The D-04 gap every ``--apply`` report and the deployment docs name."""


def _index_name(schema_id: uuid.UUID) -> str:
    return f"objects_{schema_id}"


def _flatten_properties(properties: dict | None, prefix: str = "") -> dict:
    """Flatten a mapping's ``properties`` into dotted keys.

    OpenSearch stores an attribute name that contains a dot as a nested object
    path, so an unflattened comparison would report such an attribute as missing
    on every run. A node that carries its own ``properties`` is a container
    (object mapping or multi-field parent's child block) and is descended into;
    every other node is a leaf whose ``type`` is compared and whose analyzer or
    ``fields`` additions are ignored.
    """
    flat: dict[str, dict] = {}
    for key, value in (properties or {}).items():
        full_key = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(value.get("properties"), dict):
            flat.update(_flatten_properties(value["properties"], prefix=f"{full_key}."))
            continue
        flat[full_key] = value if isinstance(value, dict) else {}
    return flat


def _es_type(property_definition: dict) -> str | None:
    if not isinstance(property_definition, dict):
        return None
    return property_definition.get("type")


def _object_type_id_from_key(field: str) -> str:
    """The ObjectType id a property key carries, or ``"-"`` when it carries none."""
    if not field.startswith(ATTR_PREFIX):
        return "-"
    candidate = field[len(ATTR_PREFIX) :].partition("_")[0]
    try:
        return str(uuid.UUID(candidate))
    except (ValueError, AttributeError):
        return "-"


def _row(schema_id: str, field: str, reason: str) -> dict:
    return {
        "schema_id": schema_id,
        "object_type_id": _object_type_id_from_key(field),
        "field": field,
        "reason": reason,
    }


def detect_divergences(entries) -> list[dict]:
    """Compare expected and actual properties, one row per diverging field.

    ``entries`` is an iterable of ``(schema_id, expected_properties,
    actual_properties)`` — one entry per Schema, each properties mapping a
    ``dict[str, dict]`` as the index mapping and
    :func:`build_schema_index_mappings` produce them. The Schema id travels
    with the pair so every returned row can name its Schema.

    A property is reported ``missing_property`` when the definition declares it
    and the index lacks it, and ``wrong_type`` when only the ``type`` differs
    (analyzer and multi-field additions are ignored). An ``attr_*`` property the
    index carries without a definition is reported ``stale_property``: it is
    listed, but it does not make the Schema diverging, because no rebuild could
    drop the field for a result-changing reason.
    """
    rows: list[dict] = []
    for schema_id, expected_properties, actual_properties in entries:
        schema_key = str(schema_id)
        expected = _flatten_properties(expected_properties)
        actual = _flatten_properties(actual_properties)
        for field in sorted(expected):
            if field not in actual:
                rows.append(_row(schema_key, field, REASON_MISSING))
            elif _es_type(actual[field]) != _es_type(expected[field]):
                rows.append(_row(schema_key, field, REASON_WRONG_TYPE))
        for field in sorted(actual):
            if field.startswith(ATTR_PREFIX) and field not in expected:
                rows.append(_row(schema_key, field, REASON_STALE))
    return rows


def _actual_properties(mapping: dict | None, index_name: str) -> dict:
    """The ``properties`` block ``indices.get_mapping`` returned for one index."""
    entry = (mapping or {}).get(index_name) or {}
    return ((entry.get("mappings") or {}).get("properties")) or {}


async def _rebuild_schema(db, schema_id: uuid.UUID, object_type_ids: list) -> list[str]:
    """Delete and recreate one Schema's index, then reindex its documents.

    ``task_id=None`` keeps the rebuild a taskless system run — no Task row is
    fabricated for an operator command. The D-04 gap applies: between the delete
    and the completed reindex this Schema's search serves no hits.
    """
    warnings: list[str] = []
    if not object_type_ids:
        warnings.append(
            f"schema {schema_id}: no ObjectType to create the index for, left untouched"
        )
        return warnings
    await delete_index(schema_id)
    await ensure_index(db, schema_id, object_type_ids[0])
    result = await reindex_schema(db, schema_id, None)
    if not result.get("success", False):
        warnings.append(
            f"schema {schema_id}: the rebuild reported "
            f"{result.get('errors', 0)} error(s); {NO_HITS_GAP}"
        )
    return warnings


async def run_repair(session_maker, *, apply: bool) -> dict:
    """Detect divergences on every ``objects_<schema_id>`` index; rebuild with ``apply``.

    Reads the Schema list and its ObjectTypes, then for every Schema whose index
    exists compares the live mapping with :func:`build_schema_index_mappings`
    through :func:`detect_divergences`. A Schema without an index is collected
    in ``unmapped`` and never created or deleted. With ``apply`` only the
    diverging Schemas are deleted, recreated with the schema-wide mapping and
    reindexed; a healthy Schema is never touched, which is what makes the run
    safe on a partially damaged installation.

    Schemas are visited in ascending ``schema_id`` order and each Schema's field
    rows are sorted by property key, so two runs over the same state produce a
    byte-identical report.
    """
    report: dict = {
        "ok": True,
        "applied": apply,
        "diverged": [],
        "stale": [],
        "unmapped": [],
        "rebuilt": [],
        "warnings": [],
        "exit_code": EXIT_CLEAN,
    }
    try:
        es = await get_opensearch()
        async with session_maker() as db:
            schema_ids = list(
                (await db.execute(select(Schema.id))).scalars().all()
            )
            object_types_by_schema: dict[uuid.UUID, list[uuid.UUID]] = {}
            for schema_id, object_type_id in (
                await db.execute(
                    select(ObjectType.schema_id, ObjectType.id).order_by(
                        ObjectType.schema_id, ObjectType.id
                    )
                )
            ).all():
                object_types_by_schema.setdefault(schema_id, []).append(object_type_id)

            for schema_id in sorted(schema_ids, key=str):
                index_name = _index_name(schema_id)
                if not await es.indices.exists(index=index_name):
                    report["unmapped"].append(str(schema_id))
                    continue
                expected = (await build_schema_index_mappings(db, schema_id))[
                    "properties"
                ]
                mapping = await es.indices.get_mapping(index=index_name)
                rows = detect_divergences(
                    [(schema_id, expected, _actual_properties(mapping, index_name))]
                )
                report["stale"].extend(
                    row for row in rows if row["reason"] == REASON_STALE
                )
                diverged = [
                    row for row in rows if row["reason"] in DIVERGING_REASONS
                ]
                report["diverged"].extend(diverged)
                if apply and diverged:
                    report["warnings"].extend(
                        await _rebuild_schema(
                            db, schema_id, object_types_by_schema.get(schema_id, [])
                        )
                    )
                    report["rebuilt"].append(str(schema_id))
    except Exception as exc:  # the operator tool reports, it never raises
        logger.exception("search index repair failed")
        report["ok"] = False
        report["warnings"].append(f"the run failed: {type(exc).__name__}")

    report["diverged"].sort(key=lambda row: (row["schema_id"], row["field"]))
    report["stale"].sort(key=lambda row: (row["schema_id"], row["field"]))
    report["unmapped"].sort()
    report["rebuilt"].sort()
    report["exit_code"] = _exit_code(report)
    return report


def _exit_code(report: dict) -> int:
    """``0`` clean, ``1`` divergence / completed with warnings, ``2`` failed."""
    if not report["ok"]:
        return EXIT_FAILED
    if report["diverged"] and (not report["applied"] or report["warnings"]):
        return EXIT_DIVERGENCE
    return EXIT_CLEAN


def _format_row(row: dict) -> str:
    return (
        f"schema {row['schema_id']} object_type {row['object_type_id']} "
        f"field {row['field']}: {row['reason']}"
    )


def format_report(report: dict) -> str:
    """Render the operator report: schema and field rows, then the summary."""
    if report.get("applied"):
        lines = [
            "Search index repair (--apply): only diverging schemas are rebuilt, and "
            f"{NO_HITS_GAP}."
        ]
    else:
        lines = ["Search index repair (dry run): no index was written."]
    for row in report.get("diverged", []):
        lines.append(_format_row(row))
    for row in report.get("stale", []):
        lines.append(f"{_format_row(row)} (listed only, never rebuilt)")
    for schema_id in report.get("unmapped", []):
        lines.append(f"schema {schema_id}: no index — nothing to repair")
    for schema_id in report.get("rebuilt", []):
        lines.append(f"schema {schema_id}: index rebuilt")
    for warning in report.get("warnings", []):
        lines.append(f"warning: {warning}")
    lines.append(
        f"summary: {len(report.get('diverged', []))} diverging field(s), "
        f"{len(report.get('stale', []))} stale field(s), "
        f"{len(report.get('rebuilt', []))} schema(s) rebuilt, "
        f"{len(report.get('unmapped', []))} schema(s) without an index"
    )
    lines.append(f"exit code: {report.get('exit_code', EXIT_CLEAN)}")
    return "\n".join(lines)


def format_report_json(report: dict) -> str:
    """Render the same report as a JSON document."""
    return json.dumps(report)
