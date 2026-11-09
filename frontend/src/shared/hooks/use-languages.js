import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import { createCrudHooks } from './create-crud-hooks'
import api from './use-api'

const { useList, useCreate, useUpdate, useDelete } = createCrudHooks(
  'language',
  '/languages/',
  'languages',
  { listEnabled: true }
)

// Default limit 200 mirrors the backend's reference default and
// use-ui-language.jsx: the admin table has no pagination, so a 50-row cut would
// silently hide (and therefore make undeletable) the 51st language onward.
export function useLanguages(skip = 0, limit = 200, options) {
  return useList(undefined, skip, limit, options)
}
export function useCreateLanguage() {
  return useCreate()
}
export function useUpdateLanguage() {
  return useUpdate()
}
export function useDeleteLanguage() {
  return useDelete()
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
