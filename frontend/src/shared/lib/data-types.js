import i18next from 'i18next'

function t(key, defaultValue) {
  return i18next.isInitialized ? i18next.t(key, defaultValue) : defaultValue
}

export const CATEGORIES = [
  {
    id: 'default',
    label: t('common.data_type_category.default', 'Default'),
    types: [
      { label: t('common.data_type.text', 'Text'), value: 'text' },
      { label: t('common.data_type.number', 'Number'), value: 'double' },
      { label: t('common.data_type.integer', 'Integer'), value: 'integer' },
      { label: t('common.data_type.boolean', 'Boolean'), value: 'boolean' },
      { label: t('common.data_type.datetime', 'Datetime'), value: 'datetime' },
      { label: t('common.data_type.json', 'JSON'), value: 'json' },
      { label: t('common.data_type.url', 'URL'), value: 'url' },
      { label: t('common.data_type.email', 'Email'), value: 'email' },
      { label: t('common.data_type.select', 'Select'), value: 'select' },
      { label: t('common.data_type.textarea', 'Textarea'), value: 'textarea' },
    ],
  },
  {
    id: 'object',
    label: t('common.data_type_category.object', 'Object'),
    types: [
      { label: t('common.data_type.reference', 'Reference'), value: 'reference', needsObjectType: true },
    ],
  },
  {
    id: 'user',
    label: t('common.data_type_category.user', 'User'),
    types: [
      { label: t('common.data_type.group', 'Group'), value: 'group' },
      { label: t('common.data_type.user', 'User'), value: 'user' },
    ],
  },
  {
    id: 'project',
    label: t('common.data_type_category.project', 'Project'),
    types: [],
    directValue: 'version',
  },
  {
    id: 'status',
    label: t('common.data_type_category.status', 'Status'),
    types: [],
    directValue: 'status',
  },
]

export const DATA_TYPE_LABELS = {
  text: t('common.data_type.text', 'Text'),
  double: t('common.data_type.number', 'Number'),
  integer: t('common.data_type.integer', 'Integer'),
  boolean: t('common.data_type.boolean', 'Boolean'),
  datetime: t('common.data_type.datetime', 'Datetime'),
  json: t('common.data_type.json', 'JSON'),
  url: t('common.data_type.url', 'URL'),
  email: t('common.data_type.email', 'Email'),
  textarea: t('common.data_type.textarea', 'Textarea'),
  select: t('common.data_type.select', 'Select'),
  reference: t('common.data_type.reference', 'Reference'),
  group: t('common.data_type.group', 'Group'),
  user: t('common.data_type.user', 'User'),
  status: t('common.data_type.status', 'Status'),
  version: t('common.data_type.version', 'Version'),
}
