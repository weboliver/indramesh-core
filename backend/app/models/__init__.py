from app.models.source import Source
from app.models.schema import Schema
from app.models.icon import Icon
from app.models.language import Language
from app.models.schema_language import SchemaLanguage
from app.models.translation_type import TranslationType
from app.models.translation import Translation
from app.models.ui_string import UiString
from app.models.object_type import ObjectType
from app.models.object import Object
from app.models.object_file import ObjectFile
from app.models.object_comment import ObjectComment
from app.models.object_type_attribute import ObjectTypeAttribute
from app.models.object_value import ObjectValue
from app.models.object_ref import ObjectRef
from app.models.object_write_protection_release import ObjectWriteProtectionRelease
from app.models.attribute_type import AttributeType, AttributeTypeDataType
from app.models.api_client import ApiClient
from app.models.audit_log import AuditLog
from app.models.audit_log_config import AuditLogConfig
from app.models.reference_type import ReferenceType
from app.models.status import Status, StatusCategory
from app.models.import_export import Import, Export, ImportStatusEnum, ExportStatusEnum
from app.models.import_export_status import ImportStatus, ExportStatus
from app.models.schedule import Schedule
from app.models.task import Task, TaskStep, TaskStatus, TaskType
from app.models.task_log import TaskLog, TaskLogState
from app.models.target import Target
from app.models.service_account import ServiceAccount
from app.models.user import User, UserProfile
from app.models.permission import Permission, RolePermission
from app.models.record_permission import RecordPermission, RecordEntityType
from app.models.role import Role, UserRole
from app.models.user_group import UserGroup
from app.models.group_role import GroupRole
from app.models.saved_query import SavedQuery
from app.models.index_outbox import IndexOutbox, IndexOutboxStatus, IndexOutboxOperation
from app.models.schema_template import SchemaTemplate
from app.models.export_definition import ExportDefinition
from app.models.import_definition import ImportDefinition
from app.models.group import Group
from app.models.plugin import Plugin, PluginState
from app.models.system_setting import SystemSetting
from app.models.classification import (
    ClassificationFamily,
    ClassificationLabel,
    SchemaClassificationLabel,
    ObjectTypeClassificationLabel,
    ObjectTypeAttributeClassificationLabel,
    ObjectClassificationLabel,
)

# Last: registers the indexes the Core migration baseline owns on
# ``Base.metadata`` (see the module docstring). Kept at the end so every table
# it references already exists.
from app.db import schema_contract as _schema_contract  # noqa: E402,F401