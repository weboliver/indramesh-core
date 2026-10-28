import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import api, { serializeRepeatedParams } from './use-api'
import { createCrudHooks } from './create-crud-hooks'

const { useGet, useCreate, useUpdate, useDelete } = createCrudHooks(
  'group',
  '/groups/',
  'groups',
  {}
)

// GET /groups/ with q + ids filters (245-05 D-03: group picker data source and
// group-value read-back resolution share this one hook/endpoint). ids present
// -> exact-match read-back resolution ignoring q/skip/limit (mirrors
// use-users.js's useUserPicker ids-filter convention). roleId/assigned is the
// roles-editor "assigned"/"not assigned" group filter.
export function useGroups(q, ids, skip = 0, limit = 25, options = {}) {
  const { enabled = true, roleId, assigned } = options
  const idsKey = ids && ids.length ? [...ids].map(String).sort().join(',') : null
  return useQuery({
    queryKey: ['groups', { q, ids: idsKey, skip, limit, roleId, assigned }],
    queryFn: () => {
      const params = ids && ids.length
        ? { ids }
        : {
            skip, limit,
            ...(q ? { q } : {}),
            ...(roleId ? { role_id: roleId } : {}),
            ...(assigned !== undefined && assigned !== null ? { assigned } : {}),
          }
      return api.get('/groups/', { params, paramsSerializer: serializeRepeatedParams }).then(r => ({
        items: r.data,
        total: parseInt(r.headers['x-total-count'] || '0', 10),
      }))
    },
    enabled: enabled && (ids ? ids.length > 0 : true),
  })
}

export function useGroup(id) {
  return useGet(id)
}

export function useCreateGroup() {
  return useCreate()
}

export function useUpdateGroup() {
  return useUpdate()
}

export function useDeleteGroup() {
  return useDelete()
}

// ── Group members (D-14/D-17) ──────────────────────────────────────────────
// GET /groups/{id}/members returns List[UserResponse]. The checkbox list on
// GroupDetail renders ALL users (useUsers) and checks membership against this
// member-id set, mirroring RoleAssignmentList.
export function useGroupMembers(groupId, skip = 0, limit = 100) {
  return useQuery({
    queryKey: ['groups', groupId, 'members', { skip, limit }],
    queryFn: () => api.get(`/groups/${groupId}/members`, { params: { skip, limit } }).then(r => r.data),
    enabled: !!groupId,
  })
}

function invalidateGroupMemberQueries(qc, groupId, userId) {
  qc.invalidateQueries({ queryKey: ['groups', groupId, 'members'] })
  if (userId) qc.invalidateQueries({ queryKey: ['users', userId, 'groups'] })
  qc.invalidateQueries({ queryKey: ['groups'] })
}

export function useAssignGroupMember() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ groupId, userId }) =>
      api.post(`/groups/${groupId}/members/${userId}`).then(r => r.data),
    onSuccess: (data, { groupId, userId }) => invalidateGroupMemberQueries(qc, groupId, userId),
  })
}

export function useRemoveGroupMember() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ groupId, userId }) => api.delete(`/groups/${groupId}/members/${userId}`),
    onSuccess: (data, { groupId, userId }) => invalidateGroupMemberQueries(qc, groupId, userId),
  })
}

// ── Group->role assignment read (246-04 GET /groups/{id}/roles, D-17) ─────
export function useGroupRoles(groupId) {
  return useQuery({
    queryKey: ['groups', groupId, 'roles'],
    queryFn: () => api.get(`/groups/${groupId}/roles`).then(r => r.data),
    enabled: !!groupId,
  })
}

function groupRoleInvalidate(qc, groupId, roleId) {
  qc.invalidateQueries({ queryKey: ['groups', groupId, 'roles'] })
  if (roleId) qc.invalidateQueries({ queryKey: ['roles', roleId, 'groups'] })
  qc.invalidateQueries({ queryKey: ['groups'] })
}

export function useAssignGroupRole() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ groupId, roleId }) => api.post(`/groups/${groupId}/roles/${roleId}`).then(r => r.data),
    onSuccess: (data, { groupId, roleId }) => groupRoleInvalidate(qc, groupId, roleId),
  })
}

export function useRemoveGroupRole() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ groupId, roleId }) => api.delete(`/groups/${groupId}/roles/${roleId}`),
    onSuccess: (data, { groupId, roleId }) => groupRoleInvalidate(qc, groupId, roleId),
  })
}

// ── User-oriented group membership (D-14/D-17): GET /users/{id}/groups ─────
export function useUserGroups(userId) {
  return useQuery({
    queryKey: ['users', userId, 'groups'],
    queryFn: () => api.get(`/users/${userId}/groups`).then(r => r.data),
    enabled: !!userId,
  })
}

function userGroupInvalidate(qc, userId, groupId) {
  qc.invalidateQueries({ queryKey: ['users', userId, 'groups'] })
  if (groupId) qc.invalidateQueries({ queryKey: ['groups', groupId, 'members'] })
  qc.invalidateQueries({ queryKey: ['users'] })
}

export function useAssignUserGroup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, groupId }) => api.post(`/users/${userId}/groups/${groupId}`).then(r => r.data),
    onSuccess: (data, { userId, groupId }) => userGroupInvalidate(qc, userId, groupId),
  })
}

export function useRemoveUserGroup() {
  const qc = useQueryClient()
  return useMutation({
    mutationFn: ({ userId, groupId }) => api.delete(`/users/${userId}/groups/${groupId}`),
    onSuccess: (data, { userId, groupId }) => userGroupInvalidate(qc, userId, groupId),
  })
}

// ── Role editor: groups holding a role (246-04 GET /roles/{id}/groups) ─────
export function useRoleGroups(roleId) {
  return useQuery({
    queryKey: ['roles', roleId, 'groups'],
    queryFn: () => api.get(`/roles/${roleId}/groups`).then(r => r.data),
    enabled: !!roleId,
  })
}