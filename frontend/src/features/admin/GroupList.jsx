import { useState, useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import {
  useGroups, useCreateGroup, useUpdateGroup, useDeleteGroup,
} from '../../shared/hooks/use-groups'
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
import GroupForm from './GroupForm'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

export default function GroupListPage() {
  const { t } = useTranslation()
  const canRead = useCan('groups', 'read')
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize

  // D-02 standard search adapter: URL-synced q, preserves unrelated params,
  // resets page to 1 on change or clear.
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

  const { data: groupData, isLoading } = useGroups(q, null, skip, pageSize, { enabled: canRead })
  const groups = groupData?.items || []
  const total = groupData?.total || 0

  const createGroup = useCreateGroup()
  const updateGroup = useUpdateGroup()
  const deleteGroup = useDeleteGroup()

  const [showCreate, setShowCreate] = useState(false)
  const [editingId, setEditingId] = useState(null)
  const [actionLoading, setActionLoading] = useState(null)

  const handleCreate = async (values) => {
    await createGroup.mutateAsync(values)
    setShowCreate(false)
  }

  const handleUpdate = async (group, values) => {
    await updateGroup.mutateAsync({ id: group.id, ...values })
    setEditingId(null)
  }

  const handleDelete = async (group) => {
    if (!(await confirmDialog({
      message: interpolateDefault(t('admin.groups.confirm_delete', 'Delete group "{{name}}"? This cannot be undone.'), { name: group.name }),
      danger: true,
    }))) return
    setActionLoading(group.id)
    try {
      await deleteGroup.mutateAsync(group.id)
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="group-list">
        <PageHeader title={t('admin.groups.title', 'Groups')} description={t('admin.groups.description', 'Manage groups and their members')} icon="users-round">
          <Can resource="groups" action="create">
            <Button
              onClick={() => setShowCreate(true)}
              variant="primary"
              size="sm"
              leftIcon="plus"
            >
              {t('admin.groups.create', '+ Create Group')}
            </Button>
          </Can>
        </PageHeader>

        {!canRead && (
          <EmptyState
            variant="error"
            title={t('common.access_denied', 'Access denied')}
            description={t('admin.groups.access_denied_description', 'You do not have permission to view groups.')}
          />
        )}
        <Can resource="groups" action="read">
          <>
            {showCreate && (
              <GroupForm
                mode="create"
                onSubmit={handleCreate}
                onCancel={() => setShowCreate(false)}
              />
            )}

            <TableToolbar
              urlQ={q}
              setQ={setQ}
              placeholder={t('admin.groups.search_placeholder', 'Search Groups...')}
              ariaLabel={t('admin.groups.search_label', 'Search Groups')}
            />

            {isLoading ? (
              <TableSkeleton rows={5} cols={3} />
            ) : total === 0 ? (
              q ? (
                <EmptyState
                  variant="no-results"
                  title={t('admin.groups.no_results_title', 'No matching groups')}
                  description={t('admin.groups.no_results_description', 'Try adjusting your search or filters.')}
                />
              ) : (
                <EmptyState
                  variant="empty"
                  title={t('admin.groups.empty_title', 'No groups yet')}
                  description={t('admin.groups.empty_description', 'There are no groups yet. Create the first group to get started.')}
                />
              )
            ) : (
              <div className="space-y-3">
                <Table>
                  <thead className="sticky top-0 z-10">
                    <tr className="border-b bg-background">
                      <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                      <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                    </tr>
                  </thead>
                  <tbody>
                    {groups.map((group) => (
                      <tr key={group.id} className="border-b last:border-0 hover:bg-muted/30" data-testid="group-row">
                        {editingId === group.id ? (
                          <td colSpan={2} className="px-4 py-2">
                            <GroupForm
                              mode="edit"
                              initialValues={group}
                              onSubmit={(values) => handleUpdate(group, values)}
                              onCancel={() => setEditingId(null)}
                            />
                          </td>
                        ) : (
                          <>
                            <td className="px-4 py-3 text-sm font-normal">
                              <Link to={`/groups/${group.id}`} className="font-semibold text-primary hover:underline">
                                {group.name}
                              </Link>
                            </td>
                            <td className="px-4 py-3 text-right">
                              <div className="flex items-center justify-end gap-2">
                                <Button asChild variant="outline" size="sm">
                                  <Link to={`/groups/${group.id}`}>{t('admin.groups.members', 'Members')}</Link>
                                </Button>
                                <Can resource="groups" action="update">
                                  <Button
                                    type="button"
                                    variant="outline"
                                    size="sm"
                                    onClick={() => setEditingId(group.id)}
                                  >
                                    {t('common.edit', 'Edit')}
                                  </Button>
                                </Can>
                                <Can resource="groups" action="delete">
                                  <Button
                                    type="button"
                                    variant="destructive"
                                    size="sm"
                                    onClick={() => handleDelete(group)}
                                    disabled={actionLoading === group.id}
                                    loading={actionLoading === group.id}
                                  >
                                    {t('common.delete', 'Delete')}
                                  </Button>
                                </Can>
                              </div>
                            </td>
                          </>
                        )}
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