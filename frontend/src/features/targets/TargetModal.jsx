import { useState, useEffect } from 'react'
import { createPortal } from 'react-dom'
import { useTranslation } from 'react-i18next'
import { Button } from '../../shared/components/Button'
import { useCreateTarget, useUpdateTarget } from '../../shared/hooks/use-targets'
import { useSchemas } from '../../shared/hooks/use-schemas'
import { useSources } from '../../shared/hooks/use-sources'
import { extractErrorMessage } from '../../shared/lib/utils'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import {
  usePluginConnectorEditors,
  usePluginText,
} from '../../shared/plugin-runtime/PluginProvider'
import {
  PluginConnectorEditorGroup,
  connectorTypeOptions,
} from '../../shared/plugin-runtime/PluginConnectorEditor'

const EXPORT_ROOT_PREFIX = '/tmp/exports/'
// D-05: CSV is the only built-in target connector. The Jira target editor is a
// plugin contribution, never a host type.
const CORE_TARGET_TYPES = ['csv']

export default function TargetModal({ isOpen, onClose, target }) {
  const { t } = useTranslation()
  const isEditing = !!target
  const [name, setName] = useState('')
  const [targetUrl, setTargetUrl] = useState('')
  const [connectorType, setConnectorType] = useState('csv')
  const [schemaId, setSchemaId] = useState('')
  const [folderName, setFolderName] = useState('')
  const [delimiter, setDelimiter] = useState(',')
  const [encoding, setEncoding] = useState('utf-8')
  const [configDrafts, setConfigDrafts] = useState({})
  const [pluginDrafts, setPluginDrafts] = useState({})
  const [editorFaulted, setEditorFaulted] = useState(false)
  const [error, setError] = useState('')
  // R2-02: after a Core save succeeds but the plugin-owned post-save bind fails,
  // the modal stays open holding the saved row so a retry PATCHes the same id.
  const [savedRecord, setSavedRecord] = useState(null)
  const [partialSaveError, setPartialSaveError] = useState('')
  const createTarget = useCreateTarget()
  const updateTarget = useUpdateTarget()
  const [saving, setSaving] = useState(false)

  // Jira targets require schema_id server-side (TargetCreate.validate_config_by_connector) —
  // fetch unfiltered so the picker works regardless of which source a schema belongs to.
  const { data: schemasData } = useSchemas(null, 0, 100, { enabled: isOpen })
  const schemas = schemasData?.items || schemasData || []
  const { data: sources = [] } = useSources(0, 100, { enabled: isOpen })
  const getSourceName = (sourceId) => sources.find(s => s.id === sourceId)?.name || sourceId

  // S6: one editor per `connector_type` for the target role; the active one is
  // chosen by the selected type, including a plugin editor claiming a core
  // target type (the core option stays and its host fields stay required).
  const editors = usePluginConnectorEditors('target')
  const pluginText = usePluginText()

  const activeEditor = editors.find((editor) => editor.connector_type === connectorType) ?? null
  const isCsv = connectorType === 'csv'
  // A non-core type: either a contributed plugin type or an unknown persisted
  // type whose plugin is inactive (raw option).
  const isContributed = !isCsv
  const isRawType = isContributed && !activeEditor
  // Host-owned Target URL / Schema, never part of `config` (user decision:
  // Host-Felder behalten). They render for every non-CSV type; a contributed
  // type requires each only when its active descriptor declares the matching
  // boolean (D-05). No connector name is special-cased.
  const showUrlSchema = isContributed
  const requiresUrl = activeEditor?.targetFields?.requiresUrl === true
  const requiresSchema = activeEditor?.targetFields?.requiresSchemaId === true

  useEffect(() => {
    if (isOpen) {
      setName(target?.name || '')
      setTargetUrl(target?.target_url || '')
      setSchemaId(target?.schema_id || '')
      setConnectorType(target?.connector_type || 'csv')
      setConfigDrafts(target?.connector_type ? { [target.connector_type]: target.config ?? {} } : {})
      setPluginDrafts({})
      setEditorFaulted(false)
      setError('')
      setSavedRecord(null)
      setPartialSaveError('')
      if (target?.connector_type === 'csv') {
        const rawPath = target?.export_folder || ''
        setFolderName(rawPath.startsWith(EXPORT_ROOT_PREFIX) ? rawPath.slice(EXPORT_ROOT_PREFIX.length) : rawPath)
        setDelimiter(target?.config?.delimiter || ',')
        setEncoding(target?.config?.encoding || 'utf-8')
      } else {
        setFolderName('')
        setDelimiter(',')
        setEncoding('utf-8')
      }
    }
  }, [isOpen, target])

  const coreLabels = {
    csv: t('settings.targets.connector.csv', 'CSV'),
  }

  const buildConfig = () => {
    if (isCsv) {
      return { delimiter, encoding }
    }
    return {}
  }

  const handleSubmit = async (e) => {
    e.preventDefault()
    if (!name.trim()) {
       setError(t('settings.targets.validation.name', 'Target name is required'))
      return
    }
    if (requiresUrl && !targetUrl.trim()) {
       setError(t('settings.targets.validation.url', 'Target URL is required for this connector'))
      return
    }
    if (requiresSchema && !schemaId) {
       setError(t('settings.targets.validation.schema', 'Schema is required for this connector'))
      return
    }
    if (isCsv && !folderName.trim()) {
       setError(t('settings.targets.validation.folder', 'Folder name is required for CSV targets'))
      return
    }

    // R2-02: a retried save after a partial success targets the already-saved
    // row (same id) instead of creating a duplicate Target.
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
      const rawTypeUnchanged = editing && isRawType && connectorType === currentRecord?.connector_type

      const payload = { name: name.trim() }
      if (!rawTypeUnchanged) {
        payload.connector_type = connectorType
      }
      if (activeEditor) {
        // A faulted editor omits config so the stored record is never
        // overwritten; on create the server applies its connector defaults.
        if (!editorFaulted) {
          payload.config = configDrafts[connectorType] ?? {}
        }
      } else if (!isRawType) {
        payload.config = buildConfig()
      }
      // Host fields are submitted as top-level TargetCreate/TargetUpdate
      // columns, never nested in config; omitting an empty optional value
      // preserves the stored one on edit.
      if (showUrlSchema) {
        const trimmedUrl = targetUrl.trim()
        if (trimmedUrl) payload.target_url = trimmedUrl
        if (schemaId) payload.schema_id = schemaId
      }
      if (isCsv && folderName.trim()) {
        payload.export_folder = `${EXPORT_ROOT_PREFIX}${folderName.trim()}`
      }

      if (editing) {
        saved = await updateTarget.mutateAsync({ id: currentRecord.id, ...payload })
      } else {
        saved = await createTarget.mutateAsync(payload)
      }
      setSavedRecord(saved ?? currentRecord)

      // R2-01: a generic, opt-in post-Core-save hook. The host awaits it and, on
      // rejection, keeps the modal open with the saved id recoverable so the
      // two-store save never claims a false full success (R2-02).
      if (activeEditor?.afterCoreSave && !editorFaulted) {
        await activeEditor.afterCoreSave({
          role: 'target',
          record: saved ?? currentRecord,
          previousRecord: target ?? null,
          draft: pluginDrafts[connectorType] ?? {},
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
        setError(extractErrorMessage(err, t('settings.targets.error.save', 'Failed to save target. Check your connection and try again.')))
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
      data-testid="target-modal"
    >
      <div
        className="rounded-lg border bg-card p-6 w-full max-w-xl space-y-4 shadow-lg max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <h2 className="text-lg font-semibold">{isEditing ? t('settings.targets.edit', 'Edit Target') : t('settings.targets.create', 'Create Target')}</h2>

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
              data-testid="target-modal-partial-save"
            >
              {partialSaveError}
            </div>
          )}

          <div className="space-y-1.5">
            <label className="text-sm font-medium">{t('settings.targets.target_name', 'Target Name')}</label>
            <input
              type="text"
              value={name}
              onChange={(e) => setName(e.target.value)}
              className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
              placeholder={t('settings.targets.name_placeholder', 'My Target')}
              data-testid="target-modal-name"
            />
          </div>

          <div className="grid grid-cols-2 gap-4">
            {showUrlSchema && (
              <div className="space-y-1.5">
                <label className="text-sm font-medium">{t('settings.targets.target_url', 'Target URL')}</label>
                <input
                  type="text"
                  value={targetUrl}
                  onChange={(e) => setTargetUrl(e.target.value)}
                  className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                  placeholder={t('settings.targets.url_placeholder', 'https://example.com')}
                  data-testid="target-modal-url"
                />
              </div>
            )}

            <div className="space-y-1.5">
              <label className="text-sm font-medium">{t('settings.targets.connector_type', 'Connector Type')}</label>
              <select
                value={connectorType}
                onChange={(e) => {
                  setConnectorType(e.target.value)
                  setEditorFaulted(false)
                }}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                data-testid="target-modal-type"
              >
                {connectorTypeOptions(CORE_TARGET_TYPES, editors, connectorType).map((option) => (
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
          </div>

          {showUrlSchema && (
            <div className="space-y-1.5">
              <label className="text-sm font-medium">{t('tasks.form.schema', 'Schema')}</label>
              <select
                value={schemaId}
                onChange={(e) => setSchemaId(e.target.value)}
                className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                data-testid="target-modal-schema"
              >
                <option value="">{t('tasks.form.select_schema', 'Select a schema...')}</option>
                {schemas.map((schema) => (
                  <option key={schema.id} value={schema.id}>
                    {schema.name} ({getSourceName(schema.source_id)})
                  </option>
                ))}
              </select>
            </div>
          )}

          {isCsv && (
            <div className="grid grid-cols-[2fr_1fr_1fr] gap-4">
              <div className="space-y-1.5">
                <label className="text-sm font-medium">{t('settings.targets.file_path', 'File Path')}</label>
                <div className="flex items-center rounded-md border bg-background text-sm focus-within:ring-2 focus-within:ring-primary">
                  <span className="pl-3 py-2 text-muted-foreground select-none">{EXPORT_ROOT_PREFIX}</span>
                  <input
                    type="text"
                    value={folderName}
                    onChange={(e) => setFolderName(e.target.value)}
                    className="w-full bg-transparent py-2 pr-3 focus:outline-none"
                  placeholder={t('settings.targets.file_path_placeholder', 'my-export')}
                    data-testid="target-modal-filepath"
                  />
                </div>
              </div>
              {!activeEditor && (
                <>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">{t('settings.targets.delimiter', 'Delimiter')}</label>
                    <input
                      type="text"
                      value={delimiter}
                      onChange={(e) => setDelimiter(e.target.value)}
                      className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                      maxLength={1}
                      data-testid="target-modal-delimiter"
                    />
                  </div>
                  <div className="space-y-1.5">
                    <label className="text-sm font-medium">{t('settings.targets.encoding', 'Encoding')}</label>
                    <select
                      value={encoding}
                      onChange={(e) => setEncoding(e.target.value)}
                      className="w-full rounded-md border bg-background px-3 py-2 text-sm focus:outline-none focus:ring-2 focus:ring-primary"
                      data-testid="target-modal-encoding"
                    >
                      <option value="utf-8">{t('settings.targets.encoding.utf8', 'UTF-8')}</option>
                      <option value="utf-16">{t('settings.targets.encoding.utf16', 'UTF-16')}</option>
                      <option value="latin-1">{t('settings.targets.encoding.latin1', 'Latin-1')}</option>
                    </select>
                  </div>
                </>
              )}
            </div>
          )}

          {activeEditor && (
            <PluginConnectorEditorGroup
              editor={activeEditor}
              role="target"
              value={configDrafts[connectorType] ?? {}}
              onChange={(config) =>
                setConfigDrafts((prev) => ({ ...prev, [connectorType]: config }))
              }
              record={savedRecord ?? target ?? null}
              draft={pluginDrafts[connectorType] ?? {}}
              onDraftChange={(next) =>
                setPluginDrafts((prev) => ({ ...prev, [connectorType]: next }))
              }
              draftFields={{ url: targetUrl }}
              onError={() => setEditorFaulted(true)}
            />
          )}

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" variant="outline" size="sm" onClick={onClose} data-testid="target-modal-cancel">
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button type="submit" variant="primary" size="sm" loading={saving} data-testid="target-modal-submit">
              {isEditing ? t('settings.targets.save_changes', 'Save Changes') : t('settings.targets.create', 'Create Target')}
            </Button>
          </div>
        </form>
      </div>
    </div>,
    document.body
  )
}