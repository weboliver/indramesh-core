import { Graph, layout } from '@dagrejs/dagre'
import { MarkerType } from '@xyflow/react'

const NODE_WIDTH = 160
const NODE_HEIGHT = 48

function validateEdgeColor(color) {
  return typeof color === 'string' && /^#(?:[0-9a-fA-F]{3,4}|[0-9a-fA-F]{6}|[0-9a-fA-F]{8})$/.test(color) ? color : null
}

export function mapGraphPayload(payload) {
  const nodes = payload.nodes.map(n => ({
    id: n.id,
    type: 'card',
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    data: { id: n.id, name: n.name, icon_local_name: n.icon_local_name, is_anchor: n.is_anchor },
  }))
  const edges = payload.edges.map(e => {
    const stroke = validateEdgeColor(e.reference_type_color) ?? 'hsl(var(--muted-foreground))'
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.attribute_name,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke },
      labelStyle: { fill: stroke },
    }
  })
  return { nodes, edges }
}

export function applyDagreLayout(nodes, edges) {
  const g = new Graph()
  g.setDefaultEdgeLabel(() => ({}))
  g.setGraph({ rankdir: 'TB', ranker: 'network-simplex', acyclicer: 'greedy' })
  nodes.forEach(n => g.setNode(n.id, { width: NODE_WIDTH, height: NODE_HEIGHT }))
  edges.forEach(e => {
    if (e.source !== e.target) g.setEdge(e.source, e.target)
  })
  layout(g)
  return nodes.map(n => ({
    ...n,
    position: { x: g.node(n.id).x - NODE_WIDTH / 2, y: g.node(n.id).y - NODE_HEIGHT / 2 },
  }))
}

export function typeColorFor(objectTypeId) {
  let h = 0x811c9dc5
  for (let i = 0; i < objectTypeId.length; i++) {
    h ^= objectTypeId.charCodeAt(i)
    h = Math.imul(h, 0x01000193) >>> 0 // 32-bit FNV-1a
  }
  return `hsl(${h % 360} 65% 45%)` // stable per object_type_id across opens/sessions (D-05)
}

export function mapObjectGraphPayload(payload) {
  const nodes = payload.nodes.map(n => ({
    id: n.id,
    type: 'card',
    width: NODE_WIDTH,
    height: NODE_HEIGHT,
    data: {
      id: n.id,
      name: n.name,
      icon_local_name: n.icon_local_name,
      is_anchor: n.is_anchor,
      object_type_id: n.object_type_id,
      object_type_name: n.object_type_name,
      schema_id: n.schema_id,
      type_color: typeColorFor(String(n.object_type_id)),
    },
  }))
  const edges = payload.edges.map(e => {
    const stroke = validateEdgeColor(e.reference_type_color) ?? 'hsl(var(--muted-foreground))'
    return {
      id: e.id,
      source: e.source,
      target: e.target,
      label: e.from_attribute_name,
      markerEnd: { type: MarkerType.ArrowClosed },
      style: { stroke },
      labelStyle: { fill: stroke },
    }
  })
  return { nodes, edges }
}
