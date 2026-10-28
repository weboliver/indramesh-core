"""Read-only revalidation of stored saved queries against the Phase-312 limits.

The operator tool of GQLC-05 (D-15/D-16): enumerate every stored saved query in
ONE statement, run the SHARED analyzer (``analyze_document`` — the same rule
classes the execution path registers and the save path rejects with) against
each stored document, and report which queries the limits would now reject.

Strictly read-only and DB-only: no write, no cache invalidation, no Redis, and
the stored query TEXT never reaches stdout, the JSON document or a log record
(T-312-16/T-312-17). ``build_report`` carries the verb's exit code: ``0`` clean,
``1`` at least one breached stored query.
"""

from __future__ import annotations

import json
import logging

from graphql import GraphQLError, parse
from sqlalchemy import select

from app.graphql.limits import DocumentAnalysis, analyze_document
from app.models import SavedQuery

logger = logging.getLogger(__name__)

TABLE_HEADER = "id | name | cost | breached"
"""Header of the plain-text operator table."""

EXIT_CLEAN = 0
"""No stored saved query breaches a limit axis."""

EXIT_VIOLATIONS = 1
"""At least one stored saved query breaches a limit axis."""


async def revalidate_saved_queries(session) -> list[dict]:
    """One report dict per stored saved query — one load, no writes.

    The whole stock is loaded in ONE statement, ordered ``(name, id)`` — the
    ordering ``export_saved_queries`` uses — so repeated runs against an
    unchanged stock produce the identical table and JSON document.

    Each row carries ``id``, ``name``, ``cost`` and ``breached``: the ``cost``
    is the SHARED analyzer's ``estimate_cost`` number (never a locally computed
    figure) and ``breached`` is the ``limit`` axis of the first violation, or
    ``None``. A document that cannot be parsed at all is reported with
    ``breached="syntax"`` and ``cost=None`` instead of crashing the run —
    including a legacy row whose nesting exceeds the interpreter's recursion
    limit, which is exactly the stock this tool exists to find (graphql-core's
    parser is recursive; measured: ~250 levels). The query text is never
    returned.
    """
    # Function-local, the phase's stated convention for every resolver site
    # (mirrors limits.validate_document_limits): app.maintenance is not imported
    # by the graphql package, so a module-level import would be cycle-safe too,
    # but the two resolver sites stay symmetric.
    from app.graphql.schema import schema as strawberry_schema

    result = await session.execute(
        select(SavedQuery).order_by(SavedQuery.name, SavedQuery.id)
    )
    reports: list[dict] = []
    for row in result.scalars().all():
        try:
            document_ast = parse(row.query_text)
        except (GraphQLError, RecursionError):
            # Reportable, never fatal: "syntax" names an axis no rule measures.
            reports.append(_report(row, cost=None, breached="syntax"))
            continue
        analysis: DocumentAnalysis = analyze_document(
            strawberry_schema._schema, document_ast, row.query_text
        )
        breached = None
        if analysis.errors:
            breached = (analysis.errors[0].extensions or {}).get("limit")
        reports.append(_report(row, cost=analysis.cost, breached=breached))
    return reports


def _report(row, *, cost: int | None, breached: str | None) -> dict:
    """The per-row output schema: id, name, cost, breached — never the text."""
    return {
        "id": str(row.id),
        "name": row.name,
        "cost": cost,
        "breached": breached,
    }


def format_report(reports: list[dict]) -> str:
    """Render the plain-text table: header plus one row per stored query."""
    lines = [TABLE_HEADER]
    for report in reports:
        lines.append(
            f"{report['id']} | {report['name']} | "
            f"{report['cost']} | {report['breached']}"
        )
    return "\n".join(lines)


def format_report_json(reports: list[dict]) -> str:
    """Render the same rows as a JSON document (``[]`` for an empty stock)."""
    return json.dumps(reports)


def violation_count(reports: list[dict]) -> int:
    """How many stored queries breach at least one limit axis."""
    return sum(1 for report in reports if report["breached"])


def build_report(reports: list[dict]) -> dict:
    """The verb's structured completion record, including its exit code.

    ``exit_code`` is ``0`` for a clean stock and ``1`` when at least one stored
    query is breached, so an operator script can gate on it. A run that itself
    fails (e.g. the database is unreachable) never reaches this function and is
    the dispatcher's distinct ``2``.
    """
    violations = violation_count(reports)
    return {
        "ok": True,
        "queries": len(reports),
        "violations": violations,
        "exit_code": EXIT_VIOLATIONS if violations else EXIT_CLEAN,
    }
