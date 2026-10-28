export const PHASE231_EMPTY_STATE_DEFAULTS = {
  empty: {
    icon: null,
    title: 'No data yet',
    titleKey: 'common.empty.no_data_title',
    description: 'There are no items to display. Create your first entry to get started.',
    descriptionKey: 'common.empty.no_data_description',
  },
  error: {
    icon: null,
    title: 'Something went wrong',
    titleKey: 'common.empty.error_title',
    description: 'An unexpected error occurred while loading this page. Please try again.',
    descriptionKey: 'common.empty.error_description',
  },
  'no-results': {
    icon: null,
    title: 'No results found',
    titleKey: 'common.empty.no_results_title',
    description: 'No items match your current search or filters. Try adjusting your criteria.',
    descriptionKey: 'common.empty.no_results_description',
  },
}

export const PHASE231_AUTO_BUTTONS = {
  error: { label: 'Try again', key: 'common.try_again', variant: 'primary' },
  'no-results': { label: 'Clear filters', key: 'common.clear_filters', variant: 'outline' },
}

export const PHASE231_AQL_KIND_LABELS = {
  'syntax-keyword': { key: 'aql.suggestion_kind_syntax', defaultValue: 'Syntax' },
  'boolean-keyword': { key: 'aql.suggestion_kind_boolean', defaultValue: 'Boolean' },
  field: { key: 'aql.suggestion_kind_field', defaultValue: 'Field' },
  'ref-fn': { key: 'aql.suggestion_kind_reference', defaultValue: 'Reference' },
  operator: { key: 'aql.suggestion_kind_operator', defaultValue: 'Operator' },
  'objectType-value': { key: 'aql.suggestion_kind_value', defaultValue: 'Value' },
}

export const PHASE231_SCHEDULE_PRESETS = [
  { value: 'EVERY_N_MINUTES', key: 'settings.schedule.preset.every_minutes', defaultLabel: 'Every N minutes' },
  { value: 'EVERY_N_HOURS', key: 'settings.schedule.preset.every_hours', defaultLabel: 'Every N hours' },
  { value: 'DAILY', key: 'settings.schedule.preset.daily', defaultLabel: 'Daily' },
  { value: 'WEEKLY', key: 'settings.schedule.preset.weekly', defaultLabel: 'Weekly' },
]

export const PHASE231_FILTER_OPERATOR_LABELS = {
  eq: { key: 'browse.filters.operator.equals', defaultText: 'equals' },
  gt: { key: 'browse.filters.operator.greater_than', defaultText: '>' },
  lt: { key: 'browse.filters.operator.less_than', defaultText: '<' },
  gte: { key: 'browse.filters.operator.greater_or_equal', defaultText: '≥' },
  lte: { key: 'browse.filters.operator.less_or_equal', defaultText: '≤' },
  between: { key: 'browse.filters.operator.between', defaultText: 'between' },
  after: { key: 'browse.filters.operator.after', defaultText: 'after' },
  before: { key: 'browse.filters.operator.before', defaultText: 'before' },
  contains: { key: 'browse.filters.operator.contains', defaultText: 'contains' },
  starts_with: { key: 'browse.filters.operator.starts_with', defaultText: 'starts with' },
}

export const PHASE231_IMPORT_TABS = [
  { value: 'Field Mappings', key: 'settings.import.tab.field_mappings', defaultText: 'Field Mappings' },
  { value: 'Settings', key: 'settings.import.tab.settings', defaultText: 'Settings' },
]

export const PHASE231_SEARCH_ENTITY_TYPES = {
  object: { key: 'search.entity.objects', defaultText: 'Objects', badgeClass: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200' },
  object_type: { key: 'search.entity.object_types', defaultText: 'Object Types', badgeClass: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' },
  schema: { key: 'search.entity.schemas', defaultText: 'Schemas', badgeClass: 'bg-purple-100 text-purple-800 dark:bg-purple-900 dark:text-purple-200' },
}

export const PHASE231_TRANSLATION_ENTITY_LABELS = {
  object_type: { key: 'translations.entity.object_type', defaultText: 'Object Type' },
  object_type_attribute: { key: 'translations.entity.object_type_attribute', defaultText: 'Attribute' },
  status: { key: 'translations.entity.status', defaultText: 'Status' },
  reference_type: { key: 'translations.entity.reference_type', defaultText: 'Reference Type' },
  object_value: { key: 'translations.entity.object_value', defaultText: 'Value' },
  select_option: { key: 'translations.entity.select_option', defaultText: 'Option' },
}

export const PHASE231_PERMISSION_ENTITY_LABELS = {
  source: { key: 'permissions.entity.source', defaultText: 'Source' },
  schema: { key: 'permissions.entity.schema', defaultText: 'Schema' },
  object_type: { key: 'permissions.entity.object_type', defaultText: 'Object Type' },
  attribute_type: { key: 'permissions.entity.attribute_type', defaultText: 'Attribute Type' },
  object: { key: 'permissions.entity.object', defaultText: 'Object' },
  target: { key: 'permissions.entity.target', defaultText: 'Target' },
}
