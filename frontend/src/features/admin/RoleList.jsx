import { useState, useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useRoles, useCreateRole, useDeleteRole,
} from '../../shared/hooks/use-roles'
import { Button } from '../../shared/components/Button'
import { Can, useCan } from '../../shared/components/Can'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { PaginationBar } from '../../shared/components/PaginationBar'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import RoleForm from './RoleForm'
import Shield from 'lucide-react/dist/esm/icons/shield'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

export default function RoleListPage() {
  const { t } = useTranslation()
  const canRead = useCan('roles', 'read')
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const { data: roleData, isLoading } = useRoles(skip, pageSize, { includeTotal: true, enabled: canRead })
  const roles = roleData?.items || []
  const total = roleData?.total || 0

  // D-02 standard search adapter for a local loaded-list filter surface
  // (manifest #02): the query is URL-synced q via useSearchParams, preserves
  // unrelated params, and resets page to 1 on change or clear. The loaded
  // current page is filtered in memory — no backend search contract.
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

  const query = q.trim().toLowerCase()
  const filteredRoles = query
    ? roles.filter((role) =>
        (role.name || '').toLowerCase().includes(query) ||
        (role.description || '').toLowerCase().includes(query)
      )
    : roles

  const createRole = useCreateRole()
  const deleteRole = useDeleteRole()

  const [showCreate, setShowCreate] = useState(false)
  const [actionLoading, setActionLoading] = useState(null)

  const handleCreate = async (values) => {
    await createRole.mutateAsync(values)
    setShowCreate(false)
  }

  const handleDelete = async (role) => {
     if (!(await confirmDialog({ message: interpolateDefault(t('admin.roles.confirm_delete', 'Delete role "{{name}}"? This cannot be undone.'), { name: role.name }), danger: true }))) return
    setActionLoading(role.id)
    try {
      await deleteRole.mutateAsync(role.id)
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="role-list">
         <PageHeader title={t('admin.roles.title', 'Roles')} description={t('admin.roles.description', 'Manage roles and their permissions')} icon="shield">
          <Can resource="roles" action="create">
            <Button
              onClick={() => setShowCreate(true)}
              variant="primary"
              size="sm"
            >
               {t('admin.roles.create', 'Create Role')}
            </Button>
          </Can>
        </PageHeader>

        {!canRead && (
          <EmptyState
            variant="error"
             title={t('common.access_denied', 'Access denied')}
             description={t('admin.roles.access_denied_description', 'You do not have permission to view roles.')}
          />
        )}
        <Can resource="roles" action="read">
          <>
            {showCreate && (
              <RoleForm
                mode="create"
                onSubmit={handleCreate}
                onCancel={() => setShowCreate(false)}
              />
            )}

            <TableToolbar
              urlQ={q}
              setQ={setQ}
              placeholder={t('admin.roles.search_placeholder', 'Search Roles...')}
              ariaLabel={t('admin.roles.search_label', 'Search Roles')}
            />

            {isLoading ? (
              <TableSkeleton rows={5} cols={3} />
            ) : filteredRoles.length === 0 ? (
              q ? (
                <EmptyState
                  variant="no-results"
                  title={t('admin.roles.no_results_title', 'No matching roles')}
                  description={t('admin.roles.no_results_description', 'Try adjusting your search or filters.')}
                />
              ) : (
                <EmptyState
                  variant="empty"
                  title={t('admin.roles.empty_title', 'No roles yet')}
                  description={t('admin.roles.empty_description', 'There are no roles yet. Create the first role to get started.')}
                />
              )
            ) : (
              <div className="space-y-3">
                <Table>
                  <thead className="sticky top-0 z-10">
                    <tr className="border-b bg-background">
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.description', 'Description')}</th>
                      <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredRoles.map((role) => (
                      <tr key={role.id} className="border-b last:border-0 hover:bg-muted/30" data-testid="role-row">
                        <td className="px-4 py-3 text-sm font-normal">
                          <span className="inline-flex items-center gap-1.5">
                            <Link to={`/roles/${role.id}`} className="font-semibold hover:underline">
                              {role.name}
                            </Link>
                            {role.is_system && (
                              <Shield
                                size={14}
                                className="text-muted-foreground"
                                 aria-label={t('admin.roles.system_role_label', 'System role — cannot be deleted')}
                                data-testid="system-role-badge"
                              />
                            )}
                          </span>
                        </td>
                        <td className="px-4 py-3 text-sm font-normal text-muted-foreground">
                          {role.description || '-'}
                        </td>
                        <td className="px-4 py-3 text-right">
                          <div className="flex items-center justify-end gap-2">
                            <Button asChild variant="outline" size="sm">
                               <Link to={`/configuration/permissions?role=${role.id}`}>{t('common.permissions', 'Permissions')}</Link>
                            </Button>
                            <Can resource="roles" action="update">
                              <Button asChild variant="outline" size="sm">
                                 <Link to={`/roles/${role.id}`}>{t('common.edit', 'Edit')}</Link>
                              </Button>
                            </Can>
                            <Can resource="roles" action="delete">
                              {!role.is_system && (
                                <Button
                                  type="button"
                                  variant="destructive"
                                  size="sm"
                                  onClick={() => handleDelete(role)}
                                  disabled={actionLoading === role.id}
                                  loading={actionLoading === role.id}
                                >
                                   {t('common.delete', 'Delete')}
                                </Button>
                              )}
                            </Can>
                          </div>
                        </td>
                      </tr>
                    ))}
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
          </>
        </Can>
      </div>
    </ErrorBoundary>
  )
}
