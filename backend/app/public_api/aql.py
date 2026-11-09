"""Public AQL envelope builder + route (Plan 204-02).

This module is split into two parts:

1. The pure ``build_aql_envelope`` mapper — dict-in/dict-out, stack-light
   (no ``app.db``, no FastAPI imports at module scope) mirroring
   ``object_shape.py``. It turns assembled page data into the fixture-faithful
   Jira ``ObjectListResult`` envelope (D-01..D-10): golden top-level key set,
   includeAttributes omission (D-05), honest pagination metadata (D-08/D-09),
   per-type ``objectTypeAttributes`` (D-03), orderWay (D-10) and the echo
   fields. Only ``build_object_shape``/``build_attribute_definition`` (the
   allow-list mappers from Phase 201/202) touch entry/def shape (T-204-01).

2. The ``GET /public/api/v1/aql/objects`` route — a thin wiring layer
   following the Phase 201/202 endpoint precedent: rate limiting via the
   global SlowAPI middleware (D-16 — no per-route decorator), fail-fast
   param validation (D-11), engine error mapping to 400 {detail}
   (D-13), and a SINGLE parse of ``qlQuery`` per request (Task 0's ``tree``
   kwarg is what makes that possible). No envelope caching (D-12).
   Embedded referenced objects and the derived ``referenceObjectType``,
   including the top-level ``objectTypeAttributes``, are record-filtered per
   caller (quick 261012-blo).
"""
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.aql.compiler import execute_aql
from app.aql.errors import AQLParseError, AQLValidationError
from app.aql.parser import parse
from app.aql.transformer import _extract_order_by
from app.db import get_db
from app.public_api.object_read_service import (
    load_denied_attribute_type_ids,
    load_objects_read,
    load_visible_reference_targets,
)
from app.public_api.object_shape import (
    build_attribute_definition,
    build_object_shape,
    strip_hidden_references,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import require_public_hierarchy_read
from app.services.value_visibility import resolve_principal_roles

# This IQL search envelope is never scoped to a single ObjectType — the same
# reason ``objectTypeId`` below is unconditionally 0 (verified against the
# golden ``jira-iql-envelope.json`` fixture: real Jira pairs objectTypeId=0
# with abstractObjectType=false for an unscoped multi-type search result).
# INH-02's real per-object-type abstract state already flows through
# ``out_entries`` — each entry's own ``objectType.abstractObjectType`` comes
# from ``build_object_shape`` -> ``build_object_type_shape``, which reads the
# real ``is_abstract`` value (object_shape.py). This top-level field is a
# structural search-envelope constant, not a per-entry read — named here
# (rather than left as a bare literal) so it reads as an intentional design
# constant, not a leftover hardcode.
_UNSCOPED_SEARCH_ABSTRACT_OBJECT_TYPE = False


def build_aql_envelope(
    *,
    entries: list[dict],
    type_defs: dict[str, list[dict]],
    ref_object_types: dict[str, dict],
    total: int,
    page: int,
    result_per_page: int,
    order_way: str,
    ql_query: str,
    include_attributes: bool,
) -> dict:
    """Assemble the golden Jira ``ObjectListResult`` envelope (D-01..D-10).

    Pure mapper: entries are ``build_object_shape`` outputs (D-04), type_defs
    map to the top-level ``objectTypeAttributes`` via ``build_attribute_definition``
    (D-03, first-appearance order). ``include_attributes=False`` OMITS the
    ``attributes`` key from every entry copy — the caller's dicts are never
    mutated (T-204-01). Pagination metadata follows D-08 (honest pageSize =
    actual page size, NOT Jira's 2x echo quirk) and D-09 (1-based startIndex,
    empty page -> startIndex=1, toIndex=0).
    """
    if include_attributes:
        out_entries = entries
    else:
        out_entries = [dict(e) for e in entries]
        for entry in out_entries:
            entry.pop("attributes", None)

    object_type_attributes = [
        build_attribute_definition(defn, ref_object_types)
        for defs in type_defs.values()
        for defn in defs
    ]

    if out_entries:
        start_index = (page - 1) * result_per_page + 1
        to_index = min(page * result_per_page, total)
    else:
        start_index = 1
        to_index = 0

    return {
        "objectEntries": out_entries,
        "objectTypeAttributes": object_type_attributes,
        "objectTypeId": 0,
        "objectTypeIsInherited": False,
        "abstractObjectType": _UNSCOPED_SEARCH_ABSTRACT_OBJECT_TYPE,
        "totalFilterCount": total,
        "startIndex": start_index,
        "toIndex": to_index,
        "pageObjectSize": len(out_entries),
        "pageNumber": page,
        "orderWay": (
            "descending"
            if str(order_way).upper() in ("DESC", "DESCENDING")
            else "ascending"
        ),
        "qlQuery": ql_query,
        "qlQuerySearchResult": True,
        "conversionPossible": False,
        "iql": ql_query,
        "iqlSearchResult": True,
        "pageSize": len(out_entries),
    }


def order_way_from_tree(tree) -> str:
    """Derive the envelope ``orderWay`` from an ALREADY-PARSED query tree (D-10).

    Takes a tree, never parses itself — the route (Task 3) parses ``qlQuery``
    exactly ONCE per request and reuses the same tree here and in
    ``execute_aql(..., tree=tree)`` (Task 0). ``_extract_order_by`` returns
    "ASC"/"DESC"/None (None when the query has no ORDER BY).
    """
    _, direction = _extract_order_by(tree)
    return "descending" if direction == "DESC" else "ascending"


def aql_order_way(aql_string: str) -> str:
    """String-convenience wrapper for unit tests / callers without a tree.

    NOT used by the route body — the route derives orderWay from the tree it
    already parsed (``order_way_from_tree``) to keep the single-parse
    contract (Task 0/Task 3).
    """
    return order_way_from_tree(parse(aql_string))


def parse_page_params(
    page: str | None, result_per_page: str | None, page_size: str | None
) -> tuple[int, int]:
    """Fail-fast page/resultPerPage/pageSize resolution (DEV-05, CONT-03).

    Returns ``(page, result_per_page)``. Defaults: page=1, result_per_page=25.
    Constraints: page >= 1; 1 <= result_per_page <= 500 (CONT-03 max).
    ``pageSize`` is a DEV-05 alias for ``resultPerPage``: used when
    ``resultPerPage`` is absent, must equal it when both are given. Every
    invalid input raises HTTPException(400) — the route declares these as
    plain str query params, so FastAPI 422 never fires for them (D-11).
    """
    if page is None:
        page_num = 1
    else:
        try:
            page_num = int(page)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="page: must be an integer")
        if page_num < 1:
            raise HTTPException(status_code=400, detail="page: must be >= 1")

    if result_per_page is None:
        rpp = page_size
    else:
        rpp = result_per_page
        if page_size is not None and page_size != result_per_page:
            raise HTTPException(
                status_code=400, detail="resultPerPage and pageSize conflict"
            )
    if rpp is None:
        rpp = 25
    else:
        try:
            rpp_int = int(rpp)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="resultPerPage: must be an integer")
        if rpp_int < 1 or rpp_int > 500:
            raise HTTPException(
                status_code=400,
                detail="resultPerPage: must be between 1 and 500",
            )
        rpp = rpp_int
    return page_num, rpp


def parse_include_flags(
    include_attributes: str | None, include_attributes_deep: str | None
) -> bool:
    """Resolve includeAttributes/includeAttributesDeep (D-05, AQL-02).

    Default True (Jira-compatible). Explicit "false"/"0" (case-insensitive)
    wins (D-05). Deep flag: 0/1 are treated as "include attributes"; values
    > 1 are ACCEPTED AND IGNORED per AQL-02 (still included); negative or
    non-numeric deep -> 400 (D-11 fail-fast). Both flags are always
    validated regardless of each other's value — includeAttributesDeep is
    checked even when includeAttributes="false" (WR-02: param-order-independent
    fail-fast).
    """
    result = True
    if include_attributes is not None:
        lowered = include_attributes.strip().lower()
        if lowered in ("false", "0"):
            result = False
        elif lowered not in ("true", "1", ""):
            raise HTTPException(
                status_code=400,
                detail="includeAttributes: must be true or false",
            )
    if include_attributes_deep is not None:
        try:
            deep = int(include_attributes_deep)
        except (TypeError, ValueError):
            raise HTTPException(
                status_code=400,
                detail="includeAttributesDeep: must be an integer",
            )
        if deep < 0:
            raise HTTPException(
                status_code=400,
                detail="includeAttributesDeep: must be >= 0",
            )
        # deep > 1: accepted and ignored (AQL-02) — still include attributes
    return result


def validate_attributes_param(
    raw: str | None, known_names: set[str]
) -> list[str] | None:
    """D-06 attributes param validation against the page types' resolved defs.

    None -> None (all attributes, Jira-compatible default). Empty list ->
    400 (objects.py ``_parse_attributes`` precedent). Unknown names -> 400
    listing them (allow-list only, never widens — T-204-05), UNLESS
    ``known_names`` is itself empty (CR-01: an empty result page has no
    resolved type defs to validate against — skip the unknown-name check so
    the request still returns the empty envelope instead of a false 400;
    a non-empty page with a genuinely unknown name still 400s). Returns the
    requested names lowercased, deduped, in input order.
    """
    if raw is None:
        return None
    names = [n.strip() for n in raw.split(",") if n.strip()]
    if not names:
        raise HTTPException(status_code=400, detail="attributes: empty list")
    if known_names:
        unknown = [n for n in names if n.lower() not in known_names]
        if unknown:
            raise HTTPException(
                status_code=400,
                detail=f"Unknown attributes: {unknown}",
            )
    seen: set[str] = set()
    requested: list[str] = []
    for name in names:
        lowered = name.lower()
        if lowered not in seen:
            seen.add(lowered)
            requested.append(lowered)
    return requested


aql_router = APIRouter(prefix="/aql", tags=["public-aql"])


def _filter_denied_attribute_defs(
    assembly, denied_ids: set[uuid.UUID]
) -> tuple[dict, dict]:
    """Split a page assembly into DENY-filtered definition maps (D-11).

    Returns ``(page_defs_by_object_id, filtered_type_defs)``, both **new** dicts
    of **new** lists holding only the defs whose ``attribute_type_id`` is not
    denied. A denied attribute type must be indistinguishable from a
    non-existent one, so it must leave BOTH the per-entry attributes and the
    top-level ``objectTypeAttributes`` (D-11); D-03's "the allow-list never
    filters objectTypeAttributes" still holds — this is a per-caller DENY, not
    the allow-list.

    Never mutate the assembly's own lists: ``assembly.type_defs[tid]`` IS the
    tier-cached ``schema["attribute_defs"]`` list from ``_get_type_schema``
    (600 s, shared across callers), and ``page_objects[*].attribute_defs`` may
    alias that very list object — an in-place edit would poison the shared
    cache for every caller and every future request.
    """
    denied_str = {str(type_id) for type_id in denied_ids}
    page_defs = {
        object_id: [
            definition
            for definition in (data.attribute_defs or [])
            if str(definition.get("attribute_type_id")) not in denied_str
        ]
        for object_id, data in assembly.page_objects.items()
    }
    type_defs = {
        type_id: [
            definition
            for definition in definitions
            if str(definition.get("attribute_type_id")) not in denied_str
        ]
        for type_id, definitions in assembly.type_defs.items()
    }
    return page_defs, type_defs


@aql_router.get("/objects")
async def public_aql_objects(
    qlQuery: str | None = Query(None, description="AQL query string"),
    page: str | None = Query(None, description="1-based page number"),
    resultPerPage: str | None = Query(None, description="Results per page (1-500, default 25)"),
    pageSize: str | None = Query(None, description="Alias for resultPerPage (DEV-05)"),
    includeAttributes: str | None = Query(None, description="false omits the attributes key (AQL-02)"),
    includeAttributesDeep: str | None = Query(None, description=">1 accepted and ignored (AQL-02)"),
    attributes: str | None = Query(None, description="Comma-separated attribute names (D-06)"),
    lang: str | None = Query(None, description="Language code (BCP-47) for translated status names and SELECT option values"),
    db: AsyncSession = Depends(get_db),
    current_user: UserResponse = Depends(require_public_hierarchy_read("objects")),
):
    # D-11 fail-fast param validation BEFORE the engine runs; these are plain
    # str query params so FastAPI 422 never fires — every helper raises 400.
    if not qlQuery or not qlQuery.strip():
        raise HTTPException(status_code=400, detail="qlQuery is required")
    if len(qlQuery) > 10000:
        raise HTTPException(status_code=400, detail="qlQuery exceeds 10000 characters")
    page_num, rpp = parse_page_params(page, resultPerPage, pageSize)
    include_attrs = parse_include_flags(includeAttributes, includeAttributesDeep)

    # Everything that parses/executes the query sits in ONE try so any parse
    # or validation error lands in the same 400 mapping (D-11/D-13). The
    # query string is parsed EXACTLY ONCE (Task 0's tree kwarg): the same
    # tree feeds orderWay derivation and the engine call.
    try:
        tree = parse(qlQuery)
        order_way = order_way_from_tree(tree)
        user_role = "admin" if current_user.is_superuser else "user"
        items, total = await execute_aql(
            db,
            aql_string=qlQuery,
            skip=(page_num - 1) * rpp,
            limit=rpp,
            user_role=user_role,
            user_id=str(current_user.id),
            is_superuser=current_user.is_superuser,
            tree=tree,
        )
    except AQLParseError as e:
        raise HTTPException(
            status_code=400,
            detail=f"AQL parse error at line {e.line}, column {e.column}: {e}",
        )
    except AQLValidationError as e:
        raise HTTPException(status_code=400, detail=f"AQL validation error: {e}")
    except ValueError as e:
        # Backstop (317-04 Task 4): a ValueError from the filter layer (an
        # operator its type dispatch does not implement) rides this realm's
        # existing AQL-error envelope — 400, the same shape AQLValidationError
        # produces here — so a valid-looking query never answers 500.
        raise HTTPException(status_code=400, detail=f"AQL validation error: {e}")

    # D-04: same assembly path as GET /object/{id} — batched page assembly.
    # lang (223-05 D-13) resolves statuses + SELECT values page-wide; the
    # page may span schemas, so enabledness resolves per-entity (222 D-05).
    assembly = await load_objects_read(db, items, lang)

    # D-06: attributes filter — allow-list against the page types' resolved
    # defs; assembly.type_defs MUST stay unfiltered (objectTypeAttributes
    # always shows all defs — D-03). Reassignment (not in-place mutation)
    # keeps the shared type_defs lists intact.
    if attributes is not None:
        known = {
            d["name"].lower()
            for defs in assembly.type_defs.values()
            for d in defs
        }
        requested = validate_attributes_param(attributes, known)
        if requested is not None:
            keep = {a.lower() for a in requested}
            for data in assembly.page_objects.values():
                data.attribute_defs = [
                    d for d in data.attribute_defs if d["name"].lower() in keep
                ]

    # D-11: attribute-type record DENY, per caller. The filter runs HERE —
    # after the allow-list block (which must keep seeing every known name) and
    # BEFORE the shape builder, because ``build_object_shape`` reads
    # ``data.attribute_defs`` and the envelope is built at the very end: a
    # filter after the reference strip would leave the denied attribute inside
    # the already-built ``entries``. The page's whole distinct attribute-type
    # set is resolved ONCE through the same cache-through decision the object
    # route uses, so a cached decision means the same thing on both surfaces.
    envelope_type_defs = assembly.type_defs
    if not current_user.is_superuser:
        role_ids = await resolve_principal_roles(db, str(current_user.id), False)
        page_attribute_type_ids: set[uuid.UUID] = set()
        for data in assembly.page_objects.values():
            for definition in data.attribute_defs or []:
                raw_type_id = definition.get("attribute_type_id")
                if not raw_type_id:
                    continue
                try:
                    page_attribute_type_ids.add(uuid.UUID(str(raw_type_id)))
                except (TypeError, ValueError):
                    continue
        denied_ids = await load_denied_attribute_type_ids(
            db,
            page_attribute_type_ids,
            str(current_user.id),
            is_superuser=False,
            role_ids=role_ids,
        )
        page_defs, envelope_type_defs = _filter_denied_attribute_defs(
            assembly, denied_ids
        )
        for object_id, definitions in page_defs.items():
            assembly.page_objects[object_id].attribute_defs = definitions

    entries = [
        build_object_shape(assembly.page_objects[item_id])
        for item_id in assembly.page_objects
    ]
    # quick 261012-blo: per-caller referenced-object filter, page-wide (one
    # batched visibility check, only when the page loaded referenced targets).
    # Runs before the envelope builder, so it holds regardless of
    # includeAttributes.
    ref_object_types = assembly.ref_object_types
    if not current_user.is_superuser and ref_object_types:
        visible = await load_visible_reference_targets(
            db,
            [uuid.UUID(oid) for oid in assembly.page_objects],
            str(current_user.id),
            is_superuser=False,
        )
        if visible is not None:
            entries = [strip_hidden_references(entry, visible) for entry in entries]
            visible_type_ids = set(visible.values())
            ref_object_types = {
                type_id: ot for type_id, ot in ref_object_types.items()
                if type_id in visible_type_ids
            }
    return build_aql_envelope(
        entries=entries,
        type_defs=envelope_type_defs,
        ref_object_types=ref_object_types,
        total=total,
        page=page_num,
        result_per_page=rpp,
        order_way=order_way,
        ql_query=qlQuery,
        include_attributes=include_attrs,
    )
