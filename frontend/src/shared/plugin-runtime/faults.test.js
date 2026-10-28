import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { clearTransientFaults, replacePluginFaults } from './faults.js'

const LOAD_FAULT = { kind: 'load', slot: null, reason: 'bundle: failed' }
const RENDER_FAULT = { kind: 'slot_render', slot: 'page:dashboard', reason: 'boom' }
const CONFLICT_FAULT = { kind: 'slot_conflict', slot: 'actionButtons:shared', reason: 'slot_conflict' }

describe('clearTransientFaults (WR-08: retry supersedes a failed attempt)', () => {
  it('drops load and slot_render faults for the plugin', () => {
    const faults = { gamma: [LOAD_FAULT, RENDER_FAULT] }

    assert.deepEqual(clearTransientFaults(faults, 'gamma'), {})
  })

  it('keeps slot_conflict entries, which are registration outcomes', () => {
    const faults = { gamma: [LOAD_FAULT, CONFLICT_FAULT] }

    assert.deepEqual(clearTransientFaults(faults, 'gamma'), { gamma: [CONFLICT_FAULT] })
  })

  it('returns the same object when the plugin has nothing to clear', () => {
    const faults = { gamma: [CONFLICT_FAULT] }

    assert.equal(clearTransientFaults(faults, 'gamma'), faults)
    assert.equal(clearTransientFaults(faults, 'unknown'), faults)
    assert.equal(clearTransientFaults(undefined, 'gamma'), undefined)
  })

  it('leaves other plugins untouched', () => {
    const faults = { alpha: [LOAD_FAULT], beta: [LOAD_FAULT] }

    const next = clearTransientFaults(faults, 'alpha')

    assert.deepEqual(next.beta, [LOAD_FAULT])
    assert.equal('alpha' in next, false)
  })
})

describe('replacePluginFaults (WR-08: a successful reload resets the record)', () => {
  it('replaces the plugin faults with the fresh set', () => {
    const faults = { gamma: [LOAD_FAULT], alpha: [LOAD_FAULT] }

    const next = replacePluginFaults(faults, 'gamma', [CONFLICT_FAULT])

    assert.deepEqual(next.gamma, [CONFLICT_FAULT])
    assert.deepEqual(next.alpha, [LOAD_FAULT])
  })

  it('deletes the entry when the fresh set is empty', () => {
    const faults = { gamma: [LOAD_FAULT, RENDER_FAULT] }

    assert.deepEqual(replacePluginFaults(faults, 'gamma', []), {})
    assert.deepEqual(replacePluginFaults(faults, 'gamma'), {})
  })
})