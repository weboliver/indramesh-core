import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { retireTranslationJobPointers } from './retire-translation-job-pointers.js'

function createStorage(initial = {}) {
  const store = { ...initial }
  return {
    get length() { return Object.keys(store).length },
    key(index) { return Object.keys(store)[index] ?? null },
    getItem(key) { return key in store ? store[key] : null },
    setItem(key, value) { store[key] = String(value) },
    removeItem(key) { delete store[key] },
    _store: store,
  }
}

describe('retireTranslationJobPointers (T-307-18 exact family removal)', () => {
  it('removes only translate-job-* keys and preserves every unrelated key', () => {
    const storage = createStorage({
      'translate-job-abc': '1',
      'translate-job-def': '2',
      indramesh_auth: 'token',
      other_key: 'x',
      'translate-jobx': 'keep',
    })

    retireTranslationJobPointers(storage)

    assert.deepEqual(Object.keys(storage._store).sort(), ['indramesh_auth', 'other_key', 'translate-jobx'])
  })

  it('never clears the whole storage and is safe when the storage is unavailable', () => {
    const storage = createStorage({ keep: '1' })
    storage.removeItem = () => { throw new Error('blocked') }

    assert.doesNotThrow(() => retireTranslationJobPointers(storage))
    assert.deepEqual(Object.keys(storage._store), ['keep'])

    assert.doesNotThrow(() => retireTranslationJobPointers(null))
    assert.doesNotThrow(() => retireTranslationJobPointers(undefined))

    const throwing = {
      get length() { throw new Error('blocked') },
      key() { throw new Error('blocked') },
      getItem() { throw new Error('blocked') },
      setItem() { throw new Error('blocked') },
      removeItem() { throw new Error('blocked') },
    }
    Object.defineProperty(throwing, 'length', { get() { throw new Error('blocked') } })
    assert.doesNotThrow(() => retireTranslationJobPointers(throwing))
  })

  it('does nothing when no translate-job pointer exists', () => {
    const storage = createStorage({ a: '1', b: '2' })
    retireTranslationJobPointers(storage)
    assert.deepEqual(Object.keys(storage._store).sort(), ['a', 'b'])
  })
})
