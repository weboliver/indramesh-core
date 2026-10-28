import strawberry
from typing import Optional
from app.graphql.types.scalars import UUID, DateTime
from app.graphql.types.source import Source
from app.graphql.types.schema import Schema


@strawberry.type
class ReferenceType:
    id: UUID
    reference: str
    description: Optional[str] = strawberry.UNSET
    color: Optional[str] = strawberry.UNSET
    source: Source
    schema: Optional[Schema] = strawberry.UNSET
    created_at: DateTime
    updated_at: DateTime