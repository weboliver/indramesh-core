"""Search resolver functions for the GraphQL schema.

Provides the main ``resolve_search`` root field resolver and the SearchHit
entity resolvers (``object_type`` / ``schema``) wired through DataLoaders.

This module follows the established Pattern A (Private FK → by-id DataLoader →
mapper) for SearchHit's single-parent fields, matching the same convention
used in ``field_resolvers.py`` for Object/ObjectType/Schema.
"""

from __future__ import annotations

import logging
import re
import uuid
from typing import TYPE_CHECKING

from graphql import GraphQLError
from sqlalchemy import select
from strawberry.types import Info

logger = logging.getLogger(__name__)

# Default pagination for the (currently unexposed) search field — kept as
# named constants so the value passed to search_cross_entity() and the
# page/pageSize reported back on SearchHitGroup can't drift apart (D-04
# defers real pagination arguments to a later phase).
_DEFAULT_PAGE = 1
_DEFAULT_PAGE_SIZE = 20

from app.graphql.resolvers import mappers
from app.models import Object
from app.models.object_type import ObjectType
from app.models.schema import Schema as SchemaModel
from app.services.record_permission_service import (
    build_record_permission_filter,
)
from app.services.search_service import search_cross_entity

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.graphql.types.object_type import ObjectType as GQLObjectType
    from app.graphql.types.schema import Schema as GQLSchema

# ---------------------------------------------------------------------------
# SearchHit entity resolvers (Pattern A: Private FK → DataLoader → mapper)
# ---------------------------------------------------------------------------


async def resolve_search_hit_object_type(
    root, info: Info
) -> "GQLObjectType | None":
    """Resolve SearchHit.object_type from root._object_type_id via DataLoader."""
    if root._object_type_id is None:
        return None
    orm_ot = await info.context.loaders.object_types.load(root._object_type_id)
    if orm_ot is None:
        return None
    return mappers.object_type_from_orm(orm_ot)


async def resolve_search_hit_schema(
    root, info: Info
) -> "GQLSchema | None":
    """Resolve SearchHit.schema from root._schema_id via DataLoader."""
    if root._schema_id is None:
        return None
    orm_schema = await info.context.loaders.schemas.load(root._schema_id)
    if orm_schema is None:
        return None
    return mappers.schema_from_orm(orm_schema)


# ---------------------------------------------------------------------------
# Highlight parsing
# ---------------------------------------------------------------------------

# Regex to locate <em>...</em> markers in ES highlight snippets
_EM_PATTERN = re.compile(r"<em>(.*?)</em>")


def _parse_highlights(
    raw_highlights: dict | None,
) -> list:
    """Transform ES highlight dict to structured HighlightFragment list.

    ES returns highlight data as ``{"field_name": ["<em>match</em> text ..."]}``.
    This function strips the ``<em>`` tags and builds ``HighlightFragment``
    objects with character-offset indices per D-26.
    """
    from app.graphql.types.search_types import HighlightFragment, HighlightMatch

    if not raw_highlights:
        return []

    fragments: list[HighlightFragment] = []
    for field_name, snippets in raw_highlights.items():
        if not isinstance(snippets, list):
            continue
        matches: list[HighlightMatch] = []
        for snippet in snippets:
            if not isinstance(snippet, str):
                continue
            # Build the stripped value by removing <em> and </em> tags
            stripped = snippet.replace("<em>", "").replace("</em>", "")
            # Find all match offsets in the ORIGINAL snippet to compute
            # offset-corrected indices relative to the stripped text.
            indices: list[int] = []
            for m in _EM_PATTERN.finditer(snippet):
                # How many <em> chars have been removed before this match?
                preceding = snippet[: m.start()]
                opened = preceding.count("<em>")
                closed = preceding.count("</em>")
                correction = opened * 4 + closed * 5
                adj_start = m.start() - correction
                adj_end = adj_start + len(m.group(1))
                indices.append(adj_start)
                indices.append(adj_end)
            if indices:
                matches.append(
                    HighlightMatch(value=stripped, indices=indices)
                )
        if matches:
            fragments.append(
                HighlightFragment(fieldName=field_name, matches=matches)
            )
    return fragments


# ---------------------------------------------------------------------------
# Permission filtering
# ---------------------------------------------------------------------------


async def _build_entity_map(
    db: "AsyncSession",
    entity_ids: list[uuid.UUID],
    model_class,
    entity_type: str,
    user_id: str,
    is_superuser: bool,
) -> set[uuid.UUID]:
    """Return the subset of ``entity_ids`` the user is permitted to see.

    Executes ``SELECT id FROM <table> WHERE id IN (:ids)`` optionally
    combined with a record-permission filter clause.

    Returns a ``set[uuid.UUID]`` of permitted IDs.
    """
    if not entity_ids:
        return set()

    query = select(model_class.id).where(model_class.id.in_(entity_ids))

    filter_clause = await build_record_permission_filter(
        db,
        user_id,
        entity_type,
        is_superuser=is_superuser,
    )
    if filter_clause is not None:
        query = query.where(filter_clause)

    result = await db.execute(query)
    return {row[0] for row in result.all()}


# ---------------------------------------------------------------------------
# SearchHit building
# ---------------------------------------------------------------------------


def _safe_uuid(value) -> uuid.UUID | None:
    """Best-effort UUID parse for ES-sourced string fields.

    ES documents are generally trustworthy (written by our own indexing
    code), but stale documents can survive a delete/recreate of the entity
    they describe (e.g. an ObjectType deleted and recreated with a new ID
    before the old ES doc was reindexed). Treat a malformed value as
    "unresolvable" rather than crashing the whole search query.
    """
    if not value:
        return None
    try:
        return uuid.UUID(value) if isinstance(value, str) else value
    except (ValueError, TypeError, AttributeError):
        logger.warning("Skipping malformed UUID in search hit: %r", value)
        return None


def _parse_hits(
    hits: list[dict],
    entity_type_hit_ids: set[uuid.UUID],
) -> list:
    """Convert raw ES hit dicts into SearchHit instances.

    Only hits whose IDs appear in ``entity_type_hit_ids`` (permission-
    filtered set) are included.
    """
    from app.graphql.types.search_types import SearchHit

    results: list[SearchHit] = []
    for hit in hits:
        uid = _safe_uuid(hit.get("id"))
        if uid is None or uid not in entity_type_hit_ids:
            continue

        results.append(
            SearchHit(
                id=uid,
                name=hit.get("name", ""),
                score=0.0,  # cross-entity search does not return _score
                highlights=_parse_highlights(hit.get("highlights")),
                _object_type_id=_safe_uuid(hit.get("object_type_id")),
                _schema_id=_safe_uuid(hit.get("schema_id")),
            )
        )
    return results


# ---------------------------------------------------------------------------
# Build SearchHitGroup from a single entity group
# ---------------------------------------------------------------------------


async def _build_group(
    search_result: dict,
    group_key: str,
    model_class,
    entity_type_str: str,
    info: Info,
) -> object:
    """Build a SearchHitGroup for one entity type from cross-entity results.

    Applies permission filtering, parses hits, returns SearchHitGroup.
    """
    from app.graphql.types.search_types import SearchHitGroup

    group = search_result.get(group_key, {"count": 0, "results": []})
    hits = group.get("results", [])

    # Collect hit IDs for permission filtering
    all_ids: list[uuid.UUID] = [
        uid for uid in (_safe_uuid(hit.get("id")) for hit in hits) if uid is not None
    ]

    # Permission filter
    user_id = str(info.context.user.id)
    is_superuser = info.context.user.is_superuser
    permitted = await _build_entity_map(
        info.context.session,
        all_ids,
        model_class,
        entity_type_str,
        user_id,
        is_superuser,
    )

    # Parse hits (filters by permitted set)
    results_parsed = _parse_hits(hits, permitted)

    # count reflects what the user can actually see, not the raw ES total —
    # returning the unfiltered ES count here would let a client infer the
    # existence of records it doesn't have permission to read (diff count
    # vs len(results)).
    return SearchHitGroup(
        count=len(results_parsed),
        page=_DEFAULT_PAGE,
        pageSize=_DEFAULT_PAGE_SIZE,
        results=results_parsed,
    )


# ---------------------------------------------------------------------------
# Root resolver
# ---------------------------------------------------------------------------


async def resolve_search(info: Info, query: str) -> object:
    """Root resolver for ``search(query: String!)`` on the Query type.

    Delegates to ``search_cross_entity()`` for the ES query, applies
    permission filtering, and maps results to GraphQL types.

    When ES is unavailable, returns empty results with a GraphQL error
    in ``errors[]`` (D-21/D-22). ``search_cross_entity()`` already swallows
    ES connection/query failures internally and reports them via the
    ``_degraded`` marker rather than raising, so that path is handled below
    by appending a soft error (see SoftErrorExtension) instead of raising —
    raising here would null the whole response since ``search`` is
    non-nullable. The except block below is a last-resort safety net for
    genuinely unexpected errors (e.g. a bug in permission filtering).
    """
    from app.graphql.types.search_types import SearchResults

    try:
        raw = await search_cross_entity(
            query,
            page=_DEFAULT_PAGE,
            page_size=_DEFAULT_PAGE_SIZE,
            user_id=str(info.context.user.id),
            is_superuser=info.context.user.is_superuser,
        )
    except Exception as exc:
        logger.warning("Unexpected error in search_cross_entity: %s", exc, exc_info=True)
        raise GraphQLError("Search service temporarily unavailable") from exc

    if raw.get("_degraded"):
        info.context.soft_errors.append(
            GraphQLError("Search backend is temporarily unavailable; results may be incomplete")
        )

    objects_group = await _build_group(
        raw, "objects", Object, "object", info
    )
    ots_group = await _build_group(
        raw, "object_types", ObjectType, "object_type", info
    )
    schemas_group = await _build_group(
        raw, "schemas", SchemaModel, "schema", info
    )

    return SearchResults(
        objects=objects_group,
        objectTypes=ots_group,
        schemas=schemas_group,
    )
