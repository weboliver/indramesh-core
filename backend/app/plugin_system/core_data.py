"""Core Data SDK — typed domain errors and neutral Core-data write adapters (CORESEAM-10).

A plugin writes Core business entities through these adapters using only
``indramesh.sdk``: no ``app.*`` import, no Core ORM model and no raw SQL. Every
adapter delegates to the existing ``app.services.*`` write path, so the
connector-registry validation, DECOUPLE-06 write lock, audit, Elasticsearch
outbox and cache invalidation are identical to the REST API (CORESEAM-10).

Two invariants hold for every adapter:

* **Typed, safe failures (D-01/D-02/D-03).** The adapter maps only *known*
  outcomes — a ``ValueError`` from a Core service, a ``ValueLockConflict``, a
  ``None``/``False`` "not found" — onto the small :class:`CoreDataError`
  hierarchy. It never catches a blanket ``Exception`` and never lets an
  ``HTTPException`` or a raw ORM entity escape the write boundary. Messages are
  bounded, Core-authored and free of SQL/traceback/secret detail.
* **The actor is correct by construction (D-04/D-05/D-14).** The shared
  :func:`_resolve_write` resolver picks the acting user from the *context kind*
  Core set — a task-bound call uses the task's creator, an authenticated route
  call uses the logged-in user the Core dependency resolved, and a hook context
  (borrowed caller session) is refused before any write. The plugin never passes
  a session or a raw actor id.

Each adapter opens its **own** ``async_session_maker()`` session for the service
call: the existing services commit internally, so a hook's borrowed transaction
must never be committed as a side effect (that is why hook contexts are refused).
"""

import dataclasses
import logging
import uuid

from fastapi import HTTPException
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError

from app.db import async_session_maker
from app.models import (
    Object,
    ObjectType,
    ObjectTypeAttribute,
    ReferenceType,
    Schema,
    Status,
)
from app.plugin_system.core_data_schemas import (
    ConnectorCommentCreate,
    ConnectorFileCreate,
    ObjectCommentCreate,
    ObjectCreate,
    ObjectFileCreate,
    ObjectRefCreate,
    ObjectRefUpdate,
    ObjectTypeAttributeCreate,
    ObjectTypeAttributeUpdate,
    ObjectTypeCreate,
    ObjectTypeUpdate,
    ObjectUpdate,
    ObjectValueCreate,
    ObjectValueUpdate,
    ReferenceTypeCreate,
    ReferenceTypeUpdate,
    SchemaCreate,
    SchemaTreeCreate,
    SchemaTreeDraft,
    SchemaUpdate,
    SourceCreate,
    SourceUpdate,
    StatusCreate,
    StatusUpdate,
    TargetCreate,
    TargetUpdate,
    TranslationBatchResult,
    TranslationBatchWrite,
    TranslationEntityPage,
    TranslationEntityPageRequest,
    TranslationReadRequest,
    TranslationReadResult,
    TranslationReadRow,
    UiTranslationBatchWrite,
    UiTranslationCatalogItem,
    UiTranslationCatalogRequest,
    UiTranslationCatalogResult,
)
from app.schemas import crud
from app.services import (
    object_comment_service,
    object_file_service,
    object_ref_service,
    object_service,
    object_type_attribute_service,
    object_type_service,
    object_value_service,
    record_permission_service,
    reference_type_service,
    schema_service,
    source_service,
    status_service,
    target_service,
    translation_service,
    ui_translation_service,
    value_lock_service,
)
from app.services.value_lock_service import ValueLockConflict
from app.services import hook_origin

logger = logging.getLogger(__name__)

# --- stable, public error codes (D-02) ---------------------------------------
CODE_NOT_FOUND = "not_found"
CODE_INVALID_INPUT = "invalid_input"
CODE_CONFLICT = "conflict"
CODE_ACTOR_UNAVAILABLE = "actor_unavailable"
# D-15/T-307-01: the caller is authenticated and active but lacks a required
# translation/entity read or update right (or an inherited record DENY applies).
CODE_NOT_AUTHORIZED = "not_authorized"
CODE_HOOK_CONTEXT = "hook_context"
CODE_PAYLOAD_TOO_LARGE = "payload_too_large"

# A short, UI-/task-safe message never exceeds this bound (D-02).
_MESSAGE_MAX_LENGTH = 300

# The Core-set private context kinds the resolver dispatches on.
_KIND_TASK = "task"
_KIND_ROUTE = "route"
_KIND_HOOK = "hook"

# The fixed UUID namespace a keyed connector create derives its deterministic
# Core id from (D-12). Pinned to a stable literal so the same key yields the same
# id in every process and across restarts; it is never a secret and never changes.
_CONNECTOR_NAMESPACE = uuid.UUID("5d0a5c1e-8f2b-4c6d-9a3e-1b7c4d2e6f80")

# A plugin-built idempotency key is bounded (defense against an unbounded string
# reaching the uuid5 derivation / an unbounded row lookup).
_IDEMPOTENCY_KEY_MAX = 500


class CoreDataError(Exception):
    """Base class for the typed domain errors an SDK write raises (D-01).

    Plugin code branches on the class and on the stable :attr:`code`, never on
    the message text. :attr:`message` is a bounded, Core-authored string safe
    for direct task/UI display; internal diagnostics never reach it (D-02).
    """

    code = "core_data_error"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.message = _bounded(message)
        self.code = code if code is not None else type(self).code
        super().__init__(self.message)


class CoreDataNotFound(CoreDataError):
    """The addressed entity does not exist."""

    code = CODE_NOT_FOUND


class CoreDataInvalid(CoreDataError):
    """The input is invalid, or the write context cannot supply an actor."""

    code = CODE_INVALID_INPUT


class CoreDataConflict(CoreDataError):
    """The write conflicts with the current persisted state (e.g. a lock)."""

    code = CODE_CONFLICT


@dataclasses.dataclass(frozen=True)
class CoreWriteResult:
    """A neutral projection of a successful write (no ORM entity escapes).

    ``id`` is the created/updated entity id; ``key`` is the entity's business
    key when it has one (``None`` for Source/Target — the Object key is added in
    a later plan). ``created`` is ``True`` for a real insert and ``False`` when a
    keyed create replayed an already-committed row (D-12): a replay issues no
    second service call, audit entry or outbox row.
    """

    id: uuid.UUID
    key: str | None = None
    created: bool = True


def _bounded(message: object) -> str:
    """Collapse and bound a Core-authored message to the public safety limit."""
    text = " ".join(str(message).split())
    if len(text) > _MESSAGE_MAX_LENGTH:
        return text[: _MESSAGE_MAX_LENGTH - 1] + "\u2026"
    return text


def _as_uuid(value: object) -> uuid.UUID:
    """Coerce a plugin-supplied identifier; an invalid value is invalid input."""
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        raise CoreDataInvalid("Invalid entity identifier") from None


def _require_dto(data: object, expected: type, name: str) -> object:
    """Refuse anything that is not the strict public SDK input class.

    A REST DTO (``crud.*``) or a bare mapping must never slip through: the SDK
    input is the frozen connector-neutral contract (D-06/D-09), and a REST DTO
    could carry a transitional connector field.
    """
    if not isinstance(data, expected):
        raise CoreDataInvalid(f"{name} must be a {expected.__name__} instance")
    return data


def _actor_from_task(ctx) -> uuid.UUID:
    """Resolve the task-bound actor: the task's creator, else fail closed.

    ``Task.creator_id`` is the persisted ORIGINAL creator (D-04): set at
    authenticated creation, inherited by a scheduled clone from the permanent
    template and by a retry from the original row. A legacy row (or a task whose
    creator was deleted) carries no reliable actor, so the write is refused with
    ``actor_unavailable`` rather than silently attributed to a retrier,
    scheduler or worker (D-04).
    """
    try:
        task = ctx.task
    except Exception:
        raise CoreDataInvalid(
            "This write context has no task-bound actor",
            code=CODE_ACTOR_UNAVAILABLE,
        ) from None
    creator_id = getattr(task, "creator_id", None)
    if creator_id is None:
        raise CoreDataInvalid(
            "No creator is recorded for this task",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    return _as_uuid(creator_id)


def _resolve_write(ctx, user) -> uuid.UUID:
    """Resolve the acting user for this write from the Core-set context kind.

    | Context kind | Actor | Rule |
    |---|---|---|
    | ``task`` | ``ctx.task.creator_id`` | ``user`` must be ``None``; a missing creator fails ``actor_unavailable`` |
    | ``route``/lifecycle | ``user.id`` | ``user`` must be provided; a missing user fails ``actor_unavailable`` |
    | ``hook`` | — | always refused: the services commit, a hook must not commit its caller's transaction |
    """
    kind = getattr(ctx, "context_kind", _KIND_ROUTE)

    if kind == _KIND_HOOK:
        raise CoreDataInvalid(
            "Core-data writes are not available in a hook context",
            code=CODE_HOOK_CONTEXT,
        )

    if kind == _KIND_TASK:
        if user is not None:
            raise CoreDataInvalid(
                "A task-bound Core-data write does not accept a user argument"
            )
        return _actor_from_task(ctx)

    if user is None or getattr(user, "id", None) is None:
        raise CoreDataInvalid(
            "This Core-data write requires an authenticated user",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    return _as_uuid(user.id)


def _translate_service_error(exc: Exception) -> CoreDataError:
    """Map a *known* service outcome to the typed SDK error (never blanket)."""
    if isinstance(exc, ValueLockConflict):
        return CoreDataConflict(str(exc))
    # A domain conflict (e.g. a duplicate ObjectType name) is a 409-equivalent,
    # not an input-validation failure.
    if isinstance(exc, object_type_service.ObjectTypeNameConflict):
        return CoreDataConflict(str(exc))
    # A source-local schema name/key conflict (D-10) is a 409-equivalent, never a
    # silent adopt/merge/rename.
    if isinstance(exc, schema_service.SchemaNameConflict):
        return CoreDataConflict(str(exc))
    # A Core service signals invalid input (connector resolution, missing
    # referenced parent, ...) with a ``ValueError`` — bounded and Core-authored.
    return CoreDataInvalid(str(exc))


def _result(entity) -> CoreWriteResult:
    return CoreWriteResult(id=entity.id)


def _plugin_origin(ctx) -> hook_origin.MutationOrigin:
    """Build the trusted plugin origin for one Core-data write (CORESEAM-11).

    The plugin identity comes from the Core-created context (``_plugin_name``),
    which Core sets from the resolved plugin declaration — never from the
    plugin's own payload. The task id is read directly from the task-bound
    context (``ctx._task_id``, ``None`` for a route); ``ctx.task`` is
    deliberately not used because it raises for route contexts. A hook context
    never reaches this helper — ``_resolve_write`` refuses it first.
    """
    name = getattr(ctx, "_plugin_name", None) or "unknown"
    return hook_origin.plugin_origin(name, task_id=getattr(ctx, "_task_id", None))


def _require_task_plugin_name(ctx) -> str:
    """The trusted plugin identity for a connector-origin write (D-13).

    A connector media row's ``managed_by`` comes from the Core-set context, never
    from the plugin payload. Only a task-bound context may write one: the pull is
    the sole producer, and a route write could forge a connector identity.
    """
    if getattr(ctx, "context_kind", None) != _KIND_TASK:
        raise CoreDataInvalid(
            "Connector media writes require a task-bound context",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    name = getattr(ctx, "_plugin_name", None)
    if not isinstance(name, str) or not name:
        raise CoreDataInvalid(
            "The plugin identity is unavailable for a connector media write"
        )
    return name


def _require_task_source_id(ctx) -> uuid.UUID:
    """The Source id of a Source-scoped task, for the deterministic media key."""
    try:
        source_id = getattr(ctx.task, "source_id", None)
    except Exception:
        source_id = None
    if source_id is None:
        raise CoreDataInvalid(
            "A connector media write requires a Source-scoped task",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    return _as_uuid(source_id)


def _connector_media_key(source_id, object_id, external_id) -> str:
    """``{source}:{Core parent object}:{external id}`` — the deterministic media key.

    The Source and Core parent are always in the key (D-12), so the same external
    id under another Source/Object is a different Core row and never collides.
    """
    return f"{source_id}:{object_id}:{external_id}"


# --- keyed deterministic-id creates (D-12) ------------------------------------
#
# A plugin that must commit a Core create BEFORE it can write the link row that
# references it (a connector pull) passes a stable ``idempotency_key``. Core derives
# the entity id as ``uuid5(namespace, plugin:kind:key)``: the id itself IS the
# idempotency record, so a replayed create reads that exact row back instead of
# guessing by name/content. There is deliberately NO Core table, migration or
# claim state machine — the deterministic id is the whole mechanism. Core only
# prevents a silent reparent: the SAME key under a DIFFERENT parent is a typed
# ``CoreDataConflict``. A locally deleted entity frees the id again and the next
# pull re-creates it (legacy parity, D-12).


def _derive_keyed_id(ctx, entity_kind: str, idempotency_key: str | None) -> uuid.UUID | None:
    """Derive the deterministic Core id for a keyed create, or ``None`` unkeyed.

    The plugin identity comes from the Core-set context (``_plugin_name``), the
    same source :func:`_plugin_origin` uses — never from a plugin payload. A key
    outside a task-bound context is refused: only the pull task needs it, and
    accepting one from a route would let a request forge an identity.
    """
    if idempotency_key is None:
        return None
    if not isinstance(idempotency_key, str):
        raise CoreDataInvalid("idempotency_key must be a string")
    key = idempotency_key.strip()
    if not key or len(key) > _IDEMPOTENCY_KEY_MAX:
        raise CoreDataInvalid("idempotency_key must be a non-empty bounded string")
    if getattr(ctx, "context_kind", None) != _KIND_TASK:
        raise CoreDataInvalid(
            "idempotency_key is only available in a task-bound Core-data write"
        )
    plugin_name = getattr(ctx, "_plugin_name", None)
    if not isinstance(plugin_name, str) or not plugin_name:
        raise CoreDataInvalid("The plugin identity is unavailable for a keyed write")
    return uuid.uuid5(_CONNECTOR_NAMESPACE, f"{plugin_name}:{entity_kind}:{key}")


async def _read_idempotent_row(model, keyed_id, parent_matches, result_of, entity_kind):
    """Return the replay result for a committed keyed row, or ``None`` if free.

    A row whose parent differs is a typed ``CoreDataConflict`` — never a silent
    return and never a reparent (T-293-02-01).
    """
    async with async_session_maker() as session:
        row = await session.get(model, keyed_id)
        if row is None:
            return None
        if not parent_matches(row):
            raise CoreDataConflict(
                f"The idempotency key is already bound to a different {entity_kind} parent"
            )
        return dataclasses.replace(result_of(row), created=False)


async def _keyed_create(
    ctx,
    *,
    entity_kind: str,
    idempotency_key: str | None,
    model,
    parent_matches,
    result_of,
    create,
    not_found: str,
) -> CoreWriteResult:
    """Read-or-create one entity on its deterministic id (D-12).

    ``create(session, entity_id)`` runs the adapter's existing service call with
    the derived id. A concurrent insert of the same id (PK ``IntegrityError``) is
    re-read and handled as "exists" — one row, both callers get its id.
    """
    keyed_id = _derive_keyed_id(ctx, entity_kind, idempotency_key)
    if keyed_id is not None:
        replay = await _read_idempotent_row(
            model, keyed_id, parent_matches, result_of, entity_kind
        )
        if replay is not None:
            return replay

    async with async_session_maker() as session:
        try:
            entity = await create(session, keyed_id)
        except IntegrityError:
            if keyed_id is None:
                raise
            replay = await _read_idempotent_row(
                model, keyed_id, parent_matches, result_of, entity_kind
            )
            if replay is None:
                raise
            return replay

    if entity is None:
        raise CoreDataNotFound(not_found)
    return result_of(entity)


# --- Source -------------------------------------------------------------------


async def create_source(ctx, data: SourceCreate, *, user=None) -> CoreWriteResult:
    """Create a Source through the Core service and return a neutral result."""
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, SourceCreate, "data")
    async with async_session_maker() as session:
        try:
            entity = await source_service.create_source(
                session,
                crud.SourceCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    return _result(entity)


async def update_source(
    ctx, id, data: SourceUpdate, *, user=None
) -> CoreWriteResult:
    """Patch a Source through the Core service; a missing row is not-found."""
    actor_id = _resolve_write(ctx, user)
    source_id = _as_uuid(id)
    payload = _require_dto(data, SourceUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await source_service.update_source(
                session,
                source_id,
                crud.SourceUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Source not found")
    return _result(entity)


async def delete_source(ctx, id, *, user=None) -> bool:
    """Delete a Source through the Core service; a missing row is not-found."""
    actor_id = _resolve_write(ctx, user)
    source_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await source_service.delete_source(session, source_id, actor_id)
        except ValueLockConflict as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Source not found")
    return True


# --- Target -------------------------------------------------------------------


async def create_target(ctx, data: TargetCreate, *, user=None) -> CoreWriteResult:
    """Create a Target through the Core service and return a neutral result."""
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, TargetCreate, "data")
    async with async_session_maker() as session:
        try:
            entity = await target_service.create_target(
                session,
                crud.TargetCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    return _result(entity)


async def update_target(
    ctx, id, data: TargetUpdate, *, user=None
) -> CoreWriteResult:
    """Patch a Target through the Core service; a missing row is not-found."""
    actor_id = _resolve_write(ctx, user)
    target_id = _as_uuid(id)
    payload = _require_dto(data, TargetUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await target_service.update_target(
                session,
                target_id,
                crud.TargetUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Target not found")
    return _result(entity)


async def delete_target(ctx, id, *, user=None) -> bool:
    """Delete a Target through the Core service; a missing row is not-found."""
    actor_id = _resolve_write(ctx, user)
    target_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await target_service.delete_target(session, target_id, actor_id)
        except ValueLockConflict as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Target not found")
    return True


# --- Schema -------------------------------------------------------------------


async def create_schema(
    ctx,
    data: SchemaCreate,
    *,
    user=None,
    idempotency_key: str | None = None,
) -> CoreWriteResult:
    """Create a Schema through the Core service and return a neutral result.

    The Core service still owns keygen derivation, the per-schema sequence, the
    CREATE audit entry and the cache/ES side effects (CORESEAM-10). ``key`` in
    the result is the schema's native IndraMesh ``keygen`` prefix — never the
    connector-occupied ``key`` (D-09). A task-bound ``idempotency_key`` selects the
    deterministic-id read-or-create path (D-12).
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, SchemaCreate, "data")
    try:
        return await _keyed_create(
            ctx,
            entity_kind="schema",
            idempotency_key=idempotency_key,
            model=Schema,
            parent_matches=lambda row: row.source_id == payload.source_id,
            result_of=lambda row: CoreWriteResult(id=row.id, key=row.keygen),
            create=lambda session, entity_id: schema_service.create_schema(
                session,
                crud.SchemaCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                id=entity_id,
            ),
            not_found="Source not found",
        )
    except ValueError as exc:
        raise _translate_service_error(exc) from None


async def update_schema(
    ctx, id, data: SchemaUpdate, *, user=None
) -> CoreWriteResult:
    """Patch a Schema through the Core service; a missing row is not-found.

    A rejected default-language change is a domain error: the REST route maps
    it back to its original 400/detail, while the adapter surfaces it as a
    typed ``CoreDataInvalid`` (D-03).
    """
    actor_id = _resolve_write(ctx, user)
    schema_id = _as_uuid(id)
    payload = _require_dto(data, SchemaUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await schema_service.update_schema(
                session,
                schema_id,
                crud.SchemaUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Schema not found")
    return CoreWriteResult(id=entity.id, key=entity.keygen)


async def delete_schema(ctx, id, *, user=None) -> bool:
    """Delete a Schema through the Core service; a missing row is not-found.

    A locked member ObjectType protects the schema: the Core service raises
    ``ValueLockConflict``, which the adapter maps to ``CoreDataConflict``.
    """
    actor_id = _resolve_write(ctx, user)
    schema_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await schema_service.delete_schema(session, schema_id, actor_id)
        except ValueLockConflict as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Schema not found")
    return True


async def create_schema_from_tree(
    ctx, data: SchemaTreeCreate, *, user=None
) -> CoreWriteResult:
    """Strictly create one schema and its whole tree through the Core service (SEAM-04).

    The whole tree is validated by the Core materializer before any write; an
    invalid tree is rejected in full with a concrete indexed path surfaced as a
    typed ``CoreDataInvalid``, a source-local name/key conflict as
    ``CoreDataConflict`` and a missing Source as ``CoreDataNotFound``. The actor
    is Core-resolved (route ``user=`` or the task's persisted creator); the
    plugin never supplies origin or ORM ids. The result's ``key`` is the native
    IndraMesh ``keygen`` prefix.
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, SchemaTreeCreate, "data")
    async with async_session_maker() as session:
        try:
            entity = await schema_service.create_schema_from_tree(
                session, payload, actor_id
            )
        except schema_service.InvalidSchemaTree as exc:
            raise CoreDataInvalid(str(exc)) from None
        except schema_service.SchemaNameConflict as exc:
            raise CoreDataConflict(str(exc)) from None
        except schema_service.SchemaTreeSourceNotFound as exc:
            raise CoreDataNotFound(str(exc)) from None
    return CoreWriteResult(id=entity.id, key=entity.keygen)


def _draft_error_path(loc: tuple) -> str:
    """A concrete indexed path for a nested draft field (``object_types[0].name``)."""
    parts: list[str] = []
    for token in loc:
        if isinstance(token, int):
            if parts:
                parts[-1] = f"{parts[-1]}[{token}]"
            else:
                parts.append(f"[{token}]")
        else:
            parts.append(str(token))
    return ".".join(parts) or "draft"


def validate_schema_tree_draft(draft) -> SchemaTreeDraft:
    """Validate a neutral schema-tree draft in memory — no storage authority (AIWIZ-01).

    Synchronous, pure and read-only: the caller passes an already-derived draft
    (a :class:`SchemaTreeDraft` or the plain JSON mapping a plugin generated).
    The draft is parsed/copied through the strict DTO — ``extra="forbid"``
    rejects any authority field (Source, name, actor, origin) — and the whole
    graph is run through the SAME pure semantic preflight the final Core
    materializer uses (:func:`schema_service._preflight_schema_tree`), so a
    draft that validates here is accepted by final creation unchanged.

    No Source, actor, icon, session or authorization is involved and nothing is
    written or repaired. An invalid shape (unknown authority field, unknown
    attribute type, dangling/self/cyclic parent, missing or non-reference
    target, bad default/option) raises a bounded :class:`CoreDataInvalid`
    carrying a concrete indexed path. The returned model is detached — it never
    aliases the caller's input.
    """
    if isinstance(draft, SchemaTreeDraft):
        source = draft.model_dump()
    elif isinstance(draft, dict):
        source = draft
    else:
        raise CoreDataInvalid(
            "draft must be a SchemaTreeDraft or a mapping"
        )
    try:
        parsed = SchemaTreeDraft.model_validate(source)
        # Re-validate through a dump so the returned tree is a deep copy that can
        # never alias the caller's input object.
        parsed = SchemaTreeDraft.model_validate(parsed.model_dump())
        schema_service._preflight_schema_tree(parsed)
    except ValidationError as exc:
        errors = exc.errors()
        first = errors[0] if errors else {}
        path = _draft_error_path(tuple(first.get("loc", ())))
        reason = first.get("msg", "invalid schema tree draft")
        raise CoreDataInvalid(f"{path}: {reason}") from None
    except schema_service.InvalidSchemaTree as exc:
        raise CoreDataInvalid(str(exc)) from None
    return parsed


# --- ObjectType ---------------------------------------------------------------


async def create_object_type(
    ctx,
    data: ObjectTypeCreate,
    *,
    user=None,
    idempotency_key: str | None = None,
) -> CoreWriteResult:
    """Create an ObjectType through the Core service.

    The Core service provisions the four system attributes (Name, Key, Created,
    Updated) on every native create (CORESEAM-10) and owns the CREATE audit,
    index/cache effects; a duplicate name inside the schema is a
    ``CoreDataConflict``. A task-bound ``idempotency_key`` selects the
    deterministic-id read-or-create path (D-12).
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectTypeCreate, "data")
    try:
        return await _keyed_create(
            ctx,
            entity_kind="object_type",
            idempotency_key=idempotency_key,
            model=ObjectType,
            parent_matches=lambda row: row.schema_id == payload.schema_id,
            result_of=lambda row: CoreWriteResult(id=row.id),
            create=lambda session, entity_id: object_type_service.create_object_type(
                session,
                crud.ObjectTypeCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                id=entity_id,
            ),
            not_found="Schema or parent object type not found",
        )
    except ValueError as exc:
        raise _translate_service_error(exc) from None


async def update_object_type(
    ctx, id, data: ObjectTypeUpdate, *, user=None
) -> CoreWriteResult:
    """Patch an ObjectType through the Core service.

    A rename onto an existing sibling is a ``CoreDataConflict`` (REST 409); the
    remaining validation failures stay ``CoreDataInvalid`` (REST 400).
    """
    actor_id = _resolve_write(ctx, user)
    object_type_id = _as_uuid(id)
    payload = _require_dto(data, ObjectTypeUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_type_service.update_object_type(
                session,
                object_type_id,
                crud.ObjectTypeUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Object type not found")
    return _result(entity)


async def delete_object_type(ctx, id, *, user=None) -> bool:
    """Delete an ObjectType through the Core service.

    A locked type or locked descendant protects the cascade: the Core service
    raises ``ValueLockConflict``, mapped to ``CoreDataConflict``.
    """
    actor_id = _resolve_write(ctx, user)
    object_type_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await object_type_service.delete_object_type(
                session, object_type_id, actor_id
            )
        except ValueLockConflict as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Object type not found")
    return True


# --- ObjectTypeAttribute ------------------------------------------------------


async def create_object_type_attribute(
    ctx,
    data: ObjectTypeAttributeCreate,
    *,
    user=None,
    idempotency_key: str | None = None,
) -> CoreWriteResult:
    """Create an ObjectTypeAttribute through the Core service.

    The service owns structure-option normalization, the system-OTA protection,
    the descendant/public cache purges and — now — an audit entry attributed to
    the resolved actor, in the same transaction as the insert (D-04/D-10/D-11).
    A task-bound ``idempotency_key`` selects the deterministic-id read-or-create
    path (D-12).
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectTypeAttributeCreate, "data")
    try:
        return await _keyed_create(
            ctx,
            entity_kind="object_type_attribute",
            idempotency_key=idempotency_key,
            model=ObjectTypeAttribute,
            parent_matches=lambda row: row.object_type_id == payload.object_type_id,
            result_of=lambda row: CoreWriteResult(id=row.id),
            create=lambda session, entity_id: (
                object_type_attribute_service.create_object_type_attribute(
                    session,
                    crud.ObjectTypeAttributeCreate(
                        **payload.model_dump(exclude_unset=True)
                    ),
                    actor_id=actor_id,
                    id=entity_id,
                )
            ),
            not_found="Referenced entity not found",
        )
    except ValueError as exc:
        raise _translate_service_error(exc) from None


async def update_object_type_attribute(
    ctx, id, data: ObjectTypeAttributeUpdate, *, user=None
) -> CoreWriteResult:
    """Patch an ObjectTypeAttribute through the Core service; audited as the actor."""
    actor_id = _resolve_write(ctx, user)
    attribute_id = _as_uuid(id)
    payload = _require_dto(data, ObjectTypeAttributeUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_type_attribute_service.update_object_type_attribute(
                session,
                attribute_id,
                crud.ObjectTypeAttributeUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id=actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Object type attribute not found")
    return _result(entity)


async def delete_object_type_attribute(ctx, id, *, user=None) -> bool:
    """Delete an ObjectTypeAttribute through the Core service; audited as the actor.

    A system attribute is protected: the Core service refuses it with a
    ``ValueError``, surfaced as ``CoreDataInvalid``.
    """
    actor_id = _resolve_write(ctx, user)
    attribute_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await object_type_attribute_service.delete_object_type_attribute(
                session, attribute_id, actor_id=actor_id
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Object type attribute not found")
    return True


# --- Status -------------------------------------------------------------------


async def create_status(
    ctx,
    data: StatusCreate,
    *,
    user=None,
    idempotency_key: str | None = None,
) -> CoreWriteResult:
    """Create a Status through the Core service.

    The service validates the parent Schema, normalizes ``status_category``,
    writes the CREATE audit entry attributed to the resolved actor and purges
    the status cache (D-04/D-10/D-11). A missing parent Schema is a bounded
    ``ValueError`` surfaced as ``CoreDataInvalid``. A task-bound
    ``idempotency_key`` selects the deterministic-id read-or-create path (D-12).
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, StatusCreate, "data")
    try:
        return await _keyed_create(
            ctx,
            entity_kind="status",
            idempotency_key=idempotency_key,
            model=Status,
            parent_matches=lambda row: row.schema_id == payload.schema_id,
            result_of=lambda row: CoreWriteResult(id=row.id),
            create=lambda session, entity_id: status_service.create_status(
                session,
                crud.StatusCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                id=entity_id,
            ),
            not_found="Status not found",
        )
    except ValueError as exc:
        raise _translate_service_error(exc) from None


async def update_status(
    ctx, id, data: StatusUpdate, *, user=None
) -> CoreWriteResult:
    """Patch a Status through the Core service; a missing row is not-found.

    PATCH omission semantics are preserved: a field left unset stays unchanged,
    exactly like the REST contract (D-06).
    """
    actor_id = _resolve_write(ctx, user)
    status_id = _as_uuid(id)
    payload = _require_dto(data, StatusUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await status_service.update_status(
                session,
                status_id,
                crud.StatusUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Status not found")
    return _result(entity)


async def delete_status(ctx, id, *, user=None) -> bool:
    """Delete a Status through the Core service; a missing row is not-found.

    The service purges the polymorphic translation rows in the same transaction
    as the entity delete, exactly like the REST path (D-15/MULTI-07).
    """
    actor_id = _resolve_write(ctx, user)
    status_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await status_service.delete_status(session, status_id, actor_id)
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Status not found")
    return True


# --- ReferenceType ------------------------------------------------------------


async def create_reference_type(
    ctx,
    data: ReferenceTypeCreate,
    *,
    user=None,
    idempotency_key: str | None = None,
) -> CoreWriteResult:
    """Create a ReferenceType through the Core service.

    The service writes the CREATE audit entry attributed to the resolved actor,
    purges the reference-type cache and the affected type-schema public cache
    (D-04/D-10/D-11). A task-bound ``idempotency_key`` selects the
    deterministic-id read-or-create path (D-12); a ReferenceType's parent is its
    ``(source_id, schema_id)`` pair.
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ReferenceTypeCreate, "data")
    try:
        return await _keyed_create(
            ctx,
            entity_kind="reference_type",
            idempotency_key=idempotency_key,
            model=ReferenceType,
            parent_matches=lambda row: (
                row.source_id == payload.source_id
                and row.schema_id == payload.schema_id
            ),
            result_of=lambda row: CoreWriteResult(id=row.id),
            create=lambda session, entity_id: (
                reference_type_service.create_reference_type(
                    session,
                    crud.ReferenceTypeCreate(**payload.model_dump(exclude_unset=True)),
                    actor_id,
                    id=entity_id,
                )
            ),
            not_found="Reference type not found",
        )
    except ValueError as exc:
        raise _translate_service_error(exc) from None


async def update_reference_type(
    ctx, id, data: ReferenceTypeUpdate, *, user=None
) -> CoreWriteResult:
    """Patch a ReferenceType through the Core service; a missing row is not-found."""
    actor_id = _resolve_write(ctx, user)
    reference_type_id = _as_uuid(id)
    payload = _require_dto(data, ReferenceTypeUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await reference_type_service.update_reference_type(
                session,
                reference_type_id,
                crud.ReferenceTypeUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Reference type not found")
    return _result(entity)


async def delete_reference_type(ctx, id, *, user=None) -> bool:
    """Delete a ReferenceType through the Core service; a missing row is not-found.

    The service purges the polymorphic translation rows in the same transaction
    as the entity delete, exactly like the REST path (D-15/MULTI-07).
    """
    actor_id = _resolve_write(ctx, user)
    reference_type_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await reference_type_service.delete_reference_type(
                session, reference_type_id, actor_id
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Reference type not found")
    return True


# --- Object -------------------------------------------------------------------


async def create_object(
    ctx,
    data: ObjectCreate,
    *,
    user=None,
    idempotency_key: str | None = None,
) -> CoreWriteResult:
    """Create an Object through the Core service and return its native key.

    The Core service owns every write invariant (CORESEAM-10, D-12): the
    server-generated IndraMesh ``key`` from the schema sequence, the
    Name/Key/Created/Updated system-value mirrors, the CREATE audit entry
    attributed to the resolved actor, the transactional ``IndexOutbox`` row and
    the post-commit index/cache side effects. The adapter only delegates and
    projects the result — it never mirrors keygen, writes the outbox or
    invalidates the cache itself. A missing/abstract parent or a locked Name
    mirror is a typed error, not an ORM escape. A task-bound ``idempotency_key``
    selects the deterministic-id read-or-create path (D-12); a replay returns the
    committed id/key with ``created=False`` and issues no second audit/outbox row.
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectCreate, "data")
    try:
        return await _keyed_create(
            ctx,
            entity_kind="object",
            idempotency_key=idempotency_key,
            model=Object,
            parent_matches=lambda row: row.object_type_id == payload.object_type_id,
            result_of=lambda row: CoreWriteResult(id=row.id, key=row.key),
            create=lambda session, entity_id: object_service.create_object(
                session,
                crud.ObjectCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                origin=_plugin_origin(ctx),
                id=entity_id,
            ),
            not_found="Schema or object type not found",
        )
    except (ValueError, ValueLockConflict) as exc:
        raise _translate_service_error(exc) from None


async def update_object(
    ctx, id, data: ObjectUpdate, *, user=None
) -> CoreWriteResult:
    """Patch an Object through the Core service; a missing row is not-found.

    The service preserves the locked-Name-mirror guard, the success-mirror
    updates, the UPDATE audit (with the resolved actor) and the transactional
    outbox/cache side effects. A locked rename is ``CoreDataConflict``.
    """
    actor_id = _resolve_write(ctx, user)
    object_id = _as_uuid(id)
    payload = _require_dto(data, ObjectUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_service.update_object(
                session,
                object_id,
                crud.ObjectUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                origin=_plugin_origin(ctx),
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Object not found")
    return _result(entity)


async def delete_object(ctx, id, *, user=None) -> bool:
    """Delete an Object through the Core service; a missing row is not-found.

    A locked ObjectType blocks the delete before any row/blob/audit/outbox
    change; the service raises ``ValueLockConflict``, surfaced as
    ``CoreDataConflict`` (D-12/D-13).
    """
    actor_id = _resolve_write(ctx, user)
    object_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await object_service.delete_object(
                session, object_id, actor_id, origin=_plugin_origin(ctx)
            )
        except ValueLockConflict as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Object not found")
    return True


# --- ObjectValue --------------------------------------------------------------


async def create_object_value(
    ctx, data: ObjectValueCreate, *, user=None
) -> CoreWriteResult:
    """Create an ObjectValue through the Core service.

    The Core service owns every write invariant (CORESEAM-10, D-12): the
    system-OTA routing (a valueless Name binding stays a no-op), typed value
    validation (SELECT/STATUS/USER/GROUP/text-length), the DECOUPLE-06 fresh
    lock (denied before any insert), the CREATE audit entry attributed to the
    resolved actor and the post-commit value/search cache invalidation. The
    adapter is a pure delegate — a locked or invalid write is a typed error,
    never an ORM/``ValueError`` escape (T-291.1-13/14/15).
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectValueCreate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_value_service.create_object_value(
                session,
                crud.ObjectValueCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                origin=_plugin_origin(ctx),
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    return _result(entity)


async def update_object_value(
    ctx, id, data: ObjectValueUpdate, *, user=None
) -> CoreWriteResult:
    """Patch an ObjectValue through the Core service; a missing row is not-found.

    PATCH omission semantics are preserved and an unchanged payload stays a
    side-effect-free no-op (a locked value still accepts an identical patch, D-05).
    A real change on a locked value is ``CoreDataConflict``.
    """
    actor_id = _resolve_write(ctx, user)
    value_id = _as_uuid(id)
    payload = _require_dto(data, ObjectValueUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_value_service.update_object_value(
                session,
                value_id,
                crud.ObjectValueUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                origin=_plugin_origin(ctx),
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Object value not found")
    return _result(entity)


async def delete_object_value(ctx, id, *, user=None) -> bool:
    """Delete an ObjectValue through the Core service; a missing row is not-found.

    The service purges the value's translations in the same transaction and
    honours the effective lock; a system Name row is protected with a bounded
    ``ValueError`` surfaced as ``CoreDataInvalid``.
    """
    actor_id = _resolve_write(ctx, user)
    value_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await object_value_service.delete_object_value(
                session, value_id, actor_id, origin=_plugin_origin(ctx)
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Object value not found")
    return True


# --- ObjectRef ----------------------------------------------------------------


async def create_object_ref(ctx, data: ObjectRefCreate, *, user=None) -> CoreWriteResult:
    """Create an ObjectRef through the Core service.

    The service validates the bound objects/type/source, denies a locked binding
    (DECOUPLE-06) before any insert, writes the CREATE audit entry attributed to
    the resolved actor, touches the source's Updated system attribute and purges
    the ref/search caches (CORESEAM-10, D-12). A locked binding is
    ``CoreDataConflict``.
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectRefCreate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_ref_service.create_object_ref(
                session,
                crud.ObjectRefCreate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                origin=_plugin_origin(ctx),
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    return _result(entity)


async def update_object_ref(
    ctx, id, data: ObjectRefUpdate, *, user=None
) -> CoreWriteResult:
    """Patch an ObjectRef through the Core service; a missing row is not-found.

    An unchanged payload is a side-effect-free no-op (locked refs included); a
    real change to a locked binding is ``CoreDataConflict`` (D-05/D-10).
    """
    actor_id = _resolve_write(ctx, user)
    ref_id = _as_uuid(id)
    payload = _require_dto(data, ObjectRefUpdate, "data")
    async with async_session_maker() as session:
        try:
            entity = await object_ref_service.update_object_ref(
                session,
                ref_id,
                crud.ObjectRefUpdate(**payload.model_dump(exclude_unset=True)),
                actor_id,
                origin=_plugin_origin(ctx),
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    if entity is None:
        raise CoreDataNotFound("Object reference not found")
    return _result(entity)


async def delete_object_ref(ctx, id, *, user=None) -> bool:
    """Delete an ObjectRef through the Core service; a missing row is not-found.

    A locked binding is denied before the audit/delete/commit and surfaced as
    ``CoreDataConflict`` (DECOUPLE-06).
    """
    actor_id = _resolve_write(ctx, user)
    ref_id = _as_uuid(id)
    async with async_session_maker() as session:
        try:
            deleted = await object_ref_service.delete_object_ref(
                session, ref_id, actor_id, origin=_plugin_origin(ctx)
            )
        except (ValueError, ValueLockConflict) as exc:
            raise _translate_service_error(exc) from None
    if not deleted:
        raise CoreDataNotFound("Object reference not found")
    return True


# --- ObjectComment ------------------------------------------------------------


async def create_object_comment(
    ctx, data: ObjectCommentCreate, *, user=None
) -> CoreWriteResult:
    """Create a native ObjectComment through the Core service (D-12).

    The author is the resolved route/task actor (D-04/D-05, T-291.1-16) — never
    a caller-supplied field — and the strict DTO forbids the externally-managed
    provenance markers (T-291.1-17). The comment service owns the trim/hardening
    boundary and has no update operation, so the SDK publishes create/delete only
    (D-13). It writes no audit entry; the persisted ``author_id`` is the
    attribution, kept exactly as the REST path leaves it.
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectCommentCreate, "data")
    async with async_session_maker() as session:
        # Mirror the REST route's parent-Object pre-check so an unknown/foreign
        # object_id is a typed not-found, never a raw FK IntegrityError (WR-01).
        if not await object_service.object_exists(session, payload.object_id):
            raise CoreDataNotFound("Object not found")
        try:
            response = await object_comment_service.create_object_comment(
                session,
                object_id=payload.object_id,
                body=payload.body,
                author_id=actor_id,
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
        except IntegrityError:
            # Defense-in-depth for a race between the probe and the commit: the
            # FK on object_id is the only constraint this insert can violate.
            raise CoreDataNotFound("Object not found") from None
    return CoreWriteResult(id=response.id)


async def delete_object_comment(
    ctx, object_id, comment_id, *, user=None
) -> bool:
    """Delete an ObjectComment through the Core service (D-12/D-13).

    The service performs an object-scoped dual-ID lookup and refuses a
    connector-managed row with the same ``False`` as a genuinely missing one, so
    a mismatched parent, a missing id, a managed row and a repeated delete all
    surface as an indistinguishable ``CoreDataNotFound`` (T-291.1-17).
    """
    _resolve_write(ctx, user)
    object_uuid = _as_uuid(object_id)
    comment_uuid = _as_uuid(comment_id)
    async with async_session_maker() as session:
        deleted = await object_comment_service.delete_object_comment(
            session, object_uuid, comment_uuid
        )
    if not deleted:
        raise CoreDataNotFound("Object comment not found")
    return True


# --- ObjectFile ---------------------------------------------------------------


class _PluginUpload:
    """Minimal UploadFile-compatible adapter over a plugin's async stream.

    The Core file service only reads ``await stream.read(size)`` and the two
    metadata attributes ``filename``/``content_type`` (D-09). A plugin therefore
    hands a plain async file-like object — not a Starlette ``UploadFile`` — and
    the bytes are pulled through the service's bounded chunk relay unchanged,
    never buffered here.
    """

    def __init__(self, stream, filename: str, content_type: str | None) -> None:
        self._stream = stream
        self.filename = filename
        self.content_type = content_type

    async def read(self, size: int = -1) -> bytes:
        return await self._stream.read(size)


async def create_object_file(
    ctx, data: ObjectFileCreate, stream, *, user=None
) -> CoreWriteResult:
    """Stream a native ObjectFile through the Core storage service (D-12).

    The uploader is the resolved route/task actor (D-04/D-05, T-291.1-16),
    resolved *before* the stream or the storage is touched, and the strict DTO
    allows only pure metadata (T-291.1-17). The Core service owns the bounded
    chunk relay, the size limit, the server-side content sniffing, the
    storage-first metadata commit with best-effort blob compensation and the
    managed-row guard; this adapter only wraps the plugin stream in the tiny
    ``UploadFile``-compatible shape the service reads. The service's 413 is
    translated into the bounded ``payload_too_large`` code so no
    ``HTTPException`` escapes the SDK (D-03, T-291.1-18).
    """
    actor_id = _resolve_write(ctx, user)
    payload = _require_dto(data, ObjectFileCreate, "data")
    upload = _PluginUpload(stream, payload.filename, payload.content_type)
    async with async_session_maker() as session:
        # Mirror the REST route's parent-Object pre-check so an unknown/foreign
        # object_id is a typed not-found before the blob is stored, never a raw
        # FK IntegrityError after the store-then-compensate path (WR-01).
        if not await object_service.object_exists(session, payload.object_id):
            raise CoreDataNotFound("Object not found")
        try:
            response = await object_file_service.create_object_file(
                session,
                object_id=payload.object_id,
                upload=upload,
                uploader_id=actor_id,
            )
        except HTTPException as exc:
            if exc.status_code == 413:
                raise CoreDataInvalid(
                    str(exc.detail), code=CODE_PAYLOAD_TOO_LARGE
                ) from None
            raise CoreDataInvalid(str(exc.detail)) from None
        except ValueError as exc:
            # IN-01: a service-level invalid input must not escape as a bare
            # ValueError either (the store-then-compensate path already ran).
            raise _translate_service_error(exc) from None
        except IntegrityError:
            # The blob compensation already ran inside the service; surface the
            # typed not-found rather than the raw DB exception (WR-01).
            raise CoreDataNotFound("Object not found") from None
    return CoreWriteResult(id=response.id)


async def delete_object_file(ctx, object_id, file_id, *, user=None) -> bool:
    """Delete an ObjectFile through the Core service (D-12/D-13).

    The service performs an object-scoped dual-ID lookup, refuses a
    connector-managed row with the same ``False`` as a genuinely missing one and
    commits the metadata deletion before the best-effort blob cleanup; a
    mismatched parent, a missing id, a managed row and a repeated delete all
    surface as an indistinguishable ``CoreDataNotFound`` (T-291.1-17).
    """
    _resolve_write(ctx, user)
    object_uuid = _as_uuid(object_id)
    file_uuid = _as_uuid(file_id)
    async with async_session_maker() as session:
        deleted = await object_file_service.delete_object_file(
            session, object_uuid, file_uuid
        )
    if not deleted:
        raise CoreDataNotFound("Object file not found")
    return True


# --- connector-origin media (D-13) --------------------------------------------
#
# A plugin that imports external comments/files must persist a Core row whose
# provenance is EXTERNAL and read-only. The native ``ObjectCommentCreate`` /
# ``ObjectFileCreate`` deliberately cannot express that (they forbid
# ``managed_by``), so these separate adapters stamp the Core-owned ``managed_by``
# from the task plugin name, keep the local actor NULL and derive the row id from
# ``{source}:{object}:{external id}`` (D-12). The native create and delete
# contracts stay byte-identical and the native delete guard still refuses these
# rows (T-293-06-02).


async def upsert_connector_comment(
    ctx, data: ConnectorCommentCreate, *, user=None
) -> CoreWriteResult:
    """Read-or-create one externally-authored comment through the Core service.

    Requires a Source-scoped task context (Core derives the plugin identity and
    the Source), derives the deterministic row id from ``{source}:{object}:{external
    id}`` and delegates to the connector service method. A replay returns
    the committed row (``created=False``) without a second write; a changed
    body/author updates only a row owned by this connector and parent.
    """
    _resolve_write(ctx, user)
    payload = _require_dto(data, ConnectorCommentCreate, "data")
    plugin_name = _require_task_plugin_name(ctx)
    source_id = _require_task_source_id(ctx)
    key = _connector_media_key(source_id, payload.object_id, payload.external_id)
    comment_id = _derive_keyed_id(ctx, "comment", key)
    async with async_session_maker() as session:
        if not await object_service.object_exists(session, payload.object_id):
            raise CoreDataNotFound("Object not found")
        try:
            response, created = await object_comment_service.upsert_connector_comment(
                session,
                object_id=payload.object_id,
                comment_id=comment_id,
                body=payload.body,
                external_author_name=payload.external_author_name,
                managed_by=plugin_name,
            )
        except object_comment_service.ConnectorCommentConflict as exc:
            raise CoreDataConflict(str(exc)) from None
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    return CoreWriteResult(id=response.id, created=created)


async def delete_connector_comment(
    ctx, object_id, comment_id, *, user=None
) -> bool:
    """Delete a comment owned by THIS connector (D-13).

    Narrower than the native delete: only a row whose persisted ``managed_by``
    matches the calling task plugin is removable. The native read-only guard is
    untouched, so a native/foreign comment remains indistinguishable missing.
    """
    _resolve_write(ctx, user)
    plugin_name = _require_task_plugin_name(ctx)
    object_uuid = _as_uuid(object_id)
    comment_uuid = _as_uuid(comment_id)
    async with async_session_maker() as session:
        deleted = await object_comment_service.delete_connector_comment(
            session, object_uuid, comment_uuid, plugin_name
        )
    if not deleted:
        raise CoreDataNotFound("Object comment not found")
    return True


async def upsert_connector_file(
    ctx, data: ConnectorFileCreate, stream, *, user=None
) -> CoreWriteResult:
    """Read-or-create/replace one externally-authored attachment (D-13).

    Requires a Source-scoped task context, derives the deterministic row id from
    ``{source}:{object}:{external id}`` and delegates to the connector
    service method, which owns the bounded relay, the existing size/sniff
    protections and the blob compensation. A replay short-circuits before the
    stream is touched, so no second blob is ever stored.
    """
    _resolve_write(ctx, user)
    payload = _require_dto(data, ConnectorFileCreate, "data")
    plugin_name = _require_task_plugin_name(ctx)
    source_id = _require_task_source_id(ctx)
    key = _connector_media_key(
        source_id, payload.object_id, payload.external_id
    )
    file_id = _derive_keyed_id(ctx, "file", key)
    async with async_session_maker() as session:
        if not await object_service.object_exists(session, payload.object_id):
            raise CoreDataNotFound("Object not found")
        try:
            response, created = await object_file_service.upsert_connector_file(
                session,
                object_id=payload.object_id,
                file_id=file_id,
                stream=stream,
                filename=payload.filename,
                content_type=payload.content_type,
                external_author_name=payload.external_author_name,
                managed_by=plugin_name,
                content_length=payload.content_length,
            )
        except object_file_service.ConnectorFileConflict as exc:
            raise CoreDataConflict(str(exc)) from None
        except HTTPException as exc:
            if exc.status_code == 413:
                raise CoreDataInvalid(
                    str(exc.detail), code=CODE_PAYLOAD_TOO_LARGE
                ) from None
            raise CoreDataInvalid(str(exc.detail)) from None
        except ValueError as exc:
            raise _translate_service_error(exc) from None
        except IntegrityError:
            raise CoreDataNotFound("Object not found") from None
    return CoreWriteResult(id=response.id, created=created)


async def delete_connector_file(
    ctx, object_id, file_id, *, user=None
) -> bool:
    """Delete an attachment owned by THIS connector (D-13).

    Narrower than the native delete: only a row whose persisted ``managed_by``
    matches the calling task plugin is removable. The native read-only guard is
    untouched, so a native/foreign file remains indistinguishable missing.
    """
    _resolve_write(ctx, user)
    plugin_name = _require_task_plugin_name(ctx)
    object_uuid = _as_uuid(object_id)
    file_uuid = _as_uuid(file_id)
    async with async_session_maker() as session:
        deleted = await object_file_service.delete_connector_file(
            session, object_uuid, file_uuid, plugin_name
        )
    if not deleted:
        raise CoreDataNotFound("Object file not found")
    return True


# --- Plugin-owned attribute write protection (D-08, plan 294-01) --------------


async def set_attribute_write_protection(
    ctx, attribute_id, *, readonly: bool, user=None
) -> bool:
    """Set or lift this plugin's own write-protection marker on one OTA (D-08).

    The owner is Core-set from the calling plugin (``ctx._plugin_name``) and is
    never an argument, so a plugin can only mark/unmark attributes it owns. The
    call runs in its own ``async_session_maker`` session because the service
    commits. A foreign or NULL-owner marker is a ``CoreDataConflict``; an
    unknown attribute is a ``CoreDataNotFound``. There is deliberately no
    ObjectType form — D-08 writes markers on ObjectTypeAttributes only.

    Returns ``True`` when the marker changed in the requested direction and
    ``False`` for an already-in-that-state no-op.
    """
    actor_id = _resolve_write(ctx, user)
    owner = getattr(ctx, "_plugin_name", None)
    if not isinstance(owner, str) or not owner:
        raise CoreDataInvalid(
            "The plugin identity is unavailable for a write-protection marker",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    attribute_uuid = _as_uuid(attribute_id)
    async with async_session_maker() as session:
        try:
            return await value_lock_service.set_attribute_write_protection(
                session,
                attribute_uuid,
                readonly=readonly,
                owner=owner,
                actor_id=actor_id,
            )
        except ValueLockConflict as exc:
            raise _translate_service_error(exc) from None
        except ValueError as exc:
            raise CoreDataNotFound(str(exc) or "Object type attribute not found") from None


# --- Plugin-owned per-object write-protection release (D-11, 294-01) ---------


async def set_object_write_protection_release(
    ctx, object_id, *, released: bool, user=None
) -> bool:
    """Set or lift this plugin's per-object write-protection release (D-11).

    The owner is Core-set from the calling plugin (``ctx._plugin_name``) and is
    never an argument, so a plugin can only release its own ``(object, owner)``
    row — a foreign node's release is unreachable. The call runs in its own
    ``async_session_maker`` session because the service commits. An unknown
    object is a ``CoreDataNotFound``.

    Returns ``True`` when a release row was inserted/deleted and ``False`` for
    an already-in-that-state no-op.
    """
    actor_id = _resolve_write(ctx, user)
    owner = getattr(ctx, "_plugin_name", None)
    if not isinstance(owner, str) or not owner:
        raise CoreDataInvalid(
            "The plugin identity is unavailable for a write-protection release",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    object_uuid = _as_uuid(object_id)
    async with async_session_maker() as session:
        try:
            return await value_lock_service.set_object_write_protection_release(
                session,
                object_uuid,
                released=released,
                owner=owner,
                actor_id=actor_id,
            )
        except ValueError as exc:
            raise CoreDataNotFound(str(exc) or "Object not found") from None


# --- Read-only record-permission check (D-14, 293-07) ------------------------


async def check_record_permission(
    user, entity_type: str, record_id, action: str
) -> bool:
    """Read-only deny-wins record permission check for a plugin route (D-14).

    Delegates to Core's :func:`record_permission_service.resolve_record_permission`
    so the deny-wins resolver is NEVER re-implemented in plugin code. The
    semantics mirror ``require_record_permission`` exactly:

    * a superuser bypasses with zero queries (PERM-05);
    * an explicit record DENY (``False``) is refused even when a broader role
      grants the permission;
    * an explicit ALLOW (``True``) or no record entry (``None``) is permitted —
      the type-level permission the route already declared stands.

    ``entity_type`` must be one of the Core record entity types
    (``source``/``schema``/``object_type``/``attribute_type``/``object``); an
    unknown type or a non-UUID ``record_id`` is a typed ``CoreDataInvalid``
    rather than a silent allow.
    """
    if user is None:
        raise CoreDataInvalid(
            "check_record_permission requires a resolved user",
            code=CODE_ACTOR_UNAVAILABLE,
        )
    if getattr(user, "is_superuser", False):
        return True
    if entity_type not in record_permission_service.VALID_RECORD_ENTITY_TYPES:
        raise CoreDataInvalid(f"Unsupported record entity type {entity_type!r}")
    record_uuid = _as_uuid(record_id)
    resolved = await record_permission_service.resolve_record_permission(
        user_id=str(user.id),
        entity_type=entity_type,
        record_id=record_uuid,
        action=action,
    )
    return resolved is not False


# --- Translation read/write (SEAM-04, D-11/D-12) ------------------------------


async def write_translations(
    ctx, data: TranslationBatchWrite, *, user=None
) -> TranslationBatchResult:
    """Validate and write a data-translation batch atomically (D-11/D-12).

    The actor is Core-resolved (route ``user=`` or the task's persisted
    creator; a hook context is refused with the exact ``hook_context`` code) and
    every entry is authorized against the real owner before the first DML
    (D-15, T-307-01). Every entry is validated before any row is written, all
    rows are written in one independent SDK session and the batch commits exactly
    once; each affected cache tag is invalidated once after success. An expected
    skip is reported in the result — a database failure is never masked as a
    skip.
    """
    from app.plugin_system import translation_access

    actor = await translation_access.resolve_translation_actor(ctx, user)
    payload = _require_dto(data, TranslationBatchWrite, "data")
    entries = [entry.model_dump() for entry in payload.entries]
    async with async_session_maker() as session:
        try:
            result = await translation_service.apply_translation_batch(
                session,
                entries,
                mode=payload.mode,
                actor_id=actor.id,
                authorize=translation_access.batch_authorizer(actor),
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    return TranslationBatchResult(**result)


async def read_translations(
    ctx, request: TranslationReadRequest, *, user=None
) -> TranslationReadResult:
    """Read raw stored + effective/fallback values with provenance (SEAM-04).

    The read is authorized against the real Core owner (translations:read plus
    the mapped entity read right plus the deny-wins record cascade) before any
    value leaves Core (D-15, T-307-02).
    """
    from app.plugin_system import translation_access

    payload = _require_dto(request, TranslationReadRequest, "request")
    await translation_access.authorize_translation_scope(
        ctx,
        translation_access.TranslationScopeRequest(
            translation_type=payload.translation_type,
            entity_id=payload.entity_id,
            action="read",
        ),
        user=user,
    )
    async with async_session_maker() as session:
        try:
            rows = await translation_service.read_translation_values(
                session,
                payload.translation_type,
                payload.entity_id,
                list(payload.fields),
                payload.language_code,
            )
        except HTTPException as exc:
            raise CoreDataInvalid(_bounded(str(exc.detail))) from None
    return TranslationReadResult(
        translation_type=payload.translation_type,
        entity_id=payload.entity_id,
        language_code=payload.language_code,
        rows=[TranslationReadRow(**row) for row in rows],
    )


async def read_translation_entities(
    ctx, request: TranslationEntityPageRequest, *, user=None
) -> TranslationEntityPage:
    """One authorized, keyset-paginated schema entity inventory page (D-05/D-15).

    A neutral inventory/read adapter: Core performs no LLM/suggestion/candidate
    selection and exposes no aggregate count. Every owner is authorized before
    any authored or stored text is serialized (T-307-02).
    """
    from app.plugin_system import translation_access

    payload = _require_dto(request, TranslationEntityPageRequest, "request")
    return await translation_access.read_translation_entities(ctx, payload, user=user)


async def write_ui_translations(
    ctx, data: UiTranslationBatchWrite, *, user=None
) -> TranslationBatchResult:
    """Validate and write a UI (systemui) translation batch atomically (D-11/D-12).

    ``systemui`` is schema-less, so the scope carries only the translation
    right: ``translations:update`` (D-15).
    """
    from app.plugin_system import translation_access

    actor = await translation_access.authorize_ui_translation_scope(
        ctx, action="update", user=user
    )
    payload = _require_dto(data, UiTranslationBatchWrite, "data")
    entries = [
        {
            "ui_string_id": entry.ui_string_id,
            "language_code": entry.language_code,
            "value": entry.value,
        }
        for entry in payload.entries
    ]
    async with async_session_maker() as session:
        try:
            result = await ui_translation_service.apply_ui_translation_batch(
                session,
                entries,
                mode=payload.mode,
                actor_id=actor.id,
                authorize=translation_access.batch_authorizer(actor),
            )
        except ValueError as exc:
            raise _translate_service_error(exc) from None
    # apply_ui_translation_batch re-keys each result onto the UI identity with an
    # extra ``ui_string_id``; drop it so the neutral result DTO validates.
    for entry_result in result["results"]:
        entry_result.pop("ui_string_id", None)
    return TranslationBatchResult(**result)


async def read_ui_translation_catalog(
    ctx, request: UiTranslationCatalogRequest, *, user=None
) -> UiTranslationCatalogResult:
    """Read the systemui catalogue rows with raw/effective/provenance (SEAM-04).

    Requires ``translations:read`` (the schema-less UI scope has no owning
    record) and returns a keyset page whose rows carry their stable UUID and
    authored code default, so a plugin derives neither from a fallback value
    (D-15, T-307-02).
    """
    from app.plugin_system import translation_access

    await translation_access.authorize_ui_translation_scope(
        ctx, action="read", user=user
    )
    payload = _require_dto(request, UiTranslationCatalogRequest, "request")
    async with async_session_maker() as session:
        try:
            page = await ui_translation_service.read_ui_catalog_page(
                session,
                editor_language=payload.editor_language,
                namespace=payload.namespace,
                namespace_is_null=payload.namespace_is_null,
                q=payload.q,
                after=payload.after,
                limit=payload.limit,
            )
        except HTTPException as exc:
            raise CoreDataInvalid(_bounded(str(exc.detail))) from None
    return UiTranslationCatalogResult(
        editor_language=payload.editor_language,
        items=[
            UiTranslationCatalogItem(
                key=item["key"],
                ui_string_id=item["id"],
                namespace=item["namespace"],
                code_default=item["default_text"],
                stored_value=item.get("stored_value"),
                effective_value=item.get("effective_value"),
                fallback_source=item.get("fallback_source") or "code_default",
            )
            for item in page["items"]
        ],
        total=page["total"],
        next_cursor=page["next_cursor"],
    )
