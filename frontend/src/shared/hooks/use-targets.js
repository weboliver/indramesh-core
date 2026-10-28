import { createCrudHooks } from './create-crud-hooks'

const { useList, useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'target',
  '/targets/',
  'targets',
  { listEnabled: true }
)

export function useTargets(skip = 0, limit = 50, options) {
  return useList(undefined, skip, limit, options)
}

export function useTarget(id) {
  return useGet(id)
}

export function useCreateTarget() {
  return useCreate()
}

export function useUpdateTarget() {
  return useUpdate()
}

export function useDeleteTarget() {
  return useDelete()
}
