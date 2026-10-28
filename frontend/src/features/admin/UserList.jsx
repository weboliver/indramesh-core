import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useUsers, useCreateUser, useUpdateUser, useDeleteUser, useAssignRole } from '../../shared/hooks/use-users'
import { useAssignUserGroup } from '../../shared/hooks/use-groups'
import { useAuth } from '../../shared/hooks/use-auth'
import { Button } from '../../shared/components/Button'
import { Can } from '../../shared/components/Can'
import UserForm from './UserForm'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import Users from 'lucide-react/dist/esm/icons/users'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

export default function UserListPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const { user: currentUser } = useAuth()
  const [showCreate, setShowCreate] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [deletingId, setDeletingId] = useState(null)
  const skip = (page - 1) * pageSize

  // D-02 standard search adapter for a server-q surface (manifest #03): the
  // existing useUsers query argument is driven by a URL-synced q that preserves
  // unrelated params and resets page to 1 on change or clear.
  const q = searchParams.get('q') || ''
  const setQ = useCallback((value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      next.set('page', '1')
      return next
    }, { replace })
  }, [setSearchParams])

  const { data: userData, isLoading } = useUsers(q, skip, pageSize)
  const createUser = useCreateUser()
  const assignGroup = useAssignUserGroup()
  const assignRole = useAssignRole()
  const updateUser = useUpdateUser()
  const deleteUser = useDeleteUser()
  const users = userData?.items || []
  const total = userData?.total || 0

  const handleCreate = async (values, { groupIds = [], roleIds = [] } = {}) => {
    const created = await createUser.mutateAsync(values)
    for (const roleId of roleIds) {
      await assignRole.mutateAsync({ userId: created.id, roleId })
    }
    for (const groupId of groupIds) {
      await assignGroup.mutateAsync({ userId: created.id, groupId })
    }
    setShowCreate(false)
  }

  const handleUpdate = async (user, values) => {
    await updateUser.mutateAsync({ id: user.id, ...values })
    setEditingId(null)
  }

  const handleDelete = async (user) => {
     if (!(await confirmDialog({ message: interpolateDefault(t('admin.users.confirm_delete', 'Delete user "{{name}}"? This cannot be undone.'), { name: user.username }), danger: true }))) return
    setDeletingId(user.id)
    try {
      await deleteUser.mutateAsync(user.id)
    } finally {
      setDeletingId(null)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="user-list">
         <PageHeader title={t('admin.users.title', 'Users')} description={t('admin.users.description', 'Manage user accounts and role assignments')} icon="users">
          <Can resource="users" action="create">
            <Button onClick={() => setShowCreate(true)} variant="primary" size="sm">
               {t('admin.users.create', '+ Create User')}
            </Button>
          </Can>
        </PageHeader>

        <Can resource="users" action="read">
          <div className="space-y-4">
            {showCreate && (
              <UserForm
                mode="create"
                showGroups
                onSubmit={handleCreate}
                onCancel={() => setShowCreate(false)}
              />
            )}

            <TableToolbar
              urlQ={q}
              setQ={setQ}
              placeholder={t('admin.users.search_placeholder', 'Search Users...')}
              ariaLabel={t('admin.users.search_label', 'Search Users')}
              searchTestId="user-search-input"
            />

            {isLoading ? (
              <TableSkeleton rows={5} cols={5} />
            ) : total === 0 && !q ? (
              <EmptyState
                variant="empty"
                icon={Users}
                 title={t('admin.users.empty_title', 'No users yet')}
                 description={t('admin.users.empty_description', 'There are no users yet. Create the first user to get started.')}
                 buttonLabel={t('admin.users.create_plain', 'Create User')}
                onButtonClick={() => setShowCreate(true)}
              />
            ) : users.length === 0 ? (
              <EmptyState
                variant="no-results"
                title={t('admin.users.no_results_title', 'No matching users')}
                description={t('admin.users.no_results_description', 'Try adjusting your search or filters.')}
              />
            ) : (
              <div className="space-y-3">
                <Table>
                  <thead className="sticky top-0 z-10">
                    <tr className="border-b bg-background">
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.username', 'Username')}</th>
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.email', 'Email')}</th>
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.status', 'Status')}</th>
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('admin.users.roles', 'Roles')}</th>
                      <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {users.map((user) => {
                      const isSelf = currentUser?.id === user.id
                      if (editingId === user.id) {
                        return (
                          <tr key={user.id} className="border-b last:border-0" data-testid="user-row">
                            <td colSpan={5} className="px-4 py-2">
                              <UserForm
                                mode="edit"
                                initialValues={user}
                                disableIsSuperuser={isSelf}
                                showGroups
                                isSelf={isSelf}
                                currentIsSuperuser={!!currentUser?.is_superuser}
                                onSubmit={(values) => handleUpdate(user, values)}
                                onCancel={() => setEditingId(null)}
                              />
                            </td>
                          </tr>
                        )
                      }
                      return (
                      <tr key={user.id} className="border-b last:border-0 hover:bg-muted/30" data-testid="user-row">
                        <td className="px-4 py-3 text-sm font-semibold">
                          {user.username}
                        </td>
                        <td className="px-4 py-3 text-sm text-muted-foreground">{user.email}</td>
                        <td className="px-4 py-3">
                          <div className="flex items-center gap-1.5">
                            <span
                              className={`rounded-full px-2.5 py-0.5 text-xs font-normal ${
                                user.is_active
                                  ? 'bg-green-100 text-green-800 dark:bg-green-900/30 dark:text-green-400'
                                  : 'bg-gray-100 text-gray-800 dark:bg-gray-800 dark:text-gray-300'
                              }`}
                            >
                               {user.is_active ? t('common.status.active', 'Active') : t('common.status.inactive', 'Inactive')}
                            </span>
                            {user.is_superuser && (
                              <span className="rounded-full px-2.5 py-0.5 text-xs font-normal bg-primary/10 text-primary">
                                 {t('admin.users.superuser', 'Superuser')}
                              </span>
                            )}
                          </div>
                        </td>
                        <td className="px-4 py-3 text-sm text-muted-foreground">
                          {(user.roles || []).join(', ') || '-'}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <Can resource="users" action="update">
                              <Button
                                type="button"
                                variant="primary"
                                size="sm"
                                onClick={() => setEditingId(user.id)}
                              >
                                 {t('common.edit', 'Edit')}
                              </Button>
                            </Can>
                            <Can resource="users" action="delete">
                              <Button
                                type="button"
                                variant="destructive"
                                size="sm"
                                onClick={() => handleDelete(user)}
                                disabled={isSelf || deletingId === user.id}
                                loading={deletingId === user.id}
                                 title={isSelf ? t('admin.users.cannot_delete_self', 'You cannot delete your own account') : undefined}
                              >
                                 {t('common.delete', 'Delete')}
                              </Button>
                            </Can>
                          </div>
                        </td>
                      </tr>
                      )
                    })}
                  </tbody>
                </Table>
                <PaginationBar
                  page={page}
                  pageSize={pageSize}
                  total={total}
                  onPageChange={setPage}
                  onPageSizeChange={setPageSize}
                />
              </div>
            )}
          </div>
        </Can>
      </div>
    </ErrorBoundary>
  )
}
