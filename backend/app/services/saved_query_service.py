import asyncio
import json
import uuid
from typing import List, Optional

from graphql import get_operation_ast, parse
from graphql.error import GraphQLSyntaxError
from graphql.language import OperationType as GqlOperationType
from graphql.language.ast import OperationDefinitionNode
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.models import SavedQuery
from app.schemas.auth import UserResponse
from app.schemas.crud import (
    SAVED_QUERIES_EXPORT_FORMAT,
    SAVED_QUERIES_EXPORT_VERSION,
    SavedQueriesExportDocument,
    SavedQueriesExportEntry,
    SavedQueriesImportResult,
    SavedQueryCreate,
    SavedQueryResponse,
    SavedQueryUpdate,
)
from app.services.cache_service import (
    build_cache_key,
    build_list_cache_key,
    build_tag_key,
    get_cached,
    set_cached_with_tags,
    invalidate_entity_cache,
    CACHE_TTL_LIST,
    CACHE_TTL_ENTITY,
)
from app.services.audit_service import audit_log, model_to_dict, AuditEntityType

ENTITY_TYPE = "saved_query"

# 304 (research Open Question 1): the outer size bound reuses the existing
# 1 MiB request-body ceiling (MaximumContentLengthMiddleware). This is
# defence-in-depth for the service/import boundary so a stored or imported
# document can never exceed what a REST request could carry.
MAX_SAVED_QUERY_TEXT_BYTES = 1024 * 1024
MAX_SAVED_QUERY_VARIABLES_BYTES = 1024 * 1024


class SavedQueryValidationError(ValueError):
    """A saved-query document failed server-side validation (304 D-03/D-10).

    ``path`` names the offending field (e.g. ``query_text`` or ``operation_name``)
    so the import envelope can report a ``queries[i].<field>`` path and the REST
    route can map the failure to HTTP 422. The message is a bounded,
    Core-authored reason — never raw graphql-core internals.
    """

    def __init__(
        self,
        reason: str,
        path: str = "query_text",
        extensions: dict | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.path = path
        # GQLC-05/D-07: a limit breach carries the SAME machine-readable fields
        # an execute-time rejection carries (code/limit/actual/allowed) so the
        # route's 422 body and the GraphQL error envelope share one shape. Every
        # other failure leaves this ``None`` and the payload keeps path+message.
        self.extensions = extensions


def validate_saved_query_document(
    query_text: str,
    *,
    operation_name: str | None = None,
    require_operation_name: bool = False,
) -> None:
    """Reject non-query and malformed GraphQL documents before they run (D-03).

    Shared by save, import and execute. Always enforced:
      * the text must parse (syntax errors -> 422);
      * the document must contain at least one executable operation;
      * every operation must be a query — any mutation or subscription
        definition (even one that would not be selected) is rejected.

    Only at execute time (``require_operation_name=True``) the operation-name
    rule applies: a document with several operations without ``operation_name``,
    or an ``operation_name`` that matches no operation, is rejected. Save/import
    have no operation name to check, so a multi-operation query-only document is
    saveable.

    Raises ``SavedQueryValidationError`` (mapped to HTTP 422 by the router).
    """
    try:
        document = parse(query_text)
    except GraphQLSyntaxError as exc:
        raise SavedQueryValidationError(
            f"Invalid GraphQL syntax: {exc.message}", path="query_text"
        ) from exc
    except RecursionError as exc:
        # graphql-core's parser is recursive, so a document nested past the
        # interpreter's recursion limit (~250 levels, ~1 KB) raises here without
        # being a syntax error. It is rejected like any other unparseable text
        # instead of escaping as an unhandled 500 on save/import/execute.
        raise SavedQueryValidationError(
            "Invalid GraphQL syntax: document nesting too deep", path="query_text"
        ) from exc

    operations = [
        definition
        for definition in document.definitions
        if isinstance(definition, OperationDefinitionNode)
    ]
    if not operations:
        raise SavedQueryValidationError(
            "Document contains no executable operation", path="query_text"
        )

    non_query = [
        operation
        for operation in operations
        if operation.operation is not GqlOperationType.QUERY
    ]
    if non_query:
        raise SavedQueryValidationError(
            "Only query operations may be saved or executed", path="query_text"
        )

    if operation_name is not None and get_operation_ast(document, operation_name) is None:
        raise SavedQueryValidationError(
            f"Unknown operation name: {operation_name}", path="operation_name"
        )
    if require_operation_name and len(operations) > 1 and operation_name is None:
        raise SavedQueryValidationError(
            "Document contains multiple operations; an operation name is required",
            path="operation_name",
        )


def validate_saved_query_size(query_text: str, variables: dict | None) -> None:
    """Bound query text and variables to the 1 MiB request-body ceiling.

    Defence-in-depth at the service/import boundary (the HTTP middleware caps
    live request bodies, but imported/execed documents bypass it).
    """
    if len(query_text.encode("utf-8")) > MAX_SAVED_QUERY_TEXT_BYTES:
        raise SavedQueryValidationError(
            "Query text exceeds the 1 MiB limit", path="query_text"
        )
    if variables is not None:
        try:
            encoded = json.dumps(variables)
        except (TypeError, ValueError) as exc:
            raise SavedQueryValidationError(
                "Variables are not JSON-serializable", path="variables"
            ) from exc
        if len(encoded.encode("utf-8")) > MAX_SAVED_QUERY_VARIABLES_BYTES:
            raise SavedQueryValidationError(
                "Variables exceed the 1 MiB limit", path="variables"
            )


def _raise_first_limit_error(query_text: str) -> None:
    """Reject an over-limit document with the shared contract payload (GQLC-05).

    Runs the SAME analyzer the execution path's schema rules are built from
    (``validate_document_limits`` → ``analyze_document`` over the very rule
    classes registered in ``_extensions``), so a rule change can never apply to
    only one of save and execute. The first violation is raised as a
    ``SavedQueryValidationError`` whose ``extensions`` carry the four
    machine-readable fields of an execute-time rejection — ``code``, ``limit``,
    ``actual``, ``allowed`` (D-07) — plus the human-readable ``message``.
    """
    # Lazy import mirrors execute_saved_query: the GraphQL package reaches the
    # service layer through its resolvers, so the service keeps its graphql
    # imports function-local.
    from app.graphql.limits import validate_document_limits

    for error in validate_document_limits(query_text):
        raise SavedQueryValidationError(
            error.message,
            path="query_text",
            extensions=dict(error.extensions or {}),
        )


async def create_saved_query(
    db: AsyncSession, query_data: SavedQueryCreate, actor_id: uuid.UUID
) -> SavedQuery:
    # D-03: never persist a document that would be rejected at execute time.
    validate_saved_query_document(query_data.query_text)
    validate_saved_query_size(query_data.query_text, query_data.variables)
    # GQLC-05/D-07: the write-side twin of the execute-time limit rejection —
    # a document that can never execute is never persisted either.
    _raise_first_limit_error(query_data.query_text)
    db_query = SavedQuery(
        name=query_data.name,
        query_text=query_data.query_text,
        variables=query_data.variables,
        created_by_id=actor_id,
    )
    db.add(db_query)
    await db.flush()
    new_values = model_to_dict(db_query)
    await audit_log(db, AuditEntityType.SAVED_QUERIES.value, db_query.id,
                    "CREATE", actor_id, old_values=None, new_values=new_values)
    await db.commit()
    await db.refresh(db_query)
    await invalidate_entity_cache(ENTITY_TYPE)
    return db_query


async def list_saved_queries(
    db: AsyncSession, skip: int = 0, limit: int = 100, name: str | None = None
) -> tuple[List[SavedQuery], int]:
    cache_key = build_list_cache_key(ENTITY_TYPE, skip=skip, limit=limit, name=name)
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached["items"], cached["total"]
    query = select(SavedQuery)
    count_query = select(func.count()).select_from(SavedQuery)
    if name:
        pattern = f"%{name}%"
        query = query.where(SavedQuery.name.ilike(pattern))
        count_query = count_query.where(SavedQuery.name.ilike(pattern))
    # 304: stable, deterministic ordering (name, then id) so paginated pages
    # never repeat or skip rows across refetches.
    query = query.order_by(SavedQuery.name, SavedQuery.id)
    count_result = await db.execute(count_query)
    total = count_result.scalar()
    result = await db.execute(query.offset(skip).limit(limit))
    items = result.scalars().all()
    # AGENTS.md hard rule: serialize through the Pydantic response model, never
    # jsonable_encoder(), so a computed @property on the ORM model is included
    # (jsonable_encoder silently drops it).
    await set_cached_with_tags(
        cache_key,
        {
            "items": [SavedQueryResponse.model_validate(item).model_dump() for item in items],
            "total": total,
        },
        tags=[build_tag_key(ENTITY_TYPE)],
        ttl=CACHE_TTL_LIST,
    )
    return items, total


async def get_saved_query(
    db: AsyncSession, query_id: uuid.UUID
) -> SavedQuery | dict | None:
    cache_key = build_cache_key(ENTITY_TYPE, str(query_id))
    cached = await get_cached(cache_key)
    if cached is not None:
        return cached
    result = await db.execute(
        select(SavedQuery).where(SavedQuery.id == query_id)
    )
    query_item = result.scalar_one_or_none()
    if query_item:
        # AGENTS.md hard rule: serialize through the Pydantic response model so
        # computed @property fields survive the cache round-trip.
        await set_cached_with_tags(
            cache_key,
            SavedQueryResponse.model_validate(query_item).model_dump(),
            tags=[build_tag_key(ENTITY_TYPE)],
            ttl=CACHE_TTL_ENTITY,
        )
    return query_item


async def update_saved_query(
    db: AsyncSession, query_id: uuid.UUID, update_data: SavedQueryUpdate, actor_id: uuid.UUID
) -> SavedQuery | None:
    result = await db.execute(
        select(SavedQuery).where(SavedQuery.id == query_id)
    )
    query_item = result.scalar_one_or_none()
    if not query_item:
        return None
    # D-03: validate the effective document BEFORE any mutation/commit so an
    # invalid query is never persisted and nothing is written on rejection.
    effective_text = (
        update_data.query_text if update_data.query_text is not None
        else query_item.query_text
    )
    effective_variables = (
        update_data.variables if "variables" in update_data.model_fields_set
        else query_item.variables
    )
    validate_saved_query_document(effective_text)
    validate_saved_query_size(effective_text, effective_variables)
    # GQLC-05/D-14: the limit check re-runs only when the stored document is
    # actually CHANGING. Both shipped UIs resend the unchanged ``query_text`` on
    # every PATCH (a pure rename included), so presence alone would turn every
    # rename of a legacy over-limit row into a 422 and trap the user in their
    # own query. A metadata-only edit therefore still succeeds, while the stored
    # over-limit document stays unexecutable (the execute-time rejection is
    # unconditional, D-13).
    if (
        update_data.query_text is not None
        and update_data.query_text != query_item.query_text
    ):
        _raise_first_limit_error(effective_text)
    old_values = model_to_dict(query_item)
    if update_data.name is not None:
        query_item.name = update_data.name
    if update_data.query_text is not None:
        query_item.query_text = update_data.query_text
    # 304: only assign `variables` when the payload actually set the field —
    # an omitted field must not null the stored defaults, while an explicit
    # `"variables": null` clears them.
    if "variables" in update_data.model_fields_set:
        query_item.variables = update_data.variables
    new_values = model_to_dict(query_item)
    await audit_log(db, AuditEntityType.SAVED_QUERIES.value, query_id,
                    "UPDATE", actor_id, old_values=old_values, new_values=new_values)
    await db.commit()
    await db.refresh(query_item)
    await invalidate_entity_cache(ENTITY_TYPE)
    return query_item


async def delete_saved_query(
    db: AsyncSession, query_id: uuid.UUID, actor_id: uuid.UUID
) -> bool:
    result = await db.execute(
        select(SavedQuery).where(SavedQuery.id == query_id)
    )
    query_item = result.scalar_one_or_none()
    if not query_item:
        return False
    old_values = model_to_dict(query_item)
    await audit_log(db, AuditEntityType.SAVED_QUERIES.value, query_id,
                    "DELETE", actor_id, old_values=old_values, new_values=None)
    await db.delete(query_item)
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return True


async def execute_saved_query(
    db: AsyncSession,
    query_id: uuid.UUID,
    request_variables: dict | None,
    operation_name: str | None,
    actor: UserResponse,
    *,
    request,
    response,
    lang: str | None = None,
) -> dict | None:
    """Run a stored query in-process against the Core schema (304 GQL-02).

    Loads the row directly from the database — never via ``get_saved_query``,
    which may return a cached dict with stale text. Returns ``None`` when the
    row is missing (the route maps that to 404).

    The document is pre-validated (query-only + operation-name rule, D-03);
    stored default variables are flat-merged with the request variables with
    the request winning (D-02); execution runs in the *caller's* GraphQL
    context (D-04 — no creator impersonation), inheriting the schema's limit
    rules (alias/depth/cost/tokens) and soft-error extension (D-05). The
    context builder is also where the transaction-scoped ``statement_timeout``
    is armed, so this path inherits the Postgres bound with no extra wiring
    (GQLC-04/D-18). GraphQL-level failures are returned as HTTP 200
    ``{data, errors}`` by the route (D-06).

    GQLC-04/D-17: this function calls ``schema.execute`` directly and never
    passes through ``LimitedGraphQLRouter``, so it arms the same wall clock
    itself. A timeout is a GraphQL-level failure inside the 200 envelope
    (``QUERY_TIMEOUT``/``wall_clock``, ``data`` null) — never a 504, and never
    a ``SavedQueryValidationError`` (the 422 stays reserved for an invalid
    document, so a limit/timeout outcome cannot become one, D-01/D-13).
    """
    result = await db.execute(select(SavedQuery).where(SavedQuery.id == query_id))
    query_item = result.scalar_one_or_none()
    if query_item is None:
        return None

    validate_saved_query_document(
        query_item.query_text,
        operation_name=operation_name,
        require_operation_name=True,
    )

    merged_variables = {**(query_item.variables or {}), **(request_variables or {})}

    # Lazy imports avoid an import cycle between the service layer and the
    # GraphQL package (which imports services through its resolvers).
    from app.graphql.context import build_graphql_context
    from app.graphql.limits import (
        CODE_QUERY_TIMEOUT,
        TIMEOUT_SOURCE_WALL_CLOCK,
        _caller_id,
        _log_limit_rejection,
        _request_path,
        timeout_result,
    )
    from app.graphql.schema import schema

    context = await build_graphql_context(
        actor, db, request=request, response=response, lang=lang
    )
    # The wall clock, armed here because this path owns no router (D-17/D-18).
    # The envelope on expiry comes from the shared ``timeout_result()`` builder
    # in app.graphql.limits — the same one the two HTTP routers use; a private
    # copy would give the third path its own error shape. The timeout value is
    # read from settings at CALL time, exactly like the router override.
    #
    # The context manager object is kept so its own expiry can be told apart
    # from an inner ``TimeoutError`` (the same class ``socket.timeout`` /
    # ``asyncio.TimeoutError`` use on 3.11+): only a timeout THIS wall clock
    # armed becomes the QUERY_TIMEOUT envelope; anything else propagates
    # untouched instead of being rewritten into a successful rejection.
    timeout = asyncio.timeout(settings.graphql_execution_timeout)
    try:
        async with timeout:
            execution_result = await schema.execute(
                query_item.query_text,
                variable_values=merged_variables,
                context_value=context,
                operation_name=operation_name,
            )
    except TimeoutError:
        if not timeout.expired():
            # An inner timeout, not the wall clock: propagate it untouched.
            raise
        # Same structured rejection record the routers emit (D-06/D-18): path
        # and caller UUID only, never any query text.
        _log_limit_rejection(
            CODE_QUERY_TIMEOUT,
            source=TIMEOUT_SOURCE_WALL_CLOCK,
            path=_request_path(context),
            user=_caller_id(context),
        )
        execution_result = timeout_result()
    return {
        "data": execution_result.data,
        "errors": [
            error.formatted for error in (execution_result.errors or [])
        ],
    }


def _first_free_name(name: str, taken: set[str], *, max_length: int = 255) -> str:
    """Return ``name`` if unused, else the first free ``name (n)`` (n >= 2).

    The base is truncated so the suffixed name never exceeds ``max_length``
    characters (the ``saved_queries.name`` column bound). The caller adds the
    chosen name to ``taken`` so within-document duplicates get distinct
    suffixes too.
    """
    if name not in taken:
        return name
    n = 2
    while True:
        suffix = f" ({n})"
        base = name
        if len(name) + len(suffix) > max_length:
            base = name[: max_length - len(suffix)]
        candidate = f"{base}{suffix}"
        if candidate not in taken:
            return candidate
        n += 1


async def export_saved_queries(db: AsyncSession) -> SavedQueriesExportDocument:
    """Serialize every saved query into the portable envelope (304 D-08).

    Ordered by ``name``, then ``id`` for a deterministic document. Entry ids
    and user references are stripped so a document is instance-portable.
    """
    result = await db.execute(
        select(SavedQuery).order_by(SavedQuery.name, SavedQuery.id)
    )
    rows = result.scalars().all()
    return SavedQueriesExportDocument(
        format=SAVED_QUERIES_EXPORT_FORMAT,
        version=SAVED_QUERIES_EXPORT_VERSION,
        queries=[
            SavedQueriesExportEntry(
                name=row.name,
                query_text=row.query_text,
                variables=row.variables,
            )
            for row in rows
        ],
    )


async def import_saved_queries(
    db: AsyncSession, document: SavedQueriesExportDocument, actor_id: uuid.UUID
) -> SavedQueriesImportResult:
    """Import a document all-or-nothing (304 D-09/D-10).

    The whole document is validated first — every entry's GraphQL document must
    be query-only and within the size bound. The first invalid entry aborts
    with a ``queries[i].<field>`` path and inserts nothing. On success every
    entry is inserted in a single transaction, one audit row each, with one
    commit and one cache invalidation. A colliding name (against existing rows
    AND names already assigned within the document) is renamed with a numeric
    suffix (``Name (2)``, ``Name (3)``, ...). Never calls
    ``create_saved_query`` per row — that commits per row and would break the
    all-or-nothing guarantee.
    """
    if not document.queries:
        return SavedQueriesImportResult(imported=0, renamed=0)

    # 1. Validate the whole document before touching the DB (D-10).
    for index, entry in enumerate(document.queries):
        try:
            validate_saved_query_document(entry.query_text)
            validate_saved_query_size(entry.query_text, entry.variables)
            _raise_first_limit_error(entry.query_text)
        except SavedQueryValidationError as exc:
            raise SavedQueryValidationError(
                exc.reason,
                path=f"queries[{index}].{exc.path}",
                extensions=exc.extensions,
            ) from exc

    # 2. Load existing names once — never query per row.
    existing = await db.execute(select(SavedQuery.name))
    taken: set[str] = set(existing.scalars().all())

    renamed = 0
    new_rows: list[SavedQuery] = []
    for entry in document.queries:
        final_name = _first_free_name(entry.name, taken)
        if final_name != entry.name:
            renamed += 1
        taken.add(final_name)
        new_rows.append(
            SavedQuery(
                name=final_name,
                query_text=entry.query_text,
                variables=entry.variables,
                created_by_id=actor_id,
            )
        )

    # 3. One transaction: all rows plus one audit entry each, one commit,
    #    one cache invalidation.
    db.add_all(new_rows)
    await db.flush()
    for row in new_rows:
        await audit_log(
            db,
            AuditEntityType.SAVED_QUERIES.value,
            row.id,
            "CREATE",
            actor_id,
            old_values=None,
            new_values=model_to_dict(row),
        )
    await db.commit()
    await invalidate_entity_cache(ENTITY_TYPE)
    return SavedQueriesImportResult(imported=len(new_rows), renamed=renamed)
