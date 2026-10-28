import asyncio
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import text

from indramesh.sdk import (
    ApiRoutesPlugin,
    ConnectorPlugin,
    ConnectorTypeDeclaration,
    CoreDataConflict,
    CoreDataError,
    CoreDataInvalid,
    CoreDataNotFound,
    ObjectCreate,
    ObjectValueCreate,
    PluginTaskError,
    SchemaTreeAttribute,
    SchemaTreeCreate,
    SchemaTreeObjectType,
    SourceCreate,
    TaskAdmissionRejected,
    TaskStatus,
    TaskTypeDeclaration,
    TaskTypePlugin,
    TranslationBatchWrite,
    TranslationReadRequest,
    TranslationWrite,
    create_object,
    create_object_value,
    create_schema_from_tree,
    create_source,
    read_translations,
    require_permission,
    write_translations,
)

# Phase 286 (D-01): the example plugin's one exact, externally visible Task
# type. `<plugin-name>.<task-name>`, both lowercase kebab-case. It is permanent
# (D-03): a rename would be a new type, never an alias.
EXAMPLE_TASK_TYPE = "example.cleanup-assets"

# CORESEAM-02: the catalog label and the declared runtime metadata the Core
# projects from this declaration. `steps=()` (below) means the runner reports
# free-form progress instead of an ordered business-step sequence.
EXAMPLE_TASK_LABEL = "Clean Up Orphaned Assets"
EXAMPLE_TASK_SCOPE_REQUIREMENTS = ("schema_id",)
EXAMPLE_TASK_REQUIRED_PERMISSION = "sync:trigger"
EXAMPLE_TASK_INVALIDATES = ("object",)

# Phase 303 (SEAM-05): a second reference type that is deliberately kept OUT of
# the generic Create Task dialog (`catalog_visible=False`) while its runs still
# appear in the task list and stay directly startable/retryable and schedulable.
# It names a permission THIS plugin declared (`example-tools:manage`, Plan
# 303-01) — never an authorization gate, only which type the generic create
# menu offers. The permission is platform-managed, so only an Administrator
# (or a superuser) ever sees it in any catalogue surface.
EXAMPLE_HIDDEN_TASK_TYPE = "example.hidden-maintenance"
EXAMPLE_HIDDEN_TASK_LABEL = "Reference Hidden Maintenance"

# D-04/D-05: the plugin-owned parameter schema. Task parameters are ordinary
# inspectable runtime data — never secrets; no `format: password` marker.
#
# `create_sample_object` opts a run into one extra, task-scoped Core-data write
# (CORESEAM-10): the runner creates an Object under `sample_object_type_id`
# inside the task's own `schema_id` and one scalar ObjectValue on it. The type id
# is required only when the flag is set — the conditional is part of the
# validated schema, so a half-configured run is refused at admission.
EXAMPLE_TASK_PARAMETER_SCHEMA = {
    "type": "object",
    "properties": {
        "fail": {"type": "boolean", "default": False},
        "batch_size": {"type": "integer", "default": 10, "minimum": 1},
        "create_sample_object": {"type": "boolean", "default": False},
        "sample_object_type_id": {"type": "string", "format": "uuid"},
    },
    "if": {
        "properties": {"create_sample_object": {"const": True}},
        "required": ["create_sample_object"],
    },
    "then": {"required": ["sample_object_type_id"]},
    "additionalProperties": False,
}

# D-13: a short, explicitly safe business message the Core persists verbatim.
EXAMPLE_TASK_SAFE_ERROR = "Cleanup stopped: some records could not be processed."

# CORESEAM-01: the example's ordinary connector identity. Connector names are
# NOT plugin-qualified, so this descriptor must never shadow a Core built-in
# (`internal`/`api`/`csv`/`jira`) — registration would collide and isolate the
# plugin.
EXAMPLE_CONNECTOR_TYPE = "example"

# CORESEAM-01: the descriptor's display label, carried verbatim by the
# declaration (mirrors EXAMPLE_TASK_LABEL for the connector seam).
EXAMPLE_CONNECTOR_LABEL = "Example Connector"

# Total cooperative yield spread across the reported units, so a live observer
# can see free-form progress while the runner stays well inside the worker's
# JOB_TIMEOUT.
_STEP_DELAY_SECONDS = 0.2

# CORESEAM-02: the declared single-flight admission. Read-only, parameterized
# SQL through the session Core hands to the hook — the plugin's ONLY Core import
# path is ``indramesh.sdk``, which deliberately exports no Task model.
#
# A permanent, schedule-less row IS a schedule TEMPLATE (a definition that has
# already run once), not a run in flight, so it never blocks a start; every
# other pending/running row of the same task type does.
_SIBLING_RUN_SQL = text(
    "SELECT 1 FROM tasks WHERE tasktype = :tasktype "
    "AND current_status IN ('PENDING', 'RUNNING') "
    "AND NOT (is_permanent AND schedule_id IS NULL) "
    "LIMIT 1"
)

# A read-then-insert alone is racy: two concurrent starts would both see no
# sibling. The transaction-scoped advisory lock serializes admission per task
# type until the admitting transaction (which also inserts the Task) ends.
_SINGLE_FLIGHT_LOCK_SQL = text("SELECT pg_advisory_xact_lock(hashtext(:tasktype))")

# CORESEAM-10/D-12: the scalar attribute the sample ObjectValue is written to.
# Read-only through the Core-provided session — the same raw-SQL read path the
# admission hook already uses — because the SDK publishes write operations only
# and deliberately no attribute-read surface. The first non-system attribute of
# the type is chosen, so the write exercises a real ObjectValue row (the system
# Key/Created/Updated bindings are refused and Name is a valueless mirror).
_SAMPLE_VALUE_ATTRIBUTE_SQL = text(
    "SELECT id FROM object_type_attributes "
    "WHERE object_type_id = :object_type_id "
    "AND COALESCE(is_system, false) = false "
    "ORDER BY id LIMIT 1"
)


async def _create_sample_object(ctx, task_id, object_type_id_raw):
    """One task-bound Core-data write through the public SDK (CORESEAM-10).

    The Object is scoped to THIS task's ``schema_id`` and the Core service
    refuses a type that belongs to any other schema, so a mismatched
    ``sample_object_type_id`` fails safely instead of writing. The scalar value
    is written to the type's first non-system attribute. Both SDK calls commit
    on their own: when the value call fails, the already-committed Object stays
    (per-call commits, no invented deduplication).

    A ``CoreDataError`` is a bounded, Core-authored safe message, so it is
    re-raised as a ``PluginTaskError`` the worker persists verbatim; anything
    shaped like a plugin authoring mistake becomes the same safe contract.
    """
    if not object_type_id_raw:
        raise PluginTaskError(
            "sample_object_type_id is required when create_sample_object is enabled"
        )
    try:
        object_type_id = uuid.UUID(str(object_type_id_raw))
    except (TypeError, ValueError):
        raise PluginTaskError("sample_object_type_id must be a UUID string") from None

    try:
        sample = await create_object(
            ctx,
            ObjectCreate(
                schema_id=ctx.task.schema_id,
                object_type_id=object_type_id,
                name=f"example-sample-{task_id}",
            ),
        )
        async with ctx.db() as session:
            attribute_row = (
                await session.execute(
                    _SAMPLE_VALUE_ATTRIBUTE_SQL,
                    {"object_type_id": str(object_type_id)},
                )
            ).first()
        if attribute_row is not None:
            await create_object_value(
                ctx,
                ObjectValueCreate(
                    object_id=sample.id,
                    object_type_attribute_id=attribute_row[0],
                    value_simple=f"example-sample-{task_id}",
                ),
            )
    except CoreDataError as exc:
        raise PluginTaskError(exc.message) from None
    return sample.id


class ExampleConnectorConfig(BaseModel):
    """The example connector's config contract (CORESEAM-01).

    A deliberately generic connector config: ``page_size`` is validated with a
    default so a write's config block has a real schema, while unknown keys stay
    tolerated (the descriptor does not normalize the submitted config).
    """

    page_size: int = 25


# The SDK's stable CoreDataError code for an authorization refusal (307-01). The
# plugin branches on this code, never on message text — and never imports a Core
# module other than ``indramesh.sdk``.
_CODE_NOT_AUTHORIZED = "not_authorized"


class DemoTranslationApply(BaseModel):
    """The strict input of the Example-owned demonstration commit route (T-307-19).

    Exactly one ``(translation_type, entity_id, field, language_code, value)``
    pair — the same pair shape the host hands a contributor ``commitDraft``. No
    actor/owner field exists: ``extra="forbid"`` rejects a payload that tries to
    smuggle one, so Core's own route user is the only actor (never the body).
    """

    model_config = ConfigDict(extra="forbid")

    translation_type: str
    entity_id: uuid.UUID
    field: str
    language_code: str
    value: str


async def _admit_single_flight(session, task_data):
    """Refuse a second concurrent run of the same task type (CORESEAM-02).

    The declaration's ``admit`` hook runs inside Core's pre-persistence
    admission path, so refusing here means NO Task row is created and the Core
    maps :class:`TaskAdmissionRejected` to a bounded 409 with this safe reason.
    A schedule template (``is_permanent`` with no ``schedule_id``) is excluded:
    it is the definition a scheduled run is cloned FROM, so counting it would
    make every scheduled run refuse itself.
    """
    await session.execute(_SINGLE_FLIGHT_LOCK_SQL, {"tasktype": task_data.tasktype})
    existing = (
        await session.execute(_SIBLING_RUN_SQL, {"tasktype": task_data.tasktype})
    ).first()
    if existing is not None:
        raise TaskAdmissionRejected(
            "Another run of this task type is already pending or running."
        )


def _load_database_metadata():
    """Load ``database.py``'s ``MetaData`` (Phase 287 / PLUGDB-01, CORESEAM-06).

    A package-relative import: this directory is loaded as the package
    ``indramesh_plugin_example``, so ``from .database import Base`` resolves
    through that canonical identity — never a second, separately named module.

    Returning ``None`` when the file is absent lets a copied plugin be
    neutralised (for example in tests) without editing Python; a present but
    broken ``database.py`` still fails the import loudly instead of silently
    yielding no metadata.
    """
    database_py = Path(__file__).with_name("database.py")
    if not database_py.is_file():
        return None

    from .database import Base

    return Base.metadata


class ExamplePlugin(ApiRoutesPlugin, TaskTypePlugin, ConnectorPlugin):
    """The directory-scanned reference plugin (CORESEAM-01..06).

    It implements three capabilities that share one manifest: ``api_routes``,
    ``task_type`` and ``connector``. Multi-inheritance is what the manifest's
    ``["api_routes", "task_type", "connector"]`` list must match
    (``capability_declaration_mismatch``).
    """

    # Phase 287 (PLUGDB-01): the plugin's own declarative MetaData, exposed for
    # the core migration runner. The Additive class attribute on
    # ``IndraMeshPlugin`` defaults it to None for non-database plugins.
    database_metadata = _load_database_metadata()

    def register_routes(self, ctx):
        # CORESEAM-05/09/D-05: the Core mount chain independently enforces
        # `plugin-runtime:use`; this plugin-declared gate is ADDITIVE beneath it
        # and comes from the public SDK (indramesh.sdk), so authentication,
        # deny-wins resolution and the superuser bypass stay inside Core.
        #
        # The gate is attached PER ROUTE (never at the router level) so each
        # operation carries exactly its own plugin-declared permission: a later
        # operation with a different gate does not silently inherit
        # `sync:trigger`. Core's mount validation walks each route's own
        # dependency tree either way.
        sync_trigger = Depends(require_permission("sync", "trigger"))
        router = APIRouter()

        @router.get("/ping", dependencies=[sync_trigger])
        async def ping():
            return {"plugin": "example", "message": "pong"}

        # CORESEAM-10/D-05: an authenticated write through the public SDK. The
        # actor is the UserResponse Core's own `require_permission` dependency
        # resolved — the plugin never supplies a user id. The write runs on the
        # captured route context (``ctx``), which carries no task/session, so the
        # SDK resolver uses the route rule. Only `indramesh.sdk` is imported; no
        # Core ORM write and no Core model is returned.
        @router.post("/sources", status_code=201, dependencies=[sync_trigger])
        async def create_source_route(
            payload: SourceCreate,
            user=Depends(require_permission("sources", "create")),
        ):
            try:
                result = await create_source(ctx, payload, user=user)
            except CoreDataNotFound as exc:
                raise HTTPException(status_code=404, detail=exc.message)
            except CoreDataConflict as exc:
                raise HTTPException(status_code=409, detail=exc.message)
            except CoreDataInvalid as exc:
                raise HTTPException(status_code=422, detail=exc.message)
            return {"id": str(result.id)}

        # T-307-19: the Example-owned demonstration commit route. Its route gate
        # is this plugin's own `example-tools:read`; the actor is Core's resolved
        # route user (never a body field) and the public SDK
        # ``write_translations`` read/update/record authority stays authoritative
        # beneath it. ``fill_missing`` fills only an absent value and reports a
        # racing writer as a skip, so the demonstration commit can never clobber
        # a stored translation. No KI permission and no alternative AI path.
        @router.post("/demo-translation/apply")
        async def demo_translation_apply_route(
            payload: DemoTranslationApply,
            user=Depends(require_permission("example-tools", "read")),
        ):
            try:
                result = await write_translations(
                    ctx,
                    TranslationBatchWrite(
                        mode="fill_missing",
                        entries=[
                            TranslationWrite(
                                translation_type=payload.translation_type,
                                entity_id=payload.entity_id,
                                field=payload.field,
                                language_code=payload.language_code,
                                value=payload.value,
                            )
                        ],
                    ),
                    user=user,
                )
            except CoreDataNotFound as exc:
                raise HTTPException(status_code=404, detail=exc.message) from None
            except CoreDataInvalid as exc:
                status_code = (
                    403 if exc.code == _CODE_NOT_AUTHORIZED else 422
                )
                raise HTTPException(status_code=status_code, detail=exc.message) from None
            entry = result.results[0]
            return {
                "status": "written" if entry.written else "skipped",
                "reason": entry.reason,
            }

        return router

    def connector_types(self):
        return [
            ConnectorTypeDeclaration(
                name=EXAMPLE_CONNECTOR_TYPE,
                applies_to=frozenset({"source"}),
                label=EXAMPLE_CONNECTOR_LABEL,
                requires_url=frozenset({"source"}),
                config_model=ExampleConnectorConfig,
            )
        ]

    def task_types(self):
        return [
            TaskTypeDeclaration(
                name=EXAMPLE_TASK_TYPE,
                run=self._run_cleanup,
                steps=(),
                parameter_schema=EXAMPLE_TASK_PARAMETER_SCHEMA,
                label=EXAMPLE_TASK_LABEL,
                schedulable=True,
                scope_requirements=EXAMPLE_TASK_SCOPE_REQUIREMENTS,
                required_permission=EXAMPLE_TASK_REQUIRED_PERMISSION,
                admit=_admit_single_flight,
                invalidates=EXAMPLE_TASK_INVALIDATES,
            ),
            # SEAM-05: the hidden reference type. `catalog_visible=False` only
            # removes it from the generic Create Task dialog; it appears in the
            # task list and can still be started directly or scheduled. It
            # declares this plugin's own `example-tools:manage` permission.
            TaskTypeDeclaration(
                name=EXAMPLE_HIDDEN_TASK_TYPE,
                run=self._run_cleanup,
                steps=(),
                parameter_schema=EXAMPLE_TASK_PARAMETER_SCHEMA,
                label=EXAMPLE_HIDDEN_TASK_LABEL,
                schedulable=False,
                scope_requirements=EXAMPLE_TASK_SCOPE_REQUIREMENTS,
                required_permission="example-tools:manage",
                admit=_admit_single_flight,
                invalidates=EXAMPLE_TASK_INVALIDATES,
                catalog_visible=False,
            ),
        ]

    async def _run_cleanup(self, ctx, task_id, **kwargs):
        """The reference runner: free-form progress, task-bound credentials,
        PARTIAL_SUCCESS and one invalidation request (CORESEAM-04).

        Credentials are read only through the task-bound ``ctx.credentials()``
        accessor, and only when ``ctx.task`` actually carries a bound
        ServiceAccount — there is no account selector, so no path to another
        account. ``fail: true`` raises the named safe-error contract (D-13); a
        failing run deliberately selects no outcome and requests no cache work —
        the Core wrapper owns every terminal write.

        With ``create_sample_object`` set (CORESEAM-10), the run first performs
        one task-scoped Core-data write through the public SDK; its audit actor
        is the task's persisted creator, not the worker.
        """
        should_fail = bool(kwargs.get("fail"))
        batch_size = max(1, int(kwargs.get("batch_size", 10)))
        create_sample_object = bool(kwargs.get("create_sample_object"))

        account_username = None
        if ctx.task.service_account_id is not None:
            account_username = (await ctx.credentials()).username

        sample_object_id = None
        if create_sample_object:
            sample_object_id = await _create_sample_object(
                ctx, task_id, kwargs.get("sample_object_type_id")
            )

        processed = 0
        for index in range(batch_size):
            processed += 1
            await ctx.report_progress(
                processed_objects=processed, total_objects=batch_size
            )
            await asyncio.sleep(_STEP_DELAY_SECONDS / batch_size)
            if should_fail and index + 1 == max(1, batch_size // 2):
                raise PluginTaskError(EXAMPLE_TASK_SAFE_ERROR)

        # Flush the actual final pair even when the small batch was throttled.
        await ctx.report_progress(
            processed_objects=processed, total_objects=batch_size, force=True
        )
        ctx.set_outcome(TaskStatus.PARTIAL_SUCCESS)
        ctx.invalidate(*EXAMPLE_TASK_INVALIDATES)
        return {
            "task_id": str(task_id),
            "tasktype": EXAMPLE_TASK_TYPE,
            "processed": processed,
            "account": account_username,
            "sample_object_id": str(sample_object_id) if sample_object_id else None,
        }

    async def on_load(self, ctx):
        ctx.logger.info("Example plugin loaded")
        return None

    async def on_unload(self, ctx):
        ctx.logger.info("Example plugin unloaded")
        return None

    async def health_check(self, ctx):
        return True

    async def demonstrate_translation_roundtrip(
        self, ctx, *, object_type_id, language_code, value, user=None
    ):
        """Read + fill-missing-write one data translation through the SDK (SEAM-04).

        The reference demonstration of the new translation helpers: a
        ``fill_missing`` batch writes only an absent/empty stored value and the
        subsequent read returns the raw stored value AND the effective value with
        its provenance. The actor is resolved by Core from the context (route
        ``user=`` / the task's persisted creator); the plugin never supplies one.
        """
        write_result = await write_translations(
            ctx,
            TranslationBatchWrite(
                mode="fill_missing",
                entries=[
                    TranslationWrite(
                        translation_type="object_type",
                        entity_id=object_type_id,
                        field="name",
                        language_code=language_code,
                        value=value,
                    )
                ],
            ),
            user=user,
        )
        read_result = await read_translations(
            ctx,
            TranslationReadRequest(
                translation_type="object_type",
                entity_id=object_type_id,
                fields=["name"],
                language_code=language_code,
            ),
            user=user,
        )
        return write_result, read_result

    async def demonstrate_schema_tree_create(
        self, ctx, *, source_id, schema_name, user=None
    ):
        """Create one schema from a small tree through the strict SDK path (SEAM-04).

        The reference demonstration of the new ``create_schema_from_tree``
        operation: a parent/child hierarchy with an alias-typed select attribute
        and a reference binding is validated in full and created atomically. The
        actor is Core-resolved (route ``user=`` / the task's persisted creator);
        the plugin never supplies origin or ORM ids.
        """
        tree = SchemaTreeCreate(
            source_id=source_id,
            name=schema_name,
            description="Created by the example plugin schema-from-tree seam.",
            statuses=[],
            object_types=[
                SchemaTreeObjectType(
                    name="ExampleRoot",
                    icon_key="box",
                    attributes=[
                        SchemaTreeAttribute(
                            name="Hostname", type_string="string", required=True
                        ),
                        SchemaTreeAttribute(
                            name="Environment",
                            type_string="select",
                            select_options=["Production", "Staging"],
                        ),
                    ],
                ),
                SchemaTreeObjectType(
                    name="ExampleChild",
                    parent_name="ExampleRoot",
                    attributes=[
                        SchemaTreeAttribute(
                            name="Tier", type_string="int",
                        ),
                        SchemaTreeAttribute(
                            name="Root Link",
                            type_string="foreign key",
                            referenced_object_type_name="ExampleRoot",
                        ),
                    ],
                ),
            ],
        )
        return await create_schema_from_tree(ctx, tree, user=user)
