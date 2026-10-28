from __future__ import annotations

import strawberry
from typing import Optional

from app.graphql.types.scalars import UUID, DateTime
from app.graphql.types.schema import Schema
from app.graphql.types.icon import Icon
from app.graphql.types.object_type_attribute import ObjectTypeAttribute

# Resolvers imported at TOP of file (before class body).
from app.graphql.resolvers.field_resolvers import (
    resolve_object_type_schema,
    resolve_object_type_parent,
    resolve_object_type_children,
    resolve_object_type_icon,
    resolve_inherited_attributes,
)


@strawberry.type
class ObjectType:
    # Private FK attributes — populated by mappers.object_type_from_orm.
    _schema_id: strawberry.Private[UUID]
    _parent_id: strawberry.Private[Optional[UUID]]
    _icon_id: strawberry.Private[Optional[UUID]]

    # Scalar fields
    id: UUID
    inherits_attributes: bool
    is_abstract: bool
    name: str
    description: Optional[str] = strawberry.UNSET
    sort: int
    label_attribute_id: Optional[UUID] = strawberry.UNSET
    created_at: DateTime
    updated_at: DateTime

    # Single-parent field — converted from required no-resolver to Optional
    # DataLoader-backed (D-182-P-01).
    schema: Optional[Schema] = strawberry.field(resolver=resolve_object_type_schema)

    # Optional relationship fields
    parent: Optional[ObjectType] = strawberry.field(resolver=resolve_object_type_parent)
    icon: Optional[Icon] = strawberry.field(resolver=resolve_object_type_icon)

    # List relationship fields — default_factory=list REPLACED with resolver=.
    children: list[ObjectType] = strawberry.field(resolver=resolve_object_type_children)

    # New field — inherited_attributes resolves via the parent-chain CTE
    # (D-182-P-02 / D-182-P-03).  Import the GraphQL type lazily in the
    # annotation to avoid a module-level circular import back into
    # object_type_attribute.py (which imports ObjectType for defining_object_type_name).
    inherited_attributes: list[
        Optional[ObjectTypeAttribute]
    ] = strawberry.field(resolver=resolve_inherited_attributes)
