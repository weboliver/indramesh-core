import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useTarget, useUpdateTarget, useDeleteTarget } from '../../shared/hooks/use-targets'
import TableSkeleton from '../../shared/components/TableSkeleton'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { Button } from '../../shared/components/Button'
import PermissionsDialog from '../permissions/PermissionsDialog'
import { Can } from '../../shared/components/Can'
import { showToast } from '../../shared/components/Toast'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import {
  usePluginConnectorEditors,
  usePluginText,
} from '../../shared/plugin-runtime/PluginProvider'
import { connectorTypeOptions } from '../../shared/plugin-runtime/PluginConnectorEditor'

const CORE_TARGET_TYPES = ['csv']

function formatConfigValue(value) {
  if (value && typeof value === 'object') return JSON.stringify(value)
  return String(value)
}

export default function TargetDetailPage() {
  const { t } = useTranslation()
  const { targetId } = useParams()
  const { data: target, isLoading } = useTarget(targetId)
  const updateTarget = useUpdateTarget()
  const deleteTarget = useDeleteTarget()
  const navigate = useNavigate()

  const [editing, setEditing] = useState(false)
  const [name, setName] = useState('')
  const [url, setUrl] = useState('')
  const [connectorType, setConnectorType] = useState('csv')
  const [filePath, setFilePath] = useState('')
  const [delimiter, setDelimiter] = useState(',')
  const [encoding, setEncoding] = useState('utf-8')
  const [permDialogOpen, setPermDialogOpen] = useState(false)

  // D-05: the connector list is CSV plus the active plugin descriptor options;
  // a persisted unknown/deactivated type stays selectable as its raw value.
  const editors = usePluginConnectorEditors('target')
  const pluginText = usePluginText()
  const coreLabels = {
    csv: t('settings.targets.connector.csv', 'CSV'),
  }

  if (isLoading) return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <span className="h-4 w-20 rounded bg-muted animate-skeleton" />
        <span>/</span>
        <span className="h-4 w-32 rounded bg-muted animate-skeleton" />
      </div>
      <TableSkeleton rows={4} cols={1} />
    </div>
  )
  if (!target) return <div className="text-destructive p-6">{t('settings.targets.error.not_found', 'Target not found')}</div>

  const seedEditForm = () => {
    setName(target.name)
    setUrl(target.target_url || '')
    setConnectorType(target.connector_type || 'csv')
    if (target.connector_type === 'csv') {
      setFilePath(target.config?.file_path || '')
      setDelimiter(target.config?.delimiter || ',')
      setEncoding(target.config?.encoding || 'utf-8')
    }
    setEditing(true)
  }

  const handleUpdate = async (e) => {
    e.preventDefault()
    // A plugin-owned (or inactive) connector keeps its stored type and config:
    // only send them when the type changed or the CSV fields are edited here.
    const typeChanged = connectorType !== target.connector_type
    const payload = { id: targetId, name, target_url: url || null }
    if (typeChanged) payload.connector_type = connectorType
    if (connectorType === 'csv') {
      payload.config = { file_path: filePath, delimiter, encoding }
    } else if (typeChanged) {
      payload.config = {}
    }
    await updateTarget.mutateAsync(payload)
    setEditing(false)
  }

  const handleDelete = async () => {
    if (await confirmDialog({ message: t('settings.targets.confirm_delete_short', 'Delete target?'), danger: true })) {
      deleteTarget.mutate(target.id, {
        onSuccess: () => {
            showToast(interpolateDefault(t('toasts.targets.deleted', 'Target "{{name}}" deleted.'), { name: target.name }), { type: 'success' })
          navigate('/configuration/targets')
        },
        onError: () => {
           showToast(t('toasts.targets.delete_failed', 'Failed to delete target.'), { type: 'error' })
        },
      })
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6">
        {/* D-03 (UX-07): generic Breadcrumb — deterministic Configuration →
            Targets overview chain with the loaded target name as current page
            (no raw-ID fallback) */}
        <Breadcrumb
          items={[
            { label: t('nav.configuration', 'Configuration'), href: '/configuration' },
            { label: t('settings.targets.title', 'Targets'), href: '/configuration/targets' },
            { label: target.name },
          ]}
        />

        {/* Header block */}
        <div className="rounded-lg border p-6 space-y-4">
          <div className="flex items-center justify-between">
            <div>
              <h1 className="text-2xl font-bold tracking-tight">{target.name}</h1>
              {!editing && (
                <div className="flex items-center gap-2 mt-1">
                  <span className="rounded-full bg-muted px-2.5 py-0.5 text-xs font-medium text-muted-foreground">{target.connector_type}</span>
                  <span className="text-sm text-muted-foreground">{target.target_url || ''}</span>
                </div>
              )}
            </div>
            <div className="flex gap-2">
              <Can resource="roles" action="manage">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => setPermDialogOpen(true)}
                >
                   {t('common.permissions', 'Permissions')}
                </Button>
              </Can>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={seedEditForm}
              >
                 {t('common.edit', 'Edit')}
              </Button>
              <Button
                type="button"
                variant="destructive"
                size="sm"
                onClick={handleDelete}
              >
                 {t('common.delete', 'Delete')}
              </Button>
            </div>
          </div>

          {editing && (
            <form onSubmit={handleUpdate} className="space-y-4 border-t pt-4">
              <div className="grid grid-cols-1 sm:grid-cols-2 gap-3">
                <div className="space-y-1.5">
                   <label htmlFor="target-name" className="text-sm font-medium">{t('common.name', 'Name')}</label>
                  <input
                    id="target-name"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                  />
                </div>
                <div className="space-y-1.5">
                   <label htmlFor="target-url" className="text-sm font-medium">{t('settings.targets.url', 'URL')}</label>
                  <input
                    id="target-url"
                    value={url}
                    onChange={(e) => setUrl(e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                  />
                </div>
                <div className="space-y-1.5">
                   <label htmlFor="target-connector-type" className="text-sm font-medium">{t('settings.targets.connector_type', 'Connector Type')}</label>
                  <select
                    id="target-connector-type"
                    value={connectorType}
                    onChange={(e) => setConnectorType(e.target.value)}
                    className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
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

              {connectorType === 'csv' && (
                <div className="grid grid-cols-1 sm:grid-cols-3 gap-3">
                  <div className="space-y-1.5">
                     <label htmlFor="target-file-path" className="text-sm font-medium">{t('settings.targets.file_path', 'File Path')}</label>
                    <input
                      id="target-file-path"
                      value={filePath}
                      onChange={(e) => setFilePath(e.target.value)}
                      className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                    />
                  </div>
                  <div className="space-y-1.5">
                     <label htmlFor="target-delimiter" className="text-sm font-medium">{t('settings.targets.delimiter', 'Delimiter')}</label>
                    <input
                      id="target-delimiter"
                      value={delimiter}
                      onChange={(e) => setDelimiter(e.target.value)}
                      maxLength={1}
                      className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                    />
                  </div>
                  <div className="space-y-1.5">
                     <label htmlFor="target-encoding" className="text-sm font-medium">{t('settings.targets.encoding', 'Encoding')}</label>
                    <select
                      id="target-encoding"
                      value={encoding}
                      onChange={(e) => setEncoding(e.target.value)}
                      className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm text-foreground"
                    >
                       <option value="utf-8">{t('settings.targets.encoding.utf8', 'UTF-8')}</option>
                       <option value="utf-16">{t('settings.targets.encoding.utf16', 'UTF-16')}</option>
                       <option value="latin-1">{t('settings.targets.encoding.latin1', 'Latin-1')}</option>
                    </select>
                  </div>
                </div>
              )}

              <div className="flex gap-2">
                 <Button type="submit" variant="primary" size="sm">{t('common.save', 'Save')}</Button>
                 <Button type="button" variant="outline" size="sm" onClick={() => setEditing(false)}>{t('common.cancel', 'Cancel')}</Button>
              </div>
            </form>
          )}

          {!editing && (
            <div className="grid grid-cols-2 gap-4 text-sm">
               <div><span className="text-muted-foreground">{t('settings.targets.connector_type_label', 'Connector Type:')}</span> {target.connector_type}</div>
                <div><span className="text-muted-foreground">{t('settings.targets.url_label', 'URL:')}</span> {target.target_url || '-'}</div>
            </div>
          )}

          {!editing && target.config && Object.keys(target.config).length > 0 && (
            <div className="text-sm">
               <span className="text-muted-foreground">{t('settings.targets.config', 'Config:')}</span>
              <div className="mt-1 space-y-0.5 pl-1">
                {Object.entries(target.config).map(([k, v]) => (
                  <div key={k} className="flex gap-2">
                    <span className="text-muted-foreground">{k}:</span>
                    <span className="break-all">{formatConfigValue(v)}</span>
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>


        <PermissionsDialog entityType="target" recordId={targetId} open={permDialogOpen} onClose={() => setPermDialogOpen(false)} />
      </div>
    </ErrorBoundary>
  )
}
