// T-307-18: retire ONLY the old Core schema-translation localStorage pointer
// family (`translate-job-*`). The pointer survived navigation for the removed
// Redis-backed schema job; the ai-translation plugin re-finds its run from the
// Core task log instead, so the stale pointer must go. This never clears the
// whole storage and never touches an unrelated key (the separate rebrand
// migration keeps its own expectations untouched).

function readKeys(storage) {
  try {
    if (typeof storage.length === 'number' && typeof storage.key === 'function') {
      const keys = []
      for (let index = 0; index < storage.length; index += 1) {
        const key = storage.key(index)
        if (typeof key === 'string') keys.push(key)
      }
      return keys
    }
  } catch {
    return []
  }
  try {
    return Object.keys(storage)
  } catch {
    return []
  }
}

export function retireTranslationJobPointers(storage) {
  let resolvedStorage
  try {
    resolvedStorage = storage ?? globalThis.localStorage
  } catch {
    return
  }
  if (!resolvedStorage) return

  for (const key of readKeys(resolvedStorage)) {
    if (!key.startsWith('translate-job-')) continue
    try {
      resolvedStorage.removeItem(key)
    } catch {
      // Storage is inaccessible; leave this key untouched and continue.
    }
  }
}
