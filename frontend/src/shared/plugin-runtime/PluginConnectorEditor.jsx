import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { PluginSlotBoundary, PluginSlotErrorRow } from './PluginSlotBoundary'

// S6: the Source/Target type <select> option descriptors. Core types always come
// first, then one entry per contributed editor whose `connector_type` is not
// already a core type. A persisted type that is in neither list (for example a
// plugin connector whose plugin is inactive) becomes a raw extra option so
// editing other fields never silently changes the stored type (research Pitfall
// 15). A plugin editor claiming a core type adds no option — the core option
// stays — but is still found by `connector_type` when the host picks the active
// editor, so core Target types can still carry a plugin config editor.
export function connectorTypeOptions(coreTypes, editors, currentType) {
  const options = coreTypes.map((value) => ({ value }))
  const listed = new Set(coreTypes)
  editors.forEach((editor) => {
    if (listed.has(editor.connector_type)) return
    listed.add(editor.connector_type)
    options.push({ value: editor.connector_type, plugin: editor })
  })
  if (currentType && !listed.has(currentType)) {
    options.push({ value: currentType, raw: true })
  }
  return options
}

// S6: one plugin connector config editor for the selected type, in a host-drawn
// group (`Connector settings` + attribution) with a boundary-isolated plugin
// body. A throwing editor shows the error row plus the "settings stay
// unchanged" line; `onError` lets the host mark the editor faulted so it omits
// `config` from the payload instead of overwriting the stored record.
//
// R2-01: the host owns a SEPARATE `pluginDrafts` state per `connector_type`
// (distinct from the Core `configDrafts`) and passes the current `draft`, an
// `onDraftChange(nextDraft)` setter and `draftFields` (the modal's current,
// unsaved field values, e.g. `{ url }`) so a plugin editor can hand its own
// persistence payload back to the host's post-save hook without touching Core
// `config` or account credentials.
export function PluginConnectorEditorGroup({
  editor,
  role,
  value,
  onChange,
  record,
  draft,
  onDraftChange,
  draftFields,
  onError,
}) {
  const { t } = useTranslation()
  const slot = 'connectorConfigEditors:' + editor.connector_type
  const Editor = editor.component

  return (
    <div
      className="space-y-4 border-t pt-4"
      data-testid={'plugin-connector-editor-' + editor.connector_type}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
        <h3 className="text-sm font-semibold">
          {t('plugins.connector_editor.title', 'Connector settings')}
        </h3>
        <p className="text-xs text-muted-foreground">
          {interpolateDefault(
            t('plugins.sections.provided_by', 'Provided by plugin "{{plugin}}"'),
            { plugin: editor.pluginName }
          )}
        </p>
      </div>
      <PluginSlotBoundary
        key={editor.pluginName + ':' + editor.connector_type}
        slot={slot}
        pluginName={editor.pluginName}
        onError={onError}
        fallback={
          <div className="space-y-1">
            <PluginSlotErrorRow slot={slot} />
            <p
              className="text-xs text-muted-foreground"
              data-testid="plugin-connector-editor-unchanged"
            >
              {t('plugins.connector_editor.unchanged', 'Connector settings stay unchanged.')}
            </p>
          </div>
        }
      >
        <Editor
          role={role}
          connectorType={editor.connector_type}
          value={value}
          onChange={onChange}
          record={record}
          draft={draft}
          onDraftChange={onDraftChange}
          draftFields={draftFields}
        />
      </PluginSlotBoundary>
    </div>
  )
}