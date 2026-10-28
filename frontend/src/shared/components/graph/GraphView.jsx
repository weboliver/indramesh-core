import {
  ReactFlow,
  ReactFlowProvider,
  Background,
  Controls,
  Handle,
  Position,
  applyNodeChanges,
  useReactFlow,
  useNodesInitialized,
} from '@xyflow/react'
import { memo, useState, useEffect, useCallback, useRef } from 'react'
import EmptyState from '../EmptyState'
import LoadingSpinner from '../LoadingSpinner'
import { LucideIcon } from '../../lib/lucide-icons'
import Network from 'lucide-react/dist/esm/icons/network'

const NodeCard = memo(function NodeCard({ data, selected }) {
  return (
    <div
      data-testid="graph-node"
      data-node-id={data.id}
      style={data.type_color ? { borderColor: data.type_color } : undefined}
      className={`relative flex w-[160px] h-[48px] items-center gap-1.5 px-2 py-1 rounded-md border bg-card text-sm font-semibold cursor-grab ${
        data.is_anchor ? 'ring-2 ring-dashed ring-primary/60' : ''
      } ${selected ? 'ring-2 ring-primary' : ''}`}
    >
      <Handle type="target" position={Position.Top} className="!h-2 !w-2 !border-0 !bg-primary/60 !opacity-0" />
      <Handle type="source" position={Position.Bottom} className="!h-2 !w-2 !border-0 !bg-primary/60 !opacity-0" />
      {data.icon_local_name && <LucideIcon name={data.icon_local_name} size={16} className="shrink-0" />}
      <span className="truncate">{data.name}</span>
    </div>
  )
})

const nodeTypes = { card: NodeCard }

function GraphCanvas({ nodes, edges, onNodeClick, onNodesChange, onNodeMouseEnter, onNodeMouseMove, onNodeMouseLeave, onNodeDragStart }) {
  // WR-02: the info panel mounts right after ReactFlow's initial fitView, so
  // the canvas shrinks and nodes can clip. React Flow v12 never re-fits on
  // container resize — observe the wrapper and re-fit whenever it resizes.
  const containerRef = useRef(null)
  const { fitView } = useReactFlow()
  useEffect(() => {
    if (!containerRef.current) return
    const observer = new ResizeObserver(() => fitView({ padding: 0.2 }))
    observer.observe(containerRef.current)
    return () => observer.disconnect()
  }, [fitView])

  // The `fitView` prop below runs once on mount, before custom NodeCard
  // components have reported their real measured size — the fit then
  // computes against zero-size placeholders and the graph lands
  // off-center (e.g. squashed toward one edge). Re-fit once React Flow
  // confirms all nodes carry real dimensions.
  const nodesInitialized = useNodesInitialized()
  useEffect(() => {
    if (nodesInitialized) fitView({ padding: 0.2 })
  }, [nodesInitialized, fitView])

  return (
    <div className="h-full w-full" data-testid="graph-view" ref={containerRef}>
      <ReactFlow
        nodes={nodes}
        edges={edges}
        nodeTypes={nodeTypes}
        onNodesChange={onNodesChange}
        fitView
        minZoom={0.2}
        maxZoom={2}
        onNodeClick={(_, node) => onNodeClick?.(node.id)}
        onNodeMouseEnter={(e, node) => onNodeMouseEnter?.(node.id, e)}
        onNodeMouseMove={(e, node) => onNodeMouseMove?.(node.id, e)}
        onNodeMouseLeave={() => onNodeMouseLeave?.()}
        onNodeDragStart={(_, node) => onNodeDragStart?.(node.id)}
      >
        <Background variant="dots" gap={16} />
        <Controls />
      </ReactFlow>
    </div>
  )
}

export default function GraphView({
  nodes,
  edges,
  isLoading,
  error,
  onNodeClick,
  onRetry,
  emptyTitle = 'No reference relationships',
  emptyDescription = 'This object type is not connected to other object types. Relationships are created via reference-type attributes.',
  onNodeMouseEnter,
  onNodeMouseMove,
  onNodeMouseLeave,
  onNodeDragStart,
}) {
  // Controlled node state so drag positions persist (React Flow v12 reverts
  // uncontrolled prop positions on re-render). Resynced whenever the parent
  // recomputes the dagre layout (data change).
  const [rfNodes, setRfNodes] = useState(nodes)
  useEffect(() => {
    setRfNodes(nodes)
  }, [nodes])
  const onNodesChange = useCallback(
    changes => setRfNodes(ns => applyNodeChanges(changes, ns)),
    []
  )

  return (
    <div className="h-full w-full">
      {isLoading ? (
        <div className="h-full w-full" data-testid="graph-view">
          <LoadingSpinner className="h-full w-full" />
        </div>
      ) : error ? (
        <div className="h-full w-full" data-testid="graph-view">
          <EmptyState variant="error" onButtonClick={onRetry} data-testid="graph-error-state" />
        </div>
      ) : nodes.length === 0 ? (
        <div className="h-full w-full" data-testid="graph-view">
          <EmptyState
            variant="empty"
            icon={Network}
            title={emptyTitle}
            description={emptyDescription}
            data-testid="graph-empty-state"
          />
        </div>
      ) : (
        <ReactFlowProvider>
          <GraphCanvas
            nodes={rfNodes}
            edges={edges}
            onNodeClick={onNodeClick}
            onNodesChange={onNodesChange}
            onNodeMouseEnter={onNodeMouseEnter}
            onNodeMouseMove={onNodeMouseMove}
            onNodeMouseLeave={onNodeMouseLeave}
            onNodeDragStart={onNodeDragStart}
          />
        </ReactFlowProvider>
      )}
    </div>
  )
}
