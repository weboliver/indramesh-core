import re as _re
import uuid
from datetime import datetime
from pydantic import BaseModel, ConfigDict, Field, computed_field, model_validator, field_validator
from typing import Annotated, Any, List, Literal, Optional

from app.validators import EscapedStr, PlainStr
from app.schemas.retired_inputs import reject_retired_core_inputs
from app.schemas.classification import (
    ClassificationCreateFields,
    ClassificationUpdateFields,
    ClassifiedResponseMixin,
    TemplateClassification,
)
from app.models.attribute_type import AttributeTypeDataType


class _RetiredCoreInputGuard(BaseModel):
    """Reject retired Core request fields by presence, before model parsing.

    Request-only: placed on the 16 Create/Update models, never on a base shared
    with a Response (which must remain serializable). Walks ``cls.__mro__`` to
    look up the CLOSED matrix in :mod:`app.schemas.retired_inputs`, so a
    subclass of a guarded model inherits its base's rejections.
    """

    @model_validator(mode="before")
    @classmethod
    def _reject_retired_core_inputs(cls, data: Any) -> Any:
        return reject_retired_core_inputs(
            (base.__name__ for base in cls.__mro__), data
        )


class SourceBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    type: EscapedStr = Field(max_length=50)
    source_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    import_folder: Optional[EscapedStr] = Field(default=None, max_length=255)


class SourceCreate(SourceBase):
    pass


class SourceUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    type: Optional[EscapedStr] = Field(default=None, max_length=50)
    source_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    import_folder: Optional[EscapedStr] = Field(default=None, max_length=255)


class SourceResponse(SourceBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


# Reused by SchemaBase/SchemaUpdate's 10 *_color fields (direct /schemas CRUD) and by
# TemplateLightColorsUpdate/TemplateDarkColorsUpdate (the /schemas/{id}/template-colors
# sub-endpoint) so both entry points that write these columns enforce the same format.
# 6-digit hex only, deliberately — do not add 3-digit shorthand support here without
# updating both call sites, or the two endpoints will accept different color formats
# for the same underlying columns.
HEX_PATTERN = r"^#[0-9A-Fa-f]{6}$"


class SchemaBase(BaseModel):
    source_id: uuid.UUID
    key: Optional[EscapedStr] = Field(default=None, max_length=100)
    # 246 D-09: IndraMesh-native keygen prefix (distinct from key = Jira-occupied).
    keygen: Optional[EscapedStr] = Field(default=None, max_length=100)
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    primary_color: EscapedStr = Field(default="#2563EB", max_length=7, pattern=HEX_PATTERN)
    header_bg_color: EscapedStr = Field(default="#1E293B", max_length=7, pattern=HEX_PATTERN)
    sidebar_bg_color: EscapedStr = Field(default="#F1F5F9", max_length=7, pattern=HEX_PATTERN)
    section_bg_color: EscapedStr = Field(default="#F8FAFC", max_length=7, pattern=HEX_PATTERN)
    surface_bg_color: EscapedStr = Field(default="#FFFFFF", max_length=7, pattern=HEX_PATTERN)
    dark_primary_color: EscapedStr = Field(default="#3B82F6", max_length=7, pattern=HEX_PATTERN)
    dark_header_bg_color: EscapedStr = Field(default="#0F172A", max_length=7, pattern=HEX_PATTERN)
    dark_sidebar_bg_color: EscapedStr = Field(default="#1E293B", max_length=7, pattern=HEX_PATTERN)
    dark_section_bg_color: EscapedStr = Field(default="#0F172A", max_length=7, pattern=HEX_PATTERN)
    dark_surface_bg_color: EscapedStr = Field(default="#1E293B", max_length=7, pattern=HEX_PATTERN)
    status_filter_config: Optional[dict] = Field(default=None)


class SchemaCreate(_RetiredCoreInputGuard, SchemaBase):
    pass


class SchemaUpdate(_RetiredCoreInputGuard, BaseModel):
    source_id: Optional[uuid.UUID] = None
    key: Optional[EscapedStr] = Field(default=None, max_length=100)
    keygen: Optional[EscapedStr] = Field(default=None, max_length=100)
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=1000)
    primary_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    header_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    sidebar_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    section_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    surface_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    dark_primary_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    dark_header_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    dark_sidebar_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    dark_section_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    dark_surface_bg_color: Optional[EscapedStr] = Field(default=None, max_length=7, pattern=HEX_PATTERN)
    status_filter_config: Optional[dict] = None
    default_language_id: Optional[uuid.UUID] = None


class SchemaResponse(SchemaBase):
    id: uuid.UUID
    default_language_id: Optional[uuid.UUID] = None
    default_language_code: Optional[str] = None
    multilingual: bool = False
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


_TEMPLATE_FIELD_MAP = {
    "primary": ("primary_color", "dark_primary_color"),
    "header":  ("header_bg_color", "dark_header_bg_color"),
    "sidebar": ("sidebar_bg_color", "dark_sidebar_bg_color"),
    "section": ("section_bg_color", "dark_section_bg_color"),
    "surface": ("surface_bg_color", "dark_surface_bg_color"),
}


def schema_to_template_payload(schema) -> dict:
    light = {key: getattr(schema, light_f) for key, (light_f, _) in _TEMPLATE_FIELD_MAP.items()}
    dark = {key: getattr(schema, dark_f) for key, (_, dark_f) in _TEMPLATE_FIELD_MAP.items()}
    return {"light": light, "dark": dark}


def schema_name_to_slug(name: str) -> str:
    slug = name.lower().replace(" ", "-")
    slug = _re.sub(r"[^a-z0-9-]", "-", slug)
    return slug.strip("-") or "schema"


class TemplateLightColorsUpdate(BaseModel):
    primary: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    header: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    sidebar: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    section: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    surface: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)


class TemplateDarkColorsUpdate(BaseModel):
    primary: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    header: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    sidebar: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    section: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)
    surface: Optional[EscapedStr] = Field(default=None, min_length=7, max_length=7, pattern=HEX_PATTERN)


class TemplateColorsUpdate(BaseModel):
    light: Optional[TemplateLightColorsUpdate] = None
    dark: Optional[TemplateDarkColorsUpdate] = None


class TemplateBulkImportSkipped(BaseModel):
    id: EscapedStr = Field(max_length=50)
    reason: EscapedStr = Field(max_length=500)


class TemplateBulkImportError(BaseModel):
    id: EscapedStr = Field(max_length=50)
    reason: EscapedStr = Field(max_length=500)


class TemplateBulkImportResponse(BaseModel):
    updated: list[str]
    skipped: list[TemplateBulkImportSkipped]
    errors: list[TemplateBulkImportError]


def template_update_to_schema_update(template_update: TemplateColorsUpdate) -> dict:
    out: dict = {}
    if template_update.light:
        for key, (light_f, _) in _TEMPLATE_FIELD_MAP.items():
            v = getattr(template_update.light, key)
            if v is not None:
                out[light_f] = v
    if template_update.dark:
        for key, (_, dark_f) in _TEMPLATE_FIELD_MAP.items():
            v = getattr(template_update.dark, key)
            if v is not None:
                out[dark_f] = v
    return out


# D-13 adjacency: unknown data_type strings used to persist as brand-new,
# unbounded catalog rows -- exactly the re-pollution the 245-01 migration
# cleaned up. Reject anything outside the canonical enum here so
# the catalog can never drift again.
_VALID_ATTRIBUTE_DATA_TYPES = {dt.value for dt in AttributeTypeDataType}


def _validate_attribute_data_type(v: str) -> str:
    if v not in _VALID_ATTRIBUTE_DATA_TYPES:
        raise ValueError(
            f"data_type must be one of {sorted(_VALID_ATTRIBUTE_DATA_TYPES)}"
        )
    return v


class AttributeTypeBase(BaseModel):
    name: EscapedStr = Field(max_length=100)
    data_type: EscapedStr = Field(max_length=50)

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, v: str) -> str:
        return _validate_attribute_data_type(v)


class AttributeTypeCreate(AttributeTypeBase):
    pass


class AttributeTypeUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=100)
    data_type: Optional[EscapedStr] = Field(default=None, max_length=50)

    @field_validator("data_type")
    @classmethod
    def validate_data_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return v
        return _validate_attribute_data_type(v)


class AttributeTypeResponse(AttributeTypeBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class LanguageBase(BaseModel):
    code: EscapedStr = Field(max_length=10)
    name: EscapedStr = Field(max_length=100)
    is_active: Optional[bool] = True


class LanguageCreate(LanguageBase):
    pass


class LanguageUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=100)
    is_active: Optional[bool] = None


class LanguageResponse(LanguageBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class TranslationTypeResponse(BaseModel):
    id: uuid.UUID
    code: EscapedStr = Field(max_length=50)
    label: EscapedStr = Field(max_length=100)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UiStringResponse(BaseModel):
    id: uuid.UUID
    key: EscapedStr = Field(max_length=255)
    default_text: PlainStr = Field(max_length=5000)
    namespace: Optional[EscapedStr] = Field(default=None, max_length=100)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UiDictionaryResponse(BaseModel):
    lang: EscapedStr = Field(max_length=10)
    strings: dict[str, str]


class UiTranslationCatalogRow(BaseModel):
    """One catalog row: registry fields plus the selected editor language's
    stored/effective value and fallback provenance (Phase 233 / ADMIN-01/02)."""
    id: uuid.UUID
    key: EscapedStr = Field(max_length=255)
    namespace: Optional[EscapedStr] = Field(default=None, max_length=100)
    default_text: PlainStr = Field(max_length=5000)
    stored_value: Optional[PlainStr] = Field(default=None, max_length=5000)
    effective_value: PlainStr = Field(max_length=5000)
    fallback_source: Literal["explicit", "english", "code_default"]


class UiTranslationNamespaceOption(BaseModel):
    value: Optional[EscapedStr] = Field(default=None, max_length=100)


class UiTranslationCoverage(BaseModel):
    code: EscapedStr = Field(max_length=10)
    translated: int
    total: int


class UiTranslationCatalogResponse(BaseModel):
    items: List[UiTranslationCatalogRow]
    total: int
    namespaces: List[UiTranslationNamespaceOption]
    coverage: List[UiTranslationCoverage]
    editor_language: EscapedStr = Field(max_length=10)
    # Phase 235 / D-09: server-authoritative (settings.environment ==
    # "development") — never inferred from a frontend build variable.
    export_available: bool = False


class UiTranslationExportResponse(BaseModel):
    """Phase 235 / EXP-01: paths-only export result (D-04/D-17) — relative
    locale target paths written by the current run, never dictionary text,
    temporary paths, or lock identifiers."""
    paths: List[PlainStr] = Field(default_factory=list, max_length=50)


class TranslationUpsert(BaseModel):
    """Natural-key upsert contract for PUT /translations (D-11).

    translation_type is the seeded code (D-13); language_code is the BCP-47 code
    (Phase 220 D-12 — codes in the API, not UUIDs).
    """
    translation_type: EscapedStr = Field(max_length=50)
    entity_id: uuid.UUID
    field: EscapedStr = Field(max_length=50)
    language_code: EscapedStr = Field(max_length=10)
    value: PlainStr = Field(max_length=5000)


class TranslationResponse(BaseModel):
    id: uuid.UUID
    translation_type_id: uuid.UUID
    entity_id: uuid.UUID
    field: EscapedStr = Field(max_length=50)
    language_id: uuid.UUID
    language_code: Optional[EscapedStr] = Field(default=None, max_length=10)
    value: PlainStr = Field(max_length=5000)
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SchemaLanguagesUpdate(BaseModel):
    language_codes: List[Annotated[EscapedStr, Field(max_length=10)]] = Field(min_length=1, max_length=20)


class ReferenceTypeBase(BaseModel):
    reference: EscapedStr = Field(max_length=100)
    description: Optional[PlainStr] = Field(default=None, max_length=500)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)
    source_id: uuid.UUID
    schema_id: Optional[uuid.UUID] = None


class ReferenceTypeCreate(_RetiredCoreInputGuard, ReferenceTypeBase):
    pass


class ReferenceTypeUpdate(_RetiredCoreInputGuard, BaseModel):
    reference: Optional[EscapedStr] = Field(default=None, max_length=100)
    description: Optional[PlainStr] = Field(default=None, max_length=500)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)
    source_id: Optional[uuid.UUID] = None
    schema_id: Optional[uuid.UUID] = None


class ReferenceTypeResponse(ReferenceTypeBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    resolved_language: Optional[str] = Field(default=None, max_length=10)
    
    model_config = ConfigDict(from_attributes=True)


class ObjectTypeBase(BaseModel):
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
    icon_local_name: Optional[EscapedStr] = Field(default=None, max_length=100)


class ObjectTypeCreate(_RetiredCoreInputGuard, ObjectTypeBase):
    pass


class ObjectTypeUpdate(_RetiredCoreInputGuard, BaseModel):
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


class ObjectTypeResponse(ObjectTypeBase):
    id: uuid.UUID
    inherits_attributes: bool = False
    is_abstract: bool = False
    # 290 D-14: connector-neutral origin + lock markers are response-only —
    # ObjectTypeBase is inherited by Create/Update, so these live here and are
    # never accepted from a client payload (T-290-03-01).
    managed_by: Optional[str] = Field(default=None, max_length=64)
    externally_readonly: bool = False
    created_at: datetime
    updated_at: datetime
    resolved_language: Optional[str] = Field(default=None, max_length=10)

    @computed_field
    @property
    def is_external(self) -> bool:
        return self.managed_by is not None

    model_config = ConfigDict(from_attributes=True)


class ObjectBase(BaseModel):
    schema_id: uuid.UUID
    object_type_id: uuid.UUID
    name: EscapedStr = Field(max_length=500)


class ObjectCreate(_RetiredCoreInputGuard, ObjectBase, ClassificationCreateFields):
    """Create payload: the shared object fields plus the three label ids (D-07)."""


class ObjectUpdate(_RetiredCoreInputGuard, ClassificationUpdateFields):
    schema_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=500)


class ObjectResponse(ClassifiedResponseMixin, ObjectBase):
    id: uuid.UUID
    # 246 D-05: human-readable immutable IndraMesh key (schema-unique, D-06) —
    # response-only (server-generated via next_key(), never client-settable
    # on create; ObjectUpdate has no way to set it at all on PATCH either).
    key: Optional[EscapedStr] = Field(default=None, max_length=255)
    created_at: datetime
    updated_at: datetime
    has_deny_record_permission: bool = False
    has_record_permission: bool = False
    resolved_language: Optional[str] = Field(default=None, max_length=10)

    model_config = ConfigDict(from_attributes=True)


class ReferenceTypeNested(BaseModel):
    id: uuid.UUID
    reference: EscapedStr = Field(max_length=100)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)

    model_config = ConfigDict(from_attributes=True)


class ObjectTypeAttributeBase(BaseModel):
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


class ObjectTypeAttributeCreate(
    _RetiredCoreInputGuard, ObjectTypeAttributeBase, ClassificationCreateFields
):
    """Create payload: the shared attribute fields plus the three label ids (D-07)."""


class ObjectTypeAttributeUpdate(_RetiredCoreInputGuard, ClassificationUpdateFields):
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


class ObjectTypeAttributeResponse(ClassifiedResponseMixin, ObjectTypeAttributeBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    reference_type: Optional[ReferenceTypeNested] = None
    # 246 D-03: explicit system-OTA protection flag.
    is_system: bool = False
    # 290 D-14: response-only origin/lock markers — see ObjectTypeResponse.
    managed_by: Optional[str] = Field(default=None, max_length=64)
    externally_readonly: bool = False
    # 290 D-02/D-14: contextual effective lock for the requested ObjectType,
    # populated by the list/single read paths (requested type OR resolved OTA).
    is_locked: bool = False

    # Computed at query time by the recursive CTE in list_object_type_attributes
    # (not ORM-persisted — these come from the hierarchy walk, not the DB model)
    defining_object_type_id: Optional[uuid.UUID] = None
    defining_object_type_name: EscapedStr = Field(default="", max_length=255)
    is_inherited: bool = False
    has_deny_record_permission: bool = False
    resolved_language: Optional[str] = Field(default=None, max_length=10)

    @computed_field
    @property
    def is_external(self) -> bool:
        return self.managed_by is not None

    model_config = ConfigDict(from_attributes=True)


class ObjectValueBase(BaseModel):
    object_id: uuid.UUID
    object_type_attribute_id: uuid.UUID
    value_float: Optional[float] = None
    value_integer: Optional[int] = None
    value_boolean: Optional[bool] = None
    value_simple: Optional[PlainStr] = Field(default=None, max_length=64000)
    value_datetime: Optional[datetime] = None
    value_struct: Optional[dict] = None


class ObjectValueCreate(ObjectValueBase):
    pass


class ObjectValueUpdate(BaseModel):
    value_float: Optional[float] = None
    value_integer: Optional[int] = None
    value_boolean: Optional[bool] = None
    value_simple: Optional[PlainStr] = Field(default=None, max_length=64000)
    value_datetime: Optional[datetime] = None
    value_struct: Optional[dict] = None


class ObjectValueResponse(ObjectValueBase):
    id: uuid.UUID
    attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    created_at: datetime
    updated_at: datetime
    resolved_language: Optional[str] = Field(default=None, max_length=10)

    model_config = ConfigDict(from_attributes=True)


class ObjectRefBase(BaseModel):
    from_object_id: uuid.UUID
    to_object_id: uuid.UUID
    object_type_id: uuid.UUID
    source_id: uuid.UUID
    from_attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)


class ObjectRefCreate(ObjectRefBase):
    pass


class ObjectRefUpdate(BaseModel):
    from_object_id: Optional[uuid.UUID] = None
    to_object_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    source_id: Optional[uuid.UUID] = None
    from_attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)


class ObjectRefResponse(ObjectRefBase):
    id: uuid.UUID
    to_object_name: Optional[EscapedStr] = Field(default=None, max_length=500)
    from_object_name: Optional[EscapedStr] = Field(default=None, max_length=500)
    from_attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class StatusBase(BaseModel):
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


class StatusCreate(_RetiredCoreInputGuard, StatusBase):
    pass


class StatusUpdate(_RetiredCoreInputGuard, BaseModel):
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


class StatusResponse(StatusBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    resolved_language: Optional[str] = Field(default=None, max_length=10)

    model_config = ConfigDict(from_attributes=True)


class GroupBase(BaseModel):
    name: EscapedStr = Field(max_length=100)


class GroupCreate(GroupBase):
    pass


class GroupUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=100)


class GroupResponse(GroupBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ImportBase(BaseModel):
    schema_id: uuid.UUID
    object_key: Optional[EscapedStr] = Field(default=None, max_length=255)
    error_code: Optional[EscapedStr] = Field(default=None, max_length=50)
    # PlainStr, not EscapedStr: this field holds a JSON-serialized error report
    # (see import_execution_service.execute_csv_import); html.escape()-ing it
    # would corrupt the JSON (quotes get rewritten to &quot;/&#x27;).
    error_message: Optional[PlainStr] = Field(default=None, max_length=2000)
    status: EscapedStr = Field(default="pending", max_length=20)
    total_objects: int = 0
    processed_objects: int = 0


class ImportCreate(ImportBase):
    # Transient request-only field (no Import DB column): identifies the
    # ImportDefinition that drives this import. POST /imports requires it
    # (DECOUPLE-05); the persisted Task carries the FK and create_import()
    # ignores it.
    import_definition_id: Optional[uuid.UUID] = None


class ImportUpdate(BaseModel):
    object_key: Optional[EscapedStr] = Field(default=None, max_length=255)
    error_code: Optional[EscapedStr] = Field(default=None, max_length=50)
    # PlainStr — see ImportBase.error_message comment above.
    error_message: Optional[PlainStr] = Field(default=None, max_length=2000)
    status: Optional[EscapedStr] = Field(default=None, max_length=20)
    total_objects: Optional[int] = None
    processed_objects: Optional[int] = None


class ImportResponse(ImportBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class ExportBase(BaseModel):
    schema_id: uuid.UUID
    error_code: Optional[EscapedStr] = Field(default=None, max_length=50)
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    status: EscapedStr = Field(default="pending", max_length=20)
    total_objects: int = 0
    processed_objects: int = 0


class ExportCreate(ExportBase):
    # Optional ON PURPOSE (D-17): a required field would make FastAPI answer 422
    # via RequestValidationError, while the locked contract for a definition-less
    # export body is 400 — create_export_route raises that before any write.
    export_definition_id: Optional[uuid.UUID] = None


class ExportUpdate(BaseModel):
    error_code: Optional[EscapedStr] = Field(default=None, max_length=50)
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    status: Optional[EscapedStr] = Field(default=None, max_length=20)
    total_objects: Optional[int] = None
    processed_objects: Optional[int] = None


class ExportResponse(ExportBase):
    # Server-authored and read-only: the worker and the CSV executor are the only
    # writers (through update_export's keyword argument). It is deliberately
    # absent from ExportCreate/ExportUpdate so no request can redirect a row at
    # another user's file. Pydantic's default extra="ignore" keeps an old client
    # that still sends the field working — the value is simply dropped.
    object_key: Optional[EscapedStr] = Field(default=None, max_length=255)
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class ImportStatusBase(BaseModel):
    import_id: uuid.UUID
    parent_id: Optional[uuid.UUID] = None
    is_running: bool = False
    is_done: bool = False
    current_id: Optional[uuid.UUID] = None
    has_error: bool = False
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    progress_percentage: int = 0


class ImportStatusCreate(ImportStatusBase):
    pass


class ImportStatusUpdate(BaseModel):
    is_running: Optional[bool] = None
    is_done: Optional[bool] = None
    current_id: Optional[uuid.UUID] = None
    has_error: Optional[bool] = None
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    progress_percentage: Optional[int] = None


class ImportStatusResponse(ImportStatusBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class ExportStatusBase(BaseModel):
    export_id: uuid.UUID
    parent_id: Optional[uuid.UUID] = None
    is_running: bool = False
    is_done: bool = False
    current_id: Optional[uuid.UUID] = None
    has_error: bool = False
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    progress_percentage: int = 0


class ExportStatusCreate(ExportStatusBase):
    pass


class ExportStatusUpdate(BaseModel):
    is_running: Optional[bool] = None
    is_done: Optional[bool] = None
    current_id: Optional[uuid.UUID] = None
    has_error: Optional[bool] = None
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    progress_percentage: Optional[int] = None


class ExportStatusResponse(ExportStatusBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class TaskBase(BaseModel):
    parent_task_id: Optional[uuid.UUID] = None
    import_id: Optional[uuid.UUID] = None
    export_id: Optional[uuid.UUID] = None
    # Transient API field (no DB column) — identifies which ExportDefinition
    # drives a CSV export; the real Export FK is created at task creation time.
    # Phase 158 (INT-01) adds the persisted Task↔ExportDefinition FK.
    export_definition_id: Optional[uuid.UUID] = None
    import_definition_id: Optional[uuid.UUID] = None
    service_account_id: Optional[uuid.UUID] = None
    schedule_id: Optional[uuid.UUID] = None
    source_id: Optional[uuid.UUID] = None
    schema_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    # Phase 286 (D-01/D-10, PLUGTASK-01): bounded 255-char strings, not closed
    # enums. A Core request that omits current_step keeps the legacy "SCHEMA"
    # default; an explicit JSON null flows through as None so a plugin Task is
    # created with no business step (D-10). Empty tasktype is rejected.
    current_step: Optional[EscapedStr] = Field(default="SCHEMA", max_length=255)
    current_status: EscapedStr = Field(default="PENDING", max_length=20)
    sort: int = 0
    tasktype: EscapedStr = Field(min_length=1, max_length=255)
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_permanent: bool = False


class TaskCreate(_RetiredCoreInputGuard, TaskBase):
    # Phase 247 (T-247-03): counters are worker/service-only (see
    # app.services.task_service.update_task_progress) and never a public
    # request field — forbid unknown fields so a client-sent
    # processed_objects/total_objects raises a validation error instead of
    # being silently ignored.
    # Phase 286 (D-04/D-06, PLUGTASK-02): validated plugin start parameters are
    # admitted only at creation time; omitted/None for Core tasks.
    plugin_parameters: Optional[dict] = None
    model_config = ConfigDict(extra="forbid")


class TaskUpdate(_RetiredCoreInputGuard, BaseModel):
    # Phase 247 (T-247-03): same public-write boundary as TaskCreate above.
    model_config = ConfigDict(extra="forbid")

    parent_task_id: Optional[uuid.UUID] = None
    import_id: Optional[uuid.UUID] = None
    export_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    export_definition_id: Optional[uuid.UUID] = None
    import_definition_id: Optional[uuid.UUID] = None
    service_account_id: Optional[uuid.UUID] = None
    # Phase 286 (PLUGTASK-01): widened to the shared 255 bound because
    # PluginContext.report_step() writes through TaskUpdate(current_step=...);
    # a 20-char bound would reject every longer plugin step at runtime.
    current_step: Optional[EscapedStr] = Field(default=None, max_length=255)
    current_status: Optional[EscapedStr] = Field(default=None, max_length=20)
    sort: Optional[int] = None
    tasktype: Optional[EscapedStr] = Field(default=None, max_length=255)
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    error_message: Optional[EscapedStr] = Field(default=None, max_length=2000)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    is_permanent: Optional[bool] = None


class TaskResponse(TaskBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    # Phase 247 (UX-04): server-observed, response-only progress projection.
    # NULL means unknown/not yet observed; a persisted 0 is a real known
    # value distinct from NULL. Never settable via TaskCreate/TaskUpdate.
    processed_objects: Optional[int] = None
    total_objects: Optional[int] = None
    # Phase 286 (D-04/D-06): response-visible immutable admitted input. Never
    # settable via TaskCreate (TaskUpdate forbids the field entirely).
    plugin_parameters: Optional[dict] = None
    # Phase 300 (D-05): response-only cancel-request marker. Set by the cancel
    # route while the run is still RUNNING; absent from TaskCreate/TaskUpdate.
    cancel_requested_at: Optional[datetime] = None

    model_config = ConfigDict(from_attributes=True)


class TaskActionResponse(BaseModel):
    """Response for retry action — returns the newly created task."""
    task: TaskResponse
    message: EscapedStr = Field(default="Task retry initiated", max_length=500)


class TaskTypeCatalogEntry(BaseModel):
    """Phase 289 (CORESEAM-03, D-04/D-05/D-10): one task-type catalog row.

    The field names are the fixed Phase 291 frontend contract (289-04 review
    correction 7): ``name``, ``label``, ``source`` ("core"|"plugin"),
    ``plugin_name``, ``schedulable``, ``scope_requirements``,
    ``parameter_schema``, ``dispatchable``, ``unavailable_reason`` and the
    Phase 303 ``catalog_visible``.

    ``dispatchable`` is the informative snapshot of the plugin row's worker
    version — the central admission gate stays authoritative immediately before
    persistence. An ACTIVE entry with a stale worker stays visible with
    ``dispatchable=False`` and a bounded Core-authored reason (never a
    traceback, plugin config, or credential).

    ``catalog_visible`` (SEAM-05, Carry-03) is true by default and governs only
    the generic Create Task dialog's type choices: a false entry stays listed in
    the task list/type filter and remains directly startable/retryable and
    schedulable. It is display metadata, never an authorization gate; a missing
    field (an older API response) is treated as visible by the frontend.
    """

    name: str
    label: str
    source: Literal["core", "plugin"]
    plugin_name: Optional[str] = None
    schedulable: bool
    scope_requirements: List[str]
    parameter_schema: dict
    dispatchable: bool
    unavailable_reason: Optional[str] = None
    catalog_visible: bool = True


class TargetBase(BaseModel):
    schema_id: Optional[uuid.UUID] = None
    name: EscapedStr = Field(max_length=255)
    target_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    export_folder: Optional[EscapedStr] = Field(default=None, max_length=255)
    # CORESEAM-01: required and explicit — the "jira" default is deliberately
    # gone. The connector descriptor registry (app.plugin_system.connector_registry)
    # is the single source for normalization and for the per-connector
    # config/schema_id/target_url/export_folder rules; the service resolves it on
    # create/update.
    connector_type: EscapedStr = Field(max_length=50)
    is_active: bool = True


class TargetCreate(_RetiredCoreInputGuard, TargetBase):
    pass


class TargetUpdate(_RetiredCoreInputGuard, BaseModel):
    schema_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    target_url: Optional[EscapedStr] = Field(default=None, max_length=500)
    config: Optional[dict] = None
    export_folder: Optional[EscapedStr] = Field(default=None, max_length=255)
    connector_type: Optional[EscapedStr] = Field(default=None, max_length=50)
    is_active: Optional[bool] = None


class TargetResponse(TargetBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class FieldMappingItem(BaseModel):
    attr_id: uuid.UUID
    csv_column: EscapedStr = Field(max_length=255)
    column_order: int = Field(ge=0)


def _validate_field_mappings_unique(field_mappings: list["FieldMappingItem"] | None) -> None:
    """WR-05: reject duplicate csv_column or attr_id values within a single
    ExportDefinition's field_mappings. Nothing else validates this, and build_csv
    will happily emit a CSV with duplicate header names — most CSV consumers
    (including csv.DictReader) silently drop all but the last duplicate column
    on read, producing a confusing, effectively lossy export with no warning."""
    if not field_mappings:
        return
    seen_columns: set[str] = set()
    seen_attrs: set[uuid.UUID] = set()
    for mapping in field_mappings:
        if mapping.csv_column in seen_columns:
            raise ValueError(f"Duplicate csv_column in field_mappings: {mapping.csv_column!r}")
        seen_columns.add(mapping.csv_column)
        if mapping.attr_id in seen_attrs:
            raise ValueError(f"Duplicate attr_id in field_mappings: {mapping.attr_id}")
        seen_attrs.add(mapping.attr_id)


class ExportDefinitionBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    object_type_id: uuid.UUID
    target_id: Optional[uuid.UUID] = None
    field_mappings: list[FieldMappingItem] = Field(default_factory=list)
    is_permanent: bool = False

    @model_validator(mode="after")
    def validate_field_mappings_unique(self):
        _validate_field_mappings_unique(self.field_mappings)
        return self


class ExportDefinitionCreate(ExportDefinitionBase):
    # WR-03: target_id is Optional on the shared Base (ExportDefinitionResponse
    # needs it Optional — the FK is ondelete="SET NULL", so it can go null after
    # creation), but leaving it Optional at creation time meant execute_csv_export
    # always raised a confusing "Target None not found..." the first time any task
    # ran the definition. Require it up front instead.
    target_id: uuid.UUID


class ExportDefinitionUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    object_type_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    field_mappings: Optional[list[FieldMappingItem]] = None
    is_permanent: Optional[bool] = None

    @model_validator(mode="after")
    def validate_field_mappings_unique(self):
        _validate_field_mappings_unique(self.field_mappings)
        return self


class ExportDefinitionResponse(ExportDefinitionBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ImportDefinitionBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    object_type_id: uuid.UUID
    source_id: uuid.UUID
    identity_attr_id: Optional[uuid.UUID] = None
    value_separator: EscapedStr = Field(default=";", max_length=10)
    date_format: Optional[EscapedStr] = Field(default=None, max_length=50)
    encoding: EscapedStr = Field(default="utf-8", max_length=20)
    field_mappings: list[FieldMappingItem] = Field(default_factory=list)
    is_permanent: bool = False

    @model_validator(mode="after")
    def validate_field_mappings_unique(self):
        _validate_field_mappings_unique(self.field_mappings)
        return self


class ImportDefinitionCreate(ImportDefinitionBase):
    pass


class ImportDefinitionUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    object_type_id: Optional[uuid.UUID] = None
    source_id: Optional[uuid.UUID] = None
    identity_attr_id: Optional[uuid.UUID] = None
    value_separator: Optional[EscapedStr] = Field(default=None, max_length=10)
    date_format: Optional[EscapedStr] = Field(default=None, max_length=50)
    encoding: Optional[EscapedStr] = Field(default=None, max_length=20)
    field_mappings: Optional[list[FieldMappingItem]] = None
    is_permanent: Optional[bool] = None

    @model_validator(mode="after")
    def validate_field_mappings_unique(self):
        _validate_field_mappings_unique(self.field_mappings)
        return self


class ImportDefinitionResponse(ImportDefinitionBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class ServiceAccountBase(BaseModel):
    source_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    name: EscapedStr = Field(max_length=255)
    username: Optional[EscapedStr] = Field(default=None, max_length=255)
    is_active: bool = True
    expires_at: Optional[datetime] = None


class ServiceAccountCreate(ServiceAccountBase):
    # max 500: DB column stores the encrypted ciphertext (EncryptedString(800)),
    # which is larger than the plaintext after Fernet+base64 overhead
    source_bearer_token: Optional[EscapedStr] = Field(default=None, max_length=500)
    target_bearer_token: Optional[EscapedStr] = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def validate_source_or_target_present(self):
        # source_id and target_id are no longer mutually exclusive — a single
        # service account may carry credentials for both (separate
        # source_bearer_token/target_bearer_token fields already keep the
        # credentials apart). At least one of the two must still be set.
        if self.source_id is None and self.target_id is None:
            raise ValueError("At least one of source_id or target_id must be set")
        return self


class ServiceAccountUpdate(BaseModel):
    source_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    username: Optional[EscapedStr] = Field(default=None, max_length=255)
    source_bearer_token: Optional[EscapedStr] = Field(default=None, max_length=500)
    target_bearer_token: Optional[EscapedStr] = Field(default=None, max_length=500)
    is_active: Optional[bool] = None
    expires_at: Optional[datetime] = None


class ServiceAccountResponse(ServiceAccountBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime
    
    model_config = ConfigDict(from_attributes=True)


class PluginFrontendResponse(BaseModel):
    """The manifest's declared frontend half (PLUGFE-01).

    ``bundle`` is the plugin-relative bundle path and ``extension_points`` the
    declared slot families — the binding contract the browser runtime validates
    a bundle's default export against (D-06). ``deferred_assets`` (Phase 303,
    SEAM-06/D-14) maps a logical asset id to the plugin-relative heavy-payload
    path the host ``loadPluginModule`` helper may fetch on use; it is projected
    on both the admin list and the runtime discovery response so the browser can
    restrict deferred loads to manifest-declared ids.
    """

    bundle: str
    extension_points: List[str] = []
    deferred_assets: dict[str, str] = {}


class PluginRuntimeResponse(BaseModel):
    """The minimal, non-admin view of one ACTIVE plugin (CORESEAM-09/D-05).

    Bundle discovery needs nothing beyond the plugin's identity and its
    manifest's ``frontend`` block — every administrative/diagnostic field
    (config, worker/database detail, authored paths, lifecycle state) stays on
    the ``plugins:read``-gated ``PluginResponse``. ``frontend`` is resolved from
    the LIVE in-process registry, so a plugin installed but not loaded in the
    serving process reports ``None`` (same rule as the admin list's
    manifest-derived fields).
    """

    name: str
    version: str
    frontend: Optional[PluginFrontendResponse] = None
    # Phase 304 (GQL-06/D-18): additive projection of the manifest's
    # ``enables_http_graphql`` flag, so the plugin-management surface can render
    # the "not intended for production" warning without Core naming a plugin.
    enables_http_graphql: bool = False


class PluginResponse(BaseModel):
    """One row of the core ``plugins`` table (PLUGCORE-10).

    ``capabilities``/``has_frontend`` are NOT ``plugins`` columns: the router
    populates them from the live in-process registry when the plugin happens to
    be loaded in this process, and defaults them to ``[]``/``False`` otherwise
    (a plugin installed but not loaded here still lists correctly). The same
    rule applies to ``frontend`` and the manifest-derived fields
    (``author``/``api_version``/``requires_worker_update``/``config_schema``)
    that the Plugins admin page consumes. ``worker_loaded_at``/
    ``worker_loaded_version`` ARE ``plugins`` columns (PLUGWORKER-01).

    ``database_version_table``/``database_error``/``database_error_revision``
    are ALSO real ``plugins`` columns (Phase 287/D-13/D-14): the recorded
    version table and a bounded, core-authored migration diagnostic. They are
    read straight off the row (not the live registry), so a plugin not loaded in
    the serving process still reports them. ``database_migrations_dir`` is
    deliberately NOT exposed — it is an internal authoring path with no operator
    need (A-287-07).
    """

    id: uuid.UUID
    name: str
    version: str
    state: str
    capabilities: List[str] = []
    has_frontend: bool = False
    frontend: Optional[PluginFrontendResponse] = None
    author: Optional[str] = None
    api_version: Optional[str] = None
    requires_worker_update: bool = False
    # Phase 304 (GQL-06/D-18): additive projection of the manifest's
    # ``enables_http_graphql`` flag (Phase 304 gate); the Plugins admin page
    # consumes it to render the production warning.
    enables_http_graphql: bool = False
    # Phase 318: the manifest's ``requires_llm`` flag plus, only for a plugin
    # declaring it, the Core LLM client's status (``configured`` /
    # ``not_configured`` / ``misconfigured``) and its key-free config error —
    # the same view ``GET /config/llm`` reports, so the Plugins admin page can
    # say why such a plugin shows no AI actions.
    requires_llm: bool = False
    llm_status: Optional[str] = None
    llm_config_error: Optional[str] = None
    config_schema: dict[str, Any] = {}
    worker_loaded_at: Optional[datetime] = None
    worker_loaded_version: Optional[str] = None
    database_version_table: Optional[str] = None
    database_error: Optional[str] = None
    database_error_revision: Optional[str] = None
    installed_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class PluginConfigUpdate(BaseModel):
    """Raw submitted plugin config payload (D-11).

    Deliberately not schema-typed: the plugin's ``config_schema`` is
    plugin-specific and only known at request time, so validation against it
    happens in the route handler (``plugin_system.validation``), not here.
    """

    config: dict[str, Any]


class SearchHit(BaseModel):
    id: uuid.UUID
    name: EscapedStr = Field(max_length=500)
    object_type_id: uuid.UUID
    schema_id: uuid.UUID
    score: float = 0.0
    highlights: Optional[dict] = None


class SearchResponse(BaseModel):
    query: EscapedStr = Field(max_length=500)
    total: int
    page: int
    page_size: int
    results: List[SearchHit]
    degraded: bool = Field(
        default=False,
        description=(
            "True when the search backend could not answer and the empty "
            "result set is a degradation, not an authoritative 'no matches'."
        ),
    )


class AQLQuery(BaseModel):
    """Request schema for POST /api/v1/aql/objects."""
    qlQuery: str = Field(max_length=10000)
    page: int = Field(default=1, ge=1)
    pageSize: int = Field(default=100, ge=1, le=500)


class AQLResponse(BaseModel):
    """Paginated response for AQL query results.

    Reuses ObjectResponse for individual items (D-06).
    """
    items: List[ObjectResponse]
    total: int
    page: int
    page_size: int


class AQLErrorDetail(BaseModel):
    """Single error object for structured AQL error responses (D-02)."""
    code: str
    message: str
    line: Optional[int] = None
    column: Optional[int] = None


class AQLErrorResponse(BaseModel):
    """Error response wrapper supporting single error or error array (D-03)."""
    error: Optional[AQLErrorDetail] = None
    errors: Optional[List[AQLErrorDetail]] = None


class AQLSuggestRequest(BaseModel):
    """Request schema for POST /api/v1/aql/suggest.

    qlQuery max_length matches app.aql.parser._MAX_QUERY_LENGTH (2048), not
    AQLQuery's 10000 — the suggest endpoint only ever parses the pre-cursor
    slice, which is bounded by the same safety guard as the one-shot parser.
    """
    qlQuery: str = Field(max_length=2048)
    cursorPos: int = Field(ge=0)


class AQLSuggestion(BaseModel):
    """A single classified autocomplete suggestion."""
    kind: str
    value: str
    insertText: str


class AQLSuggestResponse(BaseModel):
    """Response schema for POST /api/v1/aql/suggest.

    An empty `suggestions` list is a valid, expected 200 response (D-03) —
    e.g. when the pre-cursor query text is already structurally invalid.
    It is never an error.
    """
    suggestions: List[AQLSuggestion]


class CrossEntityResult(BaseModel):
    id: uuid.UUID
    name: EscapedStr = Field(max_length=500)
    type: EscapedStr = Field(max_length=20)  # 'object', 'object_type', 'schema'
    description: Optional[PlainStr] = Field(default=None, max_length=2000)
    url: EscapedStr = Field(max_length=500)  # Frontend route: /browse/objects/{id}, /browse/schemas/{schema_id}, etc.
    highlights: Optional[dict] = None  # Same structure as SearchHit.highlights


class CrossEntityTypeGroup(BaseModel):
    count: int
    results: List[CrossEntityResult]


class CrossEntitySearchResponse(BaseModel):
    query: EscapedStr = Field(max_length=500)
    objects: CrossEntityTypeGroup
    object_types: CrossEntityTypeGroup
    schemas: CrossEntityTypeGroup


class GraphNode(BaseModel):
    id: uuid.UUID
    name: EscapedStr = Field(max_length=255)
    icon_local_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    schema_id: uuid.UUID
    is_anchor: bool = False


class GraphEdge(BaseModel):
    # id = ObjectTypeAttribute.id as string (D-06). Under inheritance an
    # OTA can be an effective edge of several nodes; the planner lock for
    # Research Pitfall 4 is: direct edge wins (source = defining type when
    # the defining type is in the node set), otherwise one inherited edge
    # per in-set effective owner with a deterministic ":<source>" suffix —
    # hence str, not uuid.UUID.
    id: str
    source: uuid.UUID
    target: uuid.UUID
    attribute_id: uuid.UUID
    attribute_name: EscapedStr = Field(max_length=255)
    reference_type_id: Optional[uuid.UUID] = None
    reference_type_color: Optional[EscapedStr] = Field(default=None, max_length=50)
    is_inherited: bool = False


class GraphResponse(BaseModel):
    nodes: List[GraphNode]
    edges: List[GraphEdge]
    truncated: bool
    totalCount: int


class ObjectGraphNode(BaseModel):
    id: uuid.UUID
    name: EscapedStr = Field(max_length=500)
    object_type_id: uuid.UUID
    schema_id: uuid.UUID
    object_type_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    icon_local_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    is_anchor: bool = False


class ObjectGraphEdge(BaseModel):
    # id = ObjectRef.id as string (D-03) — unique per ref row; parallel
    # refs between the same pair are allowed (like 216 D-07), each with
    # its own id. No inheritance suffixing — the 1-hop object graph has
    # no is_inherited concept.
    id: str
    source: uuid.UUID
    target: uuid.UUID
    from_attribute_name: Optional[EscapedStr] = Field(default=None, max_length=255)


class ObjectGraphResponse(BaseModel):
    nodes: List[ObjectGraphNode]
    edges: List[ObjectGraphEdge]
    truncated: bool
    totalCount: int


class ScheduleBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    job_function: Optional[EscapedStr] = Field(default=None, max_length=100)
    cron_expression: Optional[EscapedStr] = Field(default=None, max_length=50)
    args: Optional[dict] = {}
    enabled: bool = True
    source_id: Optional[uuid.UUID] = None
    schema_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    template_id: Optional[uuid.UUID] = None
    template_type: Optional[EscapedStr] = Field(default=None, max_length=20)
    timezone: str = Field(default="UTC", max_length=50)
    # Phase 286 (PLUGTASK-01): shared 255-char bound with the Task contract.
    # Schedule admission stays Core-only in schedule_service.
    tasktype: Optional[str] = Field(default=None, max_length=255)
    preset_type: Optional[str] = Field(default=None, max_length=20)


class ScheduleCreate(ScheduleBase):
    pass


class ScheduleUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    job_function: Optional[EscapedStr] = Field(default=None, max_length=100)
    cron_expression: Optional[EscapedStr] = Field(default=None, max_length=50)
    args: Optional[dict] = None
    enabled: Optional[bool] = None
    source_id: Optional[uuid.UUID] = None
    schema_id: Optional[uuid.UUID] = None
    object_type_id: Optional[uuid.UUID] = None
    target_id: Optional[uuid.UUID] = None
    template_id: Optional[uuid.UUID] = None
    template_type: Optional[EscapedStr] = Field(default=None, max_length=20)
    timezone: Optional[str] = Field(default=None, max_length=50)
    tasktype: Optional[str] = Field(default=None, max_length=255)
    preset_type: Optional[str] = Field(default=None, max_length=20)


class ScheduleResponse(ScheduleBase):
    id: uuid.UUID
    last_run_at: Optional[datetime] = None
    next_run_at: Optional[datetime] = None
    # Phase 289 (CORESEAM-07, D-01/D-02): response-only runtime projection of
    # PERSISTED truth — `scheduled` | `paused` | `disabled` plus a bounded,
    # secret-free reason naming plugin/state/version. Never derived from
    # APScheduler job presence and never written back into `Schedule.enabled`.
    # Optional so a path that does not project reports null instead of a
    # fabricated state.
    runtime_status: Optional[str] = None
    runtime_reason: Optional[str] = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SchedulePreviewRequest(BaseModel):
    preset_type: Optional[EscapedStr] = Field(default=None, max_length=20)
    args: Optional[dict] = None
    cron_expression: Optional[EscapedStr] = Field(default=None, max_length=50)
    timezone: str = Field(default="UTC", max_length=50)


class SchedulePreviewResponse(BaseModel):
    human_readable: str


class IconBase(BaseModel):
    schema_id: uuid.UUID
    name: EscapedStr = Field(max_length=255)
    local_name: Optional[EscapedStr] = Field(default=None, max_length=100)
    url: EscapedStr = Field(max_length=1000)
    type: EscapedStr = Field(default="url", max_length=50)


class IconCreate(IconBase):
    pass


class IconUpdate(BaseModel):
    schema_id: Optional[uuid.UUID] = None
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    local_name: Optional[EscapedStr] = Field(default=None, max_length=100)
    url: Optional[EscapedStr] = Field(default=None, max_length=1000)
    type: Optional[EscapedStr] = Field(default=None, max_length=50)


class IconResponse(IconBase):
    id: uuid.UUID
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SavedQueryBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    query_text: str
    # 304 D-01/D-02: optional default variables. Omitted or null both persist
    # as NULL ("no stored defaults").
    variables: dict | None = None


class SavedQueryCreate(SavedQueryBase):
    pass


class SavedQueryUpdate(BaseModel):
    name: EscapedStr | None = Field(default=None, max_length=255)
    query_text: str | None = None
    # Nullable on purpose: an omitted field must NOT null the stored value —
    # the service only assigns when the field is present in model_fields_set.
    variables: dict | None = None


class SavedQueryResponse(SavedQueryBase):
    id: uuid.UUID
    created_by_id: uuid.UUID | None = None
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class SavedQueryExecuteRequest(BaseModel):
    """Request body for POST /saved-queries/{id}/execute (304 D-02/D-03).

    ``variables`` are flat-merged over the stored defaults (request wins).
    ``operation_name`` is transient — it selects an operation for documents
    that carry more than one and is never persisted.
    """

    variables: dict | None = None
    operation_name: str | None = None


# 304 D-08: the portable saved-query envelope. The format discriminator is a
# literal so an incompatible document is rejected while parsing, and the
# version is pinned to 1 — a future format bump must be an explicit new value.
SAVED_QUERIES_EXPORT_FORMAT = "indramesh.saved-queries"
SAVED_QUERIES_EXPORT_VERSION = 1


class SavedQueriesExportEntry(BaseModel):
    """One query in the export/import envelope (304 D-08).

    Deliberately carries only name/query_text/variables — never ``id``,
    ``created_by_id`` or timestamps — so a document stays portable across
    instances (D-08). ``name`` is ``EscapedStr`` so an import gets the same
    write-time HTML escaping as create/update; ``_escape_str`` skips values
    that already carry entities, so a genuine export still round-trips
    verbatim.
    """

    name: EscapedStr = Field(max_length=255)
    query_text: str
    variables: dict | None = None


class SavedQueriesExportDocument(BaseModel):
    """Versioned, id-free export/import document (304 D-08/D-10)."""

    format: Literal["indramesh.saved-queries"]
    version: Literal[1]
    queries: List[SavedQueriesExportEntry]


class SavedQueriesImportResult(BaseModel):
    """Outcome of an all-or-nothing import (304 D-09/D-10)."""

    imported: int
    renamed: int


class DistinctValueItem(BaseModel):
    value: str | int | float | bool | None = None
    count: int


class SchemaTemplateBase(BaseModel):
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=500)
    category: Optional[EscapedStr] = Field(default=None, max_length=100)
    template_data: dict


class SchemaTemplateCreate(SchemaTemplateBase):
    pass


class SchemaTemplateUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=255)
    description: Optional[PlainStr] = Field(default=None, max_length=500)
    category: Optional[EscapedStr] = Field(default=None, max_length=100)
    template_data: Optional[dict] = None

    @model_validator(mode="before")
    @classmethod
    def reject_null_template_data(cls, values):
        if isinstance(values, dict) and "template_data" in values and values["template_data"] is None:
            raise ValueError("template_data cannot be set to null")
        return values


class SchemaTemplateResponse(SchemaTemplateBase):
    id: uuid.UUID
    usage_count: int = 0
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AtomicCreateAttribute(BaseModel):
    name: EscapedStr = Field(max_length=255)
    type_string: EscapedStr = Field(max_length=50)
    description: Optional[PlainStr] = None
    required: bool = False
    is_unique: bool = False
    default_value: Optional[EscapedStr] = Field(default=None, max_length=2000)
    select_options: Optional[List[str]] = None
    status_option_names: Optional[List[str]] = None
    referenced_object_type_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    reference_type_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    # A schema template's classification block, keyed by stable catalogue label
    # keys (D-08). ``None`` means "the template says nothing" — the attribute is
    # created unclassified, exactly as a template saved before this field
    # existed behaves.
    classification: Optional[TemplateClassification] = None


class AtomicCreateObjectType(BaseModel):
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = None
    icon_key: EscapedStr = Field(default="box", max_length=100)
    # icon_type/icon_url carry a REAL (e.g. Jira-synced) icon's own type+URL
    # through a schema template — None means "icon_key is a lucide slug"
    # (the original AI-schema-wizard behavior, preserved for compatibility).
    icon_type: Optional[EscapedStr] = Field(default=None, max_length=50)
    icon_url: Optional[EscapedStr] = Field(default=None, max_length=1000)
    parent_name: Optional[EscapedStr] = Field(default=None, max_length=255)
    inherits_attributes: bool = False
    is_abstract: bool = False
    attributes: List[AtomicCreateAttribute] = []


class AtomicCreateStatus(BaseModel):
    name: EscapedStr = Field(max_length=100)
    color: Optional[EscapedStr] = Field(default=None, max_length=50)
    status_category: Optional[EscapedStr] = Field(default=None, max_length=20)


class AtomicCreateRequest(BaseModel):
    source_id: uuid.UUID
    name: EscapedStr = Field(max_length=255)
    description: Optional[PlainStr] = None
    statuses: List[AtomicCreateStatus] = []
    object_types: List[AtomicCreateObjectType]


class AtomicCreateResponse(SchemaResponse):
    pass


class ProfileUpdate(BaseModel):
    first_name: Optional[EscapedStr] = Field(default=None, max_length=100)
    last_name: Optional[EscapedStr] = Field(default=None, max_length=100)
    avatar_url: Optional[EscapedStr] = Field(default=None, max_length=500, pattern=r'^https?://')
    ui_language: Optional[EscapedStr] = Field(default=None, max_length=10)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=255)
    # Length and composition come from the configurable password policy.
    new_password: str = Field(min_length=1, max_length=255)
    confirm_password: str = Field(min_length=1, max_length=255)

    @model_validator(mode="after")
    def passwords_match(self):
        if self.new_password != self.confirm_password:
            raise ValueError("new_password and confirm_password must match")
        return self


class PasswordReset(BaseModel):
    new_password: str = Field(min_length=1, max_length=255)


class UserBase(BaseModel):
    username: EscapedStr = Field(max_length=100)
    email: EscapedStr = Field(max_length=255)


class UserCreate(UserBase):
    password: PlainStr = Field(max_length=255)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


class UserUpdate(BaseModel):
    username: Optional[EscapedStr] = Field(default=None, max_length=100)
    email: Optional[EscapedStr] = Field(default=None, max_length=255)
    is_active: Optional[bool] = None
    is_superuser: Optional[bool] = None


class UserListResponse(BaseModel):
    id: uuid.UUID
    username: EscapedStr = Field(max_length=100)
    email: EscapedStr = Field(max_length=255)
    is_active: bool
    is_superuser: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class UserProfileResponse(BaseModel):
    first_name: Optional[EscapedStr] = None
    last_name: Optional[EscapedStr] = None
    avatar_url: Optional[EscapedStr] = None
    ui_language: Optional[EscapedStr] = None

    model_config = ConfigDict(from_attributes=True)


class UserAdminResponse(UserListResponse):
    profile: Optional[UserProfileResponse] = None
    roles: List[str] = Field(default_factory=list, description="Role names assigned to this user")
    # WR-04 (246 review): distinct effective roles (direct user_roles UNION
    # group-inherited via users_groups/groups_roles) — lets the frontend
    # self-lockout guard match count_effective_roles(), the same figure the
    # backend guard actually authorizes against, instead of approximating
    # from roles.length (direct roles only). Only GET /users/{id} populates
    # this (set on the response object after fetch, not via flatten_roles,
    # since it needs a DB query); list/create responses leave it None.
    effective_role_count: Optional[int] = Field(
        default=None,
        description="Distinct effective roles (direct + group-inherited); populated on GET /users/{id} only",
    )

    @model_validator(mode="before")
    @classmethod
    def flatten_roles(cls, data):
        if hasattr(data, "roles") and not isinstance(data, dict):
            return {
                "id": data.id,
                "username": data.username,
                "email": data.email,
                "is_active": data.is_active,
                "is_superuser": data.is_superuser,
                "created_at": data.created_at,
                "updated_at": data.updated_at,
                "profile": data.profile,
                "roles": [ur.role.name for ur in data.roles],
            }
        return data


class UserPickerResponse(BaseModel):
    id: uuid.UUID
    username: EscapedStr = Field(max_length=100)
    email: EscapedStr = Field(max_length=255)

    model_config = ConfigDict(from_attributes=True)


class RoleBase(BaseModel):
    name: EscapedStr = Field(max_length=100)
    description: Optional[EscapedStr] = Field(default=None, max_length=500)


class RoleCreate(RoleBase):
    pass


class RoleUpdate(BaseModel):
    name: Optional[EscapedStr] = Field(default=None, max_length=100)
    description: Optional[EscapedStr] = Field(default=None, max_length=500)


class PermissionResponse(BaseModel):
    id: uuid.UUID
    name: str
    resource: str
    action: str
    description: Optional[str] = None
    # Phase 303 (SEAM-01/D-03): additive ownership projection. ``owner_plugin``
    # is NULL for Core; ``owner_state`` is the closed vocabulary
    # core|active|disabled|unavailable; ``owner_label`` is the stable plugin
    # name (the manifest carries no separate display name).
    owner_plugin: Optional[str] = None
    owner_state: Literal["core", "active", "disabled", "unavailable"] = "core"
    owner_label: Optional[str] = None

    model_config = ConfigDict(from_attributes=True)


class RoleListResponse(BaseModel):
    id: uuid.UUID
    name: str
    description: Optional[str] = None
    is_system: bool
    created_at: datetime
    updated_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RoleResponse(RoleListResponse):
    permissions: List[dict] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def flatten_permissions(cls, data):
        if hasattr(data, "permissions") and not isinstance(data, dict):
            return {
                "id": data.id,
                "name": data.name,
                "description": data.description,
                "is_system": data.is_system,
                "created_at": data.created_at,
                "updated_at": data.updated_at,
                "permissions": [
                    {"id": rp.permission.id, "name": rp.permission.name, "is_deny": rp.is_deny}
                    for rp in data.permissions
                ],
            }
        return data


class RolePermissionAssign(BaseModel):
    permission_id: uuid.UUID
    is_deny: bool = False


class UserRoleAssign(BaseModel):
    role_id: uuid.UUID


class RecordPermissionCreate(BaseModel):
    role_id: uuid.UUID
    record_entity_type: str
    record_id: uuid.UUID
    action: Literal["read", "write", "delete", "*"] = "read"
    is_deny: bool = False


class RecordPermissionResponse(BaseModel):
    id: uuid.UUID
    role_id: uuid.UUID
    record_entity_type: str
    record_id: uuid.UUID
    resource: str
    action: str
    is_deny: bool
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class RecordPermissionListResponse(BaseModel):
    items: list[RecordPermissionResponse]
    total: int


class EffectivePermissionItem(BaseModel):
    role_id: uuid.UUID
    action: str
    is_deny: bool
    is_direct: bool
    source_entity_type: str | None = None
    source_record_id: uuid.UUID | None = None


class EffectivePermissionResponse(BaseModel):
    items: list[EffectivePermissionItem]


class AuditLogBase(BaseModel):
    entity_type: str = Field(max_length=50)
    entity_id: uuid.UUID
    action: str = Field(max_length=10)
    actor_id: uuid.UUID | None = None
    changes: dict


class AuditLogResponse(AuditLogBase):
    id: uuid.UUID
    created_at: datetime
    actor_display_name: str | None = None
    actor_initials: str | None = None
    attribute_name: str | None = None
    reference_field_name: str | None = None
    reference_old_target_name: str | None = None
    reference_new_target_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AuditLogFilter(BaseModel):
    """Query parameters for GET /audit-logs.
    Per D-04: offset-based pagination (page/page_size).
    Per D-05: actor_q does substring match on users.first_name/last_name/email.

    Param names (`entity_type`, `start_date`, `end_date`, `actor_q`) MUST match
    Phase 167's already-planned contract exactly (167-UI-SPEC.md Data Flow
    section, use-audit-logs.js) — see 166-PLAN-REVIEW.md #1. Do not rename
    these independently; Phase 167 is downstream and won't be replanned.
    """
    entity_type: str | None = Field(default=None, max_length=50)
    actor_q: str | None = Field(default=None, max_length=100)
    start_date: datetime | None = None
    end_date: datetime | None = None
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=50, ge=1, le=200)


class AuditLogPaginatedResponse(BaseModel):
    items: list[AuditLogResponse]
    total: int
    page: int
    page_size: int
    total_pages: int


class AuditLogConfigBase(BaseModel):
    entity_type: str = Field(max_length=50)
    audit_log_enabled: bool = False


class AuditLogConfigResponse(AuditLogConfigBase):
    updated_at: datetime | None = None
    updated_by: uuid.UUID | None = None

    model_config = ConfigDict(from_attributes=True)


class AuditLogConfigToggle(BaseModel):
    audit_log_enabled: bool

