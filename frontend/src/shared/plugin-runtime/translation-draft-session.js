// Pure, React-free translation draft session for the panel host (T-307-15).
// This module must stay free of React and runtime singletons so node:test can
// import it directly.
//
// It owns the per-pair draft identity of one panel session: which (field,
// language) pairs a contributor marked, the opaque reference the contributor
// gave, the registered protected commit/discard handlers and the host-stamped
// owner. The host keeps the input text; this module keeps the provenance, so a
// marked pair keeps its protected commit path even after the text is edited and
// never silently falls back to an unprotected Core upsert.

import { mergeTranslationDraft, normalizeDraftProposals, translationPairKey } from './contribution-utils.js'

export function createTranslationDraftSession({ sessionId, scopeRevision } = {}) {
  // pairKey -> { field, language, value, owner, reference, protected, sourceLabel }
  const drafts = new Map()
  // owner -> { commitDraft, discardDraft }
  const handlers = new Map()
  // owner -> label the host stamped for its contribution (for the aria description)
  const ownerLabels = new Map()
  const listeners = new Set()

  function emit() {
    listeners.forEach((listener) => {
      try {
        listener()
      } catch {
        // A subscriber throw must never break the draft state machine.
      }
    })
  }

  function isCurrent(scope) {
    return (
      !!scope &&
      scope.sessionId === sessionId &&
      scope.scopeRevision === scopeRevision
    )
  }

  // The host registers the contributor's trusted callbacks under the owner the
  // runtime stamped. Only a function commitDraft is accepted; the returned
  // unregister releases exactly this registration.
  function registerDraftHandlers(owner, handlerSet, { sourceLabel = null } = {}) {
    if (!owner || !handlerSet || typeof handlerSet.commitDraft !== 'function') {
      return () => {}
    }
    const registration = {
      commitDraft: handlerSet.commitDraft,
      discardDraft:
        typeof handlerSet.discardDraft === 'function' ? handlerSet.discardDraft : null,
    }
    handlers.set(owner, registration)
    ownerLabels.set(owner, sourceLabel)
    return () => {
      if (handlers.get(owner) === registration) {
        handlers.delete(owner)
        ownerLabels.delete(owner)
      }
    }
  }

  // Accept a delivery only from the current session/revision and from an owner
  // with registered handlers. Only plain-JSON allowed, empty, untouched pairs
  // are filled; anything else is skipped or rejected with a reason.
  function acceptDraft({ owner, sessionId: incomingSession, scopeRevision: incomingRevision, proposals, latest, touched, allowed } = {}) {
    const empty = { applied: [], skipped: [], rejected: [] }
    if (!isCurrent({ sessionId: incomingSession, scopeRevision: incomingRevision })) return empty
    if (!handlers.has(owner)) return empty

    const { accepted, rejected } = normalizeDraftProposals(proposals)
    const merged = mergeTranslationDraft(accepted, { latest, touched, allowed })
    const byPair = new Map(accepted.map((proposal) => [translationPairKey(proposal.field, proposal.language), proposal]))

    merged.applied.forEach((pair) => {
      const source = byPair.get(translationPairKey(pair.field, pair.language))
      drafts.set(translationPairKey(pair.field, pair.language), {
        field: pair.field,
        language: pair.language,
        value: pair.value,
        owner,
        reference: source?.reference,
        protected: true,
        sourceLabel: ownerLabels.get(owner) ?? null,
      })
    })

    const result = {
      applied: merged.applied,
      skipped: merged.skipped,
      rejected: [...rejected, ...merged.rejected],
    }
    if (merged.applied.length > 0) emit()
    return result
  }

  function getDraft(field, language) {
    return drafts.get(translationPairKey(field, language)) ?? null
  }

  function hasDraft(field, language) {
    return drafts.has(translationPairKey(field, language))
  }

  function getDrafts() {
    return [...drafts.values()]
  }

  // The host keeps the input text; this keeps the draft value in sync so the
  // per-pair identity survives edits without losing its protected path.
  function setDraftValue(field, language, value) {
    const key = translationPairKey(field, language)
    const draft = drafts.get(key)
    if (!draft) return false
    drafts.set(key, { ...draft, value })
    return true
  }

  function clearDraft(field, language) {
    const removed = drafts.delete(translationPairKey(field, language))
    if (removed) emit()
    return removed
  }

  // A marked pair is committed through the registered contributor callback with
  // one opaque reference and the current edited value. The result keeps the
  // server's honest outcome: written/skipped remove the marker, invalid keeps
  // it, and an unmarked pair reports `manual` so the host keeps its Core upsert.
  async function commit(field, language, value) {
    const draft = getDraft(field, language)
    if (!draft) return { status: 'manual' }
    const registration = handlers.get(draft.owner)
    if (!registration) return { status: 'manual' }

    const outcome = await registration.commitDraft({
      reference: draft.reference,
      value,
      field,
      language,
    })
    const status = outcome?.status
    if (status === 'written' || status === 'skipped') {
      clearDraft(field, language)
    }
    return outcome ?? { status: 'invalid' }
  }

  // Discard asks the owning contributor to close the suggestion. The marker is
  // cleared only on a confirmed success, so a failed discard keeps the draft.
  async function discard(field, language) {
    const draft = getDraft(field, language)
    if (!draft) return false
    const registration = handlers.get(draft.owner)
    if (!registration || typeof registration.discardDraft !== 'function') return false
    const ok = await registration.discardDraft({
      reference: draft.reference,
      field,
      language,
    })
    if (ok === true) clearDraft(field, language)
    return ok === true
  }

  // Plugin disappearance: drop exactly this owner's drafts and handlers, leaving
  // every other owner's drafts (and all non-draft manual input) untouched.
  function unregisterOwner(owner) {
    let removed = false
    for (const [key, draft] of drafts) {
      if (draft.owner === owner) {
        drafts.delete(key)
        removed = true
      }
    }
    handlers.delete(owner)
    ownerLabels.delete(owner)
    if (removed) emit()
  }

  function subscribe(listener) {
    listeners.add(listener)
    return () => listeners.delete(listener)
  }

  return {
    sessionId,
    scopeRevision,
    isCurrent,
    registerDraftHandlers,
    acceptDraft,
    getDraft,
    hasDraft,
    getDrafts,
    setDraftValue,
    clearDraft,
    commit,
    discard,
    unregisterOwner,
    subscribe,
  }
}

