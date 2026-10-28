"""Root Query type for the GraphQL schema (D-03).

All query fields live here and are registered on the schema via
``strawberry.Schema(query=Query)`` in ``schema.py``.  Each field delegates
to a resolver function in ``root_resolvers.py`` via ``strawberry.field(resolver=...)``.

Strawberry auto-maps snake_case Python arg names to camelCase in the SDL
(e.g. ``object_type_id`` → ``objectTypeId``, ``attribute_filters`` →
``attributeFilters``).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import List, Optional

import strawberry

from app.graphql.resolvers import root_resolvers
from app.graphql.resolvers.search_resolver import resolve_search
from app.graphql.types import (
    JSON,
    UUID,
    Health,
    Object,
    ObjectType,
    Schema,
    SearchResults,
    Source,
)
from app.version import __version__


def _resolve_health() -> Health:
    """Resolve the health field with live timestamp and the app version."""
    return Health(
        status="ok",
        timestamp=datetime.now(timezone.utc).isoformat(),
        version=__version__,
    )


@strawberry.type
class Query:
    health: Health = strawberry.field(resolver=_resolve_health)

    # ── Object fields ────────────────────────────────────────────

    object: Optional[Object] = strawberry.field(resolver=root_resolvers.resolve_object)
    objects: List[Object] = strawberry.field(resolver=root_resolvers.resolve_objects)

    # ── ObjectType fields ────────────────────────────────────────

    object_type: Optional[ObjectType] = strawberry.field(resolver=root_resolvers.resolve_object_type)
    object_types: List[ObjectType] = strawberry.field(resolver=root_resolvers.resolve_object_types)

    # ── Schema fields ────────────────────────────────────────────

    schema: Optional[Schema] = strawberry.field(resolver=root_resolvers.resolve_schema)
    schemas: List[Schema] = strawberry.field(resolver=root_resolvers.resolve_schemas)

    # ── Source fields ────────────────────────────────────────────

    source: Optional[Source] = strawberry.field(resolver=root_resolvers.resolve_source)
    sources: List[Source] = strawberry.field(resolver=root_resolvers.resolve_sources)

    # ── Search field ─────────────────────────────────────────────

    search: SearchResults = strawberry.field(resolver=resolve_search)
