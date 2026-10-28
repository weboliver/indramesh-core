import { useState } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useRole, useUpdateRole, useDeleteRole } from '../../shared/hooks/use-roles'
import { Can, useCan } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import PageHeader from '../../shared/components/PageHeader'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import EmptyState from '../../shared/components/EmptyState'
import TableSkeleton from '../../shared/components/TableSkeleton'
import RoleForm from './RoleForm'
import RoleUserAssignmentList from './RoleUserAssignmentList'
import RoleGroupAssignmentList from './RoleGroupAssignmentList'
import Shield from 'lucide-react/dist/esm/icons/shield'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

function RoleDetailContent() {
  const { t } = useTranslation()
  const { id } = useParams()
  const navigate = useNavigate()
  const { data: role, isLoading, isError } = useRole(id)
  const updateRole = useUpdateRole()
  const deleteRole = useDeleteRole()
  const canManage = useCan('roles', 'manage')

  const [isEditing, setIsEditing] = useState(false)
  const [deleting, setDeleting] = useState(false)

  if (isLoading) {
    return <TableSkeleton rows={4} cols={1} />
  }

  if (isError || !role) {
    return (
      <EmptyState
        variant="error"
         title={t('admin.roles.not_found_title', 'Role not found')}
         description={t('admin.roles.not_found_description', 'This role could not be loaded.')}
      />
    )
  }

  const handleUpdate = async (values) => {
    await updateRole.mutateAsync({ id: role.id, ...values })
    setIsEditing(false)
  }

  const handleDelete = async () => {
     if (!(await confirmDialog({ message: interpolateDefault(t('admin.roles.confirm_delete', 'Delete role "{{name}}"? This cannot be undone.'), { name: role.name }), danger: true }))) return
    setDeleting(true)
    try {
      await deleteRole.mutateAsync(role.id)
      navigate('/roles')
    } finally {
      setDeleting(false)
    }
  }

  return (
    <div className="space-y-6" data-testid="role-detail">
      <Breadcrumb items={[
        { label: t('admin.roles.title', 'Roles'), href: '/roles' },
        { label: role.name },
      ]} />

      <PageHeader
        title={
          <span className="inline-flex items-center gap-1.5">
            {role.name}
            {role.is_system && (
              <Shield
                size={18}
                className="text-muted-foreground"
                 aria-label={t('admin.roles.system_role_label', 'System role — cannot be deleted')}
                data-testid="system-role-badge"
              />
            )}
          </span>
        }
        description={role.description}
      >
        <div className="flex gap-2">
          <Can resource="roles" action="update">
            <Button asChild variant="outline" size="sm">
               <Link to={`/configuration/permissions?role=${role.id}`}>{t('common.permissions', 'Permissions')}</Link>
            </Button>
          </Can>
          <Can resource="roles" action="delete">
            {!role.is_system && (
              <Button
                variant="destructive"
                size="sm"
                onClick={handleDelete}
                disabled={deleting}
                loading={deleting}
              >
                {t('common.delete', 'Delete')}
              </Button>
            )}
          </Can>
        </div>
      </PageHeader>

      <Can resource="roles" action="update">
        <div className="space-y-2">
          {isEditing ? (
            <RoleForm
              mode="edit"
              initialValues={role}
              onSubmit={handleUpdate}
              onCancel={() => setIsEditing(false)}
            />
          ) : (
            <Button variant="outline" size="sm" onClick={() => setIsEditing(true)}>
               {t('common.edit', 'Edit')}
            </Button>
          )}
        </div>
      </Can>

      <div className="space-y-2">
         <h2 className="text-sm font-semibold">{t('admin.roles.assigned_users', 'Assigned Users')}</h2>
        <RoleUserAssignmentList role={role} canManage={canManage} />
      </div>

      <div className="space-y-2">
         <h2 className="text-sm font-semibold">{t('admin.roles.assigned_groups', 'Assigned Groups')}</h2>
        <RoleGroupAssignmentList role={role} canManage={canManage} />
      </div>
    </div>
  )
}

export default function RoleDetail() {
  const { t } = useTranslation()
  const canRead = useCan('roles', 'read')

  return (
    <ErrorBoundary>
      <Can resource="roles" action="read">
        <RoleDetailContent />
      </Can>
      {!canRead && (
        <EmptyState
          variant="error"
           title={t('common.access_denied', 'Access denied')}
           description={t('admin.roles.detail_access_denied_description', 'You do not have permission to view this role.')}
        />
      )}
    </ErrorBoundary>
  )
}
