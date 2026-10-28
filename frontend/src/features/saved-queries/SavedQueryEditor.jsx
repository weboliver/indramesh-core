import { useTranslation } from 'react-i18next'
import { Button } from '../../shared/components/Button'
import { FormField, TextInput, SelectInput } from '../../shared/components/FormControls'

const CONTROL_CLASSES =
  'w-full rounded-md border border-input bg-background px-3 py-1.5 font-mono text-sm text-foreground resize-y focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2'

function describedBy(id, hasHelp, hasError) {
  const ids = []
  if (hasHelp) ids.push(`${id}-help`)
  if (hasError) ids.push(`${id}-error`)
  return ids.length > 0 ? ids.join(' ') : undefined
}

function MonoTextarea({ id, help, error, className, ...props }) {
  return (
    <textarea
      id={id}
      className={`${CONTROL_CLASSES} ${className || ''}`}
      aria-invalid={error ? true : undefined}
      aria-describedby={describedBy(id, Boolean(help), Boolean(error))}
      spellCheck={false}
      {...props}
    />
  )
}

// Simple Core editor (304 D-13): monospace textareas, no editor dependency.
// Client validation (syntax / query-only / operation type / variables JSON)
// gives immediate feedback; the server 422 stays authoritative and is shown
// inline via aria-invalid / aria-describedby without clearing input (UI-SPEC E2).
export default function SavedQueryEditor({
  state,
  onChange,
  analysis,
  fieldErrors,
  variablesHelp,
  canSave,
  canExecute,
  canExecuteNow,
  onSave,
  onExecute,
  saving,
  executing,
  saveError,
  executeBlockReason,
  headingRef,
}) {
  const { t } = useTranslation()

  return (
    <section
      className="space-y-4 rounded-lg border bg-card p-4"
      data-testid="saved-query-editor"
      aria-label={t('graphql.saved_queries.editor_heading', 'Editor')}
    >
      <h2
        ref={headingRef}
        tabIndex={-1}
        className="text-base font-semibold focus:outline-none"
      >
        {t('graphql.saved_queries.editor_heading', 'Editor')}
      </h2>

      <FormField id="saved-query-name" label={t('graphql.saved_queries.field.name', 'Name')} required error={fieldErrors.name}>
        <TextInput
          id="saved-query-name"
          value={state.name}
          maxLength={255}
          autoComplete="off"
          error={fieldErrors.name}
          onChange={(event) => onChange({ name: event.target.value })}
          data-testid="saved-query-name"
        />
      </FormField>

      <FormField
        id="saved-query-query"
        label={t('graphql.saved_queries.field.query', 'Query')}
        required
        error={fieldErrors.queryText}
      >
        <MonoTextarea
          id="saved-query-query"
          value={state.queryText}
          error={fieldErrors.queryText}
          onChange={(event) => onChange({ queryText: event.target.value })}
          className="min-h-48"
          data-testid="saved-query-query"
        />
      </FormField>

      <FormField
        id="saved-query-variables"
        label={t('graphql.saved_queries.field.variables', 'Variables (JSON)')}
        help={variablesHelp}
        error={fieldErrors.variables}
      >
        <MonoTextarea
          id="saved-query-variables"
          value={state.variablesText}
          help={variablesHelp}
          error={fieldErrors.variables}
          onChange={(event) => onChange({ variablesText: event.target.value })}
          className="min-h-32"
          data-testid="saved-query-variables"
        />
      </FormField>

      {analysis.multiple && (
        <FormField
          id="saved-query-operation-name"
          label={t('graphql.saved_queries.operation_name_label', 'Operation name')}
          error={fieldErrors.operationName}
        >
          <SelectInput
            id="saved-query-operation-name"
            value={state.operationName}
            error={fieldErrors.operationName}
            placeholder={t('graphql.saved_queries.operation_name_placeholder', 'Select an operation')}
            options={analysis.operationNames.map((name) => ({ value: name, label: name }))}
            onChange={(event) => onChange({ operationName: event.target.value })}
            data-testid="saved-query-operation-name"
          />
        </FormField>
      )}

      {saveError && (
        <p className="text-xs text-destructive" role="alert" data-testid="saved-query-save-error">
          {t('graphql.saved_queries.save_failure', 'The query could not be saved. {{reason}} Fix the highlighted field and try again.', { reason: saveError })}
        </p>
      )}

      {executeBlockReason && (
        <p className="text-xs text-destructive" data-testid="saved-query-execute-blocked">
          {executeBlockReason}
        </p>
      )}

      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant={canExecute ? 'outline' : 'primary'}
          size="md"
          onClick={onSave}
          disabled={!canSave}
          loading={saving}
          className="h-auto min-h-9 whitespace-normal"
          data-testid="saved-query-save"
        >
          {t('graphql.saved_queries.save_query', 'Save query')}
        </Button>
        {canExecute ? (
          <Button
            type="button"
            variant="primary"
            size="md"
            onClick={onExecute}
            disabled={!canExecuteNow}
            loading={executing}
            className="h-auto min-h-9 whitespace-normal"
            data-testid="saved-query-execute"
          >
            {t('graphql.saved_queries.execute', 'Execute')}
          </Button>
        ) : (
          <p className="max-w-md text-xs text-muted-foreground" data-testid="saved-query-execute-not-permitted">
            {t('graphql.saved_queries.execute_not_permitted', 'You need the saved-queries:execute permission to run queries. Ask an administrator to assign it.')}
          </p>
        )}
      </div>
    </section>
  )
}