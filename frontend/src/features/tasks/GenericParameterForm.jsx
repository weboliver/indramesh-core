import { useMemo } from 'react'
import { useTranslation } from 'react-i18next'
import { useSources } from '../../shared/hooks/use-sources'
import { useSchemas } from '../../shared/hooks/use-schemas'
import { useTargets } from '../../shared/hooks/use-targets'
import { useObjectTypes } from '../../shared/hooks/use-object-types'
import { useServiceAccounts } from '../../shared/hooks/use-other-resources'
import {
  CheckboxField,
  FormField,
  NumberInput,
  SelectInput,
  TextInput,
} from '../../shared/components/FormControls'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { buildParameterFields, orderedScopeRequirements } from './task-catalog'

// Phase 291 (D-10, FEEXT-05): the generic fallback form for plugin task types
// without a contributed `taskForms` component. Scope pickers first (in the
// fixed SCOPE_FIELD_ORDER), then one control per `parameter_schema` property in
// schema order. Never a JSON text area — unsupported shapes are explained and
// keep their defaults; a required unsupported shape blocks submit.
//
// The host (CreateTaskModal) owns the form state, validation and the POST; this
// component only renders and reports changes.

const SCOPE_LABELS = {
  source_id: ['tasks.form.source', 'Source'],
  schema_id: ['tasks.form.schema', 'Schema'],
  target_id: ['tasks.form.target', 'Target'],
  object_type_id: ['tasks.form.object_type_scope', 'ObjectType (scope)'],
  service_account_id: ['tasks.form.service_account', 'Service Account'],
}

function scopeErrorText(t, field) {
  switch (field) {
    case 'source_id':
      return t('tasks.validation.source', 'Please select a source')
    case 'schema_id':
      return t('tasks.validation.schema', 'Please select a schema')
    case 'target_id':
      return t('tasks.validation.target', 'Please select a Target')
    case 'object_type_id':
      return t('tasks.validation.object_type', 'Please select an ObjectType')
    case 'service_account_id':
      return t('tasks.validation.service_account', 'Please select a service account')
    default:
      return ''
  }
}

export default function GenericParameterForm({
  entry,
  scope,
  onScopeChange,
  values,
  onValuesChange,
  errors,
  disabled = false,
}) {
  const { t } = useTranslation()
  const { fields: scopeFields, unsupportedScope } = useMemo(() => orderedScopeRequirements(entry), [entry])
  const fields = useMemo(() => buildParameterFields(entry?.parameter_schema), [entry])

  // Scope option sources (same shared read hooks/query keys as the host forms).
  const { data: sourcesData } = useSources(0, 1000)
  const sources = sourcesData || []
  const { data: schemasData } = useSchemas(scope?.source_id || null, 0, 200, { enabled: true })
  const schemas = schemasData || []
  const { data: targetsData } = useTargets(0, 200)
  const targets = Array.isArray(targetsData) ? targetsData : targetsData?.items || []
  const { data: objectTypesData } = useObjectTypes(scope?.schema_id)
  const objectTypes = objectTypesData || []
  const { data: serviceAccountsData } = useServiceAccounts(
    scope?.source_id || null,
    0,
    100,
    scope?.target_id || null
  )
  const serviceAccounts = serviceAccountsData || []

  const blocked =
    unsupportedScope.length > 0 || fields.some((field) => field.kind === 'unsupported' && field.required)

  function renderScopePicker(field) {
    const id = 'generic-scope-' + field
    const [labelKey, labelDefault] = SCOPE_LABELS[field] || []
    const error = errors?.scopeErrors?.[field] ? scopeErrorText(t, field) : ''
    let options = []
    let placeholder = ''
    let selectDisabled = disabled

    if (field === 'source_id') {
      options = sources.map((source) => ({ value: source.id, label: source.name }))
      placeholder = t('tasks.form.select_source', 'Select a source...')
    } else if (field === 'schema_id') {
      options = schemas.map((schema) => ({ value: schema.id, label: schema.name }))
      placeholder = t('tasks.form.select_schema', 'Select a schema...')
    } else if (field === 'target_id') {
      options = targets.map((target) => ({ value: target.id, label: target.name }))
      placeholder = t('tasks.form.select_target', 'Select a Target...')
    } else if (field === 'object_type_id') {
      options = objectTypes.map((objectType) => ({ value: objectType.id, label: objectType.name }))
      selectDisabled = disabled || !scope?.schema_id
      placeholder = scope?.schema_id
        ? t('tasks.form.select_object_type', 'Select an ObjectType...')
        : t('tasks.form.select_schema_first', 'Select a schema first')
    } else if (field === 'service_account_id') {
      options = serviceAccounts.map((account) => ({ value: account.id, label: account.name }))
      placeholder = t('tasks.form.select_service_account', 'Select a service account...')
    }

    return (
      <FormField key={field} id={id} label={labelKey ? t(labelKey, labelDefault) : field} required error={error}>
        <SelectInput
          id={id}
          data-testid={id}
          value={scope?.[field] || ''}
          onChange={(event) => onScopeChange(field, event.target.value)}
          options={options}
          placeholder={placeholder}
          disabled={selectDisabled}
          error={error}
          aria-required="true"
        />
      </FormField>
    )
  }

  function renderParameterField(field) {
    const id = 'generic-param-' + field.key
    const label = field.title || field.key
    const errorKey = errors?.fieldErrors?.[field.key]
    const error =
      errorKey === 'number'
        ? interpolateDefault(t('tasks.validation.number_field', 'Enter a valid number for {{field}}.'), {
            field: label,
          })
        : errorKey
          ? interpolateDefault(t('tasks.validation.required_field', 'Enter a value for {{field}}.'), {
              field: label,
            })
          : ''

    if (field.kind === 'unsupported') {
      return (
        <div key={field.key} className="space-y-1.5" data-testid={'generic-param-unsupported-' + field.key}>
          <span className="text-sm font-medium text-foreground">{label}</span>
          <p className="text-xs text-muted-foreground">
            {interpolateDefault(
              t('tasks.form.unsupported_field', "This setting ({{type}}) can't be entered here. It keeps its default."),
              { type: field.unsupportedType }
            )}
          </p>
        </div>
      )
    }

    if (field.kind === 'boolean') {
      return (
        <CheckboxField
          key={field.key}
          id={id}
          data-testid={id}
          label={label}
          checked={values?.[field.key] === true}
          onChange={(event) => onValuesChange(field.key, event.target.checked)}
          help={field.description}
          disabled={disabled}
        />
      )
    }

    if (field.kind === 'enum') {
      return (
        <FormField key={field.key} id={id} label={label} required={field.required} help={field.description} error={error}>
          <SelectInput
            id={id}
            data-testid={id}
            value={values?.[field.key] ?? ''}
            onChange={(event) => onValuesChange(field.key, event.target.value)}
            options={(field.options || []).map((option) => ({ value: String(option.value), label: option.label }))}
            placeholder={field.required ? undefined : '—'}
            disabled={disabled}
            error={error}
            aria-required={field.required ? 'true' : undefined}
          />
        </FormField>
      )
    }

    if (field.kind === 'integer' || field.kind === 'number') {
      return (
        <FormField key={field.key} id={id} label={label} required={field.required} help={field.description} error={error}>
          <NumberInput
            id={id}
            data-testid={id}
            value={values?.[field.key] ?? ''}
            onChange={(event) => onValuesChange(field.key, event.target.value)}
            min={field.min}
            max={field.max}
            step={field.step}
            disabled={disabled}
            error={error}
            aria-required={field.required ? 'true' : undefined}
          />
        </FormField>
      )
    }

    return (
      <FormField key={field.key} id={id} label={label} required={field.required} help={field.description} error={error}>
        <TextInput
          id={id}
          data-testid={id}
          value={values?.[field.key] ?? ''}
          onChange={(event) => onValuesChange(field.key, event.target.value)}
          disabled={disabled}
          error={error}
          aria-required={field.required ? 'true' : undefined}
        />
      </FormField>
    )
  }

  const hasAnyContent = scopeFields.length > 0 || unsupportedScope.length > 0 || fields.length > 0

  return (
    <div className="space-y-4" data-testid="generic-parameter-form">
      {scopeFields.length > 0 && (
        <div className="grid grid-cols-2 gap-3">{scopeFields.map((field) => renderScopePicker(field))}</div>
      )}

      {unsupportedScope.map((name) => (
        <p key={name} className="text-sm text-muted-foreground" data-testid="generic-unsupported-scope">
          {t(
            'tasks.form.unsupported_required',
            "This task type needs a setting that can't be entered here. Start it through the API."
          )}{' '}
          <span className="font-mono">{name}</span>
        </p>
      ))}

      {fields.map((field) => renderParameterField(field))}

      {!hasAnyContent && (
        <p className="text-sm text-muted-foreground" data-testid="generic-no-settings">
          {t('tasks.form.no_settings', 'This task type needs no settings.')}
        </p>
      )}

      {blocked && (
        <p className="text-sm text-muted-foreground" data-testid="generic-unsupported-required">
          {t(
            'tasks.form.unsupported_required',
            "This task type needs a setting that can't be entered here. Start it through the API."
          )}
        </p>
      )}
    </div>
  )
}
