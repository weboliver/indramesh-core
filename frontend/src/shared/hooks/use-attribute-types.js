import { useQuery } from '@tanstack/react-query'
import api from './use-api'

// AttributeType is a small, static, global reference set (no per-object pagination
// need) — consumers build a complete typeMap from it, so a partial page silently
// mistypes any attribute past the page boundary. Auto-paginate using the backend's
// X-Total-Count header until every row has been fetched, regardless of how large
// the reference set grows (not capped by the backend's MAX_LIMIT=1000 per request).
async function fetchAllAttributeTypes(pageSize) {
  const first = await api.get('/attribute-types/', { params: { skip: 0, limit: pageSize } })
  const total = Number(first.headers['x-total-count'] ?? first.data.length)
  let items = first.data
  while (items.length < total) {
    const next = await api.get('/attribute-types/', { params: { skip: items.length, limit: pageSize } })
    if (next.data.length === 0) break
    items = items.concat(next.data)
  }
  return items
}

export function useAttributeTypes(pageSize = 1000) {
  return useQuery({
    queryKey: ['attribute-types', 'all', pageSize],
    queryFn: () => fetchAllAttributeTypes(pageSize),
    staleTime: 60_000,
  })
}

