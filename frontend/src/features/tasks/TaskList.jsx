import { useState, useCallback } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import {
  useTasks, useRetryTask, useCancelTask, useDeleteTask,
  useFailedOutboxEntries, useRetryOutboxEntry,
} from '../../shared/hooks/use-other-resources'
import { useTaskTypes } from '../../shared/hooks/use-task-types'
import { sortCatalogEntries, typeOptionText } from './task-catalog'
import { useSource } from '../../shared/hooks/use-sources'
import { useSchema } from '../../shared/hooks/use-schemas'
import { useObjectType } from '../../shared/hooks/use-object-types'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import CreateTaskModal from './CreateTaskModal'
import { downloadExportFile } from '../../shared/lib/download-export'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PaginationBar } from '../../shared/components/PaginationBar'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { usePaginationParams } from '../../shared/hooks/use-pagination-params'
import TaskProgress from '../../shared/components/TaskProgress'
import ClipboardList from 'lucide-react/dist/esm/icons/clipboard-list'

const statusColors = {
  PENDING: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  RUNNING: 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200',
  SUCCESS: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200',
  PARTIAL_SUCCESS: 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200',
  FAILED: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200',
  DEAD_LETTER: 'bg-rose-200 text-rose-900 dark:bg-rose-950 dark:text-rose-300',
}

// Phase 247 (D-09): terminal statuses gate Delete; RUNNING/PENDING gate
// Cancel; only FAILED/DEAD_LETTER gate Retry. PARTIAL_SUCCESS is terminal
// (retains its final counts/result) but is never retryable.
const TERMINAL_STATUSES = new Set(['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'DEAD_LETTER'])
const RETRYABLE_STATUSES = new Set(['FAILED', 'DEAD_LETTER'])
const ACTIVE_STATUSES = new Set(['RUNNING', 'PENDING'])

function taskStatusLabel(t, status) {
  switch (status) {
    case 'PENDING': return t('tasks.status.pending', 'PENDING')
    case 'RUNNING': return t('tasks.status.running', 'RUNNING')
    case 'SUCCESS': return t('tasks.status.success', 'SUCCESS')
    case 'PARTIAL_SUCCESS': return t('tasks.status.partial_success', 'PARTIAL_SUCCESS')
    case 'FAILED': return t('tasks.status.failed', 'FAILED')
    case 'DEAD_LETTER': return t('tasks.status.dead_letter', 'DEAD_LETTER')
    default: return status
  }
}

// Phase 286 (PLUGTASK-01), Phase 296 (CLEAN-01): recognized Core values keep
// their translated labels; an unknown value (a plugin identifier, or a retired
// sync name that is no longer a Core type) is an authoritative raw diagnostic.
// Any value outside these sets is rendered as a literal React text node (never
// a guessed label, i18n lookup, or markup).
const CORE_TASK_TYPES = new Set(['IMPORT', 'EXPORT', 'REINDEX'])
const CORE_STEPS = new Set([
  'SCHEMA', 'METADATA', 'STATUSES', 'OBJECTTYPES', 'ATTRIBUTES', 'OBJECTS', 'VALUES', 'REFERENCES', 'POSTPROCESSING',
])

function taskTypeLabel(t, taskType) {
  switch (taskType) {
    case 'IMPORT': return t('tasks.type.import', 'IMPORT')
    case 'EXPORT': return t('tasks.type.export', 'EXPORT')
    case 'REINDEX': return t('tasks.type.reindex', 'REINDEX')
    default: return taskType
  }
}

// Unknown plugin raw values use the UI-SPEC mono/muted diagnostic styling.
// In the table a long value may truncate visually, but the complete literal
// stays available through both title and aria-label.
function TaskTypeValue({ t, taskType, truncate = false }) {
  if (CORE_TASK_TYPES.has(taskType)) return taskTypeLabel(t, taskType)
  if (!taskType) return null
  return (
    <span
      className={
        truncate
          ? 'inline-block max-w-[200px] truncate align-bottom font-mono text-xs font-normal text-muted-foreground'
          : 'font-mono break-all'
      }
      title={taskType}
      aria-label={taskType}
    >
      {taskType}
    </span>
  )
}

function taskStepLabel(t, step) {
  const defaults = {
    SCHEMA: 'SCHEMA',
    METADATA: 'METADATA',
    OBJECTTYPES: 'OBJECTTYPES',
    ATTRIBUTES: 'ATTRIBUTES',
    OBJECTS: 'OBJECTS',
    VALUES: 'VALUES',
    REFERENCES: 'REFERENCES',
    POSTPROCESSING: 'POSTPROCESSING',
  }
  // D-10: a plugin task has no current_step until run() starts — the list
  // shows an honest em dash, never a synthetic step or progress treatment.
  if (step === null || step === undefined || step === '') return '—'
  if (CORE_STEPS.has(step)) {
    return t(`tasks.step.${step.toLowerCase()}`, defaults[step] ?? step)
  }
  return (
    <span
      className="inline-block max-w-[200px] truncate align-bottom font-mono text-xs font-normal text-muted-foreground"
      title={step}
      aria-label={step}
    >
      {step}
    </span>
  )
}

// Phase 247 (D-09): "Started / runtime" — elapsed time from started_at to
// completed_at (terminal) or to now (still active). Render-time computed
// only — the existing 2s React-Query poll while active is the sole clock,
// no separate ticking timer.
function elapsedRuntime(startedAt) {
  if (!startedAt) return null
  const start = new Date(startedAt.endsWith('Z') ? startedAt : startedAt + 'Z')
  const totalSeconds = Math.max(0, Math.floor((new Date() - start) / 1000))
  return formatDuration(totalSeconds)
}

function formatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

function completedRuntime(startedAt, completedAt) {
  if (!startedAt || !completedAt) return null
  const start = new Date(startedAt.endsWith('Z') ? startedAt : startedAt + 'Z')
  const end = new Date(completedAt.endsWith('Z') ? completedAt : completedAt + 'Z')
  const totalSeconds = Math.max(0, Math.floor((end - start) / 1000))
  return formatDuration(totalSeconds)
}

// Phase 247 (D-09): dependent lookups mirror TaskDetail's existing
// per-record pattern (useSource/useSchema/useObjectType keyed by id) —
// React Query dedupes repeated ids across rows, so this never becomes a
// per-row Import/Export-style N+1 for the same handful of distinct scopes.
function TaskScopeCell({ task }) {
  const { data: source } = useSource(task.source_id ?? null)
  const { data: schema } = useSchema(task.schema_id ?? null)
  const { data: objectType } = useObjectType(task.object_type_id ?? null)

  const labels = [source?.name, schema?.name, objectType?.name].filter(Boolean)
  if (labels.length === 0) {
    return <span className="text-muted-foreground" data-testid="scope-cell">—</span>
  }
  const text = labels.join(' / ')
  return (
    <span
      className="inline-block max-w-[220px] truncate align-bottom"
      title={text}
      aria-label={text}
      data-testid="scope-cell"
    >
      {text}
    </span>
  )
}

function OutboxFailuresSection() {
  const { t } = useTranslation()
  const { data, isLoading } = useFailedOutboxEntries()
  const retryEntry = useRetryOutboxEntry()
  const [retryingId, setRetryingId] = useState(null)
  const entries = data?.items || []

  if (isLoading || entries.length === 0) return null

  const handleRetry = async (id) => {
    setRetryingId(id)
    try {
      await retryEntry.mutateAsync(id)
    } finally {
      setRetryingId(null)
    }
  }

  return (
    <div className="rounded-lg border border-rose-300 dark:border-rose-900">
      <div className="border-b border-rose-300 bg-rose-50 px-4 py-2 dark:border-rose-900 dark:bg-rose-950/50">
        <h2 className="text-sm font-semibold text-rose-900 dark:text-rose-300">
          {selectPluralKey('tasks.outbox.failed_jobs', entries.length) === 'tasks.outbox.failed_jobs_one'
            ? interpolateDefault(t('tasks.outbox.failed_jobs_one', 'Failed Indexing Jobs ({{count}})'), { count: entries.length })
            : interpolateDefault(t('tasks.outbox.failed_jobs_other', 'Failed Indexing Jobs ({{count}})'), { count: entries.length })}
        </h2>
        <p className="text-xs text-rose-700 dark:text-rose-400">
          {t('tasks.outbox.retry_exhausted_description', 'Index/remove jobs that exhausted retries without a Task record — needs manual replay.')}
        </p>
      </div>
      <table className="w-full">
        <thead>
          <tr className="border-b">
            <th className="text-left px-4 py-2 text-sm font-medium">{t('tasks.table.operation', 'Operation')}</th>
            <th className="text-left px-4 py-2 text-sm font-medium">{t('tasks.table.object_id', 'Object ID')}</th>
            <th className="text-left px-4 py-2 text-sm font-medium">{t('tasks.table.attempts', 'Attempts')}</th>
            <th className="text-left px-4 py-2 text-sm font-medium">{t('tasks.table.last_error', 'Last Error')}</th>
            <th className="text-right px-4 py-2 text-sm font-medium">{t('tasks.table.actions', 'Actions')}</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={entry.id} className="border-b last:border-0 hover:bg-muted/30">
              <td className="px-4 py-2 text-sm">{entry.operation}</td>
              <td className="px-4 py-2 text-sm font-mono text-xs text-muted-foreground">{entry.object_id}</td>
              <td className="px-4 py-2 text-sm">{entry.attempts}</td>
              <td className="px-4 py-2 text-sm text-muted-foreground">{entry.last_error || '-'}</td>
              <td className="px-4 py-2 text-right">
                <Button
                  type="button"
                  variant="primary"
                  size="sm"
                  onClick={() => handleRetry(entry.id)}
                  disabled={retryingId === entry.id}
                  loading={retryingId === entry.id}
                >
                  {t('tasks.action.retry', 'Retry')}
                </Button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export default function TaskListPage() {
  const { t, i18n } = useTranslation()
  const { page, pageSize, setPage, setPageSize } = usePaginationParams()
  const skip = (page - 1) * pageSize
  const limit = pageSize
  const [searchParams, setSearchParams] = useSearchParams()
  // Phase 247 (D-11): compact, URL-backed Task-type/status selects, sent to
  // the server (never client-filtered) — same URLSearchParams + page-reset
  // pattern as the existing category tabs below.
  const typeFilter = searchParams.get('type') || ''
  const statusFilter = searchParams.get('status') || ''

  // FEEXT-05: the type filter options follow the server task-type catalog
  // (same order/text as Create Task, without the unavailable suffix). A URL
  // filter value the catalog no longer lists stays selectable as its raw value.
  const { data: taskTypeCatalog = [] } = useTaskTypes()
  const sortedTypeCatalog = sortCatalogEntries(taskTypeCatalog, i18n.resolvedLanguage || 'en')
  const typeFilterInCatalog = sortedTypeCatalog.some((entry) => entry.name === typeFilter)

  const taskTypeParam = typeFilter || undefined
  const currentStatusParam = statusFilter || undefined

  const category = searchParams.get('category') || 'manual'

  const categoryToFilters = {
    manual: { scheduleIdNull: true, isPermanent: false },
    permanent: { scheduleIdNull: true, isPermanent: true },
    scheduled: { scheduleIdNull: false },
  }
  const { scheduleIdNull, isPermanent } = categoryToFilters[category] || categoryToFilters.manual

  const { data: taskData, isLoading, isFetching } = useTasks(
    skip, limit, taskTypeParam, scheduleIdNull, isPermanent, currentStatusParam
  )
  const tasks = taskData?.items || []
  const total = taskData?.total || 0
  const formatDate = useFormatDate()
  const [actionLoading, setActionLoading] = useState(null)
  // Phase 300 (D-05): a failed cancel request is shown under the row's action
  // cell (it was swallowed silently before) and cleared on the next action.
  const [cancelErrors, setCancelErrors] = useState({})
  const [showCreateModal, setShowCreateModal] = useState(false)
  const retryTask = useRetryTask()
  const cancelTask = useCancelTask()
  const deleteTask = useDeleteTask()

  // D-02 standard search adapter for the TaskList main list (manifest #17,
  // existing server/list query adapter): URL-synced q via useSearchParams,
  // preserves the page's type/status/category filter params, resets page to 1
  // on change or clear. The existing server query (type/status/category) is
  // untouched — the text query filters the loaded current page in memory
  // because the tasks backend has no free-text search contract and this
  // frontend-only phase adds none.
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
  const filteredTasks = query
    ? tasks.filter((task) =>
        (task.name || '').toLowerCase().includes(query) ||
        (task.tasktype || '').toLowerCase().includes(query)
      )
    : tasks

  const updateFilter = (key, value) => {
    const next = new URLSearchParams(searchParams)
    if (value) next.set(key, value)
    else next.delete(key)
    next.set('page', '1')
    setSearchParams(next, { replace: true })
  }

  const handleDelete = async (taskId) => {
    setActionLoading(taskId)
    try {
      await deleteTask.mutateAsync(taskId)
    } finally {
      setActionLoading(null)
    }
  }

  const handleRetry = async (taskId) => {
    setActionLoading(taskId)
    try {
      await retryTask.mutateAsync(taskId)
    } catch {
      // Error feedback
    } finally {
      setActionLoading(null)
    }
  }

  const handleCancel = async (taskId) => {
    setActionLoading(taskId)
    setCancelErrors((prev) => ({ ...prev, [taskId]: null }))
    try {
      await cancelTask.mutateAsync(taskId)
      // D-05/D-06: no optimistic "Task cancelled" message — the server-confirmed
      // "Cancellation requested" state is the feedback.
    } catch (err) {
      setCancelErrors((prev) => ({
        ...prev,
        [taskId]: err.response?.data?.detail || t('tasks.error.cancel', 'Failed to cancel task'),
      }))
    } finally {
      setActionLoading(null)
    }
  }

  const pageTitle = typeFilter === 'IMPORT'
    ? t('tasks.page.import_title', 'Import Tasks')
    : typeFilter === 'EXPORT'
      ? t('tasks.page.export_title', 'Export Tasks')
      : t('tasks.page.title', 'Tasks')

  const pageDescription = typeFilter === 'IMPORT'
    ? t('tasks.page.import_description', 'Monitor import tasks filtered by type')
    : typeFilter === 'EXPORT'
      ? t('tasks.page.export_description', 'Monitor export tasks filtered by type')
      : t('tasks.page.description', 'Monitor import, export, and sync tasks')

  return (
    <ErrorBoundary>
      <div className="space-y-6">
      <PageHeader title={pageTitle} description={pageDescription} icon="list-checks">
        <Button
          onClick={() => setShowCreateModal(true)}
          variant="primary"
          size="sm"
        >
          {t('tasks.action.new', '+ New Task')}
        </Button>
      </PageHeader>

      {/* Category tabs */}
      <div className="flex gap-1 border-b">
        {(['manual', 'permanent', 'scheduled']).map((tab) => {
          const labels = {
            manual: t('tasks.category.manually_created', 'Manually Created'),
            permanent: t('tasks.category.permanent', 'Permanent'),
            scheduled: t('tasks.category.scheduled', 'Scheduled'),
          }
          const isActive = category === tab
          return (
            <button
              key={tab}
              data-testid={`category-tab-${tab}`}
              className={`px-4 py-2 text-sm font-medium transition-colors border-b-2 -mb-px ${
                isActive
                  ? 'border-primary text-foreground'
                  : 'border-transparent text-muted-foreground hover:text-foreground'
              }`}
              data-active={isActive ? 'true' : 'false'}
              onClick={() => {
                const next = new URLSearchParams(searchParams)
                next.set('category', tab)
                next.set('page', '1')
                setSearchParams(next, { replace: true })
              }}
            >
              {labels[tab]}
            </button>
          )
        })}
      </div>

      {/* Type/status filters (D-11) compose in the TableToolbar filters slot;
          the standard search input sits beside them and combines with q. */}
      <TableToolbar
        urlQ={q}
        setQ={setQ}
        placeholder={t('tasks.search_placeholder', 'Search Tasks...')}
        ariaLabel={t('tasks.search_label', 'Search Tasks')}
        filters={
          <>
            <label className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">{t('tasks.filter.type', 'Type')}</span>
              <select
                data-testid="filter-task-type"
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={typeFilter}
                onChange={(e) => updateFilter('type', e.target.value)}
              >
                <option value="">{t('tasks.filter.all_types', 'All types')}</option>
                {sortedTypeCatalog.map((entry) => (
                  <option key={entry.name} value={entry.name}>
                    {typeOptionText(t, entry, 'filter')}
                  </option>
                ))}
                {typeFilter && !typeFilterInCatalog && (
                  <option value={typeFilter}>{typeFilter}</option>
                )}
              </select>
            </label>
            <label className="flex items-center gap-2 text-sm">
              <span className="text-muted-foreground">{t('tasks.filter.status', 'Status')}</span>
              <select
                data-testid="filter-task-status"
                className="h-8 rounded-md border border-input bg-background px-2 text-sm"
                value={statusFilter}
                onChange={(e) => updateFilter('status', e.target.value)}
              >
                <option value="">{t('tasks.filter.all_statuses', 'All statuses')}</option>
                <option value="PENDING">{t('tasks.status.pending', 'PENDING')}</option>
                <option value="RUNNING">{t('tasks.status.running', 'RUNNING')}</option>
                <option value="SUCCESS">{t('tasks.status.success', 'SUCCESS')}</option>
                <option value="PARTIAL_SUCCESS">{t('tasks.status.partial_success', 'PARTIAL_SUCCESS')}</option>
                <option value="FAILED">{t('tasks.status.failed', 'FAILED')}</option>
                <option value="DEAD_LETTER">{t('tasks.status.dead_letter', 'DEAD_LETTER')}</option>
              </select>
            </label>
            {isFetching && !isLoading && (
              <span className="text-xs text-muted-foreground animate-pulse" data-testid="tasks-updating" aria-live="polite">
                {t('tasks.list.updating', 'Updating…')}
              </span>
            )}
          </>
        }
      />

      <OutboxFailuresSection />

      {isLoading ? (
        <TableSkeleton rows={5} cols={6} />
      ) : q && filteredTasks.length === 0 ? (
        <EmptyState
          variant="no-results"
          title={t('tasks.no_results_title', 'No matching tasks')}
          description={t('tasks.no_results_description', 'Try adjusting your search or filters.')}
        />
      ) : tasks.length === 0 ? (
        category === 'manual' ? (
          <>
            <EmptyState
              variant="empty"
              icon={ClipboardList}
              title={typeFilter === 'IMPORT'
                ? t('tasks.empty.manual_import_title', 'No manually created import tasks')
                : typeFilter === 'EXPORT'
                  ? t('tasks.empty.manual_export_title', 'No manually created export tasks')
                  : t('tasks.empty.manual_title', 'No manually created tasks')}
              description={typeFilter === 'IMPORT'
                ? t('tasks.empty.manual_import_description', 'Trigger an import to see it here.')
                : typeFilter === 'EXPORT'
                  ? t('tasks.empty.manual_export_description', 'Trigger an export to see it here.')
                  : t('tasks.empty.manual_description', 'Trigger an import, export, or sync to create a task.')}
              buttonLabel={t('tasks.action.create', 'Create Task')}
              onButtonClick={() => setShowCreateModal(true)}
            />
          </>
        ) : category === 'permanent' ? (
          <EmptyState
            variant="empty"
            icon={ClipboardList}
            title={typeFilter === 'IMPORT'
              ? t('tasks.empty.permanent_import_title', 'No permanent import tasks')
              : typeFilter === 'EXPORT'
                ? t('tasks.empty.permanent_export_title', 'No permanent export tasks')
                : t('tasks.empty.permanent_title', 'No permanent tasks')}
            description={typeFilter === 'IMPORT'
              ? t('tasks.empty.permanent_import_description', 'Mark an import task as permanent during creation to see it here.')
              : typeFilter === 'EXPORT'
                ? t('tasks.empty.permanent_export_description', 'Mark an export task as permanent during creation to see it here.')
                : t('tasks.empty.permanent_description', 'Mark a task as permanent during creation to see it here.')}
          />
        ) : (
          <EmptyState
            variant="empty"
            icon={ClipboardList}
            title={typeFilter === 'IMPORT'
              ? t('tasks.empty.scheduled_import_title', 'No scheduled import tasks')
              : typeFilter === 'EXPORT'
                ? t('tasks.empty.scheduled_export_title', 'No scheduled export tasks')
                : t('tasks.empty.scheduled_title', 'No scheduled tasks')}
            description={typeFilter === 'IMPORT'
              ? t('tasks.empty.scheduled_import_description', 'Create a schedule with an import definition to generate scheduled tasks.')
              : typeFilter === 'EXPORT'
                ? t('tasks.empty.scheduled_export_description', 'Create a schedule with an export definition to generate scheduled tasks.')
                : t('tasks.empty.scheduled_description', 'Create a schedule with an export or import definition to generate scheduled tasks.')}
          />
        )
      ) : (
        <div className="space-y-3">
          <Table tableClassName="min-w-[900px]">
            <thead className="sticky top-0 z-10">
              <tr className="border-b bg-background">
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('tasks.table.task', 'Task')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('tasks.table.status', 'Status')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('tasks.table.progress', 'Current step / progress')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('tasks.table.scope', 'Scope')}</th>
                <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('tasks.table.started', 'Started / runtime')}</th>
                <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('tasks.table.actions', 'Actions')}</th>
              </tr>
            </thead>
            <tbody>
                {filteredTasks.map((task) => {
                  const isActive = ACTIVE_STATUSES.has(task.current_status)
                  const isTerminal = TERMINAL_STATUSES.has(task.current_status)
                  // D-05: server-confirmed cancel intent only.
                  const cancelRequested = Boolean(task.cancel_requested_at)
                  const runtime = isActive
                    ? elapsedRuntime(task.started_at)
                    : completedRuntime(task.started_at, task.completed_at)
                  return (
                  <tr key={task.id} className="border-b last:border-0 hover:bg-muted/30" data-testid="task-row">
                    <td className="px-4 py-3 text-sm font-medium" data-testid="type-chip">
                      {task.name ? (
                        <>
                          <div
                            className="max-w-[200px] truncate"
                            title={task.name}
                            aria-label={task.name}
                          >
                            {task.name}
                          </div>
                          <div className="text-xs text-muted-foreground font-normal">
                            <TaskTypeValue t={t} taskType={task.tasktype} truncate />
                          </div>
                        </>
                      ) : (
                        <TaskTypeValue t={t} taskType={task.tasktype} truncate />
                      )}
                    </td>
                    <td className="px-4 py-3">
                      <span className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${statusColors[task.current_status] || ''}`} data-testid="status-badge">
                        {taskStatusLabel(t, task.current_status)}
                      </span>
                    </td>
                    <td className="px-4 py-3 text-sm" data-testid="progress-cell">
                      <TaskProgress task={task} stepLabel={taskStepLabel(t, task.current_step)} />
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">
                      <TaskScopeCell task={task} />
                    </td>
                    <td className="px-4 py-3 text-sm text-muted-foreground">
                      <div>{task.started_at ? formatDate(task.started_at) : '—'}</div>
                      {runtime && <div className="text-xs">{runtime}</div>}
                    </td>
                    <td className="px-4 py-3 text-right">
                      <div className="flex items-center justify-end gap-2">
                        <Button asChild variant="outline" size="sm">
                         <Link to={`/tasks/${task.id}`}>{t('tasks.action.view', 'View')}</Link>
                        </Button>
                        {task.tasktype === 'EXPORT' && task.current_status === 'SUCCESS' && task.export_id && (
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => downloadExportFile(task.export_id).catch(() => {})}
                            data-testid={`task-export-download-btn-${task.id}`}
                          >
                            {t('settings.export.action.download', 'Download')}
                          </Button>
                        )}
                        {RETRYABLE_STATUSES.has(task.current_status) && (
                          <Button
                            type="button"
                            variant="primary"
                            size="sm"
                            onClick={() => handleRetry(task.id)}
                            disabled={actionLoading === task.id}
                            loading={actionLoading === task.id}
                          >
                            {t('tasks.action.retry', 'Retry')}
                          </Button>
                        )}
                        {isTerminal && (
                          <Button
                            type="button"
                            variant="destructive"
                            size="md"
                            onClick={() => handleDelete(task.id)}
                            disabled={actionLoading === task.id}
                          >
                            {t('tasks.action.delete', 'Delete')}
                          </Button>
                        )}
                        {isActive && !cancelRequested && (
                          <Button
                            type="button"
                            variant="outline"
                            size="sm"
                            onClick={() => handleCancel(task.id)}
                            disabled={actionLoading === task.id}
                            loading={actionLoading === task.id}
                            className="text-destructive border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
                          >
                            {t('tasks.action.cancel', 'Cancel')}
                          </Button>
                        )}
                        {isActive && cancelRequested && (
                          <span
                            className="text-xs text-amber-700 dark:text-amber-300"
                            data-testid="task-cancel-requested"
                          >
                            {t('tasks.cancel_requested.label', 'Cancellation requested')}
                          </span>
                        )}
                      </div>
                      {cancelErrors[task.id] && (
                        <p className="mt-1 text-xs text-red-700 dark:text-red-400" role="alert">
                          {cancelErrors[task.id]}
                        </p>
                      )}
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
      <CreateTaskModal
        isOpen={showCreateModal}
        onClose={() => setShowCreateModal(false)}
      />
    </div>
    </ErrorBoundary>
  )
}
