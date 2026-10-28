"""GraphQL types for cross-entity search results.

Defines the SearchResults wrapper type with per-entity SearchHitGroups,
SearchHit with DataLoader-backed entity resolvers, highlight fragments,
and the SearchHitType enum.

Per D-02/D-05/D-23-D-25:
- SearchHit entity fields resolve via existing DataLoaders (Pattern A)
- Highlights are structured HighlightFragment objects (not raw HTML)
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

import strawberry

from app.graphql.types.scalars import UUID
from app.graphql.types.schema import Schema
from app.graphql.types.object_type import ObjectType

# Resolvers imported at TOP of file (before class body) because
# strawberry.field(resolver=...) evaluates the RHS at class-definition
# time — importing after the class body would cause NameError.
from app.graphql.resolvers.search_resolver import (
    resolve_search_hit_object_type,
    resolve_search_hit_schema,
)


# ---------------------------------------------------------------------------
# SearchHitType enum  (D-04 discretion: defined but clients can also infer
# from which group the hit appears in)
# ---------------------------------------------------------------------------


@strawberry.enum
class SearchHitType(Enum):
    OBJECT = "OBJECT"
    OBJECT_TYPE = "OBJECT_TYPE"
    SCHEMA = "SCHEMA"


# ---------------------------------------------------------------------------
# Highlight types  (D-23/D-24/D-25)
# ---------------------------------------------------------------------------


@strawberry.type
class HighlightMatch:
    """A single matched term with its character offsets.

    ``indices`` is a flat list of [start, end, start, end, ...] pairs
    for each match occurrence within the snippet value.
    """

    value: str
    indices: list[int]


@strawberry.type
class HighlightFragment:
    """Highlights for a single ES document field.

    Contains one or more HighlightMatch entries — one per highlighted
    snippet returned by ES for this field.
    """

    fieldName: str
    matches: list[HighlightMatch]


# ---------------------------------------------------------------------------
# SearchHit  (D-05/D-06)
# ---------------------------------------------------------------------------


@strawberry.type
class SearchHit:
    """A single search result hit with rich entity resolution.

    Flat fields: id, name, score, highlights
    Resolver fields (via existing DataLoaders): objectType, schema

    Private ``_object_type_id`` and ``_schema_id`` are populated by the
    resolver and consumed by the DataLoader-backed field resolvers below.
    """

    # Private FK attributes — populated by _parse_hits, consumed by resolvers
    _object_type_id: strawberry.Private[Optional[UUID]] = None
    _schema_id: strawberry.Private[Optional[UUID]] = None

    # Scalar fields
    id: UUID
    name: str
    score: float
    highlights: list[HighlightFragment]

    # Single-parent resolver fields — Pattern A (Private FK → DataLoader)
    object_type: Optional[ObjectType] = strawberry.field(
        resolver=resolve_search_hit_object_type
    )
    schema: Optional[Schema] = strawberry.field(
        resolver=resolve_search_hit_schema
    )


# ---------------------------------------------------------------------------
# SearchHitGroup  (D-02/D-16/D-17)
# ---------------------------------------------------------------------------


@strawberry.type
class SearchHitGroup:
    """A group of search results for one entity type.

    Mirrors REST pagination: count is the total matching results across
    all pages, page and pageSize identify the current slice.
    """

    count: int
    page: int
    pageSize: int
    results: list[SearchHit]


# ---------------------------------------------------------------------------
# SearchResults  (D-02)
# ---------------------------------------------------------------------------


@strawberry.type
class SearchResults:
    """Top-level search result wrapper with per-entity-type groups."""

    objects: SearchHitGroup
    objectTypes: SearchHitGroup
    schemas: SearchHitGroup
