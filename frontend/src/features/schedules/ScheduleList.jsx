import { useState, useCallback } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { useSchedules, useUpdateSchedule, useDeleteSchedule } from '../../shared/hooks/use-other-resources'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import CreateScheduleModal from './CreateScheduleModal'
import { formatRecurrence } from './schedule-recurrence'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import Clock from 'lucide-react/dist/esm/icons/clock'

// Phase 296 (CLEAN-01): schedules remain Core-only and the surviving native
// task set is IMPORT/EXPORT/REINDEX — Jira sync work is dispatched through the
// plugin wrapper, never a Core task type. A persisted value outside this set is
// display-only diagnostic text; it is never offered as a template and must not
// crash filtering/rendering.
const CORE_SCHEDULE_TASKTYPES = new Set([
  'IMPORT', 'EXPORT', 'REINDEX',
])

function ScheduleTypeValue({ tasktype }) {
  if (!tasktype || CORE_SCHEDULE_TASKTYPES.has(tasktype)) {
    return <span className="text-sm text-muted-foreground">{tasktype}</span>
  }
  return (
    <span
      className="inline-block max-w-[220px] truncate align-bottom font-mono text-xs text-muted-foreground"
      title={tasktype}
      aria-label={tasktype}
    >
      {tasktype}
    </span>
  )
}

export default function ScheduleListPage() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const limit = pageSize
  const { data: schedulesData, isLoading } = useSchedules(skip, limit, { refetchInterval: 10000 })
  const schedules = schedulesData?.items || []
  const total = schedulesData?.total || 0

  // D-02 standard search adapter for a local loaded-list filter surface
  // (manifest #15): URL-synced q via useSearchParams, preserves unrelated
  // params, resets page to 1 on change or clear. The loaded current page is
  // filtered in memory by name/task type — no backend search contract.
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
  const filteredSchedules = query
    ? schedules.filter((schedule) =>
        (schedule.name || '').toLowerCase().includes(query) ||
        (schedule.tasktype || '').toLowerCase().includes(query)
      )
    : schedules
  const formatDate = useFormatDate()
  const [showCreateModal, setShowCreateModal] = useState(false)
  const [editingSchedule, setEditingSchedule] = useState(null)
  const [actionLoading, setActionLoading] = useState(null)
  const updateSchedule = useUpdateSchedule()
  const deleteSchedule = useDeleteSchedule()

  const handleToggle = async (scheduleId, currentEnabled) => {
    setActionLoading(scheduleId)
    try {
      await updateSchedule.mutateAsync({ id: scheduleId, enabled: !currentEnabled })
    } catch {
      // Error handled by axios interceptor
    } finally {
      setActionLoading(null)
    }
  }

  const handleDelete = async (scheduleId) => {
    if (!(await confirmDialog({ message: t('settings.schedule.confirm_delete', 'Delete this schedule? This cannot be undone.'), danger: true }))) return
    setActionLoading(scheduleId)
    try {
      await deleteSchedule.mutateAsync(scheduleId)
    } catch {
      // Error handled by axios interceptor
    } finally {
      setActionLoading(null)
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6">
        <PageHeader title={t('settings.schedule.title', 'Schedules')} description={t('settings.schedule.description', 'Manage scheduled import, export, and sync jobs')} icon="clock">
          <Button
            onClick={() => setShowCreateModal(true)}
            variant="primary"
            size="sm"
          >
            {t('settings.schedule.new', '+ New Schedule')}
          </Button>
        </PageHeader>

        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('settings.schedule.search_placeholder', 'Search Schedules...')}
          ariaLabel={t('settings.schedule.search_label', 'Search Schedules')}
        />

        {isLoading ? (
          <TableSkeleton rows={5} cols={7} />
        ) : schedules.length === 0 ? (
          <EmptyState
            variant="empty"
            icon={Clock}
            title={t('settings.schedule.empty.title', 'No schedules')}
            description={t('settings.schedule.empty.description', 'Create a schedule to automate recurring jobs.')}
            buttonLabel={t('settings.schedule.create', 'Create Schedule')}
            onButtonClick={() => setShowCreateModal(true)}
          />
        ) : q && filteredSchedules.length === 0 ? (
          <EmptyState
            variant="no-results"
            title={t('settings.schedule.no_results_title', 'No matching schedules')}
            description={t('settings.schedule.no_results_description', 'Try adjusting your search or filters.')}
          />
        ) : (
          <div className="space-y-3">
            <Table>
              <thead className="sticky top-0 z-10">
                <tr className="border-b bg-background">
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.name', 'Name')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.task_type', 'Task Type')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.recurrence', 'Recurrence')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.enabled', 'Enabled')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.last_run', 'Last Run')}</th>
                  <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.next_run', 'Next Run')}</th>
                  <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('settings.schedule.table.actions', 'Actions')}</th>
                </tr>
              </thead>
              <tbody>
                {filteredSchedules.map((schedule) => (
                    <tr key={schedule.id} className="border-b last:border-0 hover:bg-muted/30" data-testid="schedule-row">
                      <td className="px-4 py-3">
                        <span>{schedule.name}</span>
                      </td>
                      <td className="px-4 py-3">
                        <ScheduleTypeValue tasktype={schedule.tasktype} />
                      </td>
                      <td className="px-4 py-3">
                        <code className="text-xs bg-muted px-1.5 py-0.5 rounded">{formatRecurrence(schedule, t)}</code>
                      </td>
                      <td className="px-4 py-3">
                        {schedule.enabled ? (
                          <span className="rounded-full bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200 px-2.5 py-0.5 text-xs font-medium">
                            {t('settings.schedule.enabled', 'Enabled')}
                          </span>
                        ) : (
                          <span className="rounded-full bg-gray-100 text-gray-800 dark:bg-gray-900 dark:text-gray-200 px-2.5 py-0.5 text-xs font-medium">
                            {t('settings.schedule.disabled', 'Disabled')}
                          </span>
                        )}
                      </td>
                      <td className="px-4 py-3 text-sm text-muted-foreground">
                        {formatDate(schedule.last_run_at)}
                      </td>
                      <td className="px-4 py-3 text-sm text-muted-foreground">
                        {formatDate(schedule.next_run_at)}
                      </td>
                      <td className="px-4 py-3 text-right">
                        <div className="flex items-center justify-end gap-2">
                          {/* Enable/Disable toggle intentionally NOT migrated to <Button> — state-dependent custom colors (yellow/green) that don't map to a Button variant */}
                          <button
                            onClick={() => handleToggle(schedule.id, schedule.enabled)}
                            disabled={actionLoading === schedule.id}
                            className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
                              schedule.enabled
                                ? 'bg-yellow-100 text-yellow-800 hover:bg-yellow-200 dark:bg-yellow-900 dark:text-yellow-200'
                                : 'bg-green-100 text-green-800 hover:bg-green-200 dark:bg-green-900 dark:text-green-200'
                            } disabled:opacity-50`}
                          >
                            {actionLoading === schedule.id ? '...' : schedule.enabled
                              ? t('settings.schedule.disable', 'Disable')
                              : t('settings.schedule.enable', 'Enable')}
                          </button>
                          <Button
                            type="button"
                            variant="outline"
                            size="md"
                            onClick={() => setEditingSchedule(schedule)}
                            disabled={actionLoading === schedule.id}
                          >
                            {t('common.edit', 'Edit')}
                          </Button>
                          <Button
                            type="button"
                            variant="destructive"
                            size="md"
                            onClick={() => handleDelete(schedule.id)}
                            disabled={actionLoading === schedule.id}
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

        <CreateScheduleModal
          isOpen={showCreateModal || !!editingSchedule}
          onClose={() => { setShowCreateModal(false); setEditingSchedule(null) }}
          editingSchedule={editingSchedule}
        />
      </div>
    </ErrorBoundary>
  )
}
