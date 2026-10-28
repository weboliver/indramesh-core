import { useState, useMemo, useEffect } from 'react'
import { useParams, useNavigate, Link } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'
import { useObjectType, useUpdateObjectType } from '../../shared/hooks/use-object-types'
import { useSources } from '../../shared/hooks/use-sources'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import IconPicker from './IconPicker'
import TableSkeleton from '../../shared/components/TableSkeleton'
import { Button } from '../../shared/components/Button'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import TranslationPanel from '../../shared/components/TranslationPanel'
import { useTranslations } from '../../shared/hooks/use-translations'
import { showToast } from '../../shared/components/Toast'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { buildEditObjectTypeBreadcrumbItems } from './object-type-edit-breadcrumb'
import { PluginSectionRegion } from '../../shared/plugin-runtime/PluginSections'

export default function EditObjectTypePage() {
  const { t } = useTranslation()
  const { id } = useParams()
  const navigate = useNavigate()

  const { data: ot, isLoading: otLoading } = useObjectType(id)
  const queryClient = useQueryClient()

  const [name, setName] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [schemaId, setSchemaId] = useState('')
  const [parentId, setParentId] = useState('')
  const [sortOrder, setSortOrder] = useState(0)
  const [inheritsAttributes, setInheritsAttributes] = useState(false)
  const [isAbstract, setIsAbstract] = useState(false)
  const [multilingual, setMultilingual] = useState(false)
  const [error, setError] = useState('')
  const [selectedIcon, setSelectedIcon] = useState(null)
  const [panelOpen, setPanelOpen] = useState(false)

  const { data: sources = [], isLoading: sourcesLoading } = useSources()
  const { data: allSchemas = [], isLoading: schemasLoading } = useQuery({
    queryKey: ['schemas', { all: true }],
    queryFn: () => api.get('/schemas/', { params: { skip: 0, limit: 1000 } }).then(r => r.data),
  })
  const { data: allObjectTypes = [], isLoading: otsLoading } = useQuery({
    queryKey: ['object-types', { all: true }],
    queryFn: () => api.get('/object-types/', { params: { skip: 0, limit: 1000 } }).then(r => r.data),
  })
  const { data: icons = [] } = useQuery({
    queryKey: ['icons'],
    queryFn: () => api.get('/icons/', { params: { limit: 500 } }).then(r => r.data),
  })

  useEffect(() => {
    if (ot) {
      setName(ot.name || '')
      setSchemaId(ot.schema_id || '')
      setParentId(ot.parent_id || '')
      setSortOrder(ot.sort ?? 0)
      setInheritsAttributes(ot.inherits_attributes || false)
      setIsAbstract(ot.is_abstract || false)
      setMultilingual(ot.multilingual || false)
      if (ot.icon_local_name) {
        setSelectedIcon(ot.icon_local_name)
      }
    }
  }, [ot])

  // Resolve source from schema when both are loaded
  useEffect(() => {
    if (ot?.schema_id && allSchemas.length > 0) {
      const schema = allSchemas.find((s) => s.id === ot.schema_id)
      if (schema?.source_id) {
        setSourceId(schema.source_id)
      }
    }
  }, [ot?.schema_id, allSchemas])

  const schemas = useMemo(() => {
    if (!sourceId) return []
    return allSchemas.filter((s) => s.source_id === sourceId)
  }, [allSchemas, sourceId])

  // compute the set of descendant IDs of `currentId` within the loaded object-type list
  function getDescendantIds(list, currentId) {
    const childrenOf = {}
    for (const t of list) {
      if (t.parent_id) (childrenOf[t.parent_id] ||= []).push(t.id)
    }
    const out = new Set()
    const stack = [currentId]
    while (stack.length) {
      const cid = stack.pop()
      for (const c of childrenOf[cid] || []) {
        if (!out.has(c)) { out.add(c); stack.push(c) }
      }
    }
    return out
  }

  const parentObjectTypes = useMemo(() => {
    const descendants = getDescendantIds(allObjectTypes, id)
    return allObjectTypes.filter(
      (ot) => ot.schema_id === schemaId && ot.id !== id && !descendants.has(ot.id)
    )
  }, [allObjectTypes, schemaId, id])

  const iconNameToId = useMemo(() => {
    const map = {}
    for (const icon of icons) {
      if (icon.local_name) {
        map[icon.local_name] = icon.id
      }
    }
    return map
  }, [icons])

  function renderTreeOptions(types, parentIdVal = null, level = 0) {
    const children = types.filter(t => t.parent_id === parentIdVal)
    return children.map(child => [
      <option key={child.id} value={child.id} style={{ paddingLeft: `${level * 16}px` }}>
        {'→ '.repeat(level)}{child.name}
      </option>,
      ...renderTreeOptions(types, child.id, level + 1)
    ])
  }

  const sourceName = useMemo(() => {
    const source = sources.find((s) => s.id === sourceId)
    return source?.name || ''
  }, [sources, sourceId])

  const updateMutation = useUpdateObjectType()

  const { data: existingTranslations = [] } = useTranslations('object_type', id, {
    enabled: multilingual,
  })

  async function handleMultilingualToggle(checked) {
    if (!checked && (ot?.multilingual ?? false) && multilingual) {
      const count = existingTranslations.length
      const message = selectPluralKey('admin.object_type.disable_multilingual', count) === 'admin.object_type.disable_multilingual_one'
        ? interpolateDefault(t('admin.object_type.disable_multilingual_one', 'Disable multilingual?\n\nThis will hide {{count}} existing translation. They are kept, not deleted, and will reappear if you re-enable it.'), { count })
        : interpolateDefault(t('admin.object_type.disable_multilingual_other', 'Disable multilingual?\n\nThis will hide {{count}} existing translations. They are kept, not deleted, and will reappear if you re-enable it.'), { count })
      const confirmed = await confirmDialog({ message, danger: false })
      if (!confirmed) return
      setMultilingual(false)
      return
    }
    if (checked) {
      setMultilingual(true)
      showToast(t('admin.object_type.multilingual_enabled', 'Multilingual enabled. You can now translate name and description.'))
      showToast(t('admin.object_type.url_email_single_language', 'URL and email attributes stay single-language regardless of this setting.'))
      return
    }
    setMultilingual(false)
  }

  async function handleSubmit(e) {
    e.preventDefault()
    setError('')

    if (!name.trim()) {
      setError(t('admin.object_type.name_required_error', 'Name is required'))
      return
    }
    if (!schemaId) {
      setError(t('admin.object_type.schema_required_error', 'Schema is required'))
      return
    }

    const payload = {}
    if (name.trim() !== ot.name) payload.name = name.trim()
    if (schemaId !== ot.schema_id) payload.schema_id = schemaId
    if (parentId !== (ot.parent_id || '')) payload.parent_id = parentId || null
    if (inheritsAttributes !== (ot?.inherits_attributes ?? false)) payload.inherits_attributes = inheritsAttributes
    if (isAbstract !== (ot?.is_abstract ?? false)) payload.is_abstract = isAbstract
    if (sortOrder !== (ot.sort ?? 0)) payload.sort = sortOrder
    if (multilingual !== (ot.multilingual || false)) payload.multilingual = multilingual

    async function resolveIconId(iconName) {
      if (!iconName) return null
      if (iconNameToId[iconName]) return iconNameToId[iconName]
      try {
        const res = await api.post('/icons/', {
          schema_id: schemaId,
          name: iconName,
          local_name: iconName,
          url: `lucide:${iconName}`,
          type: 'lucide',
        })
        return res.data.id
      } catch {
        return null
      }
    }

    const resolvedIconId = await resolveIconId(selectedIcon)
    if (resolvedIconId !== (ot.icon_id || null)) {
      payload.icon_id = resolvedIconId
    }

    if (Object.keys(payload).length === 0) {
      navigate(`/browse/object-types/${id}`)
      return
    }

    try {
      await updateMutation.mutateAsync({ id, ...payload })
      await queryClient.refetchQueries({ queryKey: ['object-types'] })
      navigate(`/browse/object-types/${id}`)
    } catch (err) {
      console.error('Failed to update ObjectType', err)
      const detail = err.response?.data?.detail
      setError(detail || err.message || t('admin.object_type.update_failed', 'Failed to update ObjectType'))
    }
  }

  const isLoading = otLoading || sourcesLoading || schemasLoading || otsLoading

  if (isLoading) {
    return (
      <div className="max-w-3xl mx-auto space-y-6">
        <div className="flex items-center gap-2 text-sm text-muted-foreground">
          <span className="h-4 w-20 rounded bg-muted animate-skeleton" />
          <span>/</span>
          <span className="h-4 w-16 rounded bg-muted animate-skeleton" />
          <span>/</span>
          <span className="h-4 w-12 rounded bg-muted animate-skeleton" />
        </div>
        <div className="h-8 w-48 rounded bg-muted animate-skeleton" />
        <div className="rounded-lg border p-6 space-y-4">
          <div className="h-6 w-16 rounded bg-muted animate-skeleton" />
          <TableSkeleton rows={5} cols={1} />
        </div>
        <div className="rounded-lg border p-6 space-y-4">
          <div className="h-6 w-20 rounded bg-muted animate-skeleton" />
          <TableSkeleton rows={3} cols={1} />
        </div>
      </div>
    )
  }

  if (!ot) {
    return <div className="text-destructive p-6">{t('admin.object_type.not_found', 'Object type not found')}</div>
  }

  return (
    <ErrorBoundary>
      <div className="max-w-3xl mx-auto space-y-6">
      <Breadcrumb items={buildEditObjectTypeBreadcrumbItems({
        overviewLabel: t('admin.object_type.object_types', 'Object Types'),
        name: ot.name,
        id,
        editLabel: t('common.edit', 'Edit'),
      })} />

      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold tracking-tight">{t('admin.object_type.edit_configuration', 'Edit Configuration')}</h1>
        {multilingual && (
          <Button
            type="button"
            variant="outline"
            size="icon"
            leftIcon="globe"
            aria-label={t('admin.object_type.translate_name_description', 'Translate name and description')}
            data-testid="lang-globe-objecttype"
            onClick={() => setPanelOpen(true)}
          />
        )}
      </div>

      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        <div className="rounded-lg border p-6 space-y-4">
          <h2 className="text-lg font-semibold">{t('common.details', 'Details')}</h2>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.name_required', 'Name *')}</label>
            <div className="flex items-center gap-2 mb-2">
              {selectedIcon && (
                <LucideIcon name={selectedIcon} size={24} className="shrink-0 text-muted-foreground" />
              )}
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('admin.object_type.name_placeholder', 'e.g. Server, Network Device')}
                className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                required
              />
            </div>
            <IconPicker value={selectedIcon} onChange={setSelectedIcon} className="mt-2" />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.source', 'Source')}</label>
            <input
              type="text"
              value={sourceName}
              disabled
              className="w-full rounded-md border border-input bg-muted px-3 py-2 text-sm text-muted-foreground cursor-not-allowed"
            />
            <p className="text-xs text-muted-foreground mt-1">{t('admin.object_type.source_immutable', 'Source cannot be changed after creation')}</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.schema_required', 'Schema *')}</label>
            <select
              value={schemaId}
              onChange={(e) => {
                setSchemaId(e.target.value)
                setParentId('')
              }}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              required
            >
              {schemas.length === 0 ? (
                <option value="">{t('admin.object_type.no_schemas_available', 'No schemas available')}</option>
              ) : (
                <>
                  <option value="">{t('admin.object_type.select_schema', 'Select a schema...')}</option>
                  {schemas.map((schema) => (
                    <option key={schema.id} value={schema.id}>{schema.name}</option>
                  ))}
                </>
              )}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('admin.object_type.parent_type', 'Parent Object Type')}</label>
            <select
              value={parentId}
              onChange={(e) => setParentId(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            >
              <option value="">{t('admin.object_type.root_level', 'None (root level)')}</option>
              {parentObjectTypes.length > 0 && renderTreeOptions(parentObjectTypes)}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('admin.object_type.inheritance', 'Inheritance')}</label>
            <input
              type="checkbox"
              checked={inheritsAttributes}
              onChange={(e) => setInheritsAttributes(e.target.checked)}
              disabled={!!ot?.parent_id}
            />
            <p className="text-xs text-muted-foreground mt-1">{t('admin.object_type.inheritance_hint', "When enabled, child types receive this type's attributes.")}</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('admin.object_type.is_abstract', 'Abstract Object Type')}</label>
            <input
              type="checkbox"
              checked={isAbstract}
              onChange={(e) => setIsAbstract(e.target.checked)}
              data-testid="is-abstract-checkbox"
            />
            <p className="text-xs text-muted-foreground mt-1">{t('admin.object_type.is_abstract_hint', 'No objects can be created for or shown under this type.')}</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.multilingual', 'Multilingual')}</label>
            <input
              type="checkbox"
              checked={multilingual}
              onChange={(e) => handleMultilingualToggle(e.target.checked)}
            />
            <p className="text-xs text-muted-foreground mt-1">{t('admin.object_type.multilingual_hint', "Allow translating this object type's name and description")}</p>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('admin.object_type.sort_order', 'Sort Order')}</label>
            <input
              type="number"
              value={sortOrder}
              onChange={(e) => setSortOrder(Number(e.target.value))}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
            />
          </div>
        </div>

        <div className="flex items-center gap-3">
          <Button
            type="submit"
            disabled={updateMutation.isPending || !name.trim() || !schemaId}
            variant="primary"
            size="lg"
            loading={updateMutation.isPending}
          >
            {updateMutation.isPending ? t('common.saving', 'Saving...') : t('common.save_changes', 'Save Changes')}
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link to={`/browse/object-types/${id}`}>
              {t('common.cancel', 'Cancel')}
            </Link>
          </Button>
        </div>
      </form>

      {/* D-01/D-02: plugin sections sit after the host form and its Save/Cancel
          row, so plugin UI is never submitted by host Save. schemaId is the
          persisted schema id (ot.schema_id), not the in-progress form value. */}
      <PluginSectionRegion
        family="objectTypeSettingsSections"
        headingLevel="h2"
        gapClass="space-y-6"
        componentProps={{ objectType: ot, schemaId: ot.schema_id }}
      />

      {multilingual && (
        <TranslationPanel
          entityType="object_type"
          entityId={ot.id}
          schemaId={schemaId}
          fields={[{ key: 'name' }, { key: 'description' }]}
          fieldValues={{ name, description: ot.description }}
          open={panelOpen}
          onClose={() => setPanelOpen(false)}
        />
      )}
    </div>
    </ErrorBoundary>
  )
}
