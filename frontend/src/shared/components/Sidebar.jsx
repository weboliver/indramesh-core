import { useState, useEffect, useRef, useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api, { serializeRepeatedParams } from '../hooks/use-api'
import { useSources } from '../hooks/use-sources'
import { useObjectTypes, useUpdateObjectType } from '../hooks/use-object-types'
import { useSchemaLanguages } from '../hooks/use-languages'
import { getLanguageName } from '../hooks/use-active-language'
import { cn } from '../lib/utils'
import { LucideIcon } from '../lib/lucide-icons'
import { Button } from './Button'
import { DropdownMenu, DropdownMenuItem } from './DropdownMenu'
import { useLanguagePreview } from './LanguagePreviewProvider'
import { useActiveSchema } from './ActiveSchemaProvider'
import { useTranslation } from 'react-i18next'

// See ObjectTypeObjects.jsx for why this needs to be a stable reference
// rather than an inline `= []` destructuring default.
const EMPTY_ARRAY = []

function ObjectTypeTreeItem({ ot, childTypes, countMap, level = 0, allExpanded = false, draggedId, dragOverId, onDragStart, onDragOver, onDragLeave, onDrop, previewLang }) {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(level < 1 || allExpanded)
  const navigate = useNavigate()
  const isDragging = useRef(false)

  useEffect(() => {
    setExpanded(allExpanded)
  }, [allExpanded])

  const children = childTypes.filter((c) => c.parent_id === ot.id)
  const count = countMap.get(ot.id)
  const isDropTarget = dragOverId === ot.id && draggedId !== ot.id

  return (
    <div>
      {/* The row wrapper is the drop target; the chevron only toggles, so a
          node can be expanded without navigating (the mobile drawer closes on
          navigation). */}
      <div
        onDragOver={(e) => {
          e.preventDefault()
          e.stopPropagation()
          onDragOver(ot.id)
        }}
        onDragLeave={(e) => {
          e.stopPropagation()
          onDragLeave()
        }}
        onDrop={(e) => {
          e.preventDefault()
          e.stopPropagation()
          onDrop(ot)
        }}
        className={cn(
          'flex w-full items-center gap-1.5 px-2 text-sm rounded-md hover:bg-accent hover:text-accent-foreground transition-colors',
          isDropTarget && 'border-t-2 border-t-primary',
          draggedId === ot.id && 'opacity-40',
        )}
        style={{ paddingLeft: `${8 + level * 16}px` }}
      >
        {children.length > 0 ? (
          <button
            type="button"
            onClick={() => setExpanded((e) => !e)}
            aria-expanded={expanded}
            aria-label={expanded
              ? t('sidebar.collapse_type', 'Collapse {{name}}', { name: ot.name })
              : t('sidebar.expand_type', 'Expand {{name}}', { name: ot.name })}
            className="shrink-0 w-3.5 self-stretch flex items-center justify-center"
            data-testid="object-type-tree-toggle"
          >
            <LucideIcon name={expanded ? 'chevron-down' : 'chevron-right'} size={14} className="text-muted-foreground" />
          </button>
        ) : (
          <span className="shrink-0 w-3.5 flex items-center justify-center">
            {!ot.icon_local_name ? <span className="text-xs text-muted-foreground">○</span> : null}
          </span>
        )}
        {/* Tree item button intentionally NOT migrated — drag/drop semantics require raw <button> */}
        <button
          draggable={!previewLang}
          data-testid="object-type-tree-item"
          onDragStart={(e) => {
            isDragging.current = true
            e.stopPropagation()
            e.dataTransfer.effectAllowed = 'move'
            onDragStart(ot.id)
          }}
          onDragEnd={() => {
            setTimeout(() => { isDragging.current = false }, 0)
          }}
          onClick={() => {
            if (isDragging.current) return
            setExpanded((e) => !e)
            navigate(`/browse/object-types/${ot.id}`)
          }}
          className="flex min-w-0 flex-1 items-center gap-1.5 py-1.5 text-left"
        >
          {ot.icon_local_name && (
            <LucideIcon name={ot.icon_local_name} size={16} className="shrink-0 text-muted-foreground" />
          )}
          <span className={cn('truncate', previewLang && ot.resolved_language !== previewLang && 'italic text-muted-foreground')}>
            {ot.name}
          </span>
          {ot.is_abstract && (
            <LucideIcon
              name="cloud"
              size={14}
              className="shrink-0 text-muted-foreground"
              aria-label={t('browse.objects.abstract_type', 'Abstract')}
              title={t('browse.objects.abstract_type', 'Abstract')}
            />
          )}
          {count !== undefined && (
            <span className="ml-auto shrink-0 text-xs text-muted-foreground tabular-nums">({count})</span>
          )}
        </button>
      </div>
      {expanded && children.map((child) => (
        <ObjectTypeTreeItem
          key={child.id}
          ot={child}
          childTypes={childTypes}
          countMap={countMap}
          level={level + 1}
          allExpanded={allExpanded}
          draggedId={draggedId}
          dragOverId={dragOverId}
          onDragStart={onDragStart}
          onDragOver={onDragOver}
          onDragLeave={onDragLeave}
          onDrop={onDrop}
          previewLang={previewLang}
        />
      ))}
    </div>
  )
}

export default function Sidebar({ className }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { activeSourceId, activeSchemaId, activeObjectTypeId, setActiveSource, setActiveSchemaId } = useActiveSchema()
  const [allExpanded, setAllExpanded] = useState(false)
  const [pendingNav, setPendingNav] = useState(false)
  const [draggedId, setDraggedId] = useState(null)
  const [dragOverId, setDragOverId] = useState(null)
  const { previewLang, setPreviewLang, resetPreview } = useLanguagePreview()
  const [previewOpen, setPreviewOpen] = useState(false)

  const { data: sources = EMPTY_ARRAY } = useSources(0, 1000)
  const { data: schemas = EMPTY_ARRAY, isLoading: schemasLoading } = useQuery({
    queryKey: ['schemas', { source_id: activeSourceId }],
    queryFn: () => api.get('/schemas/', { params: { source_id: activeSourceId, skip: 0, limit: 1000 } }).then(r => r.data),
    enabled: !!activeSourceId,
  })

  const { data: objectTypes = EMPTY_ARRAY, isLoading: typesLoading, refetch: refetchObjectTypes } =
    useObjectTypes(activeSchemaId, 0, 50, { lang: previewLang || undefined })
  const updateObjectType = useUpdateObjectType()

  const objectTypeIds = useMemo(() => objectTypes.map(ot => ot.id), [objectTypes])

  const { data: countsData } = useQuery({
    queryKey: ['object-counts', 'batch', objectTypeIds],
    queryFn: () => api.get('/objects/counts', { params: { object_type_ids: objectTypeIds }, paramsSerializer: serializeRepeatedParams }).then(r => r.data),
    enabled: objectTypeIds.length > 0,
    staleTime: 30_000,
  })

  const countMap = useMemo(() => {
    const map = new Map()
    if (countsData) {
      for (const [id, count] of Object.entries(countsData)) map.set(id, count)
    }
    return map
  }, [countsData])

  const activeSource = sources.find(s => s.id === activeSourceId)

  const activeSchema = schemas.find(s => s.id === activeSchemaId)
  const { data: schemaLanguagesRaw = EMPTY_ARRAY } = useSchemaLanguages(activeSchemaId)
  const schemaLanguages = useMemo(
    () => schemaLanguagesRaw.filter(l => l.is_active),
    [schemaLanguagesRaw]
  )
  const defaultLanguageCode = activeSchema?.default_language_code ?? null
  const isMultilingual = !!activeSchema?.multilingual

  useEffect(() => {
    if (previewLang && schemaLanguages.length > 0 && !schemaLanguages.some(l => l.code === previewLang)) {
      resetPreview()
    }
  }, [previewLang, schemaLanguages, resetPreview])

  useEffect(() => {
    if (pendingNav && objectTypes.length > 0) {
      const first = objectTypes.filter(ot => !ot.parent_id)[0] || objectTypes[0]
      navigate(`/browse/object-types/${first.id}`)
      setPendingNav(false)
    }
  }, [objectTypes, pendingNav, navigate])

  function handleSourceChange(sourceId) {
    setActiveSource(sourceId)
    setPendingNav(true)
  }

  function handleSchemaChange(schemaId) {
    resetPreview()
    setActiveSchemaId(schemaId)
    setPendingNav(true)
  }

  async function handleDrop(targetOt) {
    if (previewLang) {
      setDraggedId(null)
      setDragOverId(null)
      return
    }
    if (!draggedId || draggedId === targetOt.id) {
      setDraggedId(null)
      setDragOverId(null)
      return
    }

    const draggedOt = objectTypes.find(ot => ot.id === draggedId)
    if (!draggedOt) return

    // Siblings of the target (same parent_id), sorted by current sort
    const siblings = objectTypes
      .filter(ot => ot.parent_id === targetOt.parent_id)
      .sort((a, b) => (a.sort ?? 0) - (b.sort ?? 0))

    // Remove dragged item from its current position, insert before target
    const withoutDragged = siblings.filter(ot => ot.id !== draggedId)
    const targetIdx = withoutDragged.findIndex(ot => ot.id === targetOt.id)
    const reordered = [...withoutDragged]
    reordered.splice(targetIdx === -1 ? reordered.length : targetIdx, 0, draggedOt)

    const newParentId = targetOt.parent_id ?? null

    for (let i = 0; i < reordered.length; i++) {
      const ot = reordered[i]
      const newSort = (i + 1) * 10
      const updates = {}
      if (ot.sort !== newSort) updates.sort = newSort
      if (ot.id === draggedId && (ot.parent_id ?? null) !== newParentId) {
        updates.parent_id = newParentId
      }
      if (Object.keys(updates).length > 0) {
        await updateObjectType.mutateAsync({ id: ot.id, ...updates })
      }
    }

    setDraggedId(null)
    setDragOverId(null)
  }

  const rootTypes = objectTypes.filter((ot) => !ot.parent_id)

  return (
    <aside className={cn('w-[326px] border-r bg-card h-full overflow-y-auto', className)} style={{
        background: 'hsl(var(--template-sidebar-bg, var(--card)))',
        '--foreground': 'var(--template-sidebar-fg, var(--foreground))',
        '--muted-foreground': 'var(--template-sidebar-muted-fg, var(--muted-foreground))',
      }}>
      <nav className="p-2 space-y-3">
        <div>
          <div className="px-3 mb-2">
            <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
               {t('nav.source', 'Source')}
            </h3>
            <select
              value={activeSourceId || ''}
              onChange={(e) => handleSourceChange(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
            >
              {sources.map((s) => (
                <option key={s.id} value={s.id}>{s.name}</option>
              ))}
            </select>
          </div>

          {activeSource && schemas.length > 0 && (
            <div className="px-3 mb-2">
              <h3 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-2">
                 {t('nav.schemas', 'Schemas')}
              </h3>
              <select
                value={activeSchemaId || ''}
                onChange={(e) => handleSchemaChange(e.target.value)}
                className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm"
                data-testid="sidebar-schema-select"
              >
                {schemas.map((s) => (
                  <option key={s.id} value={s.id}>{s.name}</option>
                ))}
              </select>
            </div>
          )}

          {schemasLoading || typesLoading ? (
            <p className="px-3 text-xs text-muted-foreground" data-testid="loading-indicator">{t('common.loading', 'Loading...')}</p>
          ) : !activeSource ? (
             <p className="px-3 text-xs text-muted-foreground">{t('sidebar.no_sources', 'No sources configured')}</p>
          ) : schemas.length === 0 ? (
             <p className="px-3 text-xs text-muted-foreground">{t('sidebar.no_schemas', 'No schemas in this source')}</p>
          ) : !activeSchemaId ? (
             <p className="px-3 text-xs text-muted-foreground">{t('sidebar.select_schema', 'Select a schema')}</p>
          ) : (
            <>
              <div className="flex items-center justify-between px-3 mb-1 mt-2">
                <h4 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider whitespace-nowrap">
                   {t('nav.object_types', 'Object Types')}
                </h4>
                <div className="flex gap-0.5 ml-2">
                  {isMultilingual && (
                    <DropdownMenu
                      trigger={
                        <Button
                          size="icon"
                          variant="ghost"
                          leftIcon="globe"
                           aria-label={t('sidebar.preview_language', 'Preview language')}
                           title={t('sidebar.preview_language', 'Preview language')}
                          data-testid="sidebar-preview-toggle"
                        />
                      }
                      open={previewOpen}
                      onOpenChange={setPreviewOpen}
                      align="right"
                    >
                      {(() => {
                        const items = [...schemaLanguages].sort((a, b) => {
                          const aIsDefault = a.code === defaultLanguageCode ? 0 : 1
                          const bIsDefault = b.code === defaultLanguageCode ? 0 : 1
                          return aIsDefault - bIsDefault || a.code.localeCompare(b.code)
                        })
                        return items.map((lang) => {
                          const isDefault = lang.code === defaultLanguageCode
                          const isActive = isDefault ? previewLang === null : previewLang === lang.code
                          return (
                            <DropdownMenuItem
                              key={lang.code}
                              variant={isActive ? 'secondary' : 'ghost'}
                              data-testid={`sidebar-preview-option-${lang.code}`}
                              onClick={() => {
                                setPreviewLang(isDefault ? null : lang.code)
                                setPreviewOpen(false)
                              }}
                            >
                              <span className="inline-flex items-center gap-1">
                                {isActive && <LucideIcon name="check" size={12} className="shrink-0" />}
                                {getLanguageName(lang.code, schemaLanguages)}
                              </span>
                            </DropdownMenuItem>
                          )
                        })
                      })()}
                    </DropdownMenu>
                  )}
                  <Button
                    onClick={() => setAllExpanded(true)}
                    size="icon"
                    variant="ghost"
                    leftIcon="maximize-2"
                     aria-label={t('sidebar.expand_all_object_types', 'Expand all object types')}
                     title={t('sidebar.expand_all', 'Expand all')}
                  />
                  <Button
                    onClick={() => setAllExpanded(false)}
                    size="icon"
                    variant="ghost"
                    leftIcon="minimize-2"
                     aria-label={t('sidebar.collapse_all_object_types', 'Collapse all object types')}
                     title={t('sidebar.collapse_all', 'Collapse all')}
                  />
                  <Button
                    onClick={() => refetchObjectTypes()}
                    size="icon"
                    variant="ghost"
                    leftIcon="rotate-cw"
                     aria-label={t('sidebar.refresh_object_types', 'Refresh object types')}
                     title={t('sidebar.refresh', 'Refresh')}
                  />
                  {previewLang ? (
                    <Button
                      size="icon"
                      variant="ghost"
                      leftIcon="plus"
                      disabled
                       aria-label={t('sidebar.create_object_type', 'Create new object type')}
                       title={t('sidebar.new_object_type', 'New Object Type')}
                    />
                  ) : (
                    <Button
                      asChild
                      size="icon"
                      variant="ghost"
                       aria-label={t('sidebar.create_object_type', 'Create new object type')}
                       title={t('sidebar.new_object_type', 'New Object Type')}
                    >
                      <Link to={`/object-types/new?schemaId=${activeSchemaId || ''}${activeObjectTypeId ? `&parentId=${activeObjectTypeId}` : ''}`}>
                        <LucideIcon name="plus" size={16} />
                      </Link>
                    </Button>
                  )}
                </div>
              </div>
              {objectTypes.length === 0 ? (
                 <p className="px-3 text-xs text-muted-foreground" data-testid="empty-state">{t('sidebar.no_object_types', 'No object types in this schema')}</p>
              ) : (
                rootTypes.map((ot) => (
                  <ObjectTypeTreeItem
                    key={ot.id}
                    ot={ot}
                    childTypes={objectTypes}
                    countMap={countMap}
                    level={0}
                    allExpanded={allExpanded}
                    draggedId={draggedId}
                    dragOverId={dragOverId}
                    onDragStart={setDraggedId}
                    onDragOver={setDragOverId}
                    onDragLeave={() => setDragOverId(null)}
                    onDrop={handleDrop}
                    previewLang={previewLang}
                  />
                ))
              )}
            </>
          )}
        </div>
      </nav>
    </aside>
  )
}
