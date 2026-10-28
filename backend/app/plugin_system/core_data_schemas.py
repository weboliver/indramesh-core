"""Connector-neutral SDK input contracts for Core-data writes (CORESEAM-10).

These DTOs are the public, versioned inputs a plugin passes to the SDK's
Core-data write adapters (``backend/app/plugin_system/core_data.py``). They are
deliberately **independent, field-listed** Pydantic classes rather than
subclasses of the shared REST schemas in ``app/schemas/crud.py``:

* ``extra="forbid"`` — an unknown field (for example a retired connector
  transition field, or the legacy ``Target.type`` duplicate) is a validation
  error, never silently dropped (D-09, T-291.1-02).
* the field set, defaults, constraints and PATCH (``*Update``) nullable
  semantics mirror the REST schemas exactly, so an SDK write has the same Core
  service behaviour as the REST path (D-06, CORESEAM-10).
* the connector-neutral public contract carries no connector transition field,
  so the SDK input stays neutral across the v20 cut-over (D-08/D-09).

Only the public SDK names live here; the adapters convert these inputs into the
internal REST DTOs module-qualified (``crud.SourceCreate``) so the two families
can never be confused.
"""

import uuid
from datetime import datetime
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.schemas.object_comment import sanitize_comment_body
from app.validators import EscapedStr, PlainStr

# Every SDK input rejects an unknown field instead of ignoring it.
_STRICT = ConfigDict(extra="forbid")

# Mirrors ``app/schemas/crud.HEX_PATTERN``: the native colour fields accept
# exactly a 6-digit hex value, the same constraint the REST contract enforces
# (D-06). Kept local so the frozen SDK input does not import a REST module.
_HEX_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class SourceCreate(BaseModel):
    """SDK input for creating a Source (mirrors the REST create contract)."""

    model_config = _STRICT

    name: EscapedStr = Field(max_length=255)
    type: EscapedStr = Field(max_length=50)
    source_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    import_folder: Optional[EscapedStr] = Field(default=None, max_length=255)


class SourceUpdate(BaseModel):
    """SDK input for patching a Source (PATCH: omission and explicit ``None``
    both leave a field unchanged, exactly like the REST update contract)."""

    model_config = _STRICT

    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    type: Optional[EscapedStr] = Field(default=None, max_length=50)
    source_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    import_folder: Optional[EscapedStr] = Field(default=None, max_length=255)


class TargetCreate(BaseModel):
    """SDK input for creating a Target.

    Connector-neutral (D-05/D-09): the connector identity is ``connector_type``.
    The retired legacy ``type`` duplicate, the cloud flag and the default
    conflict strategy are not public inputs; ``extra="forbid"`` rejects them.
    """

    model_config = _STRICT

    schema_id: Optional[uuid.UUID] = None
    name: EscapedStr = Field(max_length=255)
    target_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    export_folder: Optional[EscapedStr] = Field(default=None, max_length=255)
    connector_type: EscapedStr = Field(max_length=50)
    is_active: bool = True


class TargetUpdate(BaseModel):
    """SDK input for patching a Target (PATCH semantics as the REST contract).

    Like :class:`TargetCreate`, the cloud flag and the default conflict strategy
    are not inputs (D-05); omission and explicit ``None`` both leave a field
    unchanged.
    """

    model_config = _STRICT

    schema_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    target_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    export_folder: Optional[EscapedStr] = Field(default=None, max_length=255)
    connector_type: Optional[EscapedStr] = Field(default=None, max_length=50)
    is_active: Optional[bool] = None


class SchemaCreate(BaseModel):
    """SDK input for creating a Schema (mirrors the REST create contract).

    Connector-neutral (D-09): deliberately omits the retired connector
    transition fields and the connector-occupied ``key`` column (a pull used to
    overwrite it from the remote schema key). The native IndraMesh prefix is set
    through ``keygen`` only.
    """

    model_config = _STRICT

    source_id: uuid.UUID
    keygen: Optional[EscapedStr] = Field(default=None, max_length=100)
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    primary_color: EscapedStr = Field(default="#2563EB", max_length=7, pattern=_HEX_PATTERN)
    header_bg_color: EscapedStr = Field(default="#1E293B", max_length=7, pattern=_HEX_PATTERN)
    sidebar_bg_color: EscapedStr = Field(default="#F1F5F9", max_length=7, pattern=_HEX_PATTERN)
    section_bg_color: EscapedStr = Field(default="#F8FAFC", max_length=7, pattern=_HEX_PATTERN)
    surface_bg_color: EscapedStr = Field(default="#FFFFFF", max_length=7, pattern=_HEX_PATTERN)
    dark_primary_color: EscapedStr = Field(default="#3B82F6", max_length=7, pattern=_HEX_PATTERN)
    dark_header_bg_color: EscapedStr = Field(default="#0F172A", max_length=7, pattern=_HEX_PATTERN)
    dark_sidebar_bg_color: EscapedStr = Field(default="#1E293B", max_length=7, pattern=_HEX_PATTERN)
    dark_section_bg_color: EscapedStr = Field(default="#0F172A", max_length=7, pattern=_HEX_PATTERN)
    dark_surface_bg_color: EscapedStr = Field(default="#1E293B", max_length=7, pattern=_HEX_PATTERN)
    status_filter_config: Optional[dict] = None


class SchemaUpdate(BaseModel):
    """SDK input for patching a Schema (PATCH semantics as the REST contract).

    ``default_language_id`` is a nullable PATCH field: setting it to ``null``
    clears the default, and omission leaves it unchanged (D-07 escape hatch).
    Like :class:`SchemaCreate`, it exposes no connector-occupied ``key`` (D-09) —
    the native prefix is patched through ``keygen`` only.
    """

    model_config = _STRICT

    source_id: Optional[uuid.UUID] = None
    keygen: Optional[EscapedStr] = Field(default=None, max_length=100)
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    primary_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    header_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    sidebar_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    section_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    surface_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    dark_primary_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    dark_header_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    dark_sidebar_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    dark_section_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    dark_surface_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=_HEX_PATTERN)
    status_filter_config: Optional[dict] = None
    default_language_id: Optional[uuid.UUID] = None


class ObjectTypeCreate(BaseModel):
    """SDK input for creating an ObjectType (mirrors the REST create contract).

    Connector-neutral (D-09): deliberately omits the retired connector
    transition fields and the connector ownership markers (``managed_by``,
    ``externally_readonly``) the ObjectType row carries; those markers are
    Core-owned provenance, never a plugin input.
    """

    model_config = _STRICT

    schema_id: uuid.UUID
    parent_id: Optional[uuid.UUID] = None
    inherits_attributes: bool = False
    is_abstract: bool = False
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    sort: int = 0
    label_attribute_id: Optional[uuid.UUID] = None
    icon_id: Optional[uuid.UUID] = None
    multilingual: bool = False
    # Read-only on the response (derived from the icon); accepted on create only
    # to mirror the REST create contract exactly (D-06).
    icon_local_name: Optional[EscapedStr] = Field(default=None, max_length=100)


class ObjectTypeUpdate(BaseModel):
    """SDK input for patching an ObjectType (PATCH semantics as the REST contract)."""

    model_config = _STRICT

    schema_id: Optional[uuid.UUID] = None
    parent_id: Optional[uuid.UUID] = None
    inherits_attributes: Optional[bool] = None
    is_abstract: Optional[bool] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    sort: Optional[int] = None
    label_attribute_id: Optional[uuid.UUID] = None
    icon_id: Optional[uuid.UUID] = None
    multilingual: Optional[bool] = None


class ObjectTypeAttributeCreate(BaseModel):
    """SDK input for creating an ObjectTypeAttribute.

    ``attribute_type_id`` references an existing Core-managed ``AttributeType``
    lookup row only (D-11) — the SDK never creates or mutates the catalog.
    Connector-neutral (D-09): the retired connector transition fields are
    omitted.

    ``confidentiality``, ``personal_data`` and the export-control list are
    classification label ids from the Core catalogue (D-07); 1.0.0 stores them
    as markers only.
    """

    model_config = _STRICT

    object_type_id: uuid.UUID
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    sort: int = 0
    attribute_type_id: uuid.UUID
    is_required: bool = False
    minimum_cardinality: int = 0
    maximum_cardinality: int = -1
    is_unique: bool = False
    is_hidden: bool = False
    default_value: Optional[EscapedStr] = Field(default=None, max_length=2000)
    structure: Optional[dict] = None
    referenced_object_type_id: Optional[uuid.UUID] = None
    reference_type_id: Optional[uuid.UUID] = None
    multilingual: bool = False
    confidentiality: Optional[uuid.UUID] = None
    personal_data: bool = False
    export_control: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ObjectTypeAttributeUpdate(BaseModel):
    """SDK input for patching an ObjectTypeAttribute (PATCH semantics as REST).

    Classification label ids follow the REST update semantics: an omitted field
    is unchanged, an explicit ``null`` clears confidentiality and the
    export-control list, and a ``null`` personal-data marker is unchanged.
    """

    model_config = _STRICT

    object_type_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    sort: Optional[int] = None
    attribute_type_id: Optional[uuid.UUID] = None
    is_required: Optional[bool] = None
    minimum_cardinality: Optional[int] = None
    maximum_cardinality: Optional[int] = None
    is_unique: Optional[bool] = None
    is_hidden: Optional[bool] = None
    default_value: Optional[EscapedStr] = Field(default=None, max_length=2000)
    structure: Optional[dict] = None
    referenced_object_type_id: Optional[uuid.UUID] = None
    reference_type_id: Optional[uuid.UUID] = None
    multilingual: Optional[bool] = None
    confidentiality: Optional[uuid.UUID] = None
    personal_data: Optional[bool] = None
    export_control: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)


class StatusCreate(BaseModel):
    """SDK input for creating a Status (mirrors the REST create contract).

    Connector-neutral (D-09/D-11): the retired connector transition field is
    omitted. ``status_category`` is normalized exactly like REST (``active`` →
    ``Active``).
    """

    model_config = _STRICT

    schema_id: uuid.UUID
    name: EscapedStr = Field(max_length=100)
    color: Optional[EscapedStr] = Field(default=None, max_length=7)
    order: int = 0
    status_category: Optional[EscapedStr] = Field(default=None, max_length=20)

    @field_validator("status_category")
    @classmethod
    def normalize_status_category(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return {"active": "Active", "pending": "Pending", "inactive": "Inactive"}.get(
            value.lower(), value
        )


class StatusUpdate(BaseModel):
    """SDK input for patching a Status (PATCH semantics as the REST contract)."""

    model_config = _STRICT

    schema_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=100)
    color: Optional[EscapedStr] = Field(default=None, max_length=7)
    order: Optional[int] = None
    status_category: Optional[EscapedStr] = Field(default=None, max_length=20)

    @field_validator("status_category")
    @classmethod
    def normalize_status_category(cls, value: Optional[str]) -> Optional[str]:
        if value is None:
            return None
        return {"active": "Active", "pending": "Pending", "inactive": "Inactive"}.get(
            value.lower(), value
        )


class ReferenceTypeCreate(BaseModel):
    """SDK input for creating a ReferenceType (mirrors the REST create contract).

    Connector-neutral (D-09/D-11): the retired connector transition fields are
    omitted.
    """

    model_config = _STRICT

    reference: EscapedStr = Field(max_length=100)
    description: Optional[PlainStr] = Field(default=None, max_length=500)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)
    source_id: uuid.UUID
    schema_id: Optional[uuid.UUID] = None


class ReferenceTypeUpdate(BaseModel):
    """SDK input for patching a ReferenceType (PATCH semantics as the REST
    contract); the retired connector transition fields are omitted (D-09)."""

    model_config = _STRICT

    reference: Optional[EscapedStr] = Field(default=None, max_length=100)
    description: Optional[PlainStr] = Field(default=None, max_length=500)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)
    source_id: Optional[uuid.UUID] = None
    schema_id: Optional[uuid.UUID] = None


class ObjectCreate(BaseModel):
    """SDK input for creating an Object (mirrors the REST native create path).

    Connector-neutral (D-06/D-09): the retired connector transition fields are
    omitted, and there is deliberately no ``key`` field — the Core service owns
    native keygen and mirroring. A caller that tries to set ``key`` or a retired
    transition field is a validation error, never a silently accepted identity
    override. ``confidentiality``/``personal_data``/``export_control`` carry
    catalogue label ids (D-07) and are written with the row in one transaction.
    """

    model_config = _STRICT

    schema_id: uuid.UUID
    object_type_id: uuid.UUID
    name: EscapedStr = Field(max_length=500)
    confidentiality: Optional[uuid.UUID] = None
    personal_data: bool = False
    export_control: list[uuid.UUID] = Field(default_factory=list, max_length=50)


class ObjectUpdate(BaseModel):
    """SDK input for patching an Object (PATCH semantics as the REST contract).

    The native ``key`` and the sync/transition fields are not settable through
    the SDK; omission or explicit ``None`` leaves a field unchanged.
    ``confidentiality``/``personal_data``/``export_control`` follow the REST
    label semantics (label ids; ``None``/``[]`` clear a family), and a payload
    that only sets those fields has no index, hook or timestamp side effect.
    """

    model_config = _STRICT

    schema_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=500)
    confidentiality: Optional[uuid.UUID] = None
    personal_data: Optional[bool] = None
    export_control: Optional[list[uuid.UUID]] = Field(default=None, max_length=50)


class ObjectValueCreate(BaseModel):
    """SDK input for creating an ObjectValue (mirrors the REST scalar contract).

    Connector-neutral (D-06/D-09): only the scalar payload columns a plugin may
    write are settable. System-attribute bindings and typed-value validation
    stay owned by the Core service, and no connector transition field appears
    here.
    """

    model_config = _STRICT

    object_id: uuid.UUID
    object_type_attribute_id: uuid.UUID
    value_float: Optional[float] = None
    value_integer: Optional[int] = None
    value_boolean: Optional[bool] = None
    value_simple: Optional[PlainStr] = Field(default=None, max_length=64000)
    value_datetime: Optional[datetime] = None
    value_struct: Optional[dict] = None


class ObjectValueUpdate(BaseModel):
    """SDK input for patching an ObjectValue (PATCH semantics as REST).

    The owning object/attribute are immutable after create, so they are not part
    of the update contract; omission or explicit ``None`` leaves a value
    unchanged.
    """

    model_config = _STRICT

    value_float: Optional[float] = None
    value_integer: Optional[int] = None
    value_boolean: Optional[bool] = None
    value_simple: Optional[PlainStr] = Field(default=None, max_length=64000)
    value_datetime: Optional[datetime] = None
    value_struct: Optional[dict] = None


class ObjectRefCreate(BaseModel):
    """SDK input for creating an ObjectRef (mirrors the REST create contract).

    Connector-neutral (D-06/D-09): only the Core binding identity a plugin may
    set is part of the frozen SDK contract.
    """

    model_config = _STRICT

    from_object_id: uuid.UUID
    to_object_id: uuid.UUID
    object_type_id: uuid.UUID
    source_id: uuid.UUID
    from_attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)


class ObjectRefUpdate(BaseModel):
    """SDK input for patching an ObjectRef (PATCH semantics as the REST contract)."""

    model_config = _STRICT

    from_object_id: Optional[uuid.UUID] = None
    to_object_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    source_id: Optional[uuid.UUID] = None
    from_attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)


class ObjectCommentCreate(BaseModel):
    """SDK input for creating an ObjectComment (mirrors the REST create contract).

    Connector-neutral (D-06/D-09): the author is never an input — it is derived
    from the resolved route/task actor (D-04/D-05, T-291.1-16) — and the
    externally-managed provenance markers (``managed_by``,
    ``external_author_name``) are forbidden, so a plugin can never forge an
    external origin (T-291.1-17). The comment service has no update operation, so
    there is deliberately no ``ObjectCommentUpdate`` (D-13).
    """

    model_config = _STRICT

    object_id: uuid.UUID
    body: str

    @field_validator("body")
    @classmethod
    def _trim_and_validate(cls, value: str) -> str:
        # WR-02: reuse the canonical hardening helper so the native SDK path
        # and the REST/sync path share ONE implementation and cannot drift.
        return sanitize_comment_body(value)


class ObjectFileCreate(BaseModel):
    """SDK input for creating a native ObjectFile — pure metadata only (D-09).

    The file bytes travel as a **separate** stream argument to
    ``create_object_file(ctx, data, stream, ...)``; they are never a DTO field,
    so the frozen input carries no storage key, no caller-set size and no
    externally-managed provenance (``managed_by``/``external_author_name`` are
    forbidden by ``extra="forbid"``). The uploader is
    derived from the resolved actor (D-04/D-05, T-291.1-16) and the Core file
    service keeps its sniffing, size limit, blob compensation and managed-row
    guard (T-291.1-18). No ``ObjectFileUpdate`` exists (D-13).
    """

    model_config = _STRICT

    object_id: uuid.UUID
    filename: str = Field(min_length=1, max_length=500)
    content_type: Optional[str] = Field(default=None, max_length=255)


class ConnectorCommentCreate(BaseModel):
    """SDK input for creating a **connector-origin** ObjectComment (D-13).

    This is a separate contract from ``ObjectCommentCreate``: it exists so a
    connector pull can persist an externally-authored, read-only comment whose
    provenance is Core-owned (``managed_by``), the author is the bounded external
    display text (``author_id`` stays NULL) and the row id is the deterministic
    id derived from ``{source}:{object}:{external id}`` (D-12).

    Deliberately absent, so a plugin can never forge them (T-293-06-02):
    ``managed_by`` (Core stamps the task plugin name), a local actor/author id,
    and any Core row id. ``extra="forbid"`` rejects them all. The body reuses the
    canonical ``sanitize_comment_body`` hardening helper.

    ``external_id`` is the connector-neutral external identity: the caller
    supplies the same remote id here and in its own link row, and Core derives
    the deterministic row id from it (D-05/D-12).
    """

    model_config = _STRICT

    object_id: uuid.UUID
    external_id: str = Field(min_length=1, max_length=100)
    body: str
    external_author_name: Optional[EscapedStr] = Field(default=None, max_length=255)

    @field_validator("body")
    @classmethod
    def _trim_and_validate(cls, value: str) -> str:
        return sanitize_comment_body(value)


class ConnectorFileCreate(BaseModel):
    """SDK input for creating/replacing a **connector-origin** ObjectFile (D-13).

    Separate from ``ObjectFileCreate``: the bytes still travel as a separate
    async stream (never a DTO field), but the frozen input carries the
    connector-neutral ``external_id``, the bounded external author text and an
    optional declared ``content_length`` used only as a change/replay signal. It
    deliberately cannot supply ``managed_by``, a local uploader, a Core row id or
    a storage key — Core stamps the task plugin name, derives the deterministic
    id and generates the storage key (T-293-06-02).
    """

    model_config = _STRICT

    object_id: uuid.UUID
    external_id: str = Field(min_length=1, max_length=100)
    filename: str = Field(min_length=1, max_length=500)
    content_type: Optional[str] = Field(default=None, max_length=255)
    external_author_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    content_length: Optional[int] = Field(default=None, ge=0)


# --- Translation read/write surface (SEAM-04, D-11/D-12) ----------------------
#
# The strict, connector-neutral SDK contracts for data- and UI-translation
# read/write. The write has an EXPLICIT mode: ``fill_missing`` (default) touches
# only absent/whitespace-only stored values, ``replace`` overwrites existing
# content only when deliberately selected. One batch is validated and committed
# atomically; expected skips are reported, not raised.


class TranslationWrite(BaseModel):
    """One data-translation entry in a batch write (SEAM-04)."""

    model_config = _STRICT

    translation_type: str = Field(min_length=1, max_length=50)
    entity_id: uuid.UUID
    field: str = Field(min_length=1, max_length=50)
    language_code: str = Field(min_length=2, max_length=10)
    value: str


class TranslationBatchWrite(BaseModel):
    """A strict, atomic data-translation batch write (D-11/D-12)."""

    model_config = _STRICT

    mode: Literal["fill_missing", "replace"] = "fill_missing"
    entries: list[TranslationWrite]


class TranslationReadRequest(BaseModel):
    """A data-translation read request: one entity, several fields (SEAM-04)."""

    model_config = _STRICT

    translation_type: str = Field(min_length=1, max_length=50)
    entity_id: uuid.UUID
    fields: list[str]
    language_code: str = Field(min_length=2, max_length=10)


class TranslationBatchEntryResult(BaseModel):
    """The per-entry outcome of a batch write (D-11/D-12)."""

    model_config = _STRICT

    index: int
    translation_type: str
    entity_id: uuid.UUID
    field: str
    language_code: str
    written: bool
    reason: Optional[str] = None


class TranslationBatchResult(BaseModel):
    """The neutral result of one atomic translation batch write (D-11/D-12)."""

    model_config = _STRICT

    mode: str
    total: int
    written: int
    skipped: int
    results: list[TranslationBatchEntryResult]


class TranslationReadRow(BaseModel):
    """One field's raw + effective value with fallback provenance (SEAM-04)."""

    model_config = _STRICT

    field: str
    stored_value: Optional[str] = None
    effective_value: Optional[str] = None
    fallback_source: str
    serving_language_code: Optional[str] = None


class TranslationReadResult(BaseModel):
    """The result of one data-translation read (SEAM-04)."""

    model_config = _STRICT

    translation_type: str
    entity_id: uuid.UUID
    language_code: str
    rows: list[TranslationReadRow]


class UiTranslationWrite(BaseModel):
    """One UI (systemui) translation entry in a batch write (SEAM-04)."""

    model_config = _STRICT

    ui_string_id: uuid.UUID
    language_code: str = Field(min_length=2, max_length=10)
    value: str


class UiTranslationBatchWrite(BaseModel):
    """A strict, atomic UI-translation batch write (D-11/D-12)."""

    model_config = _STRICT

    mode: Literal["fill_missing", "replace"] = "fill_missing"
    entries: list[UiTranslationWrite]


class UiTranslationCatalogRequest(BaseModel):
    """The filter scope of a UI-translation catalogue read (SEAM-04)."""

    model_config = _STRICT

    editor_language: str = Field(min_length=2, max_length=10)
    namespace: Optional[str] = None
    namespace_is_null: bool = False
    q: Optional[str] = None
    limit: int = Field(default=100, ge=1, le=200)
    # Keyset cursor returned by a previous page (``next_cursor``). Opaque to the
    # caller: a plugin only ever passes it back unchanged (D-15).
    after: Optional[str] = Field(default=None, max_length=255)


class UiTranslationCatalogItem(BaseModel):
    """One UI catalogue row: raw/effective value + provenance (SEAM-04).

    ``ui_string_id``/``namespace``/``code_default`` are additive (D-15): the
    plugin receives the stable row UUID and the authored code default even when
    the effective text falls back, so neither is ever derived from a fallback.
    """

    model_config = _STRICT

    key: str
    stored_value: Optional[str] = None
    effective_value: Optional[str] = None
    fallback_source: str
    ui_string_id: Optional[uuid.UUID] = None
    namespace: Optional[str] = None
    code_default: Optional[str] = None


class UiTranslationCatalogResult(BaseModel):
    """The result of one UI-translation catalogue read (SEAM-04).

    ``total`` is the filter-wide row count and ``next_cursor`` continues the
    keyset traversal; both are additive and default to the previous shape.
    """

    model_config = _STRICT

    editor_language: str
    items: list[UiTranslationCatalogItem]
    total: int = 0
    next_cursor: Optional[str] = None


# --- Neutral schema entity inventory page (SEAM-04, D-05/D-15) ----------------
#
# A neutral, keyset-paginated read of a schema's four inventory nouns. It is an
# inventory adapter only: Core performs NO LLM/suggestion/candidate selection
# and exposes no aggregate count, so a plugin traverses authorized authored
# source and raw stored targets without any Core ORM import.


# The six schema inventory nouns, in canonical order (D-05). Kept local because
# ``translation_access`` imports this module, so the reverse import would be
# circular.
InventoryTranslationType = Literal[
    "object_type",
    "object_type_attribute",
    "select_option",
    "object_value",
    "status",
    "reference_type",
]


class TranslationEntityPageRequest(BaseModel):
    """A strict schema-scoped keyset page request (D-05/D-15).

    ``translation_types`` optionally narrows the page to those nouns; absent
    means every inventory noun.
    """

    model_config = _STRICT

    schema_id: uuid.UUID
    after: Optional[str] = Field(default=None, max_length=255)
    limit: int = Field(default=100, ge=1, le=200)
    translation_types: Optional[list[InventoryTranslationType]] = Field(
        default=None, min_length=1, max_length=6
    )


class TranslationEntityProjection(BaseModel):
    """One authorized inventory entity a plugin may read (D-15)."""

    model_config = _STRICT

    translation_type: str
    entity_id: uuid.UUID
    schema_id: uuid.UUID
    source_id: Optional[uuid.UUID] = None
    # Authored source values (field noun -> authored text), never a fallback.
    fields: dict[str, Optional[str]] = Field(default_factory=dict)
    default_language_code: Optional[str] = None
    enabled_target_languages: list[str] = Field(default_factory=list)
    # Raw stored target values: {field: {language_code: value}}.
    stored_targets: dict[str, dict[str, str]] = Field(default_factory=dict)
    datatype: Optional[str] = None
    own_multilingual: bool = False
    parent_multilingual: bool = False


class TranslationEntityPage(BaseModel):
    """One authorized keyset page (no foreign aggregate count is exposed)."""

    model_config = _STRICT

    items: list[TranslationEntityProjection] = Field(default_factory=list)
    next_cursor: Optional[str] = None


# --- Strict schema-from-tree create (SEAM-04, D-08/D-09/D-10) ------------------
#
# One authoritative, non-silently-repairing schema creation path shared by the
# ai-schema-wizard plugin (306) and the Core wizard. The DTOs deliberately reuse
# the existing ``AtomicCreate*`` field names (R05-2) as a strict (``extra="forbid"``)
# sibling family, so ``SchemaTreeReview.jsx`` renders a plugin draft without a
# mapping layer. ``create_schema_from_tree`` validates the whole tree before any
# write and rejects it in full with a concrete indexed tree path (D-09).


class SchemaTreeClassification(BaseModel):
    """Stable catalogue label keys for one tree attribute (D-08, D-18).

    Deliberately local to this module (the SDK inputs import no REST schema) and
    intentionally absent from ``sdk.__all__`` — a plugin passes a plain mapping
    that validates into it, keeping the pinned surface small. ``extra="forbid"``
    rejects an unknown sub-field; an unknown label *key* refuses the whole tree
    at creation time with its concrete tree path.
    """

    model_config = _STRICT

    confidentiality: Optional[str] = Field(default=None, pattern=r"^[a-z0-9_]{1,100}$")
    personal_data: bool = False
    export_control: list[Annotated[str, Field(pattern=r"^[a-z0-9_]{1,100}$")]] = Field(
        default_factory=list, max_length=50
    )


class SchemaTreeAttribute(BaseModel):
    """One attribute in a schema tree (strict; mirrors ``AtomicCreateAttribute``)."""

    model_config = _STRICT

    name: EscapedStr = Field(max_length=255)
    type_string: EscapedStr = Field(max_length=50)
    description: Optional[PlainStr] = None
    required: bool = False
    is_unique: bool = False
    default_value: Optional[EscapedStr] = Field(default=None, max_length=2000)
    select_options: Optional[list[str]] = None
    status_option_names: Optional[list[str]] = None
    referenced_object_type_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    reference_type_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    # Stable catalogue keys; an unknown key refuses the whole tree (D-18).
    classification: Optional[SchemaTreeClassification] = None


class SchemaTreeObjectType(BaseModel):
    """One ObjectType in a schema tree (strict; mirrors ``AtomicCreateObjectType``)."""

    model_config = _STRICT

    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = None
    icon_key: EscapedStr = Field(default="box", max_length=100)
    # icon_type/icon_url carry a REAL icon's own type+URL through a schema tree
    # (R05-2); None means "icon_key is a lucide slug".
    icon_type: Optional[EscapedStr] = Field(default=None, max_length=50)
    icon_url: Optional[EscapedStr] = Field(default=None, max_length=1000)
    parent_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    inherits_attributes: bool = False
    is_abstract: bool = False
    attributes: list[SchemaTreeAttribute] = []


class SchemaTreeStatus(BaseModel):
    """One schema-level Status in a schema tree (strict; mirrors ``AtomicCreateStatus``)."""

    model_config = _STRICT

    name: EscapedStr = Field(max_length=100)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)
    status_category: Optional[EscapedStr] = Field(default=None, max_length=20)


class SchemaTreeCreate(BaseModel):
    """A strict, whole-tree schema create for one existing Source (D-09/D-10).

    A name/key conflict with an existing schema in the same Source is reported
    and creates nothing — no adoption, merge, complement or rename (D-10).
    """

    model_config = _STRICT

    source_id: uuid.UUID
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = None
    statuses: list[SchemaTreeStatus] = []
    object_types: list[SchemaTreeObjectType]


class SchemaTreeDraft(BaseModel):
    """A strict, source-agnostic schema tree draft (AIWIZ-01, read-only).

    The neutral handoff shape an AI/plugin generates and the Core wizard
    reviews: exactly the tree fields ``SchemaTreeCreate`` carries, minus the two
    creation-authority fields (``source_id`` and ``name``). ``extra="forbid"``
    means a plugin cannot smuggle a Source, an actor, an origin, a name or any
    other write intent into a pure validation call — an unknown authority field
    is a validation error, never silently dropped (T-306-01). The DTO grants no
    creation authority: parsing it performs no lookup, no write and no
    authorization.
    """

    model_config = _STRICT

    description: Optional[PlainStr] = None
    statuses: list[SchemaTreeStatus] = []
    object_types: list[SchemaTreeObjectType]
