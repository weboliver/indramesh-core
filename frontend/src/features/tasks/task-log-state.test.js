import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

import {
  LOG_POLL_INTERVAL_MS,
  createTaskLogState,
  groupTaskLogEntries,
  isTaskLogAtEnd,
  mergeTaskLogPage,
  minLevelFor,
  resolveLogAnchor,
  taskLogPollInterval,
} from './task-log-state.js'

function entry(over = {}) {
  return {
    sequence: 1,
    emitted_at: '2026-10-05T12:00:00Z',
    attempt: 1,
    segment: 1,
    step: 'OBJECTS',
    level: 'INFO',
    category: 'task',
    logger: 'app.services.sync',
    message: 'line',
    details: null,
    ...over,
  }
}

function page(over = {}) {
  return {
    items: [],
    next_after: 0,
    has_more: false,
    pruning_revision: 1,
    dropped_count: 0,
    stored_count: 0,
    attempt_count: 1,
    expired: false,
    gap: null,
    ...over,
  }
}

describe('mergeTaskLogPage', () => {
  it('merges two pages gap-free and without duplicates', () => {
    const first = mergeTaskLogPage(createTaskLogState(), page({
      items: [entry({ sequence: 1 }), entry({ sequence: 2 })],
      next_after: 2,
      has_more: true,
      stored_count: 2,
    }))
    const second = mergeTaskLogPage(first, page({
      items: [entry({ sequence: 3 }), entry({ sequence: 4 })],
      next_after: 4,
      has_more: false,
      stored_count: 4,
    }))

    assert.deepEqual(second.items.map((item) => item.sequence), [1, 2, 3, 4])
    assert.equal(second.nextAfter, 4)
    assert.equal(second.loaded, true)
    assert.equal(second.hasMore, false)
  })

  it('ignores a row that was already merged (same sequence)', () => {
    const first = mergeTaskLogPage(createTaskLogState(), page({
      items: [entry({ sequence: 1 }), entry({ sequence: 2 })],
      next_after: 2,
    }))
    const second = mergeTaskLogPage(first, page({
      items: [entry({ sequence: 2 }), entry({ sequence: 3 })],
      next_after: 3,
    }))

    assert.deepEqual(second.items.map((item) => item.sequence), [1, 2, 3])
  })

  it('forces a reset when the pruning revision changes', () => {
    const first = mergeTaskLogPage(createTaskLogState(), page({
      items: [entry({ sequence: 1 })],
      next_after: 1,
      pruning_revision: 3,
    }))
    const merged = mergeTaskLogPage(first, page({
      items: [entry({ sequence: 2 })],
      next_after: 2,
      pruning_revision: 4,
    }))

    assert.deepEqual(merged, { reset: true })
  })
})

describe('groupTaskLogEntries', () => {
  it('keeps identically named steps in different segments as separate groups', () => {
    const attempts = groupTaskLogEntries([
      entry({ sequence: 1, segment: 1, step: 'OBJECTS' }),
      entry({ sequence: 2, segment: 2, step: 'REFERENCES' }),
      entry({ sequence: 3, segment: 3, step: 'OBJECTS' }),
    ], null)

    assert.equal(attempts.length, 1)
    const segments = attempts[0].blocks.filter((block) => block.kind === 'segment')
    assert.deepEqual(segments.map((segment) => segment.key), ['1:1', '1:2', '1:3'])
    assert.deepEqual(segments.map((segment) => segment.step), ['OBJECTS', 'REFERENCES', 'OBJECTS'])
  })

  it('keeps rows of one attempt together and separates a second attempt', () => {
    const attempts = groupTaskLogEntries([
      entry({ sequence: 1, attempt: 1, segment: 1 }),
      entry({ sequence: 2, attempt: 1, segment: 2, step: 'REFERENCES' }),
      entry({ sequence: 3, attempt: 2, segment: 1 }),
    ], null)

    assert.equal(attempts.length, 2)
    assert.deepEqual(attempts.map((attempt) => attempt.attempt), [1, 2])
    assert.equal(attempts[0].blocks.length, 2)
    assert.equal(attempts[1].blocks.length, 1)
  })

  it('attaches the dropped marker inside the segment that straddles the gap', () => {
    const attempts = groupTaskLogEntries([
      entry({ sequence: 1, attempt: 1, segment: 1 }),
      entry({ sequence: 2, attempt: 1, segment: 1 }),
      entry({ sequence: 900, attempt: 1, segment: 1 }),
    ], { after_sequence: 2, before_sequence: 900, count: 897 })

    assert.equal(attempts.length, 1)
    const segment = attempts[0].blocks[0]
    assert.equal(segment.kind, 'segment')
    assert.equal(segment.gapAfterSequence, 2)
    assert.equal(attempts[0].blocks.length, 1)
  })

  it('emits a sibling gap block when the gap falls between segments', () => {
    const attempts = groupTaskLogEntries([
      entry({ sequence: 1, attempt: 1, segment: 1 }),
      entry({ sequence: 900, attempt: 1, segment: 2, step: 'REFERENCES' }),
    ], { after_sequence: 1, before_sequence: 900, count: 898 })

    const blocks = attempts[0].blocks
    assert.deepEqual(blocks.map((block) => block.kind), ['segment', 'gap', 'segment'])
    assert.equal(blocks[1].count, 898)
  })
})

describe('taskLogPollInterval', () => {
  it('polls every 2000 ms while the task is active', () => {
    assert.equal(taskLogPollInterval('PENDING', false), LOG_POLL_INTERVAL_MS)
    assert.equal(taskLogPollInterval('RUNNING', false), LOG_POLL_INTERVAL_MS)
  })

  it('allows exactly one refetch after the task turns terminal, then stops', () => {
    assert.equal(taskLogPollInterval('SUCCESS', false), LOG_POLL_INTERVAL_MS)
    assert.equal(taskLogPollInterval('SUCCESS', true), false)
    assert.equal(taskLogPollInterval('FAILED', true), false)
  })
})

describe('isTaskLogAtEnd', () => {
  it('treats a view within 24px of the bottom as at the end', () => {
    assert.equal(isTaskLogAtEnd({ scrollHeight: 1000, scrollTop: 480, clientHeight: 496 }), true)
    assert.equal(isTaskLogAtEnd({ scrollHeight: 1000, scrollTop: 470, clientHeight: 496 }), false)
  })
})

describe('minLevelFor', () => {
  it('maps the three filters to the API minimum level', () => {
    assert.equal(minLevelFor('all'), 'INFO')
    assert.equal(minLevelFor('warning'), 'WARNING')
    assert.equal(minLevelFor('error'), 'ERROR')
  })
})

describe('resolveLogAnchor', () => {
  const items = [{ sequence: 1 }, { sequence: 2 }, { sequence: 900 }]

  it('keeps the first visible line when it is still retained', () => {
    assert.equal(resolveLogAnchor(items, 2), 2)
  })

  it('falls to the nearest later retained line when the anchor was pruned', () => {
    assert.equal(resolveLogAnchor(items, 500), 900)
  })

  it('falls to the last retained line when every anchor is above the range', () => {
    assert.equal(resolveLogAnchor(items, 10_000), 900)
  })

  it('returns null without an anchor or without rows', () => {
    assert.equal(resolveLogAnchor(items, null), null)
    assert.equal(resolveLogAnchor([], 5), null)
  })
})

// Phase 300 Plan 04 (Task 3): the approved log/cancel copy must exist in both
// English inventories, in all five frontend locale mirrors and in the four
// committed backend seed catalogs — with the German values pinned by the
// UI-SPEC Copywriting Contract.
const REPO_ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../../..')

const TASK_LOG_EN = {
  'tasks.log.download': 'Download log',
  'tasks.log.title': 'Log',
  'tasks.log.line_count_one': '{{count}} line',
  'tasks.log.line_count_other': '{{count}} lines',
  'tasks.log.filter_label': 'Minimum level',
  'tasks.log.filter.all': 'All',
  'tasks.log.filter.warning': 'Warnings',
  'tasks.log.filter.error': 'Errors',
  'tasks.log.download_hint': 'The download contains all levels.',
  'tasks.log.following': 'Following new entries',
  'tasks.log.paused': 'Following paused',
  'tasks.log.jump_latest': 'Jump to latest entry',
  'tasks.log.attempt': 'Attempt {{number}}',
  'tasks.log.no_step': 'No work step',
  'tasks.log.step_errors_one': '{{count}} error',
  'tasks.log.step_errors_other': '{{count}} errors',
  'tasks.log.dropped_one': '{{count}} line dropped',
  'tasks.log.dropped_other': '{{count}} lines dropped',
  'tasks.log.dropped_body': 'Line limit for this task reached. The beginning and end of the log are kept.',
  'tasks.log.details_show': 'Show details',
  'tasks.log.details_hide': 'Hide details',
  'tasks.log.detail.time': 'Time',
  'tasks.log.detail.logger': 'Logger',
  'tasks.log.detail.attempt': 'Attempt',
  'tasks.log.detail.step': 'Step',
  'tasks.log.region_label': 'Task log entries',
  'tasks.log.loading': 'Loading log…',
  'tasks.log.new_lines_one': '{{count}} new log line',
  'tasks.log.new_lines_other': '{{count}} new log lines',
  'tasks.log.empty_title': 'No log entries yet',
  'tasks.log.empty_pending': 'Entries appear here as soon as the task starts.',
  'tasks.log.empty_running': 'The task is running. New entries appear automatically.',
  'tasks.log.none_title': 'No log recorded',
  'tasks.log.none_body': 'This task did not write any log entries.',
  'tasks.log.expired_title': 'Log expired',
  'tasks.log.expired_body': 'Log entries are deleted automatically after the retention period. The task itself is kept.',
  'tasks.log.filtered_empty_warning': 'No warnings or errors',
  'tasks.log.filtered_empty_error': 'No errors',
  'tasks.log.filtered_empty_body': 'Choose All to see every entry.',
  'tasks.log.show_all': 'Show all',
  'tasks.log.load_error': 'The log could not be loaded. Try again.',
  'tasks.log.load_again': 'Load again',
  'tasks.log.poll_error': 'Updates interrupted: the server is not responding. Retrying automatically.',
  'tasks.log.download_error': 'The log could not be downloaded. Try again.',
  'tasks.cancel_requested.label': 'Cancellation requested',
  'tasks.cancel_requested.body': 'The task stops after the current operation and saves its complete log first.',
}

const TASK_LOG_DE = {
  'tasks.log.download': 'Protokoll herunterladen',
  'tasks.log.title': 'Protokoll',
  'tasks.log.line_count_one': '{{count}} Zeile',
  'tasks.log.line_count_other': '{{count}} Zeilen',
  'tasks.log.filter_label': 'Mindest-Level',
  'tasks.log.filter.all': 'Alle',
  'tasks.log.filter.warning': 'Warnungen',
  'tasks.log.filter.error': 'Fehler',
  'tasks.log.download_hint': 'Der Download enthält alle Level.',
  'tasks.log.following': 'Folgt neuen Einträgen',
  'tasks.log.paused': 'Folgen pausiert',
  'tasks.log.jump_latest': 'Zum neuesten Eintrag',
  'tasks.log.attempt': 'Versuch {{number}}',
  'tasks.log.no_step': 'Ohne Arbeitsschritt',
  'tasks.log.step_errors_one': '{{count}} Fehler',
  'tasks.log.step_errors_other': '{{count}} Fehler',
  'tasks.log.dropped_one': '{{count}} Zeile verworfen',
  'tasks.log.dropped_other': '{{count}} Zeilen verworfen',
  'tasks.log.dropped_body': 'Zeilenobergrenze dieser Aufgabe erreicht. Anfang und Ende des Protokolls bleiben erhalten.',
  'tasks.log.details_show': 'Details anzeigen',
  'tasks.log.details_hide': 'Details ausblenden',
  'tasks.log.detail.time': 'Zeitpunkt',
  'tasks.log.detail.logger': 'Logger',
  'tasks.log.detail.attempt': 'Versuch',
  'tasks.log.detail.step': 'Schritt',
  'tasks.log.region_label': 'Protokolleinträge der Aufgabe',
  'tasks.log.loading': 'Protokoll wird geladen…',
  'tasks.log.new_lines_one': '{{count}} neue Protokollzeile',
  'tasks.log.new_lines_other': '{{count}} neue Protokollzeilen',
  'tasks.log.empty_title': 'Noch keine Protokolleinträge',
  'tasks.log.empty_pending': 'Einträge erscheinen hier, sobald die Aufgabe startet.',
  'tasks.log.empty_running': 'Die Aufgabe läuft. Neue Einträge erscheinen automatisch.',
  'tasks.log.none_title': 'Kein Protokoll gespeichert',
  'tasks.log.none_body': 'Diese Aufgabe hat keine Protokolleinträge geschrieben.',
  'tasks.log.expired_title': 'Protokoll abgelaufen',
  'tasks.log.expired_body': 'Protokolleinträge werden nach Ablauf der Aufbewahrungsfrist automatisch gelöscht. Die Aufgabe selbst bleibt erhalten.',
  'tasks.log.filtered_empty_warning': 'Keine Warnungen oder Fehler',
  'tasks.log.filtered_empty_error': 'Keine Fehler',
  'tasks.log.filtered_empty_body': 'Wählen Sie „Alle“, um alle Einträge zu sehen.',
  'tasks.log.show_all': 'Alle anzeigen',
  'tasks.log.load_error': 'Das Protokoll konnte nicht geladen werden. Versuchen Sie es erneut.',
  'tasks.log.load_again': 'Erneut laden',
  'tasks.log.poll_error': 'Aktualisierung unterbrochen: Der Server antwortet nicht. Neuer Versuch erfolgt automatisch.',
  'tasks.log.download_error': 'Das Protokoll konnte nicht heruntergeladen werden. Versuchen Sie es erneut.',
  'tasks.cancel_requested.label': 'Abbruch angefordert',
  'tasks.cancel_requested.body': 'Die Aufgabe endet nach der laufenden Operation und speichert zuvor ihr vollständiges Protokoll.',
}

function readCatalog(relativePath) {
  return JSON.parse(readFileSync(path.join(REPO_ROOT, relativePath), 'utf8'))
}

describe('task log copy catalogs', () => {
  it('carries every new key in all six frontend locale mirrors', () => {
    for (const locale of ['en', 'de', 'es', 'fr', 'it', 'pt']) {
      const catalog = readCatalog(`frontend/locales/${locale}/systemui.json`)
      for (const key of Object.keys(TASK_LOG_EN)) {
        assert.ok(catalog[key], `${locale} catalog missing ${key}`)
      }
    }
  })

  it('carries every new key in all five backend catalogs', () => {
    const uiStrings = readCatalog('backend/app/data/ui_strings.json')
    const byKey = Object.fromEntries(uiStrings.map((record) => [record.key, record]))
    for (const key of Object.keys(TASK_LOG_EN)) {
      assert.ok(byKey[key], `backend ui_strings missing ${key}`)
      assert.equal(byKey[key].namespace, 'tasks', `backend ui_strings namespace for ${key}`)
    }
    for (const locale of ['de', 'es', 'fr', 'it', 'pt']) {
      const seed = readCatalog(`backend/app/data/ui_translations_${locale}.json`)
      for (const key of Object.keys(TASK_LOG_EN)) {
        assert.ok(seed[key], `backend seed ${locale} missing ${key}`)
      }
    }
  })

  it('matches the approved English defaults in both English inventories', () => {
    const frontend = readCatalog('frontend/locales/en/systemui.json')
    const uiStrings = readCatalog('backend/app/data/ui_strings.json')
    const byKey = Object.fromEntries(uiStrings.map((record) => [record.key, record]))
    for (const [key, value] of Object.entries(TASK_LOG_EN)) {
      assert.equal(frontend[key], value, `frontend/en drift for ${key}`)
      assert.equal(byKey[key].default_text, value, `backend/en drift for ${key}`)
    }
  })

  it('pins the German copy to the approved contract in both German catalogs', () => {
    const frontend = readCatalog('frontend/locales/de/systemui.json')
    const seed = readCatalog('backend/app/data/ui_translations_de.json')
    for (const [key, value] of Object.entries(TASK_LOG_DE)) {
      assert.equal(frontend[key], value, `frontend/de drift for ${key}`)
      assert.equal(seed[key], value, `backend/de seed drift for ${key}`)
    }
  })

  it('ships both plural forms with a count token for every count family', () => {
    const frontend = readCatalog('frontend/locales/en/systemui.json')
    for (const base of [
      'tasks.log.line_count',
      'tasks.log.step_errors',
      'tasks.log.dropped',
      'tasks.log.new_lines',
    ]) {
      assert.match(frontend[`${base}_one`], /\{\{count\}\}/)
      assert.match(frontend[`${base}_other`], /\{\{count\}\}/)
    }
  })
})
