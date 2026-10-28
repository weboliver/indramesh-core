import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'

// Phase 247 (UX-04, D-12/D-13/D-14), Phase 296 (CLEAN-01): the single shared,
// read-only progress presentation for the Task list, Task detail, and the CSV
// run-details modal. Consumes ONLY the server-observed TaskResponse counters
// (processed_objects/total_objects — see app/schemas/crud.py TaskResponse,
// Plan 247-01) and never derives a count from timestamps, step count, or an
// estimate. IMPORT uses "imported" wording; EXPORT uses "exported" wording; any
// other task type (a plugin type, or REINDEX which has no object counter) shows
// only its step (no counter, no bar) — it has no object-count progress source.
const IMPORT_WORDING_TYPES = new Set(['IMPORT'])
const EXPORT_WORDING_TYPES = new Set(['EXPORT'])

export default function TaskProgress({ task, stepLabel, className = '' }) {
  const { t } = useTranslation()
  if (!task) return null

  const { processed_objects: processed, total_objects: total, tasktype, current_status: status } = task

  const isImportWording = IMPORT_WORDING_TYPES.has(tasktype)
  const isExportWording = EXPORT_WORDING_TYPES.has(tasktype)
  const hasProgressSource = isImportWording || isExportWording
  const isActive = status === 'RUNNING' || status === 'PENDING'
  const isFailedTerminal = status === 'FAILED' || status === 'DEAD_LETTER'

  const hasKnownProcessed = processed !== null && processed !== undefined
  // A known total of exactly 0 stays in the "processed-only" branch below
  // (D-13: never show "0 of 0" or an invented percentage) — a real known
  // zero processed count is still shown as text, just without a percentage.
  const hasKnownTotal = hasKnownProcessed && total !== null && total !== undefined && total > 0

  let counterText = null
  let percent = null

  if (hasProgressSource) {
    if (hasKnownTotal) {
      percent = Math.min(100, Math.round((processed / total) * 100))
      counterText = interpolateDefault(
        isImportWording
          ? t('tasks.progress.imported', '{{processed}} of {{total}} objects imported')
          : t('tasks.progress.exported', '{{processed}} of {{total}} objects exported'),
        { processed, total }
      )
    } else if (hasKnownProcessed) {
      counterText = interpolateDefault(
        t('tasks.progress.processed_unknown_total', '{{processed}} objects processed'),
        { processed }
      )
    } else if (isActive) {
      counterText = t('tasks.progress.preparing', 'Preparing object count…')
    }
  }

  // Once terminal, a still-unknown total has nothing left to animate toward —
  // the bar only renders for a determinate result or while actively running.
  const showBar = hasProgressSource && (percent !== null || isActive)

  return (
    <div className={`space-y-1 ${className}`} data-testid="task-progress">
      {stepLabel && (
        <div className="break-all text-xs font-medium text-muted-foreground" data-testid="task-progress-step">{stepLabel}</div>
      )}
      {counterText && (
        <div className="text-sm" aria-live="polite" data-testid="task-progress-counter">{counterText}</div>
      )}
      {showBar && (
        <div
          role="progressbar"
          aria-label={t('tasks.progress.label', 'Progress')}
          aria-valuenow={percent !== null ? percent : undefined}
          aria-valuemin={percent !== null ? 0 : undefined}
          aria-valuemax={percent !== null ? 100 : undefined}
          className="h-1.5 w-full max-w-[180px] overflow-hidden rounded-full bg-muted"
          data-testid="task-progress-bar"
        >
          {percent !== null ? (
            <div
              className={`h-full rounded-full transition-all ${
                isFailedTerminal ? 'bg-destructive' : isActive ? 'bg-primary' : 'bg-muted-foreground/50'
              }`}
              style={{ width: `${percent}%` }}
            />
          ) : (
            <div className="h-full w-1/4 rounded-full bg-primary animate-indeterminate motion-reduce:w-full motion-reduce:animate-none motion-reduce:bg-muted-foreground/30" />
          )}
        </div>
      )}
    </div>
  )
}
