// Pure, React-free translation review session for the admin catalog host
// (T-307-16). This module must stay free of React and runtime singletons so
// node:test can import it directly.
//
// The plugin owns the suggestion lifecycle; this session only holds the neutral
// projection the plugin published for the CURRENT normalized scope. It is
// scope-fenced, orders rows by namespace then key, caps the visible block at
// 100 and keeps the admin's edits local until an explicit apply. It performs no
// server mutation and knows no apply API: table edits and discards are handed
// back to the publishing owner through the callbacks it published.

const MAX_REVIEW_ROWS = 100

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0
}

function compareNamespace(a, b) {
  const left = isNonEmptyString(a) ? a : '\uffff'
  const right = isNonEmptyString(b) ? b : '\uffff'
  if (left === right) return 0
  return left < right ? -1 : 1
}

function compareKey(a, b) {
  const left = typeof a === 'string' ? a : ''
  const right = typeof b === 'string' ? b : ''
  if (left === right) return 0
  return left < right ? -1 : 1
}

function normalizeDraft(draft) {
  if (!isPlainObject(draft)) return null
  return {
    value: typeof draft.value === 'string' ? draft.value : '',
    sourceLabel: isNonEmptyString(draft.sourceLabel) ? draft.sourceLabel : null,
    reasonText: isNonEmptyString(draft.reasonText) ? draft.reasonText : null,
    invalid: draft.invalid === true,
    reference: draft.reference,
  }
}

function buildRows(rows, drafts) {
  const draftByUiStringId = new Map()
  if (Array.isArray(drafts)) {
    drafts.forEach((draft) => {
      if (!isPlainObject(draft) || !isNonEmptyString(draft.uiStringId)) return
      draftByUiStringId.set(draft.uiStringId, normalizeDraft(draft))
    })
  }

  return (Array.isArray(rows) ? rows : [])
    .filter((row) => isPlainObject(row) && isNonEmptyString(row.uiStringId))
    .map((row) => ({
      uiStringId: row.uiStringId,
      namespace: isNonEmptyString(row.namespace) ? row.namespace : null,
      key: typeof row.key === 'string' ? row.key : '',
      codeDefault: typeof row.codeDefault === 'string' ? row.codeDefault : '',
      draft: draftByUiStringId.get(row.uiStringId) ?? null,
    }))
    .sort((a, b) => {
      const ns = compareNamespace(a.namespace, b.namespace)
      return ns !== 0 ? ns : compareKey(a.key, b.key)
    })
    .slice(0, MAX_REVIEW_ROWS)
}

export function createTranslationReviewSession({ scopeRevision } = {}) {
  let state = null
  const listeners = new Set()

  function emit() {
    listeners.forEach((listener) => {
      try {
        listener()
      } catch {
        // A subscriber throw must never break the review state machine.
      }
    })
  }

  function isActive() {
    return state !== null && state.rows.length > 0
  }

  function publishReview({
    owner,
    scopeRevision: incomingRevision,
    rows,
    drafts,
    total,
    onDraftChange,
    onDiscard,
  } = {}) {
    if (incomingRevision !== scopeRevision) return false
    const normalizedRows = buildRows(rows, drafts)
    if (normalizedRows.length === 0) {
      state = null
      emit()
      return false
    }
    state = {
      owner,
      rows: normalizedRows,
      total: typeof total === 'number' && total >= normalizedRows.length ? total : normalizedRows.length,
      onDraftChange: typeof onDraftChange === 'function' ? onDraftChange : null,
      onDiscard: typeof onDiscard === 'function' ? onDiscard : null,
    }
    emit()
    return true
  }

  function getReview() {
    if (!state) return null
    return { owner: state.owner, rows: state.rows, drafts: getDrafts(), total: state.total }
  }

  function getDrafts() {
    if (!state) return []
    return state.rows
      .filter((row) => row.draft)
      .map((row) => ({ uiStringId: row.uiStringId, ...row.draft }))
  }

  function setDraftValue(uiStringId, value) {
    if (!state) return false
    const row = state.rows.find((entry) => entry.uiStringId === uiStringId)
    if (!row || !row.draft) return false
    row.draft = { ...row.draft, value }
    emit()
    return true
  }

  // A table edit: update the projection and hand the value back to the owner,
  // whose apply commits the drafts it holds.
  function editDraft(uiStringId, value) {
    const owner = state?.onDraftChange
    if (!setDraftValue(uiStringId, value)) return false
    if (owner) {
      try {
        owner(uiStringId, value)
      } catch {
        // An owner throw must never break the table edit.
      }
    }
    return true
  }

  // A table discard: the row leaves only after the owner confirms it. Without an
  // owner handler nothing is discarded, so a row never disappears unconfirmed.
  async function discardRow(uiStringId) {
    const owner = state?.onDiscard
    if (!owner) return false
    let confirmed = false
    try {
      confirmed = (await owner(uiStringId)) === true
    } catch {
      confirmed = false
    }
    if (!confirmed) return false
    removeRow(uiStringId)
    return true
  }

  function removeRow(uiStringId) {
    if (!state) return false
    const nextRows = state.rows.filter((row) => row.uiStringId !== uiStringId)
    if (nextRows.length === state.rows.length) return false
    state = nextRows.length > 0 ? { ...state, rows: nextRows } : null
    emit()
    return true
  }

  function leaveReview() {
    if (!state) return
    state = null
    emit()
  }

  function subscribe(listener) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  return {
    scopeRevision,
    isActive,
    publishReview,
    getReview,
    getDrafts,
    setDraftValue,
    editDraft,
    discardRow,
    removeRow,
    leaveReview,
    subscribe,
  }
}
