import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { useTaskLogs } from '../../shared/hooks/use-task-logs'
import { useFormatDateTime, useFormatTime } from '../../shared/hooks/use-format-date'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import api from '../../shared/hooks/use-api'
import { Button } from '../../shared/components/Button'
import SegmentedControl from '../../shared/components/SegmentedControl'
import ChevronRight from 'lucide-react/dist/esm/icons/chevron-right'
import ScissorsLineDashed from 'lucide-react/dist/esm/icons/scissors-line-dashed'
import CircleAlert from 'lucide-react/dist/esm/icons/circle-alert'
import ScrollText from 'lucide-react/dist/esm/icons/scroll-text'
import { groupTaskLogEntries, isTaskLogAtEnd, resolveLogAnchor } from './task-log-state'
import { stepLabel } from './task-labels'

const ACTIVE_STATUSES = new Set(['PENDING', 'RUNNING'])

function levelTextClass(level) {
  if (level === 'WARNING') return 'text-amber-700 dark:text-amber-300'
  if (level === 'ERROR' || level === 'CRITICAL') return 'text-red-700 dark:text-red-400'
  return 'text-muted-foreground'
}

function levelRailClass(level) {
  if (level === 'WARNING') return 'border-amber-500'
  if (level === 'ERROR' || level === 'CRITICAL') return 'border-red-600 dark:border-red-500'
  return 'border-transparent'
}

function levelRowClass(level) {
  if (level === 'ERROR') return 'bg-red-500/5'
  if (level === 'CRITICAL') return 'bg-red-500/10'
  return ''
}

function formatDetailValue(value) {
  if (value === null || value === undefined) return '—'
  if (typeof value === 'string') return value
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  try {
    return JSON.stringify(value)
  } catch {
    return String(value)
  }
}

// Count copy with an `_one`/`_other` fallback independent of the runtime
// catalog, so the panel reads correctly before the catalogs ship.
function pluralText(t, base, count, otherDefault, oneDefault, formatNumber) {
  const key = selectPluralKey(base, count)
  const fallback = key.endsWith('_one') ? (oneDefault ?? otherDefault) : otherDefault
  return interpolateDefault(t(key, fallback), { count: formatNumber ? formatNumber(count) : count })
}

function LogDetails({ item }) {
  const { t } = useTranslation()
  const formatDateTime = useFormatDateTime()
  const label = stepLabel(t, item.step, { noStepKey: 'tasks.log.no_step', noStepDefault: 'No work step' })
  const fields = item.details && typeof item.details === 'object' ? Object.entries(item.details) : []

  return (
    <dl
      data-testid="task-log-line-details"
      className="sm:ml-[296px] mt-1 mb-2 rounded-md border bg-background p-2 grid grid-cols-[auto_1fr] gap-x-4 gap-y-1 text-xs"
    >
      <dt className="text-muted-foreground">{t('tasks.log.detail.time', 'Time')}</dt>
      <dd>{formatDateTime(item.emitted_at)}</dd>
      <dt className="text-muted-foreground">{t('tasks.log.detail.logger', 'Logger')}</dt>
      <dd className="font-mono break-all">{item.logger || '—'}</dd>
      <dt className="text-muted-foreground">{t('tasks.log.detail.attempt', 'Attempt')}</dt>
      <dd>{item.attempt}</dd>
      <dt className="text-muted-foreground">{t('tasks.log.detail.step', 'Step')}</dt>
      <dd className={label.mono ? 'font-mono break-all' : undefined}>{label.text}</dd>
      {fields.map(([key, value]) => (
        <Fragment key={key}>
          <dt className="font-mono text-muted-foreground break-all">{key}</dt>
          <dd className="font-mono break-all">{formatDetailValue(value)}</dd>
        </Fragment>
      ))}
    </dl>
  )
}

function LogLine({ item, open, onToggle }) {
  const { t } = useTranslation()
  const formatTime = useFormatTime()
  const formatDateTime = useFormatDateTime()
  const isCritical = item.level === 'CRITICAL'

  const toggle = (
    <button
      type="button"
      className="inline-flex h-6 w-6 shrink-0 items-center justify-center rounded hover:bg-accent focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
      aria-expanded={open}
      aria-label={open ? t('tasks.log.details_hide', 'Hide details') : t('tasks.log.details_show', 'Show details')}
      onClick={() => onToggle(item.sequence)}
    >
      <ChevronRight size={14} className={`motion-safe:transition-transform ${open ? 'rotate-90' : ''}`} />
    </button>
  )

  return (
    <div
      data-testid="task-log-line"
      data-level={item.level}
      data-sequence={item.sequence}
      className={`border-l-2 px-2 py-1 ${levelRailClass(item.level)} ${levelRowClass(item.level)}`}
    >
      {/* ≥ 640px: fixed grid (time / level / category / message / toggle) */}
      <div className="hidden sm:grid grid-cols-[64px_80px_128px_minmax(0,1fr)_24px] gap-x-2 items-start">
        <span
          className="font-mono tabular-nums text-xs text-muted-foreground"
          title={formatDateTime(item.emitted_at)}
        >
          {formatTime(item.emitted_at)}
        </span>
        <span className={`text-xs font-semibold ${levelTextClass(item.level)} ${isCritical ? 'underline decoration-2' : ''}`}>
          {item.level}
        </span>
        <span className="font-mono text-xs text-muted-foreground truncate" title={item.category}>
          {item.category}
        </span>
        <span className="text-sm whitespace-pre-wrap break-words">{item.message}</span>
        {toggle}
      </div>
      {/* < 640px: meta on the first line, message on its own full-width line */}
      <div className="flex sm:hidden flex-wrap gap-x-2 items-start">
        <span className="font-mono tabular-nums text-xs text-muted-foreground" title={formatDateTime(item.emitted_at)}>
          {formatTime(item.emitted_at)}
        </span>
        <span className={`text-xs font-semibold ${levelTextClass(item.level)} ${isCritical ? 'underline decoration-2' : ''}`}>
          {item.level}
        </span>
        <span className="font-mono text-xs text-muted-foreground truncate max-w-[128px]" title={item.category}>
          {item.category}
        </span>
        {toggle}
        <span className="w-full text-sm whitespace-pre-wrap break-words">{item.message}</span>
      </div>
      {open && <LogDetails item={item} />}
    </div>
  )
}

function GapMarker({ count }) {
  const { t, i18n } = useTranslation()
  const number = new Intl.NumberFormat(i18n.language)
  return (
    <div
      data-testid="task-log-gap"
      className="my-2 mx-2 flex items-center gap-2 rounded-md border border-dashed px-2 py-2"
    >
      <ScissorsLineDashed size={14} className="shrink-0 text-muted-foreground" />
      <span className="text-sm font-semibold">
        {pluralText(t, 'tasks.log.dropped', count, '{{count}} lines dropped', '{{count}} line dropped', number.format)}
      </span>
      <span className="text-xs text-muted-foreground">
        {t('tasks.log.dropped_body', 'Line limit for this task reached. The beginning and end of the log are kept.')}
      </span>
    </div>
  )
}

function SegmentSection({ segment, collapsed, onToggle, openLines, onToggleLine }) {
  const { t, i18n } = useTranslation()
  const formatTime = useFormatTime()
  const label = stepLabel(t, segment.step, { noStepKey: 'tasks.log.no_step', noStepDefault: 'No work step' })
  const items = segment.items
  const errorCount = items.filter((item) => item.level === 'ERROR' || item.level === 'CRITICAL').length
  const number = new Intl.NumberFormat(i18n.language)
  const start = items[0]?.emitted_at
  const end = items[items.length - 1]?.emitted_at
  const open = collapsed[segment.key] !== true

  return (
    <details
      open={open}
      onToggle={(event) => onToggle(segment.key, event.currentTarget.open)}
      data-testid={`task-log-step-${segment.attempt}-${segment.segment}`}
      className="[content-visibility:auto] [contain-intrinsic-size:auto_480px]"
    >
      <summary className="sticky top-0 z-10 bg-background/95 px-2 py-2 flex items-center gap-2 border-b cursor-pointer select-none">
        <ChevronRight
          size={14}
          className={`shrink-0 motion-safe:transition-transform ${open ? 'rotate-90' : ''}`}
        />
        <span className={`text-sm font-semibold ${label.mono ? 'font-mono break-all' : ''}`}>{label.text}</span>
        <span className="text-xs text-muted-foreground">
          {pluralText(t, 'tasks.log.line_count', items.length, '{{count}} lines', '{{count}} line', number.format)}
        </span>
        {errorCount > 0 && (
          <span className="text-xs text-red-700 dark:text-red-400">
            {pluralText(t, 'tasks.log.step_errors', errorCount, '{{count}} errors', '{{count}} error', number.format)}
          </span>
        )}
        {start && end && (
          <span className="text-xs text-muted-foreground">
            {formatTime(start)}–{formatTime(end)}
          </span>
        )}
      </summary>
      {items.map((item) => {
        const showGapAfter = segment.gapAfterSequence !== null && item.sequence === segment.gapAfterSequence
        return (
          <Fragment key={item.sequence}>
            <LogLine item={item} open={openLines[item.sequence] === true} onToggle={onToggleLine} />
            {showGapAfter && <GapMarker count={segment.gapCount} />}
          </Fragment>
        )
      })}
    </details>
  )
}

function SkeletonRows() {
  const { t } = useTranslation()
  return (
    <div className="space-y-2 p-2">
      <span className="sr-only">{t('tasks.log.loading', 'Loading log…')}</span>
      {[100, 80, 60].map((width) => (
        <div key={width} className="h-4 rounded bg-muted animate-skeleton" style={{ width: `${width}%` }} />
      ))}
    </div>
  )
}

function LogEmptyBlock({ testId, title, body, onAction, actionLabel }) {
  return (
    <div data-testid={testId} role="status" className="flex flex-col items-center gap-1 px-4 py-8 text-center">
      <ScrollText size={20} className="text-muted-foreground" />
      <p className="text-sm font-medium">{title}</p>
      <p className="text-xs text-muted-foreground">{body}</p>
      {onAction && (
        <Button type="button" variant="link" size="sm" onClick={onAction}>
          {actionLabel}
        </Button>
      )}
    </div>
  )
}

export default function TaskLogPanel({ taskId, status }) {
  const { t, i18n } = useTranslation()
  const [filter, setFilter] = useState('all')
  const { data, isLoading, isError, refetch } = useTaskLogs(taskId, status, filter)
  const [collapsed, setCollapsed] = useState({})
  const [openLines, setOpenLines] = useState({})
  const [following, setFollowing] = useState(true)
  const [atEnd, setAtEnd] = useState(true)
  const [unseen, setUnseen] = useState(false)
  const [unseenCount, setUnseenCount] = useState(0)
  const [downloading, setDownloading] = useState(false)
  const [downloadError, setDownloadError] = useState(false)
  const scrollRef = useRef(null)
  const previousCountRef = useRef(0)
  const programmaticScrollRef = useRef(false)
  const revisionRef = useRef(null)
  const firstVisibleRef = useRef(null)

  const items = useMemo(() => data?.items || [], [data])
  const revision = data?.pruningRevision ?? null
  const loaded = data?.loaded === true
  const isActive = ACTIVE_STATUSES.has(status)
  const storedCount = data?.storedCount || 0
  const number = new Intl.NumberFormat(i18n.language)

  const scrollToEnd = useCallback(() => {
    const el = scrollRef.current
    if (!el) return
    programmaticScrollRef.current = true
    el.scrollTop = el.scrollHeight
    setFollowing(true)
    setAtEnd(true)
    setUnseen(false)
    setUnseenCount(0)
  }, [])

  const handleScroll = useCallback(() => {
    if (programmaticScrollRef.current) {
      programmaticScrollRef.current = false
      return
    }
    const el = scrollRef.current
    if (!el) return
    const end = isTaskLogAtEnd(el)
    setAtEnd(end)
    setFollowing(end && isActive)
    if (!end) {
      const lines = el.querySelectorAll('[data-testid="task-log-line"]')
      for (const line of lines) {
        if (line.offsetTop >= el.scrollTop) {
          firstVisibleRef.current = Number(line.dataset.sequence)
          break
        }
      }
    }
  }, [isActive])

  const toggleSegment = useCallback((key, isOpen) => {
    setCollapsed((prev) => ({ ...prev, [key]: !isOpen }))
  }, [])

  const toggleLine = useCallback((sequence) => {
    setOpenLines((prev) => ({ ...prev, [sequence]: !prev[sequence] }))
  }, [])

  // On first successful load and after every filter change, position at the
  // end and resume following (UI-SPEC §Live follow).
  useEffect(() => {
    if (loaded) scrollToEnd()
  }, [filter, loaded, scrollToEnd])

  // Follow new lines while at the end; otherwise only announce them.
  useEffect(() => {
    const count = items.length
    if (following && loaded) {
      scrollToEnd()
    } else if (count > previousCountRef.current) {
      setUnseen(true)
      setUnseenCount(count - previousCountRef.current)
    }
    previousCountRef.current = count
  }, [items, following, loaded, scrollToEnd])

  // A changed pruning revision replaced the cached rows. While paused, keep
  // the first visible line (or the dropped marker / nearest retained line) in
  // view instead of jumping to the top; while following, the effect above
  // scrolls to the end (UI-SPEC §Polling).
  useEffect(() => {
    const el = scrollRef.current
    const previous = revisionRef.current
    revisionRef.current = revision
    if (previous === null || revision === null || revision === previous || following || !el) return
    const anchorSequence = resolveLogAnchor(items, firstVisibleRef.current)
    if (anchorSequence === null) return
    const target = el.querySelector(`[data-sequence="${anchorSequence}"]`)
    if (target) {
      programmaticScrollRef.current = true
      el.scrollTop = target.offsetTop
    }
  }, [revision, items, following])

  const handleDownload = useCallback(async () => {
    setDownloading(true)
    setDownloadError(false)
    try {
      const response = await api.get(`/tasks/${taskId}/logs`, {
        params: { download: true },
        responseType: 'blob',
      })
      const disposition = response.headers['content-disposition'] || ''
      const filename = disposition.match(/filename="([^"]+)"/)?.[1] || `task-${taskId}.log`
      const url = URL.createObjectURL(new Blob([response.data], { type: 'text/plain;charset=utf-8' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      anchor.click()
      URL.revokeObjectURL(url)
    } catch {
      setDownloadError(true)
    } finally {
      setDownloading(false)
    }
  }, [taskId])

  const attempts = groupTaskLogEntries(items, data?.gap || null)
  const showAttemptHeaders = attempts.length > 1
  const filteredEmpty = filter !== 'all' && items.length === 0 && storedCount > 0
  const terminalEmpty = items.length === 0 && loaded && !data?.expired && !filteredEmpty

  return (
    <section
      aria-labelledby="task-log-heading"
      data-testid="task-log"
      className="rounded-lg border p-6 space-y-4"
    >
      <div className="flex flex-wrap items-center gap-x-4 gap-y-2">
        <h2 id="task-log-heading" className="text-base font-semibold">
          {t('tasks.log.title', 'Log')}
        </h2>
        {loaded && (
          <span className="text-xs text-muted-foreground">
            {pluralText(t, 'tasks.log.line_count', storedCount, '{{count}} lines', '{{count}} line', number.format)}
          </span>
        )}
        {isActive && (
          <span className="flex items-center gap-1 text-xs text-muted-foreground">
            <span
              className={`inline-block h-2 w-2 rounded-full ${
                following ? 'bg-primary' : 'border border-muted-foreground'
              }`}
            />
            {following
              ? t('tasks.log.following', 'Following new entries')
              : t('tasks.log.paused', 'Following paused')}
          </span>
        )}
        <div className="ml-auto flex flex-wrap items-center gap-x-4 gap-y-2">
          <div role="group" aria-label={t('tasks.log.filter_label', 'Minimum level')}>
            <SegmentedControl
              name="task-log-level"
              active={filter}
              onChange={setFilter}
              options={[
                { value: 'all', label: t('tasks.log.filter.all', 'All') },
                { value: 'warning', label: t('tasks.log.filter.warning', 'Warnings') },
                { value: 'error', label: t('tasks.log.filter.error', 'Errors') },
              ]}
            />
          </div>
          <Button
            type="button"
            variant="outline"
            size="sm"
            leftIcon="download"
            onClick={handleDownload}
            disabled={downloading || storedCount === 0 || data?.expired === true}
            loading={downloading}
            data-testid="task-log-download"
          >
            {t('tasks.log.download', 'Download log')}
          </Button>
        </div>
      </div>

      {filter !== 'all' && (
        <p className="text-xs text-muted-foreground">
          {t('tasks.log.download_hint', 'The download contains all levels.')}
        </p>
      )}

      {downloadError && (
        <p className="text-xs text-red-700 dark:text-red-400" role="alert" data-testid="task-log-download-error">
          {t('tasks.log.download_error', 'The log could not be downloaded. Try again.')}
        </p>
      )}

      {isError && !loaded ? (
        <div data-testid="task-log-error" role="status" className="flex flex-col items-center gap-2 px-4 py-8 text-center">
          <CircleAlert size={20} className="text-red-700 dark:text-red-400" />
          <p className="text-sm">{t('tasks.log.load_error', 'The log could not be loaded. Try again.')}</p>
          <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
            {t('tasks.log.load_again', 'Load again')}
          </Button>
        </div>
      ) : (
        <>
          {isError && loaded && (
            <p className="text-xs text-amber-700 dark:text-amber-300" role="status" data-testid="task-log-poll-error">
              {t('tasks.log.poll_error', 'Updates interrupted: the server is not responding. Retrying automatically.')}
            </p>
          )}
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            tabIndex={0}
            role="region"
            aria-label={t('tasks.log.region_label', 'Task log entries')}
            aria-busy={isLoading || undefined}
            data-testid="task-log-scroll"
            className="relative max-h-[480px] overflow-y-auto rounded-md border bg-muted/20 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {isLoading ? (
              <SkeletonRows />
            ) : data?.expired ? (
              <LogEmptyBlock
                testId="task-log-expired"
                title={t('tasks.log.expired_title', 'Log expired')}
                body={t('tasks.log.expired_body', 'Log entries are deleted automatically after the retention period. The task itself is kept.')}
              />
            ) : filteredEmpty ? (
              <LogEmptyBlock
                testId="task-log-empty"
                title={
                  filter === 'error'
                    ? t('tasks.log.filtered_empty_error', 'No errors')
                    : t('tasks.log.filtered_empty_warning', 'No warnings or errors')
                }
                body={t('tasks.log.filtered_empty_body', 'Choose All to see every entry.')}
                onAction={() => setFilter('all')}
                actionLabel={t('tasks.log.show_all', 'Show all')}
              />
            ) : terminalEmpty ? (
              <LogEmptyBlock
                testId="task-log-empty"
                title={
                  isActive
                    ? t('tasks.log.empty_title', 'No log entries yet')
                    : t('tasks.log.none_title', 'No log recorded')
                }
                body={
                  isActive
                    ? (status === 'PENDING'
                        ? t('tasks.log.empty_pending', 'Entries appear here as soon as the task starts.')
                        : t('tasks.log.empty_running', 'The task is running. New entries appear automatically.'))
                    : t('tasks.log.none_body', 'This task did not write any log entries.')
                }
              />
            ) : (
              <>
                {attempts.map((attempt) => (
                  <div key={attempt.attempt}>
                    {showAttemptHeaders && (
                      <div
                        className="px-2 pt-4 pb-2 text-sm font-semibold"
                        data-testid={`task-log-attempt-${attempt.attempt}`}
                      >
                        {interpolateDefault(t('tasks.log.attempt', 'Attempt {{number}}'), { number: attempt.attempt })}
                      </div>
                    )}
                    {attempt.blocks.map((block) =>
                      block.kind === 'gap' ? (
                        <GapMarker key={block.key} count={block.count} />
                      ) : (
                        <SegmentSection
                          key={block.key}
                          segment={{ ...block, gapCount: data?.gap?.count ?? 0 }}
                          collapsed={collapsed}
                          onToggle={toggleSegment}
                          openLines={openLines}
                          onToggleLine={toggleLine}
                        />
                      )
                    )}
                  </div>
                ))}
              </>
            )}
          </div>
          {!atEnd && loaded && items.length > 0 && (
            <div className="flex justify-end">
              <Button
                type="button"
                variant={unseen ? 'primary' : 'outline'}
                size="sm"
                leftIcon="arrow-down-to-line"
                onClick={scrollToEnd}
                data-testid="task-log-jump-latest"
              >
                {t('tasks.log.jump_latest', 'Jump to latest entry')}
              </Button>
            </div>
          )}
          {!following && unseen && (
            <span className="sr-only" aria-live="polite">
              {pluralText(
                t,
                'tasks.log.new_lines',
                unseenCount,
                '{{count}} new log lines',
                '{{count}} new log line',
                number.format
              )}
            </span>
          )}
        </>
      )}
    </section>
  )
}
