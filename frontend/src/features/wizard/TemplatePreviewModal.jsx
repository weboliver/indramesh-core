import { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { Button } from '../../shared/components/Button'
import SchemaTreeReview from './SchemaTreeReview'
import SourceSelect from './SourceSelect'
import { useSchemaTemplates, useInstantiateTemplate } from '../../shared/hooks/use-schema-templates'
import { useSources } from '../../shared/hooks/use-sources'
import { useActiveSchema } from '../../shared/components/ActiveSchemaProvider'
import { showToast } from '../../shared/components/Toast'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

export default function TemplatePreviewModal({ templateId, onClose }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { data: templates } = useSchemaTemplates()
  const instantiateTemplate = useInstantiateTemplate()
  const { activeSourceId } = useActiveSchema()
  const { data: sources = [] } = useSources(0, 1000)

  const template = templates?.find(t => t.id === templateId)
  const td = template?.template_data

  const [selectedSourceId, setSelectedSourceId] = useState(null)
  const [schemaName, setSchemaName] = useState('')
  const [error, setError] = useState(null)
  const [toggledIds, setToggledIds] = useState(new Set())
  const [expandedIds, setExpandedIds] = useState(new Set())

  // Keyed to template identity, not the react-query cache array reference:
  // refetches of ['schema-templates'] (window focus after 30s staleness,
  // invalidateQueries) yield a new td.object_types reference that would
  // re-run this effect and clobber the user's toggle/expand selections.
  const initializedRef = useRef(null)

  useEffect(() => {
    if (!td || initializedRef.current === templateId) return
    initializedRef.current = templateId
    if (td.schema_name) setSchemaName(td.schema_name)
    if (td.object_types) {
      setToggledIds(new Set(td.object_types.map(ot => ot.name)))
      setExpandedIds(new Set(td.object_types.slice(0, 2).map(ot => ot.name)))
    }
  }, [td, templateId])

  // Default the Source picker to the app-global active Source (D-01/D-02),
  // but only after it is validated against the loaded source list (D-03) and
  // only when the picker is still empty — an absent/stale active ID leaves the
  // picker empty, and a user-edited choice is never overwritten. Keyed to
  // template identity like the initialization guard above: sources load
  // asynchronously, so the effect re-fires until it can resolve a default.
  const sourceInitializedRef = useRef(null)

  useEffect(() => {
    if (sourceInitializedRef.current === templateId) return
    if (!activeSourceId || !sources.some(s => s.id === activeSourceId)) return
    sourceInitializedRef.current = templateId
    if (!selectedSourceId) setSelectedSourceId(activeSourceId)
  }, [sources, activeSourceId, selectedSourceId, templateId])

  if (!template || !td) return null

  const canCreate = selectedSourceId && schemaName.trim() && !instantiateTemplate.isPending

  const handleCreate = async () => {
    if (!canCreate) return
    setError(null)
    try {
      const result = await instantiateTemplate.mutateAsync({
        templateId: template.id,
        sourceId: selectedSourceId,
        name: schemaName.trim(),
      })
       showToast(interpolateDefault(t('toasts.wizard.schema_created_from_template', 'Schema "{{name}}" created from template.'), { name: result.name }), { type: 'success' })
      onClose()
      navigate(`/configuration/schemas/${result.id}`)
    } catch (err) {
       const detail = err?.response?.data?.detail || err.message || t('common.unknown_error', 'Unknown error')
      setError(typeof detail === 'string' ? detail : JSON.stringify(detail))
    }
  }

  return createPortal(
    <div className="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center"
         onClick={onClose} data-testid="template-preview-modal">
      <div className="rounded-lg border bg-card p-6 w-full max-w-2xl space-y-5 shadow-lg max-h-[90vh] overflow-y-auto"
           onClick={e => e.stopPropagation()}>
        {/* Header */}
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-lg font-semibold">{template.name}</h2>
            {template.description && (
              <p className="text-sm text-muted-foreground mt-0.5">{template.description}</p>
            )}
          </div>
          <Button type="button" size="icon" variant="ghost" onClick={onClose}
                   className="h-7 w-7" aria-label={t('common.close', 'Close')}>×</Button>
        </div>

        {/* Schema Name */}
        <div className="space-y-1.5">
          <label htmlFor="instantiate-schema-name" className="text-sm font-medium text-foreground">
             {t('wizard.schema.name_label', 'Schema Name')} <span className="text-destructive">*</span>
          </label>
          <input
            id="instantiate-schema-name"
            type="text"
            value={schemaName}
            onChange={e => setSchemaName(e.target.value)}
            className="w-full rounded-md border px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
            data-testid="instantiate-schema-name"
          />
        </div>

        {/* Source Select */}
        <SourceSelect
          value={selectedSourceId}
          onChange={setSelectedSourceId}
          disabled={instantiateTemplate.isPending}
        />

        {/* Template Tree */}
        <div className="rounded-lg border bg-muted/20" data-testid="template-tree-preview">
          <SchemaTreeReview
            schemaData={td}
            toggledIds={toggledIds}
            onToggle={(name) => setToggledIds(prev => {
              const next = new Set(prev)
              next.has(name) ? next.delete(name) : next.add(name)
              return next
            })}
            expandedIds={expandedIds}
            onExpand={(name) => setExpandedIds(prev => {
              const next = new Set(prev)
              next.has(name) ? next.delete(name) : next.add(name)
              return next
            })}
          />
        </div>

        {/* Error */}
        {error && (
          <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3" data-testid="instantiate-error">
             <p className="text-sm text-destructive mb-2">{interpolateDefault(t('wizard.schema.instantiation_failed', 'Schema instantiation failed. {{detail}}.'), { detail: error })}</p>
            <Button type="button" variant="primary" size="sm" onClick={handleCreate} disabled={!canCreate}>
               {t('common.try_again', 'Try again')}
            </Button>
          </div>
        )}

        {/* Action Bar */}
        <div className="flex justify-end gap-3 pt-2 border-t">
           <Button type="button" variant="outline" size="sm" onClick={onClose}>{t('common.cancel', 'Cancel')}</Button>
          <Button type="button" variant="primary" size="sm"
                  onClick={handleCreate}
                  disabled={!canCreate}
                  loading={instantiateTemplate.isPending}
                  leftIcon={instantiateTemplate.isPending ? undefined : 'check'}
                  data-testid="instantiate-create-button">
             {instantiateTemplate.isPending ? t('common.creating', 'Creating...') : t('wizard.schema.create', 'Create Schema')}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}
