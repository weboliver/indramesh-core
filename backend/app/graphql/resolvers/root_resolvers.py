"""Root resolver functions for the GraphQL Query type.

Each resolver delegates to the existing REST service layer (list_*/get_*)
and maps ORM rows through the Plan 01 mappers.  List resolvers apply
pagination clamping (D-05: default 50, cap 1000) and wire user_id/is_superuser
for record-permission filtering (D-08).  The by-id resolvers and the nested
relationship fields in field_resolvers.py share app.graphql.record_visibility,
which is batched per request and uses the same semantics as the list filter,
including the ancestor chain (quick 261011-tgu/261011-tv1, closes T-182-04).

Per D-182-P-10, every resolver with defaulted params places `info:
strawberry.types.Info` as the FIRST parameter.
"""

from __future__ import annotations

import uuid
from typing import TYPE_CHECKING

import strawberry
from strawberry.scalars import JSON
from strawberry.types import Info

from app.api.pagination import MAX_LIMIT, OBJECTS_DEFAULT_LIMIT
from app.graphql.record_visibility import record_visible
from app.graphql.resolvers.mappers import (
    object_from_orm,
    object_type_from_orm,
    schema_from_orm,
    source_from_orm,
)
from app.services.object_service import get_object, list_objects
from app.services.object_type_service import get_object_type, list_object_types
from app.services.schema_service import get_schema, list_schemas
from app.services.source_service import get_source, list_sources

if TYPE_CHECKING:
    from app.graphql.types.object import Object as GQLObject
    from app.graphql.types.object_type import ObjectType as GQLObjectType
    from app.graphql.types.schema import Schema as GQLSchema


async def resolve_object(info: Info, id: uuid.UUID) -> "GQLObject | None":
    """Resolve a single Object by UUID via the by-id DataLoader.

    Honours record-level read DENY, including a DENY on an ancestor, and
    returns null (invisible, mirrors the public REST 404).  A superuser
    bypasses the check.  Closes accepted risk T-182-04 (user decision
    2026-10-11, quick 261011-tgu).
    """
    orm = await info.context.loaders.objects.load(id)
    if orm is None or isinstance(orm, dict):
        return None
    if not await record_visible(info, "object", id):
        return None
    return object_from_orm(orm)


async def resolve_objects(
    info: Info,
    object_type_id: uuid.UUID | None = None,
    schema_id: uuid.UUID | None = None,
    limit: int = OBJECTS_DEFAULT_LIMIT,
    offset: int = 0,
    attribute_filters: JSON | None = strawberry.UNSET,
) -> list["GQLObject"]:
    """Resolve a paginated list of Objects with attribute filtering.

    D-05: limit defaults to OBJECTS_DEFAULT_LIMIT (50) and is capped at
    MAX_LIMIT (1000).  D-06/D-07/D-182-P-09: attribute_filters is a JSON
    scalar that delegates straight to attr_filters (no json.loads).
    D-08: user_id/is_superuser wired for record-permission filtering.

    Bypasses the DataLoader batching layer and calls the shared per-request
    session directly, so it acquires info.context.lock: graphql-core
    resolves sibling Query fields concurrently via asyncio.gather, and an
    AsyncSession rejects overlapping operations from concurrent coroutines.
    """
    effective_limit = min(limit, MAX_LIMIT)
    filters = attribute_filters if attribute_filters is not strawberry.UNSET else None
    async with info.context.lock:
        items, _ = await list_objects(
            info.context.session,
            skip=offset,
            limit=effective_limit,
            object_type_id=object_type_id,
            schema_id=schema_id,
            attr_filters=filters,
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    return [object_from_orm(o) for o in items]


async def resolve_object_type(
    info: Info, id: uuid.UUID
) -> "GQLObjectType | None":
    """Resolve a single ObjectType by UUID via the by-id DataLoader.

    Honours record-level read DENY, including a DENY on an ancestor, and
    returns null (invisible, mirrors the public REST 404).  A superuser
    bypasses the check.  Closes accepted risk T-182-04 (user decision
    2026-10-11, quick 261011-tgu).
    """
    orm = await info.context.loaders.object_types.load(id)
    if orm is None or isinstance(orm, dict):
        return None
    if not await record_visible(info, "object_type", id):
        return None
    return object_type_from_orm(orm)


async def resolve_object_types(
    info: Info,
    schema_id: uuid.UUID | None = None,
    limit: int = OBJECTS_DEFAULT_LIMIT,
    offset: int = 0,
) -> list["GQLObjectType"]:
    """Resolve a paginated list of ObjectTypes, optionally scoped to a Schema.

    D-05: limit capped at MAX_LIMIT. D-08: permission filter wired.

    Acquires info.context.lock — see resolve_objects docstring for why.
    """
    effective_limit = min(limit, MAX_LIMIT)
    async with info.context.lock:
        items, _ = await list_object_types(
            info.context.session,
            skip=offset,
            limit=effective_limit,
            schema_id=schema_id,
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    return [object_type_from_orm(ot) for ot in items]


async def resolve_schema(
    info: Info, id: uuid.UUID
) -> "GQLSchema | None":
    """Resolve a single Schema by UUID via the by-id DataLoader.

    Honours record-level read DENY, including a DENY on an ancestor, and
    returns null (invisible, mirrors the public REST 404).  A superuser
    bypasses the check.  Closes accepted risk T-182-04 (user decision
    2026-10-11, quick 261011-tgu).
    """
    orm = await info.context.loaders.schemas.load(id)
    if orm is None or isinstance(orm, dict):
        return None
    if not await record_visible(info, "schema", id):
        return None
    return schema_from_orm(orm)


async def resolve_schemas(
    info: Info,
    source_id: uuid.UUID | None = None,
    limit: int = OBJECTS_DEFAULT_LIMIT,
    offset: int = 0,
) -> list["GQLSchema"]:
    """Resolve a paginated list of Schemas, optionally scoped to a Source.

    D-05: limit capped at MAX_LIMIT. D-08: permission filter wired.

    Acquires info.context.lock — see resolve_objects docstring for why.
    """
    effective_limit = min(limit, MAX_LIMIT)
    async with info.context.lock:
        items, _ = await list_schemas(
            info.context.session,
            source_id=source_id,
            skip=offset,
            limit=effective_limit,
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    return [schema_from_orm(s) for s in items]


async def resolve_source(
    info: Info, id: uuid.UUID
) -> "GQLSource | None":
    """Resolve a single Source by UUID via the by-id DataLoader.

    Honours record-level read DENY, including a DENY on an ancestor, and
    returns null (invisible, mirrors the public REST 404).  A superuser
    bypasses the check.  Closes accepted risk T-182-04 (user decision
    2026-10-11, quick 261011-tgu).
    """
    orm = await info.context.loaders.sources.load(id)
    if orm is None or isinstance(orm, dict):
        return None
    if not await record_visible(info, "source", id):
        return None
    return source_from_orm(orm)


async def resolve_sources(
    info: Info,
    limit: int = OBJECTS_DEFAULT_LIMIT,
    offset: int = 0,
) -> list["GQLSource"]:
    """Resolve a paginated list of all Sources.

    D-05: limit capped at MAX_LIMIT. D-08: permission filter wired.

    Acquires info.context.lock — see resolve_objects docstring for why.
    """
    effective_limit = min(limit, MAX_LIMIT)
    async with info.context.lock:
        items, _ = await list_sources(
            info.context.session,
            skip=offset,
            limit=effective_limit,
            user_id=info.context.user.id,
            is_superuser=info.context.user.is_superuser,
        )
    return [source_from_orm(s) for s in items]
