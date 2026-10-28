import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import api from './use-api'

export function useAuditLogs({ entity_type, object_id, start_date, end_date, actor_q, page, page_size } = {}) {
  return useQuery({
    queryKey: ['audit-logs', { entity_type, object_id, start_date, end_date, actor_q, page, page_size }],
    queryFn: () => api.get('/audit-logs', {
      params: { entity_type, object_id, start_date, end_date, actor_q, page, page_size },
    }).then(r => r.data),
    placeholderData: keepPreviousData,
  })
}

export function useObjectHistory(objectId, { page = 1, page_size = 25 } = {}) {
  return useQuery({
    queryKey: ['audit-logs', 'object-history', objectId, page, page_size],
    queryFn: () => api.get('/audit-logs', {
      params: { object_id: objectId, page, page_size },
    }).then(r => r.data),
    enabled: !!objectId,
    placeholderData: keepPreviousData,
  })
}

export function useAuditLogConfig() {
  return useQuery({
    queryKey: ['audit-log-config'],
    queryFn: () => api.get('/audit-log-config').then(r => r.data),
    staleTime: 60000,
  })
}

export function useUpdateAuditLogConfig() {
  const queryClient = useQueryClient()

  return useMutation({
    mutationFn: ({ entity_type, enabled }) =>
      api.patch(`/audit-log-config/${entity_type}`, { audit_log_enabled: enabled }),

    onMutate: async ({ entity_type, enabled }) => {
      await queryClient.cancelQueries({ queryKey: ['audit-log-config'] })
      const previous = queryClient.getQueryData(['audit-log-config'])
      queryClient.setQueryData(['audit-log-config'], (old) => ({
        ...old,
        [entity_type]: enabled,
      }))
      return { previous }
    },

    onError: (_err, _vars, context) => {
      if (context?.previous) {
        queryClient.setQueryData(['audit-log-config'], context.previous)
      }
    },

    onSettled: () => {
      queryClient.invalidateQueries({ queryKey: ['audit-log-config'] })
    },
  })
}
