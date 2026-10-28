"""GraphQL type for an ObjectTypeAttribute — the full attribute definition
with name, data_type, constraints, and inheritance metadata.

D-182-P-02: New ObjectTypeAttribute GraphQL type (NOT the tiny AttributeType
catalog).  data_type uses the uppercase GraphQL enum (D-182-P-04).
"""

import strawberry
from typing import Optional

from app.graphql.types.scalars import UUID, DateTime, JSON
from app.graphql.types.enums import AttributeTypeDataType
from app.graphql.types.classification import ClassificationLabel

# Resolvers imported at TOP of file (before class body): strawberry.field(resolver=…)
# evaluates the RHS at class-definition time.
from app.graphql.resolvers.field_resolvers import (
    resolve_attribute_classification_labels,
    resolve_attribute_confidentiality,
    resolve_attribute_export_control,
    resolve_attribute_personal_data,
)


@strawberry.type
class ObjectTypeAttribute:
    id: UUID
    name: str
    description: Optional[str] = strawberry.UNSET
    sort: int
    data_type: AttributeTypeDataType
    is_required: bool
    minimum_cardinality: int
    maximum_cardinality: int
    is_unique: bool
    is_hidden: bool
    default_value: Optional[str] = strawberry.UNSET
    structure: Optional[JSON] = strawberry.UNSET
    # null when the defining object type is DENY'd for the caller (quick 261011-wfr)
    defining_object_type_id: Optional[UUID]
    defining_object_type_name: Optional[str]
    is_inherited: bool
    created_at: DateTime
    updated_at: DateTime
    # Classification markers (D-07, CLASS-02/CLASS-04): read-only, never filters
    # or write arguments — GraphQL has no mutations in this schema.
    classification_labels: list[ClassificationLabel] = strawberry.field(
        resolver=resolve_attribute_classification_labels
    )
    confidentiality: Optional[ClassificationLabel] = strawberry.field(
        resolver=resolve_attribute_confidentiality
    )
    personal_data: bool = strawberry.field(resolver=resolve_attribute_personal_data)
    export_control: list[ClassificationLabel] = strawberry.field(
        resolver=resolve_attribute_export_control
    )

