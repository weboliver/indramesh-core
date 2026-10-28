import i18next from 'i18next'

function t(key, defaultValue) {
  return i18next.isInitialized ? i18next.t(key, defaultValue) : defaultValue
}

export const AUDIT_ENTITY_TYPES = [
  { value: 'sources', label: t('admin.audit.entity.sources', 'Sources') },
  { value: 'schemas', label: t('admin.audit.entity.schemas', 'Schemas') },
  { value: 'object_types', label: t('admin.audit.entity.object_types', 'Object Types') },
  { value: 'objects', label: t('admin.audit.entity.objects', 'Objects') },
  { value: 'object_values', label: t('admin.audit.entity.object_values', 'Object Values') },
  { value: 'object_refs', label: t('admin.audit.entity.object_refs', 'Object Refs') },
  { value: 'attribute_types', label: t('admin.audit.entity.attribute_types', 'Attribute Types') },
  { value: 'reference_types', label: t('admin.audit.entity.reference_types', 'Reference Types') },
  { value: 'statuses', label: t('admin.audit.entity.statuses', 'Statuses') },
  { value: 'icons', label: t('admin.audit.entity.icons', 'Icons') },
  { value: 'targets', label: t('admin.audit.entity.targets', 'Targets') },
  { value: 'service_accounts', label: t('admin.audit.entity.service_accounts', 'Service Accounts') },
  { value: 'schema_templates', label: t('admin.audit.entity.schema_templates', 'Schema Templates') },
  { value: 'import_definitions', label: t('admin.audit.entity.import_definitions', 'Import Definitions') },
  { value: 'export_definitions', label: t('admin.audit.entity.export_definitions', 'Export Definitions') },
  { value: 'tasks', label: t('admin.audit.entity.tasks', 'Tasks') },
  { value: 'audit_log_config', label: t('admin.audit.entity.audit_log_config', 'Audit Log Config') },
  { value: 'settings', label: t('admin.audit.entity.settings', 'Settings') },
  { value: 'permissions', label: t('admin.audit.entity.permissions', 'Permissions') },
  { value: 'users', label: t('admin.audit.entity.users', 'Users') },
  { value: 'roles', label: t('admin.audit.entity.roles', 'Roles') },
  { value: 'plugins', label: t('admin.audit.entity.plugins', 'Plugins') },
]

// Security/config-critical entity types whose audit toggle is always on and
// cannot be disabled — kept in sync with ALWAYS_ON_ENTITY_TYPES in
// backend/app/services/audit_service.py.
export const ALWAYS_ON_ENTITY_TYPES = ['audit_log_config', 'settings', 'permissions', 'users', 'roles', 'plugins']
