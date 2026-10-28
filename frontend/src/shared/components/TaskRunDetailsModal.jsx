import { useImportRecord, useExportRecord } from '../hooks/use-other-resources'
import { useFormatDate } from '../hooks/use-format-date'
import { Button } from './Button'
import TaskProgress from './TaskProgress'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { selectPluralKey } from '../lib/i18n'

// Task.error_message only ever reflects a whole-run exception (see worker.py
// process_import/process_export) — per-row CSV import failures live on the
// related Import record's error_message as a JSON array of {row_number, reason}
// (import_execution_service.execute_csv_import). This tries to parse that;
// plain-text error_message (export failures, whole-import exceptions) falls
// through to being rendered as-is.
function parseRowErrors(errorMessage) {
  if (!errorMessage) return null
  try {
    const parsed = JSON.parse(errorMessage)
    if (Array.isArray(parsed)) return parsed
  } catch {
    return null
  }
  return null
}

export default function TaskRunDetailsModal({ task, kind, onClose }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const importQuery = useImportRecord(kind === 'import' ? task?.import_id : undefined)
  const exportQuery = useExportRecord(kind === 'export' ? task?.export_id : undefined)
  const { data: record, isLoading } = kind === 'import' ? importQuery : exportQuery

  if (!task) return null

  const rowErrors = parseRowErrors(record?.error_message)

  // Phase 247 (D-12): prefer the durable Task projection (present on every
  // Task response since Plan 247-01) and fall back to the linked CSV
  // record's counters only where the Task itself has not observed any yet —
  // one shared progress presentation instead of reading record fields raw.
  const progressTask = {
    ...task,
    processed_objects: task.processed_objects ?? record?.processed_objects ?? null,
    total_objects: task.total_objects ?? record?.total_objects ?? null,
  }
  const hasProgress = progressTask.processed_objects !== null && progressTask.processed_objects !== undefined

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="rounded-lg border bg-card p-4 w-full max-w-2xl shadow-xl max-h-[80vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between mb-3">
           <h2 className="text-lg font-semibold">{t('tasks.run_details', 'Run Details')}</h2>
           <Button type="button" variant="outline" size="sm" onClick={onClose}>{t('common.close', 'Close')}</Button>
        </div>

        <dl className="grid grid-cols-2 gap-x-4 gap-y-1 text-sm mb-4">
           <dt className="text-muted-foreground">{t('common.status', 'Status')}</dt>
          <dd>{task.current_status}</dd>
           <dt className="text-muted-foreground">{t('common.started', 'Started')}</dt>
          <dd>{formatDate(task.started_at)}</dd>
           <dt className="text-muted-foreground">{t('common.completed', 'Completed')}</dt>
          <dd>{formatDate(task.completed_at)}</dd>
          {hasProgress && (
            <>
               <dt className="text-muted-foreground">{t('tasks.objects_processed', 'Objects Processed')}</dt>
              <dd><TaskProgress task={progressTask} /></dd>
            </>
          )}
        </dl>

        {task.error_message && (
          <div className="mb-4">
             <h3 className="text-sm font-medium mb-1">{t('tasks.task_error', 'Task Error')}</h3>
            <p className="text-sm text-destructive whitespace-pre-wrap break-words">{task.error_message}</p>
          </div>
        )}

        {isLoading && (
           <p className="text-sm text-muted-foreground">{t('tasks.loading_run_details', 'Loading run details…')}</p>
        )}

        {rowErrors ? (
          <div>
            <h3 className="text-sm font-medium mb-1">{interpolateDefault(
              selectPluralKey('tasks.row_errors', rowErrors.length) === 'tasks.row_errors_one'
                ? t('tasks.row_errors_one', 'Row Errors ({{count}})')
                : t('tasks.row_errors_other', 'Row Errors ({{count}})'),
              { count: rowErrors.length }
            )}</h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                   <th className="text-left py-1 pr-2">{t('common.row', 'Row')}</th>
                   <th className="text-left py-1 pr-2">{t('common.reason', 'Reason')}</th>
                </tr>
              </thead>
              <tbody>
                {rowErrors.map((e, i) => (
                  <tr key={i} className="border-b last:border-0">
                    <td className="py-1 pr-2 text-muted-foreground">{e.row_number ?? '-'}</td>
                    <td className="py-1 pr-2 text-destructive">{e.reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        ) : record?.error_message ? (
          <div>
             <h3 className="text-sm font-medium mb-1">{t('common.error', 'Error')}</h3>
            <p className="text-sm text-destructive whitespace-pre-wrap break-words">{record.error_message}</p>
          </div>
        ) : (
          !isLoading && !task.error_message && (
             <p className="text-sm text-muted-foreground">{t('tasks.no_errors_recorded', 'No errors recorded for this run.')}</p>
          )
        )}
      </div>
    </div>
  )
}
