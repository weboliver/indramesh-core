import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function useSchemaTemplates() {
  return useQuery({
    queryKey: ['schema-templates'],
    queryFn: () => api.get('/schema-templates/', { params: { limit: 100 } }).then(r => r.data),
    staleTime: 30_000,
  })
}

export function useDeleteSchemaTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (templateId) => api.delete(`/schema-templates/${templateId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['schema-templates'] }),
  })
}

export function useInstantiateTemplate() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ templateId, sourceId, name }) =>
      api.post(`/schema-templates/${templateId}/instantiate`, {
        source_id: sourceId,
        ...(name ? { name } : {}),
      }).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['schemas'] })
      qc.invalidateQueries({ queryKey: ['schema-templates'] })
    },
  })
}
