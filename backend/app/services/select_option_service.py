"""SELECT-option id domain helpers (Phase 223, D-01/D-02/D-11/D-16).

Canonical shape contract: every option in `structure["options"]` is a dict
``{"id": str(uuid4), "value": str, "position": int}``.

- ``id`` is a stable UUID string (D-01) referenced BOTH by SELECT
  ``object_values.value_simple`` AND by ``translations.entity_id`` for the
  ``select_option`` translation type (D-16) — one id, two referrers.
- ``value`` is the base/default-language display string (what Jira exposes).
- ``position`` is the display order within the attribute.

Every non-sync structure write site routes through ``ensure_option_ids`` so
no site can write an id-less or legacy-shaped options list (D-02). The helper
is idempotent by contract: existing ids are NEVER regenerated, so re-runs
over already-migrated structures (and sync re-pulls, D-03 rename-safety) are
no-ops. Pure functions — no DB access, no imports from translation_service
(also importable inside the alembic data migration).
"""

import uuid
from typing import Any


def ensure_option_ids(raw_options: Any) -> list[dict]:
    """Normalize ANY legacy option shape into the canonical dict list.

    Accepted inputs:
    - ``List[str]`` (atomic-create/template shape) -> fresh uuid4 ids.
    - ``List[dict]`` without "id" (Jira's ``{"value", "position"}`` legacy
      shape) -> fresh uuid4 ids; value/position preserved.
    - ``List[dict]`` WITH "id" (already canonical) -> ids kept verbatim
      (mandatory idempotency — re-runs are no-ops).
    - ``None`` / ``[]`` -> ``[]``.

    Empty-string values are dropped. Missing ``position`` is filled with the
    enumerate index. Always returns a NEW list of NEW dicts — input dicts are
    copied, never mutated.
    """
    if not raw_options:
        return []
    normalized: list[dict] = []
    for idx, opt in enumerate(raw_options):
        if isinstance(opt, str):
            value = opt
            entry: dict = {"value": value}
        elif isinstance(opt, dict):
            entry = dict(opt)
            value = entry.get("value")
        else:
            continue
        if not value:
            continue  # drop empty-string values
        if not isinstance(value, str):
            value = str(value)
        entry["value"] = value
        if not entry.get("id"):
            entry["id"] = str(uuid.uuid4())
        if entry.get("position") is None:
            entry["position"] = idx
        normalized.append(entry)
    return normalized


def removed_option_ids(old_options: list[dict], new_options: list[dict]) -> set[str]:
    """Ids present in old_options but absent in new_options (D-11 diff purge).

    Options without ids are ignored (they can never own translation rows).
    """
    old_ids = {str(opt["id"]) for opt in old_options if opt.get("id")}
    new_ids = {str(opt["id"]) for opt in new_options if opt.get("id")}
    return old_ids - new_ids


def option_ids_from_structure(structure: Any) -> list[uuid.UUID]:
    """Every option id owned by a SELECT attribute's ``structure`` (WR-01).

    Used by the attribute/object-type/schema delete paths to purge
    ``select_option`` translation rows alongside the attribute itself —
    mirroring the diff-based purge already applied on structure update.
    Options without an ``id`` are ignored (they can never own translation
    rows).
    """
    options = structure.get("options") if isinstance(structure, dict) else None
    if not options:
        return []
    return [uuid.UUID(opt["id"]) for opt in options if isinstance(opt, dict) and opt.get("id")]


def resolve_select_literal(
    structure: dict | None, literal: str, attr_name: str | None = None
) -> str:
    """Idempotent SELECT literal -> option id resolution (D-05/D-06).

    Query authors keep writing display strings (`Type = "Rot"`) while
    post-migration ``value_simple`` stores option ids — this helper resolves
    the literal to the canonical option id BEFORE any SQL comparison, at
    every query boundary (AQL resolver, filter-tree evaluation, attr_filters).

    Resolution order:
    1. The literal already IS a known option id -> returned unchanged
       (idempotent pass-through — the AQL resolver resolves first, then the
       filter-tree path sees an id and passes it through untouched).
    2. The literal equals an option's base ``value`` -> that option's id.
    3. Otherwise -> ``ValueError`` naming the unknown value and (when given)
       the attribute. Callers convert per their error semantics: the AQL
       resolver re-raises as ``AQLValidationError`` (D-06), the REST filter
       APIs surface the ValueError as 400/422.

    Matching is against base values ONLY (D-07) — translation rows are never
    consulted, AQL stays language-agnostic.
    """
    options = (structure or {}).get("options") or []
    for opt in options:
        if isinstance(opt, dict) and opt.get("id") == literal:
            return literal
    for opt in options:
        if isinstance(opt, dict) and opt.get("value") == literal and opt.get("id"):
            return str(opt["id"])
    where = f" for attribute '{attr_name}'" if attr_name else ""
    raise ValueError(f"Unknown option '{literal}'{where}")


# ---------------------------------------------------------------------------
# Read-side resolution (223-04, D-12/D-14/D-15/D-19) — the ONE shared resolver
# every non-sync read boundary (export, ES indexing, audit display, internal
# object/object-value reads, and Plan 05's public API + GraphQL) uses to turn
# stored option ids back into display values. Batched by design: callers build
# per-attribute option maps once and reuse them across every stored value.
# ---------------------------------------------------------------------------


def _option_map_from_structure(structure: Any) -> dict[str, str]:
    """``{option id: base value}`` from a structure dict.

    Defensive by contract: only real dicts with an ``options`` list produce
    entries (non-SELECT structures, ``None``, and mock/ORM-proxy objects all
    yield ``{}``); entries without an id or value are skipped. Ids are
    normalized via ``str()`` so uuid.UUID option ids match stored strings.
    """
    if not isinstance(structure, dict):
        return {}
    options = structure.get("options")
    if not isinstance(options, list):
        return {}
    return {
        str(opt["id"]): str(opt["value"])
        for opt in options
        if isinstance(opt, dict) and opt.get("id") is not None and opt.get("value") is not None
    }


def build_option_map(attr: Any) -> dict[str, str]:
    """``{option id: base display value}`` for a SELECT attribute (D-12/D-14).

    Accepts either an ``ObjectTypeAttribute`` (uses ``attr.structure``) or a
    raw structure dict (the audit/ES glue call sites hold only the dict).
    Empty/``None`` structures -> ``{}``.
    """
    if isinstance(attr, dict):
        structure = attr
    else:
        structure = getattr(attr, "structure", None)
    return _option_map_from_structure(structure)


def resolve_display_value(stored: str | None, option_map: dict[str, str]) -> str | None:
    """Resolve one stored SELECT value to its base display value (D-15).

    - ``None`` -> ``None``.
    - uuid-parseable AND present in ``option_map`` -> the option's value.
    - Anything else (pre-migration stale strings per D-04, deleted-option
      uuids, non-SELECT pass-through) -> ``stored`` unchanged. NO empty-string
      placeholder, NO ``[deleted option]`` marker — the STATUS precedent shape.
    """
    if stored is None or not isinstance(stored, str):
        return stored
    try:
        parsed = uuid.UUID(stored)
    except (ValueError, TypeError, AttributeError):
        return stored
    return option_map.get(str(parsed), stored)


async def resolve_select_values(
    db: Any,
    attrs: list,
    stored_by_attr: dict[uuid.UUID, list[str]],
    lang: str | None = None,
) -> dict[uuid.UUID, list[str]]:
    """Batched SELECT id -> display resolution for read endpoints.

    Builds every attribute's option map in ONE pass. When ``lang`` is set, a
    SINGLE ``resolve_translations`` call (per-entity fallback with
    ``schema_id=None`` — the 222 D-05 shape) overlays translated values for
    multilingual-gated owners; flag-off owners are dormant (D-09) and yield no
    entries, so they keep base values. Per stored value the fallback chain is:
    requested-language translation -> default-language translation -> base
    value -> raw (D-15). Returns ``{attr_id: [display, ...]}``.
    """
    option_maps = {a.id: build_option_map(a) for a in attrs}
    translated: dict[uuid.UUID, str] = {}
    if lang is not None and option_maps:
        all_option_ids = [oid for om in option_maps.values() for oid in om]
        if all_option_ids:
            # Lazy import: translation_service imports audit_service (not this
            # module), so there is no cycle — lazy keeps it explicit anyway.
            from app.services.translation_service import resolve_translations

            resolved = await resolve_translations(
                db, "select_option",
                [uuid.UUID(oid) for oid in all_option_ids],
                ["value"], lang, schema_id=None,
            )
            translated = {
                entity_id: fields["value"][0]
                for entity_id, fields in resolved.items()
                if "value" in fields
            }
    out: dict[uuid.UUID, list[str]] = {}
    for attr_id, stored_values in stored_by_attr.items():
        option_map = option_maps.get(attr_id, {})
        displays: list[str] = []
        for stored in stored_values:
            if stored is None or not isinstance(stored, str):
                displays.append(stored)
                continue
            if translated:
                try:
                    overlaid = translated.get(uuid.UUID(stored))
                except (ValueError, TypeError, AttributeError):
                    overlaid = None
                if overlaid is not None:
                    displays.append(overlaid)
                    continue
            displays.append(resolve_display_value(stored, option_map))
        out[attr_id] = displays
    return out
