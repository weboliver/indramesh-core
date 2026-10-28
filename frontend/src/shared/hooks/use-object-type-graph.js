import { useQuery } from '@tanstack/react-query'
import api from './use-api'

export function useObjectTypeGraph(objectTypeId) {
  return useQuery({
    queryKey: ['object-type-graph', objectTypeId],
    queryFn: () => api.get(`/object-types/${objectTypeId}/graph`).then(r => r.data),
    enabled: !!objectTypeId,
    staleTime: 60_000,
  })
}
