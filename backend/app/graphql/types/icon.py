import strawberry
from typing import Optional
from app.graphql.types.scalars import UUID, DateTime


@strawberry.type
class Icon:
    id: UUID
    name: str
    local_name: Optional[str] = strawberry.UNSET
    url: str
    type: str
    created_at: DateTime
    updated_at: DateTime