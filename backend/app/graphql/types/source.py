import strawberry
from typing import Optional
from app.graphql.types.scalars import UUID, DateTime, JSON


@strawberry.type
class Source:
    id: UUID
    name: str
    # CORESEAM-01 (Phase 289 Plan 03): the connector identity is an open
    # lowercase string, projected verbatim. It is deliberately NOT an enum —
    # an unknown/historical identifier (e.g. from a deactivated plugin) must
    # stay readable and must never be resolved against the registry.
    type: str
    source_url: Optional[str] = strawberry.UNSET
    config: Optional[JSON] = strawberry.UNSET
    import_folder: Optional[str] = strawberry.UNSET
    created_at: DateTime
    updated_at: DateTime