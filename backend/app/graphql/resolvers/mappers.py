"""ORM → GraphQL mapping helpers.

Each *_from_orm function constructs a Strawberry GraphQL type instance from
an ORM row or dict, normalising DB-lowercase enum values to their Strawberry
(uppercase) counterparts (D-182-P-04).

CRITICAL: ALL type imports are LAZY — done inside the function body, not at
module level — to break the type → field_resolvers → mappers → type cycle.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from app.graphql.types.object import Object
    from app.graphql.types.object_type import ObjectType
    from app.graphql.types.schema import Schema
    from app.graphql.types.source import Source
    from app.graphql.types.object_value import ObjectValue
    from app.graphql.types.object_ref import ObjectRef


def _or_none(value):
    """Return None if value is None, else the value.

    For nullable scalar fields the GraphQL type declares as
    ``Optional[...] = strawberry.UNSET``, passing ``None`` is the correct
    serialisation: the UNSET sentinel is only the constructor default, not
    a valid runtime value.  Emitting UNSET at runtime causes GraphQL scalar
    serialisers to fail with ``Boolean cannot represent a non boolean value``
    or ``'UnsetType' object has no attribute 'isoformat'``.
    """
    return None if value is None else value


def _attr(orm, key, is_dict):
    """Get an attribute from an ORM object or a dict."""
    return orm[key] if is_dict else getattr(orm, key)


def _optional_attr(orm, key, is_dict):
    """Get an optional attribute from an ORM object or a dict."""
    return orm.get(key) if is_dict else getattr(orm, key, None)


def _attr_uuid(orm, key, is_dict):
    """Get an id-typed attribute, coerced to uuid.UUID.

    list_objects/list_object_types cache their results via jsonable_encoder,
    so a cache-hit dict carries ids as plain strings while a cache-miss ORM
    row carries real uuid.UUID objects. DataLoader keys (root.id,
    root._object_type_id, etc.) must be uuid.UUID to match the by-id
    DataLoader's ORM-sourced result_map keys — a str/UUID mismatch causes
    keyed_results() to silently return None for every key.
    """
    value = _attr(orm, key, is_dict)
    if value is None or isinstance(value, uuid.UUID):
        return value
    return uuid.UUID(value)


def object_from_orm(orm):
    """Construct a GraphQL Object from an ORM row or cached dict.

    Populates:
    - All native scalar fields (id, key, name, timestamps)
    - _schema_id and _object_type_id as Private attrs
      (the client never sees these; the resolver fetches the full
      Schema/ObjectType via DataLoader)
    """
    from app.graphql.types.object import Object

    _d = isinstance(orm, dict)

    return Object(
        id=_attr_uuid(orm, "id", _d),
        _schema_id=_attr_uuid(orm, "schema_id", _d),
        _object_type_id=_attr_uuid(orm, "object_type_id", _d),
        name=_attr(orm, "name", _d),
        key=_or_none(_optional_attr(orm, "key", _d)),
        created_at=_attr(orm, "created_at", _d),
        updated_at=_attr(orm, "updated_at", _d),
    )


def object_type_from_orm(orm):
    """Construct a GraphQL ObjectType from an ORM row or cached dict."""
    from app.graphql.types.object_type import ObjectType

    _d = isinstance(orm, dict)

    return ObjectType(
        id=_attr_uuid(orm, "id", _d),
        _schema_id=_attr_uuid(orm, "schema_id", _d),
        _parent_id=_attr_uuid(orm, "parent_id", _d),
        _icon_id=_attr_uuid(orm, "icon_id", _d),
        name=_attr(orm, "name", _d),
        description=_or_none(_attr(orm, "description", _d)) if not _d else _or_none(orm.get("description")),
        sort=_attr(orm, "sort", _d),
        inherits_attributes=_attr(orm, "inherits_attributes", _d),
        is_abstract=_attr(orm, "is_abstract", _d),
        label_attribute_id=_attr(orm, "label_attribute_id", _d) if not _d else orm.get("label_attribute_id"),
        created_at=_attr(orm, "created_at", _d),
        updated_at=_attr(orm, "updated_at", _d),
    )


def schema_from_orm(orm):
    """Construct a GraphQL Schema from an ORM row or cached dict."""
    from app.graphql.types.schema import Schema

    _d = isinstance(orm, dict)

    return Schema(
        id=_attr_uuid(orm, "id", _d),
        _source_id=_attr_uuid(orm, "source_id", _d),
        key=_or_none(_attr(orm, "key", _d)) if not _d else _or_none(orm.get("key")),
        name=_attr(orm, "name", _d),
        description=_or_none(_attr(orm, "description", _d)) if not _d else _or_none(orm.get("description")),
        primary_color=_attr(orm, "primary_color", _d) if not _d else orm.get("primary_color"),
        header_bg_color=_attr(orm, "header_bg_color", _d) if not _d else orm.get("header_bg_color"),
        sidebar_bg_color=_attr(orm, "sidebar_bg_color", _d) if not _d else orm.get("sidebar_bg_color"),
        section_bg_color=_attr(orm, "section_bg_color", _d) if not _d else orm.get("section_bg_color"),
        surface_bg_color=_attr(orm, "surface_bg_color", _d) if not _d else orm.get("surface_bg_color"),
        dark_primary_color=_attr(orm, "dark_primary_color", _d) if not _d else orm.get("dark_primary_color"),
        dark_header_bg_color=_attr(orm, "dark_header_bg_color", _d) if not _d else orm.get("dark_header_bg_color"),
        dark_sidebar_bg_color=_attr(orm, "dark_sidebar_bg_color", _d) if not _d else orm.get("dark_sidebar_bg_color"),
        dark_section_bg_color=_attr(orm, "dark_section_bg_color", _d) if not _d else orm.get("dark_section_bg_color"),
        dark_surface_bg_color=_attr(orm, "dark_surface_bg_color", _d) if not _d else orm.get("dark_surface_bg_color"),
        status_filter_config=_or_none(_attr(orm, "status_filter_config", _d)) if not _d else _or_none(orm.get("status_filter_config")),
        created_at=_attr(orm, "created_at", _d),
        updated_at=_attr(orm, "updated_at", _d),
    )


def source_from_orm(orm):
    """Construct a GraphQL Source from an ORM row or cached dict.

    CORESEAM-01 (Phase 289 Plan 03): the persisted connector identity is an
    open lowercase string and is projected VERBATIM — no enum construction and
    no registry lookup, so unknown/historical identifiers stay readable.
    """
    from app.graphql.types.source import Source

    _d = isinstance(orm, dict)
    return Source(
        id=_attr_uuid(orm, "id", _d),
        name=_attr(orm, "name", _d),
        type=_attr(orm, "type", _d),
        source_url=_or_none(_attr(orm, "source_url", _d)) if not _d else _or_none(orm.get("source_url")),
        config=_or_none(_attr(orm, "config", _d)) if not _d else _or_none(orm.get("config")),
        import_folder=_or_none(_attr(orm, "import_folder", _d)) if not _d else _or_none(orm.get("import_folder")),
        created_at=_attr(orm, "created_at", _d),
        updated_at=_attr(orm, "updated_at", _d),
    )


def object_value_from_orm(orm):
    """Construct a GraphQL ObjectValue from an ORM row."""
    from app.graphql.types.object_value import ObjectValue

    return ObjectValue(
        id=orm.id,
        _object_type_attribute_id=orm.object_type_attribute_id,
        object_type_attribute_id=orm.object_type_attribute_id,
        value_float=_or_none(orm.value_float),
        value_integer=_or_none(orm.value_integer),
        value_boolean=_or_none(orm.value_boolean),
        value_simple=_or_none(orm.value_simple),
        value_datetime=_or_none(orm.value_datetime),
        value_struct=_or_none(orm.value_struct),
        created_at=orm.created_at,
        updated_at=orm.updated_at,
    )


def object_ref_from_orm(orm):
    """Construct a GraphQL ObjectRef from an ORM row.

    Populates _object_type_id, _source_id as Private attrs so the resolvers
    can fetch the full ObjectType/Source via DataLoader.  from_object_id and
    to_object_id are public fields — no Private duplication needed.
    """
    from app.graphql.types.object_ref import ObjectRef

    return ObjectRef(
        id=orm.id,
        _object_type_id=orm.object_type_id,
        _source_id=orm.source_id,
        from_attribute_name=orm.from_attribute_name,
        created_at=orm.created_at,
        from_object_id=orm.from_object_id,
        to_object_id=orm.to_object_id,
    )


def object_type_attribute_from_orm(row: dict, data_type_db_value: str):
    """Construct a GraphQL ObjectTypeAttribute from a CTE result dict.

    Maps DB `data_type` value (lowercase, e.g. "double") to GraphQL
    AttributeTypeDataType (uppercase, e.g. AttributeTypeDataType.DOUBLE)
    via .upper() per D-182-P-04.
    """
    from app.graphql.types.object_type_attribute import ObjectTypeAttribute
    from app.graphql.types.enums import AttributeTypeDataType

    return ObjectTypeAttribute(
        id=row["id"],
        name=row["name"],
        description=_or_none(row.get("description")),
        sort=row["sort"],
        data_type=AttributeTypeDataType(data_type_db_value.upper()),
        is_required=row["is_required"],
        minimum_cardinality=row["minimum_cardinality"],
        maximum_cardinality=row["maximum_cardinality"],
        is_unique=row["is_unique"],
        is_hidden=row["is_hidden"],
        default_value=_or_none(row.get("default_value")),
        structure=_or_none(row.get("structure")),
        defining_object_type_id=row["defining_object_type_id"],
        defining_object_type_name=row["defining_object_type_name"],
        is_inherited=bool(row["is_inherited"]),
        created_at=row.get("created_at"),
        updated_at=row.get("updated_at"),
    )
