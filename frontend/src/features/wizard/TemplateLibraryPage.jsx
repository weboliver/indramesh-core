import { useState } from 'react'
import { Link } from 'react-router-dom'
import Layers from 'lucide-react/dist/esm/icons/layers'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import PageHeader from '../../shared/components/PageHeader'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import TableSkeleton from '../../shared/components/TableSkeleton'
import SchemaTreeReview from './SchemaTreeReview'
import TemplatePreviewModal from './TemplatePreviewModal'
import { useSchemaTemplates, useDeleteSchemaTemplate } from '../../shared/hooks/use-schema-templates'
import { unescapeHtmlEntities } from '../../shared/utils/html-entities'
import api from '../../shared/hooks/use-api'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

function TemplateCard({ template, onUse, onDelete, isDeleting, isExpanded, onToggleExpand }) {
  const { t } = useTranslation()
  const td = template.template_data || {}
  const firstIconKey = td.object_types?.[0]?.icon_key || 'layers'
  const objTypeCount = td.object_types?.length || 0
  const attrCount = td.object_types?.reduce(
    (sum, ot) => sum + (ot.attributes?.length || 0),
    0
  )
  const usageCount = template.usage_count || 0

  return (
    <div
      className="rounded-lg border bg-card flex flex-col gap-0 overflow-hidden"
      data-testid="template-card"
    >
      {/* Header */}
      <div className="flex items-start gap-3 p-4 pb-3">
        <div className="flex-shrink-0 rounded-md bg-primary/10 p-2 mt-0.5">
          <LucideIcon name={firstIconKey} size={18} className="text-primary" />
        </div>
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <h3 className="text-sm font-semibold text-foreground truncate">
              {unescapeHtmlEntities(template.name)}
            </h3>
            {template.category && (
              <span className="inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium text-muted-foreground flex-shrink-0">
                {unescapeHtmlEntities(template.category)}
              </span>
            )}
          </div>
          {template.description && (
            <p className="mt-1 text-xs text-muted-foreground line-clamp-2">
              {unescapeHtmlEntities(template.description)}
            </p>
          )}
        </div>
      </div>

      {/* Stats */}
      <div className="flex items-center gap-4 px-4 pb-3 text-xs text-muted-foreground">
        <span className="flex items-center gap-1">
          <LucideIcon name="box" size={12} />
          {interpolateDefault(
            objTypeCount === 1
              ? t('wizard.template.object_types_one', '{{count}} type')
              : t('wizard.template.object_types_other', '{{count}} types'),
            { count: objTypeCount }
          )}
        </span>
        <span className="flex items-center gap-1">
          <LucideIcon name="tag" size={12} />
          {interpolateDefault(
            attrCount === 1
              ? t('wizard.template.attributes_one', '{{count}} attribute')
              : t('wizard.template.attributes_other', '{{count}} attributes'),
            { count: attrCount }
          )}
        </span>
        {usageCount > 0 && (
          <span className="flex items-center gap-1">
            <LucideIcon name="activity" size={12} />
            {interpolateDefault(
              usageCount === 1
                ? t('wizard.template.uses_one', '{{count}} use')
                : t('wizard.template.uses_other', '{{count}} uses'),
              { count: usageCount }
            )}
          </span>
        )}
      </div>

      {/* Expanded tree preview */}
      {isExpanded && objTypeCount > 0 && (
        <div className="px-4 pb-3">
          <SchemaTreeReview
            schemaData={td}
            toggledIds={new Set(td.object_types?.map(ot => ot.name) || [])}
            onToggle={() => {}}
            expandedIds={new Set(td.object_types?.slice(0, 2).map(ot => ot.name) || [])}
            onExpand={() => {}}
          />
        </div>
      )}

      {/* Actions */}
      <div className="flex items-center gap-2 px-4 py-3 border-t mt-auto">
        <Button
          variant="outline"
          size="sm"
          leftIcon={isExpanded ? 'eye-off' : 'eye'}
          onClick={onToggleExpand}
          data-testid="template-preview-btn"
        >
           {isExpanded ? t('wizard.template.hide', 'Hide') : t('wizard.template.preview', 'Preview')}
        </Button>
        <Button
          variant="primary"
          size="sm"
          leftIcon="plus"
          onClick={() => onUse(template.id)}
          data-testid="template-use-btn"
        >
           {t('wizard.template.use', 'Use Template')}
        </Button>
        <Button
          variant="destructive"
          size="icon"
          leftIcon="trash-2"
          onClick={async () => {
             if (await confirmDialog({ message: interpolateDefault(t('wizard.template.delete_confirm', 'Delete template "{{name}}"? This cannot be undone.'), { name: unescapeHtmlEntities(template.name) }), danger: true })) {
              onDelete(template.id)
            }
          }}
          disabled={isDeleting}
          loading={isDeleting}
          className="ml-auto shrink-0"
           aria-label={t('wizard.template.delete', 'Delete template')}
          data-testid="template-delete-btn"
        />
      </div>
    </div>
  )
}

export default function TemplateLibraryPage() {
  const { t } = useTranslation()
  const { data, isLoading, error } = useSchemaTemplates()
  const [expandedCardId, setExpandedCardId] = useState(null)
  const [useTemplateId, setUseTemplateId] = useState(null)
  const deleteTemplate = useDeleteSchemaTemplate()
  const [deletingId, setDeletingId] = useState(null)

  const handleDelete = async (templateId) => {
    setDeletingId(templateId)
    try {
      await deleteTemplate.mutateAsync(templateId)
    } catch (err) {
      console.error('Failed to delete template', err)
    } finally {
      setDeletingId(null)
    }
  }

  const handleExportAll = async () => {
    try {
      const response = await api.get('/templates/export', { responseType: 'blob' })
      const url = URL.createObjectURL(new Blob([response.data], { type: 'application/json' }))
      const a = document.createElement('a')
      a.href = url
      a.download = 'schemas-templates-export.json'
      a.click()
      URL.revokeObjectURL(url)
    } catch (err) {
      console.error('Export failed', err)
    }
  }

  const templates = Array.isArray(data) ? data : (data?.items ?? [])

  return (
    <ErrorBoundary>
      <div className="space-y-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
         <Link to="/configuration/sources" className="hover:text-foreground">{t('nav.configuration', 'Configuration')}</Link>
        <span>/</span>
         <span className="text-foreground">{t('wizard.library.breadcrumb', 'Template Library')}</span>
      </div>

      <PageHeader
         title={t('wizard.library.title', 'Template Library')}
         description={t('wizard.library.description', 'Browse and apply schema templates to quickly scaffold new schemas.')}
        icon="file-text"
      >
        <Button
          variant="outline"
          size="md"
          leftIcon="download"
          onClick={handleExportAll}
          data-testid="template-export-all-btn"
        >
           {t('wizard.library.export_all', 'Export All')}
        </Button>
      </PageHeader>

      {isLoading && <TableSkeleton rows={3} cols={2} />}

      {!isLoading && error && (
        <div className="p-6 text-destructive">
           {interpolateDefault(t('wizard.library.load_failed', 'Failed to load templates. {{message}}'), { message: error.message })}
        </div>
      )}

      {!isLoading && !error && templates.length === 0 && (
        <EmptyState
          variant="empty"
          icon={Layers}
           title={t('wizard.library.empty_title', 'No templates yet')}
           description={t('wizard.library.empty_description', 'Save an AI-generated schema as a template from the Schema Wizard, or create one from scratch.')}
        />
      )}

      {!isLoading && !error && templates.length > 0 && (
        <div
          className="grid grid-cols-1 gap-4 sm:grid-cols-2 lg:grid-cols-3"
          data-testid="template-grid"
        >
          {templates.map(template => (
            <TemplateCard
              key={template.id}
              template={template}
              onUse={id => setUseTemplateId(id)}
              onDelete={handleDelete}
              isDeleting={deletingId === template.id}
              isExpanded={expandedCardId === template.id}
              onToggleExpand={() =>
                setExpandedCardId(prev => (prev === template.id ? null : template.id))
              }
            />
          ))}
        </div>
      )}

      {useTemplateId && (
        <TemplatePreviewModal
          templateId={useTemplateId}
          onClose={() => setUseTemplateId(null)}
        />
      )}
    </div>
    </ErrorBoundary>
  )
}
