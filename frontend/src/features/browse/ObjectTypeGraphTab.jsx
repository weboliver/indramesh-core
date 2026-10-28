import { useState, useEffect, useMemo } from 'react'
import { useNavigate } from 'react-router-dom'
import { useObjectTypeGraph } from '../../shared/hooks/use-object-type-graph'
import { useObjectTypeGraphInfo } from '../../shared/hooks/use-object-type-graph-info'
import GraphView from '../../shared/components/graph/GraphView'
import { mapGraphPayload, applyDagreLayout } from '../../shared/components/graph/graph-utils'
import { Button } from '../../shared/components/Button'
import LoadingSpinner from '../../shared/components/LoadingSpinner'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'

export default function ObjectTypeGraphTab({ objectTypeId }) {
  const { t } = useTranslation()
  const { data, isLoading, isError, refetch } = useObjectTypeGraph(objectTypeId)
  const [selectedNodeId, setSelectedNodeId] = useState(null)
  const navigate = useNavigate()

  // dagre layout must run in useMemo keyed on the fetched payload — never
  // during render or in effects (research anti-pattern).
  const model = useMemo(() => {
    if (!data) return { nodes: [], edges: [] }
    const { nodes, edges } = mapGraphPayload(data)
    return { nodes: applyDagreLayout(nodes, edges), edges }
  }, [data])

  // Anchor pre-selection (D-07): the current ObjectType is pre-selected when
  // the graph opens, so its info shows immediately. `prev` guard: only
  // pre-select while the user has not made a manual choice — a background
  // refetch (new data reference) must NOT snap the selection back (WR-01).
  useEffect(() => {
    if (!data) return
    const anchor = data.nodes.find(n => n.is_anchor)
    setSelectedNodeId(prev => prev ?? (anchor ? anchor.id : null))
  }, [data])

  const selectedNode = model.nodes.find(n => n.id === selectedNodeId) ?? null
  const info = useObjectTypeGraphInfo(selectedNodeId)

  return (
    <div className="space-y-3">
      {data?.truncated && (
        <div
          className="h-8 px-3 py-2 bg-muted text-xs text-muted-foreground"
          data-testid="graph-banner"
        >
           {selectPluralKey('browse.graph.showing_object_types', model.nodes.length) === 'browse.graph.showing_object_types_one'
             ? interpolateDefault(t('browse.graph.showing_object_types_one', 'Showing first {{count}} of {{total}} ObjectTypes'), { count: model.nodes.length, total: data.totalCount })
             : interpolateDefault(t('browse.graph.showing_object_types_other', 'Showing first {{count}} of {{total}} ObjectTypes'), { count: model.nodes.length, total: data.totalCount })}
        </div>
      )}

      <div className="flex h-[600px] gap-4">
        <div className="flex-1 min-w-0">
          <GraphView
            nodes={model.nodes}
            edges={model.edges}
            isLoading={isLoading}
            error={isError}
            onNodeClick={(id) => setSelectedNodeId(id)}
            onRetry={refetch}
          />
        </div>

        {selectedNode && (
          <aside className="w-80 shrink-0 border-l bg-card p-4" data-testid="graph-panel">
            <h3 className="text-base font-semibold" data-testid="graph-panel-name">
              {selectedNode.data.name}
            </h3>
            <div className="mt-2">
              {info.isLoading ? (
                <LoadingSpinner className="h-full" size={16} />
              ) : info.isError ? (
                <p className="text-sm text-destructive" data-testid="graph-panel-error">
                  {t('browse.graph.info_load_failed', 'Failed to load object type info')}
                </p>
              ) : (
                <p className="text-sm text-muted-foreground" data-testid="graph-panel-count">
                  {selectPluralKey('browse.graph.object_count', info.count) === 'browse.graph.object_count_one'
                    ? interpolateDefault(t('browse.graph.object_count_one', '{{count}} object'), { count: info.count })
                    : interpolateDefault(t('browse.graph.object_count_other', '{{count}} objects'), { count: info.count })}
                </p>
              )}
            </div>
            {info.attributes.length > 0 && (
                <h4 className="mt-4 text-sm font-semibold">{t('browse.graph.attributes', 'Attributes')}</h4>
            )}
            <ul className="mt-2 space-y-1" data-testid="graph-panel-attributes">
              {info.attributes.map(a => (
                <li key={a.id} className="text-sm text-muted-foreground">
                  {a.name}
                </li>
              ))}
            </ul>
            <div className="mt-4">
              <Button
                variant="primary"
                size="sm"
                data-testid="graph-panel-go"
                onClick={() => navigate(`/browse/object-types/${selectedNodeId}`)}
              >
                  {t('browse.graph.go_to_object_type', 'Go to object type')}
              </Button>
            </div>
          </aside>
        )}
      </div>
    </div>
  )
}
