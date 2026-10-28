import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function useApiClients(skip = 0, limit = 50, options = {}) {
  const { includeTotal = false, ...queryOptions } = options
  return useQuery({
    queryKey: ['api-clients', { skip, limit, includeTotal }],
    queryFn: () => api.get('/api-clients/', { params: { skip, limit } }).then(r =>
      includeTotal
        ? { items: r.data, total: parseInt(r.headers['x-total-count'] || '0', 10) }
        : r.data
    ),
    ...queryOptions,
  })
}

export function useCreateApiClient() {
  const qc = useQueryClient()
  return useMutation({
    // Response includes `token` (raw imsh_... secret) — present only in this
    // one response, never again on list/get. Callers must hold onto it.
    mutationFn: (data) => api.post('/api-clients/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['api-clients'] }),
  })
}
export function useRevokeApiClient() {
  const qc = useQueryClient()
  return useMutation({
    // Revoke sets revoked_at and deactivates the linked service user; the
    // client row stays visible in the list until explicitly deleted.
    mutationFn: (id) => api.post(`/api-clients/${id}/revoke`).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['api-clients'] }),
  })
}

export function useDeleteApiClient() {
  const qc = useQueryClient()
  return useMutation({
    // Backend enforces the client must already be revoked (400 otherwise).
    mutationFn: (id) => api.delete(`/api-clients/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['api-clients'] }),
  })
}
