import { useQuery } from '@tanstack/react-query'
import api from './use-api'

export function useObjectRefs(objectId) {
  return useQuery({
    queryKey: ['object-refs', { objectId }],
    queryFn: () => api.get('/object-refs/', { params: { from_object_id: objectId } }).then(r => r.data),
    enabled: !!objectId,
  })
}

export function useInboundRefs(objectId) {
  return useQuery({
    queryKey: ['object-refs', { toObjectId: objectId }],
    queryFn: () => api.get('/object-refs/', { params: { to_object_id: objectId } }).then(r => r.data),
    enabled: !!objectId,
  })
}
