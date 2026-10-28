"""The Core-side definition of the plugin SDK surface (CORESEAM-05).

Plugins import this surface ONLY through the public path ``indramesh.sdk``
(``backend/indramesh/sdk``), which re-exports this module's ``__all__``
unchanged, so a name added here becomes public there automatically. A plugin
must import no ``app.*`` module, this one included: the AST gate in
``tests/test_plugin_discovery.py`` scans every git-tracked ``plugins/**/*.py``
and rejects any such import, aliased ``import`` and ``from`` forms included.

The surface is deliberately narrow and explicit (``__all__`` below):

* the plugin base class and the capability interfaces a plugin subclasses,
* the immutable declaration records a capability returns,
* the bounded, Core-authored errors a plugin may raise,
* the task-bound runtime types the plugin context hands back
  (``TaskCredentials``, ``TaskStatus``, ``ALLOWED_SUCCESS_OUTCOMES``),
* the shared secret column type and the public cache/outbox helpers a plugin's
  own models and post-run effects use,
* ``require_permission(resource, action)`` — the UNCHANGED Core FastAPI
  dependency factory. A plugin route can add a specific permission of its own
  with it, while authentication (``get_current_user``), deny-wins
  effective-permission resolution and the superuser bypass stay inside Core,
* the provider-neutral LLM surface (D-09, 302-04): the Core-authored
  ``LLMClient`` facade type plus the typed errors a plugin may catch. A plugin
  obtains the client through ``ctx.llm`` and never the raw vendor client or the
  API key.

Usage rules for the Core-data write surface (CORESEAM-10, D-01..D-14)
--------------------------------------------------------------------

1. **Actor supply.** A plugin ROUTE adds its own ``require_permission(...)`` and
   passes the ``UserResponse`` Core resolved as ``user=`` to every SDK call
   (``create_source(ctx, data, user=user)``). A plugin TASK passes NO ``user``:
   the acting user is the task's persisted ``creator_id``, resolved by Core, and
   an explicit ``user=`` in a task context is invalid input.
2. **One session, one commit per call (D-12).** The SDK opens its own
   ``async_session_maker()`` session for every call and the delegated Core
   service commits it before the call returns. There is no multi-call
   transaction and no cross-call rollback: an interruption between two calls
   leaves the earlier committed write in place (partial success), and a
   repeated unkeyed call is a repeated write. Where a plugin needs replay-safe
   behavior it opts in explicitly: a task-bound ``idempotency_key`` on the keyed
   creates (Schema, ObjectType, ObjectTypeAttribute, Status, ReferenceType,
   Object) derives a deterministic Core id and reads the committed row back, and
   the connector media upserts derive their id from
   ``{source}:{object}:{external_id}`` and short-circuit a replay. The per-call
   commit boundary still applies to every call, keyed or not.
3. **Hooks must not write.** A hook context borrows the caller's transaction, so
   the SDK refuses every write from it with the stable ``hook_context`` code.
   To react to a hook, schedule a task (the supported path) rather than writing
   Core data inline.
4. **Trust model (D-14).** Plugins run in-process with full host access and are
   trusted; attribution is correct BY CONSTRUCTION for a cooperative plugin, not
   a defense against a hostile one. Isolation is PLUGISO-01 (v2). Do not build
   anti-forgery machinery on this surface.
5. **Deprecation (D-08).** The surface stays broadly additive: if an export is
   superseded, the old name stays available as deprecated for at least one full
   release cycle after its replacement ships. This promise has one explicit,
   coordinated exception — the v20 cut-over changes the connector-media identity
   inputs to ``external_id`` and removes the Target cloud/conflict-strategy
   inputs, rejects the retired inputs with no alias and updates every tracked
   consumer in the same release (see "Coordinated breaking SDK contract" below).
   That specific input change is breaking and is not covered by the additive
   promise; DTO class and operation export names are unchanged.

Coordinated breaking SDK contract (v20 cut-over)
------------------------------------------------

The v20 Jira-connector cut-over changes two public SDK input contracts as one
coordinated breaking change. There is no compatibility alias and no separate
deploy step: the cut-over ships as one release.

* **Connector media.** ``ConnectorCommentCreate`` and ``ConnectorFileCreate``
  take the connector-neutral ``external_id`` (required ``str``, length 1..100)
  instead of a connector-named identity input. The same remote id still feeds the
  deterministic ``{source}:{object}:{external_id}`` id, so a replay of an
  unchanged item returns the committed row with ``created=False`` and stores no
  second blob — the media idempotency identity is unchanged.
* **Target.** ``TargetCreate`` and ``TargetUpdate`` no longer accept the cloud
  flag or the default conflict strategy. A strict input (``extra="forbid"``)
  rejects any retired or renamed field.

All tracked consumers change in the same release and no Core data is preserved or
migrated. DTO class and operation export names (``ConnectorCommentCreate``,
``upsert_connector_comment``, ``TargetCreate``, ...) are unchanged, and the
deprecation promise for unrelated exports stands. On the REST side the retired
request keys are rejected selectively by presence (a tombstone), while the SDK
inputs are strict — the two contracts stay deliberately different. Native
provenance stays Core-owned: a plugin still cannot supply ``managed_by``, a local
actor/uploader or a Core row id, and Core keeps the task actor, source and
ownership guards.

Deliberately NOT exported: ``get_current_user``, ``resolve_user_permissions``,
``require_record_permission``, the loader's path-import helpers, and every
plugin-registry/lifecycle internal. Importing those from a plugin is a boundary
violation the AST gate fails on.
"""

from app.crypto import EncryptedString
from app.llm import (
    LLMAuthFailed,
    LLMClient,
    LLMConfigError,
    LLMError,
    LLMModelNotFound,
    LLMNotConfigured,
    LLMRateLimited,
    LLMResponseInvalid,
    LLMTimeout,
    LLMUnreachable,
)
from app.models.index_outbox import IndexOutbox, IndexOutboxStatus
from app.models.task import TaskStatus
from app.plugin_system.base import IndraMeshPlugin
from app.plugin_system.capabilities import (
    ApiRoutesPlugin,
    ConnectorPlugin,
    ConnectorSession,
    GraphExtensionPlugin,
    PluginTaskError,
    SearchProviderPlugin,
    TaskAdmissionRejected,
    TaskTypeDeclaration,
    TaskTypePlugin,
)
from app.plugin_system.connector_registry import ConnectorTypeDeclaration
from app.plugin_system.context import ALLOWED_SUCCESS_OUTCOMES
from app.plugin_system.core_data import (
    CoreDataConflict,
    CoreDataError,
    CoreDataInvalid,
    CoreDataNotFound,
    CoreWriteResult,
    check_record_permission,
    create_object,
    create_object_comment,
    create_object_file,
    create_object_ref,
    create_object_type,
    create_object_type_attribute,
    create_object_value,
    create_reference_type,
    create_schema,
    create_schema_from_tree,
    create_source,
    create_status,
    create_target,
    delete_object,
    delete_object_comment,
    delete_object_file,
    delete_connector_comment,
    delete_connector_file,
    delete_object_ref,
    delete_object_type,
    delete_object_type_attribute,
    delete_object_value,
    delete_reference_type,
    delete_schema,
    delete_source,
    delete_status,
    delete_target,
    read_translations,
    read_translation_entities,
    read_ui_translation_catalog,
    set_attribute_write_protection,
    set_object_write_protection_release,
    update_object,
    update_object_ref,
    update_object_type,
    update_object_type_attribute,
    update_object_value,
    update_reference_type,
    update_schema,
    update_source,
    update_status,
    update_target,
    upsert_connector_comment,
    upsert_connector_file,
    validate_schema_tree_draft,
    write_translations,
    write_ui_translations,
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
    SchemaTreeAttribute,
    SchemaTreeCreate,
    SchemaTreeDraft,
    SchemaTreeObjectType,
    SchemaTreeStatus,
    SchemaUpdate,
    SourceCreate,
    SourceUpdate,
    StatusCreate,
    StatusUpdate,
    TargetCreate,
    TargetUpdate,
    TranslationBatchEntryResult,
    TranslationBatchResult,
    TranslationBatchWrite,
    TranslationEntityPage,
    TranslationEntityPageRequest,
    TranslationEntityProjection,
    TranslationReadRequest,
    TranslationReadResult,
    TranslationReadRow,
    TranslationWrite,
    UiTranslationBatchWrite,
    UiTranslationCatalogItem,
    UiTranslationCatalogRequest,
    UiTranslationCatalogResult,
    UiTranslationWrite,
)
from app.services.auth_service import require_permission
from app.services.cache_service import invalidate_entity_cache
from app.services.task_service import TaskCredentials
from app.plugin_system.translation_access import (
    TranslationRecordOwner,
    TranslationScopeRequest,
    TranslationScopeResult,
    authorize_translation_scope,
)

__all__ = [
    # Plugin base + capability interfaces.
    "IndraMeshPlugin",
    "ApiRoutesPlugin",
    "ConnectorPlugin",
    "ConnectorSession",
    "GraphExtensionPlugin",
    "SearchProviderPlugin",
    "TaskTypePlugin",
    # Immutable declarations a capability returns.
    "ConnectorTypeDeclaration",
    "TaskTypeDeclaration",
    # Bounded, Core-authored errors a plugin may raise.
    "PluginTaskError",
    "TaskAdmissionRejected",
    # Task-bound runtime types (what ``ctx`` hands back).
    "ALLOWED_SUCCESS_OUTCOMES",
    "TaskCredentials",
    "TaskStatus",
    # Core helpers for a plugin's own models and post-run effects.
    "EncryptedString",
    "IndexOutbox",
    "IndexOutboxStatus",
    "invalidate_entity_cache",
    # Authorization: the unchanged Core dependency factory.
    "require_permission",
    # Provider-neutral LLM surface (D-09, 302-04): the Core-authored facade and
    # the typed errors a plugin may catch. The raw vendor client and the API key
    # accessor are deliberately NOT exported — a plugin reaches the client only
    # through ``ctx.llm``, enforced by the plugin AST gate.
    "LLMClient",
    "LLMError",
    "LLMConfigError",
    "LLMNotConfigured",
    "LLMAuthFailed",
    "LLMUnreachable",
    "LLMTimeout",
    "LLMModelNotFound",
    "LLMRateLimited",
    "LLMResponseInvalid",
    # Read-only record-level authorization (D-14, 293-07): the plugin route
    # delegates the deny-wins record resolver to Core instead of re-implementing
    # it in plugin SQL.
    "check_record_permission",
    # Plugin-owned attribute write protection (D-08, 294-01): the marking plugin
    # sets/lifts its own marker and is the sole writer exempt from it.
    "set_attribute_write_protection",
    # Plugin-owned per-object release (D-11, 294-01): the marking plugin unlocks
    # single objects it detaches for every native writer.
    "set_object_write_protection_release",
    # Core-data write surface (CORESEAM-10): typed errors, neutral result and
    # strict connector-neutral inputs plus the Source/Target CRUD adapters.
    "CoreDataError",
    "CoreDataNotFound",
    "CoreDataInvalid",
    "CoreDataConflict",
    "CoreWriteResult",
    "SourceCreate",
    "SourceUpdate",
    "TargetCreate",
    "TargetUpdate",
    "create_source",
    "update_source",
    "delete_source",
    "create_target",
    "update_target",
    "delete_target",
    "SchemaCreate",
    "SchemaUpdate",
    "create_schema",
    "update_schema",
    "delete_schema",
    # Strict schema-from-tree create (SEAM-04, D-09/D-10): the whole tree is
    # validated before any write and rejected in full with a concrete indexed
    # path; a source-local name/key conflict creates nothing.
    "SchemaTreeCreate",
    "SchemaTreeDraft",
    "SchemaTreeObjectType",
    "SchemaTreeAttribute",
    "SchemaTreeStatus",
    "create_schema_from_tree",
    "validate_schema_tree_draft",
    "ObjectTypeCreate",
    "ObjectTypeUpdate",
    "create_object_type",
    "update_object_type",
    "delete_object_type",
    "ObjectTypeAttributeCreate",
    "ObjectTypeAttributeUpdate",
    "create_object_type_attribute",
    "update_object_type_attribute",
    "delete_object_type_attribute",
    # Status + ReferenceType CRUD (D-10/D-11): native Core-managed catalog rows
    # stay Core-managed (no AttributeType/language mutators are exported).
    "StatusCreate",
    "StatusUpdate",
    "ReferenceTypeCreate",
    "ReferenceTypeUpdate",
    "create_status",
    "update_status",
    "delete_status",
    "create_reference_type",
    "update_reference_type",
    "delete_reference_type",
    # Object lifecycle (D-10/D-12): native keygen, lock, audit and the
    # transactional search outbox stay owned by the Core service.
    "ObjectCreate",
    "ObjectUpdate",
    "create_object",
    "update_object",
    "delete_object",
    # Object content (D-12): scalar values and reference links delegate to the
    # Core services, so the DECOUPLE-06 lock, typed value/binding validation,
    # audit and cache stay identical to the REST path.
    "ObjectValueCreate",
    "ObjectValueUpdate",
    "create_object_value",
    "update_object_value",
    "delete_object_value",
    "ObjectRefCreate",
    "ObjectRefUpdate",
    "create_object_ref",
    "update_object_ref",
    "delete_object_ref",
    # Object content — comments (D-12/D-13): author derives from the resolved
    # actor and the Core service keeps its managed-row/dual-ID delete guard; no
    # update surface is published because no Core update operation exists.
    "ObjectCommentCreate",
    "create_object_comment",
    "delete_object_comment",
    # Connector-origin media (D-13): separate inputs/ops that stamp Core-owned
    # ``managed_by`` and a deterministic id, keeping a connector-imported comment
    # external and read-only. The neutral native names above stay
    # behaviour-identical and never admit provenance fields.
    "ConnectorCommentCreate",
    "upsert_connector_comment",
    "delete_connector_comment",
    # Connector-origin attachment (D-13): metadata DTO + a separate async stream,
    # a deterministic Core row id, Core-owned provenance and blob compensation.
    "ConnectorFileCreate",
    "upsert_connector_file",
    "delete_connector_file",
    # Object content — files (D-12/D-13): pure-metadata create input plus a
    # separate plugin stream, streamed through the existing Core storage service
    # (size limit, blob compensation, managed-row guard); no update surface.
    "ObjectFileCreate",
    "create_object_file",
    "delete_object_file",
    # Translation read/write surface (SEAM-04, D-11/D-12): strict DTOs plus the
    # read/write helpers. A write has an explicit fill_missing/replace mode, is
    # atomic per call and reports expected skips; reads expose raw + effective
    # values with their provenance so a non-empty fallback is never mistaken for
    # an explicit target-language translation.
    "TranslationWrite",
    "TranslationBatchWrite",
    "TranslationBatchEntryResult",
    "TranslationBatchResult",
    "TranslationReadRequest",
    "TranslationReadRow",
    "TranslationReadResult",
    "read_translations",
    "write_translations",
    # Translation scope authorization (D-14/D-15): Core resolves the real actor
    # and the real owner records and reports whether an update is permitted, so
    # actor attribution is never mistaken for authorization (T-307-01).
    "TranslationScopeRequest",
    "TranslationScopeResult",
    "TranslationRecordOwner",
    "authorize_translation_scope",
    # Neutral schema entity inventory page (D-05/D-15): a keyset-paginated,
    # authorized read of authored source and raw stored targets with no Core ORM
    # import and no aggregate count.
    "TranslationEntityPageRequest",
    "TranslationEntityProjection",
    "TranslationEntityPage",
    "read_translation_entities",
    "UiTranslationWrite",
    "UiTranslationBatchWrite",
    "read_ui_translation_catalog",
    "write_ui_translations",
    "UiTranslationCatalogRequest",
    "UiTranslationCatalogItem",
    "UiTranslationCatalogResult",
]