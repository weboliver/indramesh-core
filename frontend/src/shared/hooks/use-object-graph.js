import { useQuery } from '@tanstack/react-query'
import api from './use-api'

export function useObjectGraph(objectId, enabled = true) {
  return useQuery({
    queryKey: ['object-graph', objectId],
    queryFn: () => api.get(`/objects/${objectId}/graph`).then(r => r.data),
    enabled: enabled && !!objectId,
    staleTime: 60_000,
  })
}
