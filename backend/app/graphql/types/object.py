from __future__ import annotations

import strawberry
from typing import Optional

from app.graphql.types.scalars import UUID, DateTime
from app.graphql.types.schema import Schema
from app.graphql.types.object_type import ObjectType
from app.graphql.types.object_value import ObjectValue
from app.graphql.types.object_ref import ObjectRef
from app.graphql.types.classification import ClassificationLabel

# Resolvers imported at TOP of file (before class body) because
# strawberry.field(resolver=...) evaluates the RHS at class-definition
# time — importing after the class body would cause NameError.
from app.graphql.resolvers.field_resolvers import (
    resolve_object_schema,
    resolve_object_object_type,
    resolve_object_values,
    resolve_object_references_from,
    resolve_object_references_to,
    resolve_object_classification_labels,
    resolve_object_confidentiality,
    resolve_object_personal_data,
    resolve_object_export_control,
)


@strawberry.type
class Object:
    # Private FK attributes — populated by mappers.object_from_orm,
    # consumed by the resolvers below, NEVER exposed in the GraphQL schema.
    _schema_id: strawberry.Private[UUID]
    _object_type_id: strawberry.Private[UUID]

    # Scalar fields
    id: UUID
    # 246 D-07: human-readable IndraMesh key (`keygen-Nummer`), schema-unique.
    key: Optional[str] = strawberry.UNSET
    name: str
    created_at: DateTime
    updated_at: DateTime

    # Single-parent fields — converted from required no-resolver to
    # Optional DataLoader-backed (D-182-P-01 Pattern A: Private FK → DataLoader)
    schema: Optional[Schema] = strawberry.field(resolver=resolve_object_schema)
    object_type: Optional[ObjectType] = strawberry.field(resolver=resolve_object_object_type)

    # List relationship fields — default_factory=list REPLACED with resolver=
    # (Strawberry forbids both simultaneously)
    values: list[ObjectValue] = strawberry.field(resolver=resolve_object_values)
    references_from: list[ObjectRef] = strawberry.field(resolver=resolve_object_references_from)
    references_to: list[ObjectRef] = strawberry.field(resolver=resolve_object_references_to)

    # Classification markers (D-07, CLASS-05): read-only, never filters or
    # write arguments — GraphQL has no mutations in this schema. The parent
    # object was already visibility-filtered, so no label of an invisible
    # record is reachable (T-317.1-14).
    classification_labels: list[ClassificationLabel] = strawberry.field(
        resolver=resolve_object_classification_labels
    )
    confidentiality: Optional[ClassificationLabel] = strawberry.field(
        resolver=resolve_object_confidentiality
    )
    personal_data: bool = strawberry.field(resolver=resolve_object_personal_data)
    export_control: list[ClassificationLabel] = strawberry.field(
        resolver=resolve_object_export_control
    )
