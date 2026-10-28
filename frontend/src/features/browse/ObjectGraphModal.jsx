import { useState, useEffect, useMemo } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { useObjectGraph } from '../../shared/hooks/use-object-graph'
import { useObject } from '../../shared/hooks/use-objects'
import { useSchema, useSchemas } from '../../shared/hooks/use-schemas'
import GraphView from '../../shared/components/graph/GraphView'
import { mapObjectGraphPayload, applyDagreLayout, typeColorFor } from '../../shared/components/graph/graph-utils'
import { Button } from '../../shared/components/Button'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'

export default function ObjectGraphModal({ objectId, open, onClose }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { data, isLoading, isError, refetch } = useObjectGraph(objectId, open && !!objectId)
  const { data: obj } = useObject(open ? objectId : null)
  const { data: schema } = useSchema(open ? obj?.schema_id : null)
  const { data: schemas } = useSchemas(open ? schema?.source_id : null, 0, 200)
  const [hoveredNodeId, setHoveredNodeId] = useState(null)
  const [hoverPos, setHoverPos] = useState({ x: 0, y: 0 })
  const model = useMemo(() => {
    if (!data) return { nodes: [], edges: [] }
    const { nodes, edges } = mapObjectGraphPayload(data)
    return { nodes: applyDagreLayout(nodes, edges), edges }
  }, [data])
  const nonAnchorNodes = useMemo(() => model.nodes.filter(n => !n.data.is_anchor), [model.nodes])
  const legendEntries = useMemo(() => {
    const byType = new Map()
    for (const n of nonAnchorNodes) {
      const key = n.data.object_type_id
      if (!byType.has(key)) byType.set(key, { object_type_id: key, object_type_name: n.data.object_type_name, count: 0 })
      byType.get(key).count += 1
    }
    return [...byType.values()].sort((a, b) => (a.object_type_name || '').localeCompare(b.object_type_name || ''))
  }, [nonAnchorNodes])
  const schemaMap = useMemo(() => new Map((schemas ?? []).map(s => [s.id, s.name])), [schemas])

  const anchorId = data?.nodes?.find(n => n.is_anchor)?.id ?? null
  const hoveredNode = model.nodes.find(n => n.id === hoveredNodeId) ?? null
  const schemaName = hoveredNode ? schemaMap.get(hoveredNode.data.schema_id) : null

  const handleNodeClick = (id) => {
    if (id === anchorId) return
    navigate(`/browse/objects/${id}`)
    onClose()
  }

  const handleNodeMouseEnter = (nodeId, event) => {
    setHoveredNodeId(nodeId)
    setHoverPos({ x: event.clientX, y: event.clientY })
  }

  const handleNodeMouseMove = (nodeId, event) => {
    setHoverPos({ x: event.clientX, y: event.clientY })
  }

  const handleNodeMouseLeave = () => setHoveredNodeId(null)
  const handleNodeDragStart = () => setHoveredNodeId(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e) => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // Hide the tooltip while the modal body scrolls so it never floats detached
  // from the node it describes (capture phase sees the nested overflow-y-auto
  // container's scroll events).
  useEffect(() => {
    if (!open) return
    const onScroll = () => setHoveredNodeId(null)
    window.addEventListener('scroll', onScroll, true)
    return () => window.removeEventListener('scroll', onScroll, true)
  }, [open])

  useEffect(() => {
    if (!open) setHoveredNodeId(null)
  }, [open])

  if (!open) return null

  return createPortal(
    <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center" onClick={onClose}>
      <div className="relative z-50 w-[640px] max-w-[calc(100vw-2rem)] max-h-[85vh] overflow-y-auto rounded-lg border bg-card shadow-lg"
        onClick={e => e.stopPropagation()} role="dialog" aria-modal="true" aria-label={t('browse.graph.view_object_graph', 'View Object Graph')} data-testid="graph-modal">
        <div className="sticky top-0 z-10 bg-card border-b px-6 py-2 flex items-center justify-between">
          <h2 className="text-lg font-semibold">{t('browse.graph.view_object_graph', 'View Object Graph')}</h2>
          <Button variant="ghost" size="icon" className="h-8 w-8" leftIcon="x" aria-label={t('common.close', 'Close')} data-testid="graph-modal-close" onClick={onClose} />
        </div>
        <div className="px-6 py-2 space-y-2">
          {data?.truncated && (
            <div className="h-8 px-3 py-2 bg-muted text-xs text-muted-foreground" data-testid="graph-banner">
              {selectPluralKey('browse.graph.showing_neighbors', nonAnchorNodes.length) === 'browse.graph.showing_neighbors_one'
                 ? interpolateDefault(t('browse.graph.showing_neighbors_one', 'Showing first {{count}} of {{total}} neighbors'), { count: nonAnchorNodes.length, total: data.totalCount })
                 : interpolateDefault(t('browse.graph.showing_neighbors_other', 'Showing first {{count}} of {{total}} neighbors'), { count: nonAnchorNodes.length, total: data.totalCount })}
            </div>
          )}
          {legendEntries.length > 0 && (
            <div className="flex flex-wrap items-center gap-x-4 gap-y-1 border-b border-muted px-3 py-1 text-xs text-muted-foreground" data-testid="graph-legend">
              {legendEntries.map(e => (
                <span key={e.object_type_id} className="inline-flex items-center gap-1.5">
                  <span className="inline-block h-3 w-3 rounded-sm" style={{ backgroundColor: typeColorFor(String(e.object_type_id)) }} />
                  {selectPluralKey('browse.graph.legend_count', e.count) === 'browse.graph.legend_count_one'
                    ? interpolateDefault(t('browse.graph.legend_count_one', '{{name}} ({{count}})'), { name: e.object_type_name, count: e.count })
                    : interpolateDefault(t('browse.graph.legend_count_other', '{{name}} ({{count}})'), { name: e.object_type_name, count: e.count })}
                </span>
              ))}
            </div>
          )}
          <div className="h-[480px] w-full">
            <GraphView
              nodes={model.nodes} edges={model.edges}
              isLoading={isLoading} error={isError} onRetry={refetch}
              emptyTitle={t('browse.graph.no_reference_relationships', 'No reference relationships')}
              emptyDescription={t('browse.graph.no_references_description', 'This object has no references to other objects. References are created via reference-type attributes.')}
              onNodeClick={handleNodeClick}
              onNodeMouseEnter={handleNodeMouseEnter}
              onNodeMouseMove={handleNodeMouseMove}
              onNodeMouseLeave={handleNodeMouseLeave}
              onNodeDragStart={handleNodeDragStart}
            />
          </div>
          {hoveredNode && (
            <div
              className="fixed z-[60] pointer-events-none rounded-md border bg-popover px-3 py-2 shadow-lg"
              style={{
                left: Math.max(8, Math.min(hoverPos.x + 12, window.innerWidth - 260)),
                top: Math.max(8, Math.min(hoverPos.y + 12, window.innerHeight - 100)),
                maxWidth: '240px',
              }}
              data-testid="graph-tooltip"
            >
              <div className="text-sm font-semibold truncate">{hoveredNode.data.name}</div>
              <div className="mt-1 flex items-center gap-1.5 text-xs text-muted-foreground">
                <span className="inline-block h-2 w-2 rounded-sm" style={{ backgroundColor: hoveredNode.data.type_color }} />
                {hoveredNode.data.object_type_name}
              </div>
              {schemaName && <div className="text-xs text-muted-foreground">{schemaName}</div>}
            </div>
          )}
        </div>
      </div>
    </div>,
    document.body
  )
}
