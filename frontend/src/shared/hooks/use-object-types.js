import { useQuery } from '@tanstack/react-query'
import { createCrudHooks } from './create-crud-hooks'
import api from './use-api'

const { useList, useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'object-type',
  '/object-types/',
  'object-types',
  { filterKey: 'schemaId', filterParam: 'schema_id', updateRefetchType: 'all' }
)

export function useObjectTypes(schemaId, skip = 0, limit = 50, options = {}) {
  const { lang, ...rest } = options
  const extraParams = lang ? { lang } : {}
  return useList(schemaId, skip, limit, { staleTime: 60_000, extraParams, ...rest })
}

export function useObjectType(id, lang) {
  const fallback = useGet(!lang ? id : undefined)
  const withLang = useQuery({
    queryKey: ['object-types', { id, lang }],
    queryFn: () => api.get(`/object-types/${id}`, { params: lang ? { lang } : {} }).then(r => r.data),
    enabled: !!id && !!lang,
  })
  return lang ? withLang : fallback
}

export function useCreateObjectType() {
  return useCreate()
}

export function useUpdateObjectType() {
  return useUpdate()
}

export function useDeleteObjectType() {
  return useDelete()
}
