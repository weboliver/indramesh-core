import { useQuery } from '@tanstack/react-query'
import api from './use-api'

export function useCrossEntitySearch(q, page = 1, pageSize = 5) {
  return useQuery({
    queryKey: ['crossEntitySearch', q, page, pageSize],
    queryFn: () => api.get('/search/cross-entity', {
      params: { q, page, page_size: pageSize }
    }).then(r => r.data),
    enabled: !!q && q.length >= 2,
  })
}
