# backend/app/graphql/loaders/__init__.py
"""DataLoader package for IndraMesh GraphQL — batching and caching for relationship resolvers."""

from app.graphql.loaders.id_loaders import (
    attribute_type_by_id_loader,
    icon_by_id_loader,
    object_by_id_loader,
    object_type_by_id_loader,
    reference_type_by_id_loader,
    schema_by_id_loader,
    source_by_id_loader,
)
from app.graphql.loaders.relationship_loaders import (
    object_refs_by_from_object_id_loader,
    object_refs_by_to_object_id_loader,
    object_types_by_parent_id_loader,
    object_types_by_schema_id_loader,
    object_values_by_object_id_loader,
)

__all__ = [
    "attribute_type_by_id_loader",
    "icon_by_id_loader",
    "object_by_id_loader",
    "object_refs_by_from_object_id_loader",
    "object_refs_by_to_object_id_loader",
    "object_type_by_id_loader",
    "object_types_by_parent_id_loader",
    "object_types_by_schema_id_loader",
    "object_values_by_object_id_loader",
    "reference_type_by_id_loader",
    "schema_by_id_loader",
    "source_by_id_loader",
]