import { useQuery } from '@tanstack/react-query'
import api from './use-api'

export function useObjectValues(objectId, lang) {
  return useQuery({
    queryKey: ['object-values', { objectId, lang }],
    queryFn: () => api.get('/object-values/', {
      params: { object_id: objectId, ...(lang ? { lang } : {}) },
    }).then(r => r.data),
    enabled: !!objectId,
  })
}
