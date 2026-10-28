// Pure task-log helpers — no React or Vite imports. Consumed by
// shared/hooks/use-task-logs.js and features/tasks/TaskLogPanel.jsx, and
// exercised directly by task-log-state.test.js (node --test).
//
// The API contract is frozen by Phase 300 Plan 03: a page carries
// `items`, `next_after`, `has_more`, `pruning_revision`, `dropped_count`,
// `stored_count`, `attempt_count`, `expired` and `gap`. A changed
// `pruning_revision` means the server pruned/reloaded history, so the client
// discards its cache and reloads from `after=0` (D-04/D-10).

const LOG_END_THRESHOLD_PX = 24
export const LOG_POLL_INTERVAL_MS = 2000

// Empty cache shape. `loaded` distinguishes "never fetched" (cursor starts at
// 0 and every page is loaded) from "fetched, following the cursor".
export function createTaskLogState() {
  return {
    items: [],
    nextAfter: 0,
    hasMore: false,
    pruningRevision: null,
    attemptCount: 0,
    storedCount: 0,
    droppedCount: 0,
    expired: false,
    gap: null,
    loaded: false,
  }
}

// Minimum-level semantics (D-04): All = INFO+, Warnings = WARNING+,
// Errors = ERROR+ (the server includes CRITICAL in ERROR).
export function minLevelFor(filter) {
  if (filter === 'warning') return 'WARNING'
  if (filter === 'error') return 'ERROR'
  return 'INFO'
}

// Merge one page into the cache. Appends new rows without duplicates, keeps
// the rows ordered by sequence, and advances the cursor to the server's
// scanned-sequence position (so a fully filtered page still moves forward).
// Returns `{ reset: true }` when the server's pruning revision changed — the
// caller then starts a fresh cache from `after=0`.
export function mergeTaskLogPage(state, page) {
  const base = state || createTaskLogState()
  const incomingRevision = page?.pruning_revision
  if (
    base.pruningRevision !== null &&
    incomingRevision !== null &&
    incomingRevision !== undefined &&
    incomingRevision !== base.pruningRevision
  ) {
    return { reset: true }
  }

  const seen = new Set(base.items.map((item) => item.sequence))
  const items = base.items.slice()
  for (const item of page?.items || []) {
    if (!seen.has(item.sequence)) {
      seen.add(item.sequence)
      items.push(item)
    }
  }
  items.sort((a, b) => a.sequence - b.sequence)

  return {
    items,
    nextAfter: page?.next_after ?? base.nextAfter,
    hasMore: page?.has_more === true,
    pruningRevision: incomingRevision ?? base.pruningRevision,
    attemptCount: page?.attempt_count ?? base.attemptCount,
    storedCount: page?.stored_count ?? base.storedCount,
    droppedCount: page?.dropped_count ?? base.droppedCount,
    expired: page?.expired === true,
    gap: page?.gap ?? null,
    loaded: true,
  }
}

// Group rows into attempts, then into chronological step segments (D-13/D-14).
// The grouping key is `attempt:segment`, never the step name — two OBJECTS
// segments split by REFERENCES stay distinct. The head/tail dropped marker is
// attached inside the segment that straddles the gap; when the gap falls
// between segments it becomes a sibling block.
export function groupTaskLogEntries(items, gap) {
  const sorted = (items || []).slice().sort((a, b) => a.sequence - b.sequence)
  const attempts = []
  let currentAttempt = null
  let currentSegment = null
  let gapPlaced = !gap

  const ensureAttempt = (number) => {
    if (!currentAttempt || currentAttempt.attempt !== number) {
      currentAttempt = { attempt: number, blocks: [] }
      attempts.push(currentAttempt)
      currentSegment = null
    }
    return currentAttempt
  }

  const pushGap = (attemptNumber) => {
    const target = ensureAttempt(attemptNumber)
    target.blocks.push({
      kind: 'gap',
      key: `gap-${gap.after_sequence}`,
      count: gap.count,
      afterSequence: gap.after_sequence,
    })
  }

  for (const item of sorted) {
    const segmentKey = `${item.attempt}:${item.segment}`
    if (!gapPlaced && item.sequence > gap.after_sequence) {
      if (currentSegment && currentSegment.key === segmentKey && currentSegment.items.length > 0) {
        currentSegment.gapAfterSequence = gap.after_sequence
      } else {
        pushGap(item.attempt)
      }
      gapPlaced = true
    }
    ensureAttempt(item.attempt)
    if (!currentSegment || currentSegment.key !== segmentKey) {
      currentSegment = {
        kind: 'segment',
        key: segmentKey,
        attempt: item.attempt,
        segment: item.segment,
        step: item.step,
        items: [],
        gapAfterSequence: null,
      }
      currentAttempt.blocks.push(currentSegment)
    }
    currentSegment.items.push(item)
  }

  if (!gapPlaced) {
    const last = attempts[attempts.length - 1]
    if (last) {
      last.blocks.push({
        kind: 'gap',
        key: `gap-${gap.after_sequence}`,
        count: gap.count,
        afterSequence: gap.after_sequence,
      })
    }
  }

  return attempts
}

// 2000 ms while the run is PENDING/RUNNING; after the task turns terminal
// exactly one more fetch is allowed, then polling stops (D-04, Carry-02).
export function taskLogPollInterval(status, terminalFetched) {
  if (status === 'PENDING' || status === 'RUNNING') return LOG_POLL_INTERVAL_MS
  return terminalFetched ? false : LOG_POLL_INTERVAL_MS
}

// "At end" per UI-SPEC §Live follow: within 24px of the bottom.
export function isTaskLogAtEnd({ scrollHeight = 0, scrollTop = 0, clientHeight = 0 } = {}) {
  return scrollHeight - scrollTop - clientHeight <= LOG_END_THRESHOLD_PX
}

// After a pruning-revision reset the panel keeps a paused reader anchored to
// the first line they were looking at. If that line is no longer retained, the
// nearest still-retained later line (ultimately the last one) is used, so the
// dropped marker generally ends up in view (UI-SPEC §Polling).
export function resolveLogAnchor(items, firstVisibleSequence) {
  if (firstVisibleSequence === null || firstVisibleSequence === undefined) return null
  const sequences = (items || []).map((item) => item.sequence)
  if (sequences.length === 0) return null
  if (sequences.includes(firstVisibleSequence)) return firstVisibleSequence
  const next = sequences.find((sequence) => sequence >= firstVisibleSequence)
  return next ?? sequences[sequences.length - 1]
}
