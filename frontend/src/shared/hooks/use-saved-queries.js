import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'

export function useSavedQueries(skip = 0, limit = 25, name = '') {
  const params = { skip, limit }
  if (name) params.name = name
  return useQuery({
    queryKey: ['saved-queries', skip, limit, name],
    queryFn: () => api.get('/saved-queries/', { params }).then(r => ({
      items: r.data,
      total: parseInt(r.headers['x-total-count'] || '0', 10),
    })),
    staleTime: 10_000,
  })
}

export function useSaveQuery() {
  const qc = useQueryClient()
  return useMutation({
    // 304 D-01/D-02: the optional `variables` object is the query's stored
    // default variables. Omitted (undefined) must not null stored defaults.
    mutationFn: ({ id, name, queryText, variables }) => {
      const payload = { name, query_text: queryText, variables }
      if (id) {
        return api.patch(`/saved-queries/${id}`, payload).then(r => r.data)
      }
      return api.post('/saved-queries/', payload).then(r => r.data)
    },
    onSuccess: () => qc.invalidateQueries({ queryKey: ['saved-queries'] }),
  })
}

export function useDeleteSavedQuery() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (id) => api.delete(`/saved-queries/${id}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['saved-queries'] }),
  })
}

// 304 GQL-02/D-06: run a stored query in-process. GraphQL-level failures stay
// HTTP 200 with a {data, errors} envelope; request-level failures are 4xx.
export function useExecuteSavedQuery() {
  return useMutation({
    mutationFn: ({ id, variables, operationName }) =>
      api.post(`/saved-queries/${id}/execute`, {
        variables: variables ?? null,
        operation_name: operationName || null,
      }).then(r => r.data),
  })
}

// 304 D-08/D-11: versioned, id-free export/import document.
export function fetchSavedQueriesExport() {
  return api.get('/saved-queries/export').then(r => r.data)
}

export function importSavedQueries(document) {
  return api.post('/saved-queries/import', document).then(r => r.data)
}