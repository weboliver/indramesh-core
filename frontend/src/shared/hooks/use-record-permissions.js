import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function useEffectivePermissions(entityType, recordId) {
  return useQuery({
    queryKey: ['record-permissions', 'effective', entityType, recordId],
    queryFn: () => api.get('/record-permissions/effective', {
      params: { record_entity_type: entityType, record_id: recordId }
    }).then(r => r.data),
    enabled: !!entityType && !!recordId,
  })
}

// EffectivePermissionItem entries (from useEffectivePermissions) carry no `id` —
// they're a resolved/deduplicated view, not raw rows. Direct entries need the
// underlying RecordPermission row id to be deletable, so the dialog also fetches
// the raw direct-only list (scoped to this exact record) and maps by
// (role_id, action, is_deny) to recover the id for the delete mutation.
export function useDirectRecordPermissions(entityType, recordId) {
  return useQuery({
    queryKey: ['record-permissions', 'direct', entityType, recordId],
    queryFn: () => api.get('/record-permissions/', {
      params: { record_entity_type: entityType, record_id: recordId, limit: 100 }
    }).then(r => r.data),
    enabled: !!entityType && !!recordId,
  })
}

export function useCreateRecordPermission() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (data) => api.post('/record-permissions/', data).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['record-permissions'] }),
  })
}

export function useDeleteRecordPermission() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/record-permissions/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['record-permissions'] }),
  })
}
