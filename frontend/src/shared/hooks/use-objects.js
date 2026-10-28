import { useQuery, useMutation, useQueryClient, keepPreviousData } from '@tanstack/react-query'
import api from './use-api'

export function useObjects(objectTypeId, skip = 0, limit = 50, attrFilters = null, q = null, filterTree = null, fields = null, lang = null) {
  return useQuery({
    queryKey: ['objects', { objectTypeId, skip, limit, attrFilters, q, filterTree, lang }],
    queryFn: () => {
      const params = { object_type_id: objectTypeId, skip, limit }
      if (fields) {
        params.fields = fields
      }
      if (!filterTree && attrFilters && Object.keys(attrFilters).length > 0) {
        params.attr_filters = JSON.stringify(attrFilters)
      }
      if (filterTree) {
        params.filter_tree = typeof filterTree === 'string' ? filterTree : JSON.stringify(filterTree)
      }
      if (q && q.length >= 2) {
        params.q = q
      }
      if (lang) {
        params.lang = lang
      }
      return api.get('/objects/', { params }).then(r => ({
        items: r.data,
        total: parseInt(r.headers['x-total-count'] || '0', 10),
      }))
    },
    placeholderData: keepPreviousData,
    enabled: !!objectTypeId,
  })
}

// 290 D-11: attributes whose external marker a per-object release (e.g. a
// detached Jira object) lifts — the server-side projection of the write check.
export function useReleasedAttributeIds(id) {
  return useQuery({
    queryKey: ['objects', { id, released: true }],
    queryFn: () => api.get(`/objects/${id}/released-attributes`).then(r => r.data),
    enabled: !!id,
  })
}

export function useObject(id, lang) {
  return useQuery({
    queryKey: ['objects', { id, lang }],
    queryFn: () => api.get(`/objects/${id}`, { params: lang ? { lang } : {} }).then(r => r.data),
    enabled: !!id,
    retry: (failureCount, error) => {
      if (error?.response?.status === 404) return false
      return failureCount < 3
    },
  })
}
export function useDeleteObject() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/objects/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['objects'] }),
  })
}
