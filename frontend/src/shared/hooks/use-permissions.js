import { useQuery } from '@tanstack/react-query'
import api from './use-api'

export function usePermissions() {
  return useQuery({
    queryKey: ['permissions'],
    queryFn: () => api.get('/permissions/').then(r => r.data),
  })
}
