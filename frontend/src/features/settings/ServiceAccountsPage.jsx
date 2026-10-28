import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useSources } from '../../shared/hooks/use-sources'
import { useTargets } from '../../shared/hooks/use-targets'
import {
  useServiceAccounts, useDeleteServiceAccount, useUpdateServiceAccount,
} from '../../shared/hooks/use-other-resources'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import EmptyState from '../../shared/components/EmptyState'
import { Button } from '../../shared/components/Button'
import ServiceAccountModal from './ServiceAccountModal'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import TableSkeleton from '../../shared/components/TableSkeleton'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import UserCog from 'lucide-react/dist/esm/icons/user-cog'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'


export default function ServiceAccountsPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { data: sourcesData } = useSources(0, 1000)
  const sources = sourcesData?.items || sourcesData || []
  const { data: targetsData } = useTargets(0, 100)
  const targets = targetsData?.items || targetsData || []

  const [filterSourceId, setFilterSourceId] = useState(null)
  const [filterTargetId, setFilterTargetId] = useState(null)
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const limit = pageSize

  const { data: accountsData, isLoading, isError, refetch } = useServiceAccounts(
    filterSourceId, skip, limit, filterTargetId, { includeTotal: true }
  )
  const accounts = accountsData?.items || []
  const total = accountsData?.total || 0

  // D-02 standard search adapter for a local loaded-list filter surface
  // (manifest #12): URL-synced q, preserves unrelated params, resets page to 1.
  // The loaded current page is filtered in memory — no backend search contract.
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
  const filteredAccounts = query
    ? accounts.filter((acc) =>
        (acc.name || '').toLowerCase().includes(query) ||
        (acc.username || '').toLowerCase().includes(query)
      )
    : accounts

  const updateAccount = useUpdateServiceAccount()
  const deleteAccount = useDeleteServiceAccount()

  const [showModal, setShowModal] = useState(false)
  const [editingAccount, setEditingAccount] = useState(null)

  const clearFilters = () => {
    setFilterSourceId(null)
    setFilterTargetId(null)
    setQ('')
    setPage(1)
  }

  const handleDelete = async (account) => {
     if (!(await confirmDialog({ message: interpolateDefault(t('settings.service_accounts.confirm_delete', 'Delete service account "{{name}}"? This action cannot be undone.'), { name: account.name }), danger: true }))) return
    deleteAccount.mutate(account.id)
  }

  const selectClass = 'rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground'

  return (
    <ErrorBoundary>
      <div className="space-y-6" data-testid="service-account-list-page">
         <PageHeader title={t('settings.service_accounts.title', 'Service Accounts')} description={t('settings.service_accounts.description', 'Manage API credentials for sources and targets.')} icon="user-cog">
          <Button onClick={() => setShowModal(true)} variant="primary" size="sm" data-testid="service-account-new-btn">
             {t('settings.service_accounts.add', '+ Add Account')}
          </Button>
        </PageHeader>

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.service_accounts.search_placeholder', 'Search Service Accounts...')}
          ariaLabel={t('settings.service_accounts.search_label', 'Search Service Accounts')}
          filters={
            <>
              <select
                value={filterSourceId || ''}
                onChange={(e) => { setFilterSourceId(e.target.value || null); setFilterTargetId(null) }}
                className={selectClass}
                data-testid="sa-filter-source"
              >
                <option value="">{t('settings.service_accounts.filter_source', 'Filter by Source')}</option>
                {sources.map(s => <option key={s.id} value={s.id}>{s.name}</option>)}
              </select>
              <select
                value={filterTargetId || ''}
                onChange={(e) => { setFilterTargetId(e.target.value || null); setFilterSourceId(null) }}
                className={selectClass}
                data-testid="sa-filter-target"
              >
                <option value="">{t('settings.service_accounts.filter_target', 'Filter by Target')}</option>
                {targets.map(t => <option key={t.id} value={t.id}>{t.name}</option>)}
              </select>
            </>
          }
        />

        {isLoading ? (
          <TableSkeleton rows={5} cols={5} />
        ) : isError ? (
          <EmptyState
            variant="error"
             title={t('settings.service_accounts.load_failed_title', 'Failed to load service accounts')}
             description={t('settings.service_accounts.load_failed_description', 'Failed to load service accounts. The server may be unavailable.')}
             buttonLabel={t('common.retry', 'Retry')}
            onButtonClick={() => refetch()}
          />
        ) : filteredAccounts.length === 0 ? (
          (q || filterSourceId || filterTargetId) ? (
            <EmptyState
              variant="no-results"
              title={t('settings.service_accounts.no_results_title', 'No matching service accounts')}
              description={t('settings.service_accounts.no_results_description', 'Try adjusting your search or filters.')}
              onButtonClick={clearFilters}
            />
          ) : (
            <EmptyState
              variant="empty"
              icon={UserCog}
               title={t('settings.service_accounts.empty_title', 'No service accounts')}
               description={t('settings.service_accounts.empty_description', 'No service accounts configured. Add one to get started.')}
               buttonLabel={t('settings.service_accounts.add', '+ Add Account')}
              onButtonClick={() => setShowModal(true)}
            />
          )
        ) : (
          <div className="space-y-3">
            <Table>
              <thead className="sticky top-0 z-10">
                <tr className="border-b bg-background">
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.username', 'Username')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.type', 'Type')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.status', 'Status')}</th>
                  <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filteredAccounts.map((account) => (
                  <tr key={account.id} className="border-b last:border-0 hover:bg-muted/30">
                    <td className="px-4 py-3 text-sm font-medium">{account.name}</td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">
                      {account.username || '-'}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      {account.source_id && (
                        <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary" data-testid="sa-type-badge">
                           {t('common.source', 'Source')}
                        </span>
                      )}
                      {account.target_id && (
                        <span className="rounded-full bg-primary/10 px-2 py-0.5 text-xs font-medium text-primary" data-testid="sa-type-badge">
                           {t('common.target', 'Target')}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 text-sm">
                      <span className={`rounded-full px-2 py-0.5 text-xs font-medium ${
                        account.is_active
                          ? 'bg-green-100 dark:bg-green-900/30 text-green-700 dark:text-green-400'
                          : 'bg-muted text-muted-foreground'
                      }`}>
                         {account.is_active ? t('common.status.active', 'Active') : t('common.status.inactive', 'Inactive')}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          onClick={() => updateAccount.mutateAsync({ id: account.id, is_active: !account.is_active })}
                          variant="outline"
                          size="sm"
                        >
                           {account.is_active ? t('common.disable', 'Disable') : t('common.enable', 'Enable')}
                        </Button>
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          onClick={() => setEditingAccount(account)}
                        >
                           {t('common.edit', 'Edit')}
                        </Button>
                        <Button
                          type="button"
                          variant="destructive"
                          size="sm"
                          onClick={() => handleDelete(account)}
                        >
                           {t('common.delete', 'Delete')}
                        </Button>
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

        <ServiceAccountModal
          isOpen={showModal || !!editingAccount}
          onClose={() => { setShowModal(false); setEditingAccount(null) }}
          account={editingAccount}
        />
      </div>
    </ErrorBoundary>
  )
}
