import strawberry
from typing import Optional
from app.graphql.types.scalars import UUID, DateTime, JSON
from app.graphql.types.object_type_attribute import ObjectTypeAttribute

# Resolvers imported at TOP of file (before class body) because
# strawberry.field(resolver=...) evaluates the RHS at class-definition
# time — importing after the class body would cause NameError.
from app.graphql.resolvers.field_resolvers import (
    resolve_object_value_attribute,
    resolve_object_value_group_name,
    resolve_object_value_user_name,
)


@strawberry.type
class ObjectValue:
    # Private FK attribute — populated by mappers.object_value_from_orm,
    # consumed by resolve_object_value_attribute, NEVER exposed in the
    # GraphQL schema (the public object_type_attribute_id stays available).
    _object_type_attribute_id: strawberry.Private[UUID]

    id: UUID
    object_type_attribute_id: UUID
    value_float: Optional[float] = strawberry.UNSET
    value_integer: Optional[int] = strawberry.UNSET
    value_boolean: Optional[bool] = strawberry.UNSET
    value_simple: Optional[str] = strawberry.UNSET
    value_datetime: Optional[DateTime] = strawberry.UNSET
    value_struct: Optional[JSON] = strawberry.UNSET
    created_at: DateTime
    updated_at: DateTime

    # Single-parent field — DataLoader-backed attribute definition
    # (Pattern A: Private FK → by-id DataLoader → mapper).
    attribute: Optional[ObjectTypeAttribute] = strawberry.field(
        resolver=resolve_object_value_attribute
    )

    # D-17: resolved USER/GROUP display name. Both parse value_simple as a
    # UUID and check the attribute's data_type via the object_type_attributes
    # + attribute_types DataLoaders (same chain resolve_object_value_attribute
    # uses); null for every other data_type or an unresolvable UUID — never
    # fabricated.
    user_name: Optional[str] = strawberry.field(resolver=resolve_object_value_user_name)
    group_name: Optional[str] = strawberry.field(resolver=resolve_object_value_group_name)
