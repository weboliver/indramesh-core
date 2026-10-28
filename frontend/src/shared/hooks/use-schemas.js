import { createCrudHooks } from './create-crud-hooks'

const { useList, useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'schema',
  '/schemas/',
  'schemas',
  { filterKey: 'sourceId', filterParam: 'source_id' }
)

export function useSchemas(sourceId, skip = 0, limit = 50, options) {
  return useList(sourceId, skip, limit, options)
}

export function useSchema(id) {
  return useGet(id)
}

export function useCreateSchema() {
  return useCreate()
}

export function useUpdateSchema() {
  return useUpdate()
}

export function useDeleteSchema() {
  return useDelete()
}
