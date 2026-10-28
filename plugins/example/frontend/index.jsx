import { useEffect, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import {
  Button,
  showToast,
  api,
  usePluginTranslation,
  interpolateDefault,
  FormField,
  NumberInput,
  SelectInput,
  CheckboxField,
  useSchemas,
  useTasks,
} from '@indramesh/plugin-api'
import en from './locales/en.json'
import de from './locales/de.json'
import es from './locales/es.json'
import fr from './locales/fr.json'
import pt from './locales/pt.json'

function ExampleDashboardPage() {
  const [count, setCount] = useState(0)
  const navigate = useNavigate()
  const { data: ping } = useQuery({
    queryKey: ['plugin-example-ping'],
    queryFn: async () => 'ok',
  })

  return (
    <div className="space-y-4" data-testid="plugin-page-example">
      <h1 className="text-lg font-semibold">Example plugin dashboard</h1>
      <p className="text-sm text-muted-foreground">
        This page is rendered by the plugin bundle; React, the router and the
        QueryClient are the host's own instances.
      </p>
      <p data-testid="plugin-example-ping">Query result: {ping ?? 'pending'}</p>
      <p data-testid="plugin-example-counter">Counter: {count}</p>
      <Button data-testid="plugin-example-increment" onClick={() => setCount((value) => value + 1)}>
        Increment
      </Button>
      <Button data-testid="plugin-example-navigate" onClick={() => navigate('/search')}>
        Go to search
      </Button>
    </div>
  )
}

function ExampleUrlEditor({ attribute, value, onChange, error }) {
  const label = attribute?.name || 'URL'

  return (
    <div className="space-y-1.5" data-testid="plugin-attribute-editor-example">
      <input
        type="url"
        value={value ?? ''}
        onChange={(event) => onChange(event.target.value)}
        aria-invalid={error ? true : undefined}
        aria-label={label}
        placeholder="https://example.com"
        className={`w-full rounded-md border px-3 py-2 text-sm bg-background ${
          error ? 'border-destructive' : 'border-input'
        } focus:outline-none focus:ring-1 focus:ring-primary`}
      />
      <Button type="button" variant="outline" size="sm" onClick={() => onChange('')}>
        Clear
      </Button>
    </div>
  )
}

function ExampleActionButton({ objectType }) {
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      data-testid="plugin-action-button-example"
      onClick={() => showToast(`Example plugin action for ${objectType?.name ?? 'this Object Type'}`)}
    >
      Example action
    </Button>
  )
}

// D-05/D-06: the reference schema-wizard entry. The host renders it inside the
// shared entry grid; the plugin owns only its label copy. The plugin input step
// and the Core review/create path are wired by the consumer plan (Plan 08).
function ExampleSchemaWizardEntry({ onSelect }) {
  const { t } = usePluginTranslation('example')
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      data-testid="plugin-schema-wizard-entry-example"
      className="h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold"
      onClick={() => onSelect?.()}
    >
      {t('wizard.entry.label', 'Use example schema draft')}
    </Button>
  )
}

// D-07/D-06: reference translation-panel action. The host owns the fields and
// the save; this action supplies its OWN inert demonstration pair values and
// registers the protected commit/discard handlers the host calls on Save. Core
// never manufactures a proposal from the field values, and a marked pair's Save
// routes through this plugin's own protected commit callback.
function ExampleTranslationPanelAction({
  entityType,
  entityId,
  eligiblePairs = [],
  sessionId,
  scopeRevision,
  registerDraftHandlers,
  acceptDraft,
  getCurrentEligiblePairs,
  canWrite,
  disabled,
}) {
  const { t } = usePluginTranslation('example')
  // The local demonstration state: the pairs THIS action delivered. Discard
  // clears only this local bookkeeping (never a Core call); the host clears its
  // own marker on the confirmed `true`.
  const deliveredRef = useRef(new Set())

  useEffect(() => {
    if (typeof registerDraftHandlers !== 'function') return undefined
    const key = (field, language) => `${field}\u0000${language}`
    return registerDraftHandlers({
      // The host hands one opaque reference and the current edited value; the
      // Example-owned route delegates to the public SDK `write_translations`
      // (fill_missing), whose actor and read/update/record checks stay
      // authoritative. A non-2xx (denied/invalid) surfaces as `invalid`.
      commitDraft: async ({ value, field, language }) => {
        if (!deliveredRef.current.has(key(field, language))) {
          return { status: 'invalid' }
        }
        try {
          const response = await api.post(
            'plugins/example/demo-translation/apply',
            {
              translation_type: entityType,
              entity_id: entityId,
              field,
              language_code: language,
              value,
            },
          )
          const status = response?.data?.status
          if (status === 'written' || status === 'skipped') {
            deliveredRef.current.delete(key(field, language))
            return { status }
          }
          return { status: 'invalid' }
        } catch {
          return { status: 'invalid' }
        }
      },
      discardDraft: async ({ field, language }) => {
        deliveredRef.current.delete(key(field, language))
        return true
      },
    })
  }, [registerDraftHandlers, entityType, entityId])

  const blocked = disabled || canWrite === false

  const handleApply = () => {
    if (blocked || typeof acceptDraft !== 'function') return
    const pairs = typeof getCurrentEligiblePairs === 'function'
      ? getCurrentEligiblePairs()
      : eligiblePairs
    const list = Array.isArray(pairs) ? pairs : []
    if (list.length === 0) return
    const proposals = list.map((pair) => {
      deliveredRef.current.add(`${pair.field}\u0000${pair.language}`)
      return {
        field: pair.field,
        language: pair.language,
        // An inert, plugin-owned demonstration value — never a Core-derived or
        // field-value-derived text.
        value: interpolateDefault(
          t('translation.panel.demo_value', 'Example draft for {{field}} ({{language}})'),
          { field: pair.field, language: pair.language },
        ),
        reference: `${pair.field}:${pair.language}`,
      }
    })
    acceptDraft({ sessionId, scopeRevision, proposals })
  }

  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      data-testid="plugin-translation-panel-action-example"
      className="h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold"
      disabled={blocked}
      onClick={handleApply}
    >
      {t('translation.panel.label', 'Fill example translation drafts')}
    </Button>
  )
}

// D-06: reference translation-admin action — a non-writing scope inspection.
function ExampleTranslationAdminAction({ onInspect }) {
  const { t } = usePluginTranslation('example')
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      data-testid="plugin-translation-admin-action-example"
      className="h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold"
      onClick={() => onInspect?.()}
    >
      {t('translation.admin.label', 'Inspect translation scope')}
    </Button>
  )
}

// D-06: reference task-detail action — a read-only local toggle over the current
// host task response; it never runs or retries a task.
function ExampleTaskDetailAction({ onToggle }) {
  const { t } = usePluginTranslation('example')
  return (
    <Button
      type="button"
      variant="outline"
      size="sm"
      data-testid="plugin-task-detail-action-example"
      className="h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold"
      onClick={() => onToggle?.()}
    >
      {t('task.detail.label', 'Show example task context')}
    </Button>
  )
}

function ExampleSettingsSection() {
  return (
    <div className="rounded-lg border p-6 space-y-4" data-testid="plugin-settings-section-example">
      <h2 className="text-lg font-semibold">Example plugin settings</h2>
      <p className="text-sm text-muted-foreground">
        This section is appended by the example plugin's bundle. It receives no
        props — a settings section owns its data, exactly like a page.
      </p>
    </div>
  )
}

// FEEXT-06/S8: the object-detail reference section. Every visible string comes
// from the plugin's own translation files via usePluginTranslation; the object
// name is filled with the host interpolateDefault helper (never a raw HTML
// injection). No own border or card — the host draws the section frame.
function ExampleObjectSection({ object }) {
  const { t } = usePluginTranslation('example')
  const body = t(
    'section.object.body',
    'This section comes from the example plugin. It shows object "{{name}}".'
  )
  return (
    <p className="text-sm text-muted-foreground" data-testid="plugin-example-object-section">
      {interpolateDefault(body, { name: object?.name ?? '' })}
    </p>
  )
}

// S8: schema-settings reference section. Receives the host's schema object.
function ExampleSchemaSection({ schema }) {
  const { t } = usePluginTranslation('example')
  const body = t('section.schema.body', 'Shown for schema "{{name}}".')
  return (
    <p className="text-sm text-muted-foreground" data-testid="plugin-example-schema-section">
      {interpolateDefault(body, { name: schema?.name ?? '' })}
    </p>
  )
}

// S8: object-type-settings reference section. Receives the host's object type.
function ExampleObjectTypeSection({ objectType }) {
  const { t } = usePluginTranslation('example')
  const body = t('section.object_type.body', 'Shown for object type "{{name}}".')
  return (
    <p className="text-sm text-muted-foreground" data-testid="plugin-example-object-type-section">
      {interpolateDefault(body, { name: objectType?.name ?? '' })}
    </p>
  )
}

// S8/FEEXT-05: the reference task form for `example.cleanup-assets`. It is
// controlled: the host owns type/permanent/name/submit, this form owns the
// plugin scope + parameters and reports validity on every change and once on
// mount with the defaults.
function ExampleCleanupForm({ value, onChange, disabled }) {
  const { t } = usePluginTranslation('example')
  const schemaId = value?.scope?.schema_id ?? ''
  const batchSize = value?.plugin_parameters?.batch_size ?? 10
  const fail = value?.plugin_parameters?.fail ?? false
  const { data: schemas } = useSchemas(null, 0, 200, { enabled: true })

  const report = (next) => {
    onChange({
      scope: { schema_id: next.schemaId },
      plugin_parameters: { batch_size: Number(next.batchSize), fail: next.fail },
      valid: Boolean(next.schemaId) && Number(next.batchSize) >= 1,
    })
  }

  useEffect(() => {
    report({ schemaId: '', batchSize: 10, fail: false })
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const options = (schemas ?? []).map((schema) => ({ value: schema.id, label: schema.name }))

  return (
    <div className="space-y-4" data-testid="plugin-example-task-form">
      <FormField id="plugin-example-task-schema" label={t('task.schema', 'Schema')} required>
        <SelectInput
          id="plugin-example-task-schema"
          data-testid="plugin-example-task-schema"
          value={schemaId}
          placeholder={t('task.schema_placeholder', 'Select a schema…')}
          options={options}
          disabled={disabled}
          onChange={(event) => report({ schemaId: event.target.value, batchSize, fail })}
        />
      </FormField>
      <FormField
        id="plugin-example-task-batch"
        label={t('task.batch_size', 'Batch size')}
        help={t('task.batch_size_help', 'Number of assets checked per step.')}
      >
        <NumberInput
          id="plugin-example-task-batch"
          data-testid="plugin-example-task-batch"
          min={1}
          value={batchSize}
          disabled={disabled}
          onChange={(event) => report({ schemaId, batchSize: event.target.value, fail })}
        />
      </FormField>
      <CheckboxField
        id="plugin-example-task-fail"
        data-testid="plugin-example-task-fail"
        label={t('task.fail', 'Simulate a failure')}
        checked={fail}
        disabled={disabled}
        onChange={(event) => report({ schemaId, batchSize, fail: event.target.checked })}
      />
    </div>
  )
}

// S6/S8: the reference source-connector editor. It receives the record's
// `config` as `value`; `onChange` carries only `config` (host fields stay
// top-level and host-owned).
function ExampleConnectorEditor({ value, onChange }) {
  const { t } = usePluginTranslation('example')
  const pageSize = value?.page_size ?? 25
  return (
    <FormField
      id="plugin-example-page-size"
      label={t('connector.page_size', 'Page size')}
      help={t('connector.page_size_help', 'Records fetched per request.')}
    >
      <NumberInput
        id="plugin-example-page-size"
        data-testid="plugin-example-page-size"
        min={1}
        value={pageSize}
        onChange={(event) => onChange({ ...value, page_size: Number(event.target.value) })}
      />
    </FormField>
  )
}

// S8: one batch resolve call per rendered object list (FEEXT-01/T-291-04-02);
// it returns data only, never markup. The `/ping` route is `sync:trigger`-gated
// and this resolver declares the same permission.
async function resolveExampleBadges({ ids }) {
  await api.get('plugins/example/ping', { params: { ids: ids.join(',') } })
  const badges = new Map()
  ids.forEach((id) => {
    badges.set(id, [
      {
        label: 'Example',
        labelKey: 'badge.label',
        title: 'Marked by the example plugin',
        titleKey: 'badge.title',
        icon: 'puzzle',
      },
    ])
  })
  return badges
}

// S8/nav badge (A5): the live count of `example.cleanup-assets` tasks from the
// shared read hook — no extra polling of the plugin's own.
function useExampleTaskCount() {
  return useTasks(0, 1, 'example.cleanup-assets').data?.total
}

export default {
  // FEEXT-04/D9: the plugin ships its own per-language JSON; the host installs
  // it under the plugin-assigned namespace (never core `systemui`).
  translations: { en, de, es, fr, pt },
  pages: [
    {
      id: 'example-dashboard',
      path: 'dashboard',
      component: ExampleDashboardPage,
    },
  ],
  navItems: [
    {
      id: 'example-nav',
      label: 'Example',
      labelKey: 'nav.label',
      path: '/plugins/example/dashboard',
      icon: 'puzzle',
      group: 'Example',
    },
    {
      id: 'example-nav-tools',
      label: 'Example',
      labelKey: 'nav.label',
      path: '/plugins/example/dashboard',
      icon: 'puzzle',
      group: 'Tools',
      permission: 'sync:trigger',
      badge: useExampleTaskCount,
    },
  ],
  attributeEditors: [
    {
      id: 'example-url-editor',
      data_type: 'url',
      component: ExampleUrlEditor,
    },
  ],
  actionButtons: [
    {
      id: 'example-action',
      objectType: '*',
      component: ExampleActionButton,
    },
  ],
  settingsSections: [
    {
      id: 'example-settings',
      label: 'Example',
      component: ExampleSettingsSection,
    },
  ],
  schemaWizardEntries: [
    {
      id: 'example-schema-wizard-entry',
      label: 'Use example schema draft',
      labelKey: 'wizard.entry.label',
      permission: 'example-tools:read',
      component: ExampleSchemaWizardEntry,
    },
  ],
  translationPanelActions: [
    {
      id: 'example-translation-panel-action',
      label: 'Fill example translation drafts',
      labelKey: 'translation.panel.label',
      permission: 'example-tools:read',
      component: ExampleTranslationPanelAction,
    },
  ],
  translationAdminActions: [
    {
      id: 'example-translation-admin-action',
      label: 'Inspect translation scope',
      labelKey: 'translation.admin.label',
      permission: 'example-tools:read',
      component: ExampleTranslationAdminAction,
    },
  ],
  taskDetailActions: [
    {
      id: 'example-task-detail-action',
      label: 'Show example task context',
      labelKey: 'task.detail.label',
      permission: 'example-tools:read',
      component: ExampleTaskDetailAction,
    },
  ],
  objectDetailSections: [
    {
      id: 'example-object-details',
      label: 'Example details',
      labelKey: 'section.object.title',
      component: ExampleObjectSection,
    },
  ],
  schemaSettingsSections: [
    {
      id: 'example-schema-settings',
      label: 'Example schema settings',
      labelKey: 'section.schema.title',
      component: ExampleSchemaSection,
    },
  ],
  objectTypeSettingsSections: [
    {
      id: 'example-object-type-settings',
      label: 'Example object type settings',
      labelKey: 'section.object_type.title',
      component: ExampleObjectTypeSection,
    },
  ],
  itemBadges: [
    {
      id: 'example-object-badge',
      kinds: ['object'],
      permission: 'sync:trigger',
      resolve: resolveExampleBadges,
    },
  ],
  taskForms: [
    {
      id: 'example-cleanup-form',
      tasktype: 'example.cleanup-assets',
      component: ExampleCleanupForm,
    },
  ],
  connectorConfigEditors: [
    {
      id: 'example-connector-editor',
      connector_type: 'example',
      label: 'Example Connector',
      labelKey: 'connector.label',
      roles: ['source'],
      component: ExampleConnectorEditor,
    },
  ],
}