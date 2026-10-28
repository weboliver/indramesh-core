import { useMutation, useQueryClient } from '@tanstack/react-query'
import api from './use-api'
import { createCrudHooks } from './create-crud-hooks'

const { useList, useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'role',
  '/roles/',
  'roles',
  { listEnabled: true }
)

export function useRoles(skip = 0, limit = 50, options) {
  return useList(undefined, skip, limit, options)
}

export function useRole(id) {
  return useGet(id)
}

export function useCreateRole() {
  return useCreate()
}

export function useUpdateRole() {
  return useUpdate()
}

export function useDeleteRole() {
  return useDelete()
}

export function useAssignPermission() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ roleId, permissionId, isDeny }) =>
      api.post(`/roles/${roleId}/permissions`, { permission_id: permissionId, is_deny: isDeny }).then(r => r.data),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['roles'] }),
  })
}

export function useRemovePermission() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ roleId, permissionId }) => api.delete(`/roles/${roleId}/permissions/${permissionId}`),
    onSuccess: () => qc.invalidateQueries({ queryKey: ['roles'] }),
  })
}
