import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useUser, useUpdateUser, useDeleteUser } from '../../shared/hooks/use-users'
import { useAuth } from '../../shared/hooks/use-auth'
import { Can, useCan } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import PageHeader from '../../shared/components/PageHeader'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import EmptyState from '../../shared/components/EmptyState'
import TableSkeleton from '../../shared/components/TableSkeleton'
import UserForm from './UserForm'
import RoleAssignmentList from './RoleAssignmentList'
import GroupAssignmentList from './GroupAssignmentList'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

function UserDetailContent() {
  const { t } = useTranslation()
  const { id } = useParams()
  const navigate = useNavigate()
  const { user: currentUser } = useAuth()
  const { data: user, isLoading, isError } = useUser(id)
  const updateUser = useUpdateUser()
  const deleteUser = useDeleteUser()
  const canManageRoles = useCan('users', 'manage')
  const canManageGroups = useCan('groups', 'manage')

  const [isEditing, setIsEditing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  if (isLoading) {
    return <TableSkeleton rows={4} cols={1} />
  }

  if (isError || !user) {
    return (
      <EmptyState
        variant="error"
         title={t('admin.users.not_found_title', 'User not found')}
         description={t('admin.users.not_found_description', 'This user could not be loaded.')}
      />
    )
  }

  const roles = user.roles || []
  const isSelf = currentUser?.id === user.id
  // WR-04: use the backend's effective role count (direct + group-inherited,
  // the same figure the self-lockout guard actually authorizes against)
  // rather than roles.length, which only counts direct roles and can
  // disagree with the backend in both directions. Falls back to
  // roles.length if the API hasn't been redeployed yet (effective_role_count
  // undefined) so the guard degrades to the old approximation instead of
  // disabling nothing.
  const effectiveRoleCount = user.effective_role_count ?? roles.length
  const lastRoleGuard = isSelf && !currentUser?.is_superuser && effectiveRoleCount <= 1

  const handleUpdate = async (values) => {
    await updateUser.mutateAsync({ id: user.id, ...values })
    setIsEditing(false)
  }

  const handleDelete = async () => {
     if (!(await confirmDialog({ message: interpolateDefault(t('admin.users.confirm_delete', 'Delete user "{{name}}"? This cannot be undone.'), { name: user.username }), danger: true }))) return
    setDeleting(true)
    try {
      await deleteUser.mutateAsync(user.id)
      navigate('/users')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-6" data-testid="user-detail">
      <Breadcrumb items={[
        { label: t('admin.users.title', 'Users'), href: '/users' },
        { label: user.username },
      ]} />
      <PageHeader title={user.username} description={user.email}>
        <Can resource="users" action="delete">
          <Button
            variant="destructive"
            size="sm"
            onClick={handleDelete}
            disabled={deleting}
            loading={deleting}
          >
             {t('common.delete', 'Delete')}
          </Button>
        </Can>
      </PageHeader>

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

      <Can resource="users" action="update">
        <div className="space-y-2">
          {isEditing ? (
            <UserForm
              mode="edit"
              initialValues={user}
              onSubmit={handleUpdate}
              onCancel={() => setIsEditing(false)}
              disableIsActive={isSelf}
              disableIsSuperuser={isSelf}
            />
          ) : (
            <Button variant="outline" size="sm" onClick={() => setIsEditing(true)}>
               {t('common.edit', 'Edit')}
            </Button>
          )}
        </div>
      </Can>

      <div className="space-y-2">
         <h2 className="text-sm font-semibold">{t('admin.users.roles', 'Roles')}</h2>
        <RoleAssignmentList
          userId={user.id}
          assignedRoleNames={roles}
          canManage={canManageRoles}
          disableRemoveGuard={lastRoleGuard}
        />
      </div>

      <div className="space-y-2">
        <h2 className="text-sm font-semibold">{t('admin.users.groups', 'Groups')}</h2>
        <GroupAssignmentList
          userId={user.id}
          canManage={canManageGroups}
          isSelf={isSelf}
          isSuperuser={currentUser?.is_superuser}
        />
      </div>
    </div>
  )
}

export default function UserDetail() {
  const { t } = useTranslation()
  const canRead = useCan('users', 'read')

  return (
    <ErrorBoundary>
      <Can resource="users" action="read">
        <UserDetailContent />
      </Can>
      {!canRead && (
        <EmptyState
          variant="error"
           title={t('common.access_denied', 'Access denied')}
           description={t('admin.users.access_denied_description', 'You do not have permission to view this user.')}
        />
      )}
    </ErrorBoundary>
  )
}
