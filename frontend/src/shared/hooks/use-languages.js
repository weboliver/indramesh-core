import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { createCrudHooks } from './create-crud-hooks'
import api from './use-api'

const { useList, useUpdate } = createCrudHooks(
  'language',
  '/languages/',
  'languages',
  { listEnabled: true }
)

export function useLanguages(skip = 0, limit = 50, options) {
  return useList(undefined, skip, limit, options)
}
export function useUpdateLanguage() {
  return useUpdate()
}
export function useSchemaLanguages(schemaId) {
  return useQuery({
    queryKey: ['schema-languages', schemaId],
    queryFn: () => api.get(`/schemas/${schemaId}/languages`).then(r => r.data),
    enabled: !!schemaId,
  })
}

export function useSetSchemaLanguages(schemaId) {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: (language_codes) => api.put(`/schemas/${schemaId}/languages`, { language_codes }).then(r => r.data),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['schema-languages', schemaId] })
      qc.invalidateQueries({ queryKey: ['schemas'] })
    },
  })
}
