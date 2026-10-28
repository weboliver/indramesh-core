import { createCrudHooks } from './create-crud-hooks'

const { useList, useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'source',
  '/sources/',
  'sources',
  { listEnabled: true }
)

export function useSources(skip = 0, limit = 1000, options) {
  return useList(undefined, skip, limit, options)
}

export function useSource(id) {
  return useGet(id)
}

export function useCreateSource() {
  return useCreate()
}

export function useUpdateSource() {
  return useUpdate()
}

export function useDeleteSource() {
  return useDelete()
}
