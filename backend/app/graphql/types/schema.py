import strawberry
from typing import Annotated, Optional, TYPE_CHECKING

from app.graphql.types.scalars import UUID, DateTime, JSON
from app.graphql.types.source import Source

# Resolvers imported at TOP of file (before class body).
from app.graphql.resolvers.field_resolvers import (
    resolve_schema_source,
    resolve_schema_object_types,
)

if TYPE_CHECKING:
    from app.graphql.types.object_type import ObjectType


@strawberry.type
class Schema:
    # Private FK attribute — populated by mappers.schema_from_orm.
    _source_id: strawberry.Private[UUID]

    # Scalar fields
    id: UUID
    key: Optional[str] = strawberry.UNSET
    name: str
    description: Optional[str] = strawberry.UNSET
    primary_color: str
    header_bg_color: str
    sidebar_bg_color: str
    section_bg_color: str
    surface_bg_color: str
    dark_primary_color: str
    dark_header_bg_color: str
    dark_sidebar_bg_color: str
    dark_section_bg_color: str
    dark_surface_bg_color: str
    status_filter_config: Optional[JSON] = strawberry.UNSET
    created_at: DateTime
    updated_at: DateTime

    # Single-parent field — converted from required no-resolver to Optional
    # DataLoader-backed (D-182-P-01).
    source: Optional[Source] = strawberry.field(resolver=resolve_schema_source)

    # List relationship field — Annotated lazy reference preserved,
    # default_factory=list REPLACED with resolver= (D-182-P-05 pattern).
    object_types: list[
        Annotated["ObjectType", strawberry.lazy("app.graphql.types.object_type")]
    ] = strawberry.field(resolver=resolve_schema_object_types)
