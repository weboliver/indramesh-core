import asyncio
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from fastapi import Depends, Request, Response
from graphql import GraphQLError
from sqlalchemy import text
from strawberry.dataloader import DataLoader
from strawberry.fastapi import BaseContext

from app.config import settings
from app.db import get_db
from app.graphql.loaders.id_loaders import (
    attribute_type_by_id_loader,
    group_by_id_loader,
    icon_by_id_loader,
    object_by_id_loader,
    object_type_attribute_by_id_loader,
    object_type_by_id_loader,
    reference_type_by_id_loader,
    schema_by_id_loader,
    source_by_id_loader,
    user_by_id_loader,
)
from app.graphql.loaders.relationship_loaders import (
    classification_labels_by_attribute_id_loader,
    classification_labels_by_object_id_loader,
    inherited_attributes_by_object_type_id_loader,
    object_refs_by_from_object_id_loader,
    object_refs_by_to_object_id_loader,
    object_types_by_parent_id_loader,
    object_types_by_schema_id_loader,
    object_values_by_object_id_loader,
)
from app.schemas.auth import UserResponse
from app.services.auth_service import (
    require_permission,
    require_public_hierarchy_read,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class DataLoaders:
    """Request-scoped DataLoader instances for all GraphQL entity types.

    All 18 loaders are created per-request and cached for the duration of
    the GraphQL query.  By-ID loaders can be primed after list queries to
    avoid duplicate DB hits in resolver chains.
    """

    objects: DataLoader
    object_types: DataLoader
    schemas: DataLoader
    sources: DataLoader
    icons: DataLoader
    reference_types: DataLoader
    object_values_by_object: DataLoader
    object_refs_by_from_object: DataLoader
    object_refs_by_to_object: DataLoader
    object_types_by_schema: DataLoader
    object_types_by_parent: DataLoader
    attribute_types: DataLoader
    object_type_attributes: DataLoader
    inherited_attributes: DataLoader
    users: DataLoader
    groups: DataLoader
    # One label-read per target kind, shared by all four classification fields
    # on that target (D-07, T-317.1-15).
    classification_labels_by_attribute: DataLoader
    classification_labels_by_object: DataLoader


@dataclass
class GraphQLContext(BaseContext):
    request: Request
    response: Response
    user: UserResponse
    session: "AsyncSession"
    loaders: DataLoaders | None = None
    # Requested read language (223-05 D-13): read from the X-Lang header by
    # get_public_context. GraphQL has no URL query params, so the header IS
    # the lang channel. None -> base-language reads (byte-identical to
    # pre-phase output). Validated against the language table at resolve
    # time (unknown/inactive -> the 400-convention error envelope).
    lang: str | None = None
    # Non-fatal errors a resolver wants surfaced in the response's errors[]
    # array without nulling the field's data (e.g. graceful ES degradation).
    # Appended to the execution result by SoftErrorExtension (see
    # app/graphql/extensions.py).
    soft_errors: list[GraphQLError] = field(default_factory=list)
    # Per-request memo of list fields already reported as truncated
    # (GQLC-03/D-19): ``cap_list`` appends ONE LIST_TRUNCATED soft error per
    # field NAME per request — however many parents a document truncated —
    # and records the field here so later parents stay silent. Without it a
    # request over 1,000 over-cap parents would put 1,000 near-identical
    # entries into errors[] on exactly the request the cap exists to bound.
    truncated_fields: dict[str, GraphQLError] = field(default_factory=dict)
    # graphql-core resolves sibling/list fields concurrently via
    # asyncio.gather, but `session` above is a single AsyncSession shared by
    # every resolver and DataLoader for the request — AsyncSession rejects
    # overlapping operations from concurrent coroutines. Every direct
    # `session.execute()`/DataLoader batch dispatch acquires this lock first
    # to serialize access.
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Created lazily by app.graphql.record_visibility on the first non-superuser check (per-request memo).
    record_visibility: DataLoader | None = None

    async def prime_loader(
        self,
        loader: DataLoader,
        keys: list,
        entities: list,
    ) -> None:
        """Prime a by-ID DataLoader with already-loaded entities.

        For each pair in ``zip(keys, entities)``, calls ``loader.prime(key, entity)``
        so subsequent ``loader.load(key)`` calls return the cached value without
        a DB query.

        This is intended for use after list queries — e.g., after loading a list
        of Objects, prime the ``object_by_id_loader`` so resolver chains that look
        up individual objects by ID get the cached result.
        """
        for key, entity in zip(keys, entities):
            if entity is not None:
                loader.prime(key, entity)


async def build_graphql_context(
    user: UserResponse,
    session: "AsyncSession",
    *,
    request: Request,
    response: Response,
    lang: str | None = None,
) -> GraphQLContext:
    """Build the request-scoped GraphQLContext shared by every entry point.

    Both HTTP context getters below and the REST saved-query execute route
    build their context through this single helper, so the 18 DataLoaders, the
    concurrency lock, the caller identity and the session are identical no
    matter how execution is triggered (304 D-04/D-05). Execution therefore
    always runs as the calling user — record permissions wired from
    ``user.id``/``user.is_superuser`` apply by construction.

    This is also the ONE seam for the GQLC-04/D-18 statement timeout: the
    ``SET LOCAL`` below lands on all three execution paths at once. It is
    transaction-scoped, so it can never leak through the connection pool, and
    it works because SQLAlchemy's autobegin has already opened the request
    transaction — the GraphQL schema is query-only (no Mutation), so no
    resolver commit can end that transaction mid-request. The value is
    interpolated as a plain INTEGER of milliseconds: Postgres rejects bind
    parameters in ``SET``, and the ``int`` field type is what keeps SQL text
    out of the statement. The two steps that run BEFORE this builder (the
    auth/user lookup on the HTTP paths, the ``SavedQuery`` row read on the
    saved-query path) stay deliberately outside the timeout — single-row
    lookups, called out in 312-RESEARCH.md §Pitfall 10.
    """
    await session.execute(
        text(f"SET LOCAL statement_timeout = {int(settings.graphql_statement_timeout)}")
    )
    lock = asyncio.Lock()
    return GraphQLContext(
        request=request,
        response=response,
        user=user,
        session=session,
        lang=lang,
        lock=lock,
        loaders=DataLoaders(
            objects=object_by_id_loader(session, lock),
            object_types=object_type_by_id_loader(session, lock),
            schemas=schema_by_id_loader(session, lock),
            sources=source_by_id_loader(session, lock),
            icons=icon_by_id_loader(session, lock),
            reference_types=reference_type_by_id_loader(session, lock),
            object_values_by_object=object_values_by_object_id_loader(
                session, user.id, user.is_superuser, lock
            ),
            object_refs_by_from_object=object_refs_by_from_object_id_loader(
                session, lock, is_superuser=user.is_superuser
            ),
            object_refs_by_to_object=object_refs_by_to_object_id_loader(
                session, lock, is_superuser=user.is_superuser
            ),
            object_types_by_schema=object_types_by_schema_id_loader(session, lock),
            object_types_by_parent=object_types_by_parent_id_loader(session, lock),
            attribute_types=attribute_type_by_id_loader(session, lock),
            object_type_attributes=object_type_attribute_by_id_loader(session, lock),
            inherited_attributes=inherited_attributes_by_object_type_id_loader(
                session, user.id, user.is_superuser, lock
            ),
            users=user_by_id_loader(session, lock),
            groups=group_by_id_loader(session, lock),
            classification_labels_by_attribute=classification_labels_by_attribute_id_loader(
                session, lock
            ),
            classification_labels_by_object=classification_labels_by_object_id_loader(
                session, lock
            ),
        ),
    )


async def get_context(
    request: Request,
    response: Response,
    user: UserResponse = Depends(require_permission("objects", "read")),
    db_session: "AsyncSession" = Depends(get_db),
) -> GraphQLContext:
    """Internal GraphQL context getter (D-06, 317-04).

    The whole request is refused without the type-level ``objects:read`` grant
    the REST read routes require. One gate here is the cheapest correct option:
    every consumer of the internal schema reads objects, and a per-field check
    is exactly the kind of thing a new resolver forgets. Record-level
    filtering stays in force alongside it — this gate only decides whether the
    caller may read objects at all.
    """
    return await build_graphql_context(
        user, db_session, request=request, response=response
    )


async def get_public_context(
    request: Request,
    response: Response,
    user: UserResponse = Depends(require_public_hierarchy_read("objects")),
    db_session: "AsyncSession" = Depends(get_db),
) -> GraphQLContext:
    """Public GraphQL context getter (D-06, 317-04).

    Reuses the existing public-realm hierarchy gate instead of inventing a
    second mechanism: a service account without ``objects:read`` gets the
    same 403 detail the public read routes produce.
    """
    # D-13 (223-05): the public GraphQL lang channel is the X-Lang header —
    # GraphQL has no URL query params. Validation happens at resolve time via
    # the language-table machinery (unknown/inactive -> 400-convention error).
    return await build_graphql_context(
        user,
        db_session,
        request=request,
        response=response,
        lang=request.headers.get("X-Lang"),
    )
