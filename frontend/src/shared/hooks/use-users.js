import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query'
import api, { serializeRepeatedParams } from './use-api'
import { createCrudHooks } from './create-crud-hooks'

const { useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'user',
  '/users/',
  'users',
  {}
)

// roleId/assigned (options) is the roles-editor "assigned"/"not assigned"
// filter — both must be given together (assigned undefined/null -> no
// server-side filter beyond q), mirroring use-groups.js's useGroups.
export function useUsers(q, skip = 0, limit = 25, options = {}) {
  const { roleId, assigned } = options
  return useQuery({
    queryKey: ['users', { q, skip, limit, roleId, assigned }],
    queryFn: () => {
      const params = { skip, limit }
      if (q) {
        params.q = q
      }
      if (roleId) {
        params.role_id = roleId
      }
      if (assigned !== undefined && assigned !== null) {
        params.assigned = assigned
      }
      return api.get('/users/', { params }).then(r => ({
        items: r.data,
        total: parseInt(r.headers['x-total-count'] || '0', 10),
      }))
    },
  })
}

// GET /users/picker (245-05 D-03/D-07) — content-gated (objects:read) source
// for the USER attribute value picker AND its resolved-name read-back, so
// editors/viewers never hit the admin-gated GET /users/ list (would 403).
// ids present -> exact-match read-back resolution ignoring q/skip/limit
// (mirrors use-groups.js's useGroups ids-filter convention). groupId/assigned
// is the group-editor "assigned"/"not assigned" member filter.
export function useUserPicker(q, ids, skip = 0, limit = 25, options = {}) {
  const { enabled = true, groupId, assigned } = options
  const idsKey = ids && ids.length ? [...ids].map(String).sort().join(',') : null
  return useQuery({
    queryKey: ['users-picker', { q, ids: idsKey, skip, limit, groupId, assigned }],
    queryFn: () => {
      const params = ids && ids.length
        ? { ids }
        : {
            skip, limit,
            ...(q ? { q } : {}),
            ...(groupId ? { group_id: groupId } : {}),
            ...(assigned !== undefined && assigned !== null ? { assigned } : {}),
          }
      return api.get('/users/picker', { params, paramsSerializer: serializeRepeatedParams }).then(r => ({
        items: r.data,
        total: parseInt(r.headers['x-total-count'] || '0', 10),
      }))
    },
    enabled: enabled && (ids ? ids.length > 0 : true),
  })
}

export function useUser(id) {
  return useGet(id)
}

export function useCreateUser() {
  return useCreate()
}

export function useUpdateUser() {
  return useUpdate()
}

export function useDeleteUser() {
  return useDelete()
}

export function useAssignRole() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, roleId }) => api.post(`/users/${userId}/roles`, { role_id: roleId }).then(r => r.data),
    onSuccess: (data, { userId }) => {
      qc.invalidateQueries({ queryKey: ['users', userId] })
      qc.invalidateQueries({ queryKey: ['users'] })
    },
  })
}

export function useRemoveRole() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, roleId }) => api.delete(`/users/${userId}/roles/${roleId}`),
    onSuccess: (data, { userId }) => {
      qc.invalidateQueries({ queryKey: ['users', userId] })
      qc.invalidateQueries({ queryKey: ['users'] })
    },
  })
}
