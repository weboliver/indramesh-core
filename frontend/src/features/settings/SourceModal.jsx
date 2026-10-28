import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../../shared/components/Button'
import { useCreateSource, useUpdateSource } from '../../shared/hooks/use-sources'
import { extractErrorMessage } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import {
  usePluginConnectorEditors,
  usePluginText,
} from '../../shared/plugin-runtime/PluginProvider'
import {
  PluginConnectorEditorGroup,
  connectorTypeOptions,
} from '../../shared/plugin-runtime/PluginConnectorEditor'

// D-05: the neutral core source types. `jira` is a plugin contribution, never a
// host type; an unknown/deactivated persisted type stays raw.
const CORE_SOURCE_TYPES = ['internal', 'api']

export default function SourceModal({ isOpen, onClose, target }) {
  const { t } = useTranslation()
  const isEditing = !!target
  const [name, setName] = useState('')
  const [type, setType] = useState('internal')
  const [sourceUrl, setSourceUrl] = useState('')
  const [configDrafts, setConfigDrafts] = useState({})
  const [pluginDrafts, setPluginDrafts] = useState({})
  const [editorFaulted, setEditorFaulted] = useState(false)
  const [error, setError] = useState('')
  // R2-02: after a Core save succeeds but the plugin-owned post-save bind fails,
  // the modal stays open holding the saved row so a retry PATCHes the same id.
  const [savedRecord, setSavedRecord] = useState(null)
  const [partialSaveError, setPartialSaveError] = useState('')
  const createSource = useCreateSource()
  const updateSource = useUpdateSource()
  const [saving, setSaving] = useState(false)

  // S6: one editor per `connector_type` for the source role; the active one is
  // chosen by the selected type. The plugin's label resolves through its own
  // i18n namespace with the descriptor label as the fallback.
  const editors = usePluginConnectorEditors('source')
  const pluginText = usePluginText()

  useEffect(() => {
    if (isOpen) {
      setName(target?.name || '')
      setType(target?.type || 'internal')
      setSourceUrl(target?.source_url || '')
      setConfigDrafts(target?.type ? { [target.type]: target.config ?? {} } : {})
      setPluginDrafts({})
      setEditorFaulted(false)
      setError('')
      setSavedRecord(null)
      setPartialSaveError('')
    }
  }, [isOpen, target])

  const activeEditor = editors.find((editor) => editor.connector_type === type) ?? null

  const coreLabels = {
    internal: t('settings.sources.type.internal', 'Internal'),
    api: t('settings.sources.type.api', 'API'),
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) {
       setError(t('settings.sources.validation.name_required', 'Source name is required'))
      return
    }
    if (!type) {
       setError(t('settings.sources.validation.type_required', 'Type is required'))
      return
    }

    // R2-02: a retried save after a partial success targets the already-saved
    // row (same id) instead of creating a duplicate Source.
    const currentRecord = savedRecord ?? target ?? null
    const editing = !!currentRecord

    setSaving(true)
    setError('')
    setPartialSaveError('')
    let saved = null
    try {
      // An inactive/unknown persisted connector type stays read-tolerant: keep
      // the stored identity and config by omitting both from the PATCH instead
      // of re-resolving a connector the server cannot see (research Pitfall 15).
      const isRawType = !CORE_SOURCE_TYPES.includes(type) && !activeEditor
      const rawTypeUnchanged = editing && isRawType && type === currentRecord?.type

      const payload = {
        name: name.trim(),
        source_url: sourceUrl.trim() || undefined,
      }
      if (!rawTypeUnchanged) {
        payload.type = type
        if (activeEditor && !editorFaulted) {
          payload.config = configDrafts[type] ?? {}
        }
      }
      if (editing) {
        saved = await updateSource.mutateAsync({ id: currentRecord.id, ...payload })
      } else {
        saved = await createSource.mutateAsync(payload)
      }
      setSavedRecord(saved ?? currentRecord)

      // R2-01: a generic, opt-in post-Core-save hook. The host awaits it and, on
      // rejection, keeps the modal open with the saved id recoverable so the
      // two-store save never claims a false full success (R2-02).
      if (activeEditor?.afterCoreSave && !editorFaulted) {
        await activeEditor.afterCoreSave({
          role: 'source',
          record: saved ?? currentRecord,
          previousRecord: target ?? null,
          draft: pluginDrafts[type] ?? {},
        })
      }
      onClose()
    } catch (err) {
      if (saved) {
        setPartialSaveError(
          interpolateDefault(
            t(
              'plugins.connector_editor.partial_save',
              'Saved, but the connector settings could not be saved: {{detail}}'
            ),
            { detail: extractErrorMessage(err, '') }
          )
        )
      } else {
        setError(extractErrorMessage(err, t('settings.sources.save_failed', 'Failed to save source. Check your connection and try again.')))
      }
    } finally {
      setSaving(false)
    }
  }

  if (!isOpen) return null

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center"
      onClick={onClose}
      data-testid="source-modal"
    >
      <div
        className="rounded-lg border bg-card p-6 w-full max-w-xl space-y-4 shadow-lg max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
         <h2 className="text-lg font-semibold">{isEditing ? t('settings.sources.edit_title', 'Edit Source') : t('settings.sources.create_title', 'Create Source')}</h2>

        <form onSubmit={handleSubmit} className="space-y-4">
          {error && (
            <div className="rounded-md bg-destructive/10 border border-destructive/30 px-3 py-2 text-sm text-destructive">
              {error}
            </div>
          )}

          {partialSaveError && (
            <div
              role="alert"
              className="rounded-md bg-destructive/10 border border-destructive/30 px-3 py-2 text-sm text-destructive"
              data-testid="source-modal-partial-save"
            >
              {partialSaveError}
            </div>
          )}

          <div className="space-y-1.5">
             <label className="text-sm font-medium">{t('settings.sources.name', 'Source Name')}</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
               placeholder={t('settings.sources.name_placeholder', 'My Source')}
              data-testid="source-modal-name"
            />
          </div>

          <div className="space-y-1.5">
             <label className="text-sm font-medium">{t('common.type', 'Type')}</label>
            <select
              value={type}
              onChange={(e) => {
                setType(e.target.value)
                setEditorFaulted(false)
              }}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              data-testid="source-modal-type"
            >
              {connectorTypeOptions(CORE_SOURCE_TYPES, editors, type).map((option) => (
                <option key={option.value} value={option.value}>
                  {option.plugin
                    ? interpolateDefault(
                        t('settings.connector.type_option_plugin', '{{label}} (Plugin: {{plugin}})'),
                        {
                          label: pluginText(
                            option.plugin.pluginName,
                            option.plugin.labelKey,
                            option.plugin.label
                          ),
                          plugin: option.plugin.pluginName,
                        }
                      )
                    : option.raw
                      ? option.value
                      : coreLabels[option.value]}
                </option>
              ))}
            </select>
          </div>

          <div className="space-y-1.5">
             <label className="text-sm font-medium">{t('common.url', 'URL')}</label>
            <input
              type="text"
              value={sourceUrl}
              onChange={(e) => setSourceUrl(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
               placeholder={t('settings.sources.url_placeholder', 'https://example.com (optional)')}
              data-testid="source-modal-url"
            />
          </div>

          {activeEditor && (
            <PluginConnectorEditorGroup
              editor={activeEditor}
              role="source"
              value={configDrafts[type] ?? {}}
              onChange={(config) =>
                setConfigDrafts((prev) => ({ ...prev, [type]: config }))
              }
              record={savedRecord ?? target ?? null}
              draft={pluginDrafts[type] ?? {}}
              onDraftChange={(next) =>
                setPluginDrafts((prev) => ({ ...prev, [type]: next }))
              }
              draftFields={{ url: sourceUrl }}
              onError={() => setEditorFaulted(true)}
            />
          )}

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose} data-testid="source-modal-cancel">
               {t('common.cancel', 'Cancel')}
            </Button>
            <Button type="submit" variant="primary" size="sm" loading={saving} data-testid="source-modal-submit">
               {isEditing ? t('common.save_changes', 'Save Changes') : t('settings.sources.create_title', 'Create Source')}
            </Button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  )
}