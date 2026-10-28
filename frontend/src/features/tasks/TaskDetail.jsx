import { useState } from 'react'
import { useParams, useNavigate } from 'react-router-dom'
import { useTranslation } from 'react-i18next'
import {
  useTask, useRetryTask, useCancelTask, useDeleteTask,
  useServiceAccount, useImportDefinition, useExportDefinition,
} from '../../shared/hooks/use-other-resources'
import { useSource } from '../../shared/hooks/use-sources'
import { useSchema } from '../../shared/hooks/use-schemas'
import { useTarget } from '../../shared/hooks/use-targets'
import { useObjectType } from '../../shared/hooks/use-object-types'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Button } from '../../shared/components/Button'
import TaskProgress from '../../shared/components/TaskProgress'
import TableSkeleton from '../../shared/components/TableSkeleton'
import Breadcrumb from '../../shared/components/Breadcrumb'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { useCan } from '../../shared/components/Can'
import { usePluginActionFamily } from '../../shared/plugin-runtime/PluginProvider'
import { PluginContributionBoundary } from '../../shared/plugin-runtime/PluginSlotBoundary'
import TaskLogPanel from './TaskLogPanel'
import { downloadExportFile } from '../../shared/lib/download-export'
import { CORE_TASK_TYPES, stepLabel } from './task-labels'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

// Phase 247 (D-10): terminal statuses gate Delete; RUNNING/PENDING gate
// Cancel; only FAILED/DEAD_LETTER gate Retry — same gates as TaskList.jsx.
const TERMINAL_STATUSES = new Set(['SUCCESS', 'PARTIAL_SUCCESS', 'FAILED', 'DEAD_LETTER'])
const RETRYABLE_STATUSES = new Set(['FAILED', 'DEAD_LETTER'])
const ACTIVE_STATUSES = new Set(['RUNNING', 'PENDING'])

// Phase 300 (D-05): the "Cancellation requested" pill reuses the existing
// status-badge geometry verbatim so the two adjacent chips are identical.
const STATUS_BADGE_CLASS = 'rounded-full px-3 py-1 text-xs font-medium'
const CANCEL_BADGE_CLASS = `${STATUS_BADGE_CLASS} bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200`

function statusClass(status) {
  if (status === 'SUCCESS') return 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200'
  if (status === 'PARTIAL_SUCCESS') return 'bg-amber-100 text-amber-800 dark:bg-amber-900 dark:text-amber-200'
  if (status === 'FAILED' || status === 'DEAD_LETTER') return 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200'
  if (status === 'RUNNING') return 'bg-blue-100 text-blue-800 dark:bg-blue-900 dark:text-blue-200'
  return 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200'
}

function statusLabel(t, status) {
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
// their translated labels; any value outside the Core set (a plugin identifier,
// or a retired sync name that is no longer a Core type) renders as literal raw
// diagnostic text, never a guessed label or markup.
// CORE_TASK_TYPES and stepLabel live in task-labels.js and are shared with the
// Phase 300 log panel.

function taskTypeLabel(t, taskType) {
  switch (taskType) {
    case 'IMPORT': return t('tasks.type.import', 'IMPORT')
    case 'EXPORT': return t('tasks.type.export', 'EXPORT')
    case 'REINDEX': return t('tasks.type.reindex', 'REINDEX')
    default: return taskType
  }
}

function TaskTypeValue({ t, taskType }) {
  if (CORE_TASK_TYPES.has(taskType)) return taskTypeLabel(t, taskType)
  if (!taskType) return null
  return (
    <span className="font-mono break-all" title={taskType} aria-label={taskType}>
      {taskType}
    </span>
  )
}

function InfoRow({ label, value }) {
  if (!value) return null
  return (
    <div className="flex gap-2 text-sm">
      <span className="text-muted-foreground w-40 shrink-0">{label}</span>
      <span className="break-all">{value}</span>
    </div>
  )
}

// Phase 247 (D-09/D-10): elapsed/total runtime, render-time computed from
// the existing 2s React-Query detail poll while active — no separate timer.
function formatDuration(totalSeconds) {
  const hours = Math.floor(totalSeconds / 3600)
  const minutes = Math.floor((totalSeconds % 3600) / 60)
  const seconds = totalSeconds % 60
  if (hours > 0) return `${hours}h ${minutes}m`
  if (minutes > 0) return `${minutes}m ${seconds}s`
  return `${seconds}s`
}

function elapsedRuntime(startedAt) {
  if (!startedAt) return null
  const start = new Date(startedAt.endsWith('Z') ? startedAt : startedAt + 'Z')
  const totalSeconds = Math.max(0, Math.floor((new Date() - start) / 1000))
  return formatDuration(totalSeconds)
}

function completedRuntime(startedAt, completedAt) {
  if (!startedAt || !completedAt) return null
  const start = new Date(startedAt.endsWith('Z') ? startedAt : startedAt + 'Z')
  const end = new Date(completedAt.endsWith('Z') ? completedAt : completedAt + 'Z')
  const totalSeconds = Math.max(0, Math.floor((end - start) / 1000))
  return formatDuration(totalSeconds)
}

export default function TaskDetailPage() {
  const { t } = useTranslation()
  const { taskId } = useParams()
  const navigate = useNavigate()
  const { data: task, isLoading } = useTask(taskId)
  const { data: source } = useSource(task?.source_id ?? null)
  const { data: schema } = useSchema(task?.schema_id ?? null)
  const { data: target } = useTarget(task?.target_id ?? null)
  const { data: objectType } = useObjectType(task?.object_type_id ?? null)
  const { data: serviceAccount } = useServiceAccount(task?.service_account_id ?? null)
  const { data: importDefinition } = useImportDefinition(task?.import_definition_id ?? null)
  const { data: exportDefinition } = useExportDefinition(task?.export_definition_id ?? null)
  const formatDate = useFormatDate()
  const [actionLoading, setActionLoading] = useState(false)
  const [actionError, setActionError] = useState(null)
  const [actionSuccess, setActionSuccess] = useState(null)
  const retryTask = useRetryTask()
  const cancelTask = useCancelTask()
  const deleteTask = useDeleteTask()
  // D-06: taskDetailActions append to the existing status-card action row; they
  // receive a sanitized task context and add no task/log polling loop.
  const taskActions = usePluginActionFamily('taskDetailActions')
  const [pluginContextOpen, setPluginContextOpen] = useState(false)
  // Starting an import needs imports:create (Administrator-only by default since
  // 261022-nl6) on the route that a retry re-runs. Read here, above every early
  // return, because hooks must run in the same order on every render.
  const canImportCreate = useCan('imports', 'create')

  const handleDelete = async () => {
    setActionLoading(true)
    try {
      await deleteTask.mutateAsync(task.id)
      navigate('/tasks')
    } catch (err) {
      setActionError(err.response?.data?.detail || t('tasks.error.delete', 'Failed to delete task'))
      setActionLoading(false)
    }
  }

  const handleRetry = async () => {
    setActionLoading(true)
    setActionError(null)
    setActionSuccess(null)
    try {
      const result = await retryTask.mutateAsync(task.id)
      setActionSuccess(result.message || t('tasks.success.retry_initiated', 'Task retry initiated'))
    } catch (err) {
      setActionError(err.response?.data?.detail || t('tasks.error.retry', 'Failed to retry task'))
    } finally {
      setActionLoading(false)
    }
  }

  const handleCancel = async () => {
    setActionLoading(true)
    setActionError(null)
    setActionSuccess(null)
    try {
      await cancelTask.mutateAsync(task.id)
      // D-05/D-06: the click is a request, not a terminal outcome. Do not claim
      // "Task cancelled" — the server-confirmed pill is the only feedback.
    } catch (err) {
      setActionError(err.response?.data?.detail || t('tasks.error.cancel', 'Failed to cancel task'))
    } finally {
      setActionLoading(false)
    }
  }

  if (isLoading) return (
    <div className="space-y-6 max-w-2xl">
      <div className="flex items-center gap-2 text-sm">
        <div className="h-4 w-12 rounded bg-muted animate-skeleton" />
        <span className="text-muted-foreground">/</span>
        <div className="h-4 w-20 rounded bg-muted animate-skeleton" />
      </div>
      <TableSkeleton rows={6} cols={1} />
    </div>
  )
  if (!task) return <div className="text-destructive">{t('tasks.error.not_found', 'Task not found')}</div>

  const isActive = ACTIVE_STATUSES.has(task.current_status)
  const isTerminal = TERMINAL_STATUSES.has(task.current_status)
  // A user without imports:create must not see a Retry that the backend would
  // refuse for an IMPORT run; other task types keep their existing gate.
  const canRetry = RETRYABLE_STATUSES.has(task.current_status)
    && (task.tasktype !== 'IMPORT' || canImportCreate)
  // D-05: the cancel intent comes only from the server response field, never
  // from local click state.
  const cancelRequested = Boolean(task.cancel_requested_at)

  // D-13 defense-in-depth: a failed/dead-letter plugin task with no persisted
  // error_message still shows the localized generic safe fallback — never raw
  // exception text or a stack trace.
  const isPluginTask = !!task.tasktype && !CORE_TASK_TYPES.has(task.tasktype)
  const showGenericPluginError =
    isPluginTask &&
    (task.current_status === 'FAILED' || task.current_status === 'DEAD_LETTER') &&
    !task.error_message
  const errorText = task.error_message
    || (showGenericPluginError
      ? t('tasks.plugin.generic_error', 'The plugin task could not be executed. Check the task status or try again.')
      : null)

  // Phase 247 (D-10): the execution summary's Source/Schema/Object Type rows
  // always show a resolved label or an honest em dash — never a raw UUID.
  // IDs themselves live only in the Technical details disclosure below.
  const sourceLabel = source?.name || '—'
  const schemaLabel = schema?.name || '—'
  const objectTypeLabel = objectType?.name || '—'

  const runtime = isActive
    ? elapsedRuntime(task.started_at)
    : completedRuntime(task.started_at, task.completed_at)

  const currentStep = stepLabel(t, task.current_step)

  return (
    <ErrorBoundary>
      <div className="space-y-6 max-w-4xl">
      <Breadcrumb
        data-testid="breadcrumb-detail"
        items={[
          // D-03 explicit items: deterministic overview link + loaded current
          // label (task.name || type label, same display-safe fallback as the
          // heading — never a raw ID).
          { label: t('tasks.page.title', 'Tasks'), href: '/tasks' },
          { label: task.name || taskTypeLabel(t, task.tasktype) },
        ]}
      />

      {/* Status/context header (D-10) — keeps its 672px width while the page
          wrapper grows to 896px for the log card below (UI-SPEC §Layout). */}
      <div className="rounded-lg border p-6 space-y-5 max-w-2xl">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h1 className="text-2xl font-bold tracking-tight" data-testid="task-detail-heading">
              {task.name || <TaskTypeValue t={t} taskType={task.tasktype} />}
            </h1>
            {task.name && (
              <p className="text-sm text-muted-foreground"><TaskTypeValue t={t} taskType={task.tasktype} /></p>
            )}
          </div>
          <div className="flex shrink-0 flex-wrap items-center justify-end gap-2">
            <span className={`${STATUS_BADGE_CLASS} ${statusClass(task.current_status)}`} data-testid="status-badge">
              {statusLabel(t, task.current_status)}
            </span>
            {cancelRequested && (
              <span className={CANCEL_BADGE_CLASS} data-testid="task-cancel-requested">
                {t('tasks.cancel_requested.label', 'Cancellation requested')}
              </span>
            )}
          </div>
        </div>

        {/* Execution summary (D-10): current step/progress, context, timing */}
        <div className="space-y-2" data-testid="execution-summary">
           <h2 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider">{t('tasks.detail.execution', 'Execution')}</h2>
          <TaskProgress
            task={task}
            stepLabel={
              currentStep.mono ? (
                <span className="font-mono break-all" title={currentStep.text} aria-label={currentStep.text}>
                  {currentStep.text}
                </span>
              ) : currentStep.empty ? (
                <span className="text-muted-foreground">{currentStep.text}</span>
              ) : currentStep.text
            }
          />
          {cancelRequested && (
            <p className="text-sm text-muted-foreground" data-testid="task-cancel-requested-body">
              {t('tasks.cancel_requested.body', 'The task stops after the current operation and saves its complete log first.')}
            </p>
          )}
          <div className="space-y-1.5 pt-1">
            <InfoRow label={t('tasks.detail.source', 'Source')} value={sourceLabel} />
            <InfoRow label={t('tasks.detail.schema', 'Schema')} value={schemaLabel} />
            <InfoRow label={t('tasks.detail.object_type', 'Object Type')} value={objectTypeLabel} />
            <InfoRow label={t('tasks.detail.started', 'Started')} value={task.started_at ? formatDate(task.started_at) : '—'} />
            {runtime && <InfoRow label={t('tasks.detail.runtime', 'Runtime')} value={runtime} />}
            {isTerminal && (
              <InfoRow label={t('tasks.detail.completed', 'Completed')} value={task.completed_at ? formatDate(task.completed_at) : '—'} />
            )}
          </div>
        </div>

        {/* Distinct error panel (D-10) — separate from action success feedback,
            never replaced or hidden by it. */}
        {errorText && (
          <div className="rounded-md border border-destructive/30 bg-destructive/10 p-4" data-testid="task-error">
             <h2 className="text-sm font-semibold text-destructive mb-1">{t('tasks.detail.error_heading', 'Error')}</h2>
            <p className="text-sm text-destructive whitespace-pre-wrap break-words">{errorText}</p>
          </div>
        )}

        {/* Actions — same status gates as TaskList.jsx. Phase-303 plugin actions
            append after the Core Retry/Cancel/Delete in the same row. */}
        <div className="flex flex-wrap items-start gap-2">
          {canRetry && (
            <Button
              type="button"
              variant="primary"
              size="md"
              onClick={handleRetry}
              disabled={actionLoading}
              loading={actionLoading}
            >
               {t('tasks.action.retry_task', 'Retry Task')}
            </Button>
          )}
          {isActive && !cancelRequested && (
            <Button
              type="button"
              variant="outline"
              size="md"
              onClick={handleCancel}
              disabled={actionLoading}
              loading={actionLoading}
              className="text-destructive border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
            >
               {t('tasks.action.cancel_task', 'Cancel Task')}
            </Button>
          )}
          {isTerminal && (
            <Button
              type="button"
              variant="outline"
              size="md"
              onClick={handleDelete}
              disabled={actionLoading}
              loading={actionLoading}
              className="text-destructive border-destructive/30 hover:bg-destructive/10 hover:text-destructive"
            >
               {t('tasks.action.delete_task', 'Delete Task')}
            </Button>
          )}
          {task.tasktype === 'EXPORT' && task.current_status === 'SUCCESS' && task.export_id && (
            <Button
              type="button"
              variant="outline"
              size="md"
              onClick={() => downloadExportFile(task.export_id).catch(() => {})}
              data-testid="task-export-download-btn"
            >
               {t('settings.export.action.download', 'Download')}
            </Button>
          )}
          {taskActions.map((entry) => {
            const TaskAction = entry.component
            return (
              <div key={`${entry.pluginName}:${entry.id}`} className="min-w-0">
                <PluginContributionBoundary
                  slot={`taskDetailActions:${entry.id}`}
                  pluginName={entry.pluginName}
                  label={entry.displayLabel}
                  owner={entry.owner}
                >
                  <TaskAction
                    task={{
                      id: task.id,
                      tasktype: task.tasktype,
                      current_status: task.current_status,
                      source_id: task.source_id,
                      schema_id: task.schema_id,
                    }}
                    taskId={task.id}
                    onToggle={() => setPluginContextOpen((open) => !open)}
                  />
                </PluginContributionBoundary>
                <p className="mt-1 text-xs text-muted-foreground break-words">
                  {interpolateDefault(t('plugins.sections.provided_by', 'Provided by plugin "{{plugin}}"'), { plugin: entry.owner })}
                </p>
              </div>
            )
          })}
        </div>

        {pluginContextOpen && (
          <div className="rounded-md border p-4 space-y-1.5" data-testid="task-plugin-context">
            <InfoRow label={t('tasks.detail.task_id', 'Task ID')} value={task.id} />
            <InfoRow label={t('tasks.detail.type', 'Type')} value={task.tasktype} />
            <InfoRow label={t('tasks.detail.status', 'Status')} value={statusLabel(t, task.current_status)} />
            <InfoRow label={t('tasks.detail.source', 'Source')} value={sourceLabel} />
            <InfoRow label={t('tasks.detail.schema', 'Schema')} value={schemaLabel} />
          </div>
        )}

        {actionError && (
          <div className="rounded-md bg-destructive/10 p-3 text-sm text-destructive" data-testid="task-action-error">
            {actionError}
          </div>
        )}
        {actionSuccess && (
          <div className="rounded-md bg-green-100 dark:bg-green-900/30 p-3 text-sm text-green-800 dark:text-green-200" data-testid="task-action-success">
            {actionSuccess}
          </div>
        )}

        {/* Technical details (D-10): every UUID/technical identifier lives
            here, never in the title or execution summary above. Native
            <details>/<summary> — accessible disclosure semantics for free. */}
        <details className="rounded-md border p-3" data-testid="technical-details">
          <summary className="cursor-pointer text-sm font-medium select-none">
            {t('tasks.detail.technical_details', 'Technical details')}
          </summary>
          <div className="mt-3 space-y-1.5">
            <InfoRow label={t('tasks.detail.task_id', 'Task ID')} value={task.id} />
            <InfoRow label={t('tasks.detail.type', 'Type')} value={task.tasktype} />
            <InfoRow label={t('tasks.detail.created', 'Created')} value={formatDate(task.created_at)} />
            <InfoRow label={t('tasks.detail.import_id', 'Import ID')} value={task.import_id} />
            <InfoRow label={t('tasks.detail.export_id', 'Export ID')} value={task.export_id} />
            <InfoRow label={t('tasks.detail.schedule_id', 'Schedule ID')} value={task.schedule_id} />
            <InfoRow label={t('tasks.detail.parent_task', 'Parent Task')} value={task.parent_task_id} />
            <InfoRow label={t('tasks.detail.target', 'Target')} value={target?.name ?? task.target_id} />
            <InfoRow label={t('tasks.detail.service_account', 'Service Account')} value={serviceAccount?.name ?? task.service_account_id} />
            <InfoRow label={t('tasks.detail.import_definition', 'Import Definition')} value={importDefinition?.name ?? task.import_definition_id} />
            <InfoRow label={t('tasks.detail.export_definition', 'Export Definition')} value={exportDefinition?.name ?? task.export_definition_id} />
            <InfoRow label={t('tasks.detail.sort', 'Sort')} value={task.sort !== null && task.sort !== undefined ? String(task.sort) : null} />
            <InfoRow label={t('tasks.detail.permanent', 'Permanent')} value={task.is_permanent ? t('common.yes', 'Yes') : t('common.no', 'No')} />
          </div>
        </details>
      </div>

      {/* Phase 300 (D-01): the saved log is its own card directly below the
          status card, wrapped in its own ErrorBoundary so a log render fault
          never blanks the status/actions above. */}
      <ErrorBoundary>
        <TaskLogPanel taskId={task.id} status={task.current_status} />
      </ErrorBoundary>
    </div>
    </ErrorBoundary>
  )
}
