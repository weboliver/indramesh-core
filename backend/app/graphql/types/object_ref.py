import strawberry
from typing import Annotated, Optional

from app.graphql.types.scalars import UUID, DateTime
from app.graphql.types.object_type import ObjectType
from app.graphql.types.source import Source

# Resolvers imported at TOP of file (before class body).
from app.graphql.resolvers.field_resolvers import (
    resolve_object_ref_object_type,
    resolve_object_ref_source,
    resolve_object_ref_from_object,
    resolve_object_ref_to_object,
)


@strawberry.type
class ObjectRef:
    # Private FK attributes — populated by mappers.object_ref_from_orm.
    _object_type_id: strawberry.Private[UUID]
    _source_id: strawberry.Private[UUID]

    # Scalar fields
    id: UUID
    from_object_id: UUID
    to_object_id: UUID
    from_attribute_name: Optional[str] = strawberry.UNSET
    created_at: DateTime

    # Single-parent fields — converted from required no-resolver to Optional
    # DataLoader-backed (D-182-P-01).
    object_type: Optional[ObjectType] = strawberry.field(resolver=resolve_object_ref_object_type)
    source: Optional[Source] = strawberry.field(resolver=resolve_object_ref_source)

    # Object endpoints (D-182-P-05) — declared with the same lazy Annotated
    # pattern schema.py uses for its object_types field.  Because object.py
    # already imports ObjectRef from this module at module level, a plain
    # `Optional[Object]` would create a object.py ⇄ object_ref.py circular
    # import. The lazy import breaks the cycle.
    from_object: Optional[
        Annotated["Object", strawberry.lazy("app.graphql.types.object")]
    ] = strawberry.field(resolver=resolve_object_ref_from_object)

    to_object: Optional[
        Annotated["Object", strawberry.lazy("app.graphql.types.object")]
    ] = strawberry.field(resolver=resolve_object_ref_to_object)
