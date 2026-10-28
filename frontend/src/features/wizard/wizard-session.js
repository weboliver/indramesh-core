// Pure, React-free wizard session controller. The host (`SchemaWizardModal`) owns
// one controller per open wizard session. It gives the plugin input an opaque,
// detached snapshot channel plus a per-generation guarded attempt facade, so a
// late success/error from a replaced input, a closed wizard, a lost right, a
// plugin-version change or a newer draft can never mutate host state.
//
// The controller is deliberately NOT a general JSON-syntax gate: the only
// contributor is a first-party plugin and the server stays the semantic
// authority. It validates exactly the inert bounded shape Core accepts and
// deep-copies with structuredClone, without accessor/prototype/depth
// special-casing beyond the existing host validator.
// (D-03, D-04, D-06, D-08, Carry-01/02.)

// The strict draft allowlist Core accepts from a plugin: no source_id/name/
// actor/origin — those are Core-owned. Anything else is rejected locally.
export const PLUGIN_DRAFT_KEYS = new Set(['object_types', 'statuses', 'description'])

// The only envelope keys a handoff may carry. `suggestedName` is a non-authoritative
// hint placed BESIDE the draft, never inside it.
export const HANDOFF_ENVELOPE_KEYS = new Set(['draft', 'warnings', 'suggestedName'])

export const MAX_WARNINGS = 20
export const MAX_WARNING_LENGTH = 500
export const MAX_NAME_LENGTH = 255
export const MAX_SNAPSHOT_BYTES = 16 * 1024
export const MAX_ENVELOPE_BYTES = 128 * 1024

export function isPlainJsonObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function utf8Bytes(text) {
  return new TextEncoder().encode(text).length
}

function clone(value) {
  return structuredClone(value)
}

// Validate and deep-copy one inert handoff envelope, all-or-none. Accepts only
// `draft`, `warnings` and an optional `suggestedName`; draft keys stay exactly
// description/statuses/object_types. Bounds are checked before a single
// caller-side state commit, so a malformed or oversized envelope changes
// nothing.
export function normalizeHandoff(envelope) {
  if (!isPlainJsonObject(envelope)) return { ok: false, reason: 'envelope_not_object' }

  const unknownEnvelopeKey = Object.keys(envelope).find((key) => !HANDOFF_ENVELOPE_KEYS.has(key))
  if (unknownEnvelopeKey) return { ok: false, reason: `unknown_envelope_field:${unknownEnvelopeKey}` }

  const draft = envelope.draft
  if (!isPlainJsonObject(draft) || !Array.isArray(draft.object_types)) {
    return { ok: false, reason: 'invalid_draft' }
  }
  const unknownDraftKey = Object.keys(draft).find((key) => !PLUGIN_DRAFT_KEYS.has(key))
  if (unknownDraftKey) return { ok: false, reason: `unknown_draft_field:${unknownDraftKey}` }

  let warnings = []
  if (envelope.warnings !== undefined) {
    if (!Array.isArray(envelope.warnings)) return { ok: false, reason: 'invalid_warnings' }
    if (envelope.warnings.length > MAX_WARNINGS) return { ok: false, reason: 'too_many_warnings' }
    for (const warning of envelope.warnings) {
      if (typeof warning !== 'string') return { ok: false, reason: 'invalid_warning' }
      if (warning.length > MAX_WARNING_LENGTH) return { ok: false, reason: 'warning_too_long' }
    }
    warnings = envelope.warnings.slice()
  }

  let suggestedName = null
  if (envelope.suggestedName !== undefined && envelope.suggestedName !== null) {
    if (typeof envelope.suggestedName !== 'string') return { ok: false, reason: 'invalid_name' }
    if (envelope.suggestedName.length > MAX_NAME_LENGTH) return { ok: false, reason: 'name_too_long' }
    suggestedName = envelope.suggestedName
  }

  let detached
  try {
    const bounded = { draft, warnings, suggestedName }
    if (utf8Bytes(JSON.stringify(bounded)) > MAX_ENVELOPE_BYTES) {
      return { ok: false, reason: 'envelope_too_large' }
    }
    detached = clone(bounded)
  } catch {
    // A non-cloneable value (function/accessor) is simply not inert data.
    return { ok: false, reason: 'unserializable' }
  }

  return { ok: true, data: detached }
}

// Pure review write-rights gate (S6 / T-306-13). The wizard is reachable only
// through ProtectedAdminRoute (superuser) today, so both rights are normally
// present, but the decision is kept pure and independent of the React permission
// hook. Hiding a control never replaces server authorization; the API stays the
// authority.
export function reviewWriteRights(canCreateSchema, canSaveTemplate) {
  return {
    showCreateSchema: canCreateSchema === true,
    showSaveTemplate: canSaveTemplate === true,
  }
}

// Reacquire the entry currently offering an accepted draft's origin, matched by
// owner plugin name and stable contribution id (D-08 lifecycle). Returns the
// offered entry, or null when the origin is no longer offered (disabled plugin,
// lost permission, owner-version change) so the host can fall back to the entry
// choices with the draft-retained notice.
export function resolveReviewOrigin(activeEntry, entries) {
  if (!activeEntry || !Array.isArray(entries)) return null
  return (
    entries.find(
      (entry) => entry.pluginName === activeEntry.pluginName && entry.id === activeEntry.id
    ) ?? null
  )
}

// Serialize an accepted, reviewed draft into the exact library format the Core
// template library already stores (see schema_service.get_schema_template_data):
// schema_name / schema_description / object_types / statuses. Only these four
// fields are ever emitted — no plugin suggestion, provenance, session or Source
// metadata crosses into template_data; Core owns the final name/description and
// the selected tree. The name falls back to the library's existing
// "Untitled Schema" default only when Core supplied none.
export function reviewedDraftToTemplateData({ schemaName, schemaDescription, objectTypes, statuses } = {}) {
  const trimmedName = typeof schemaName === 'string' ? schemaName.trim() : ''
  return {
    schema_name: trimmedName || 'Untitled Schema',
    schema_description: typeof schemaDescription === 'string' ? schemaDescription : null,
    object_types: Array.isArray(objectTypes) ? objectTypes : [],
    statuses: Array.isArray(statuses) ? statuses : [],
  }
}

// One controller per open wizard session. `commitAccepted(data, origin)` runs
// exactly once for a validated handoff; `origin` is the host-stamped inert
// `{ owner, contributionId }` the caller augments with a display label.
export function createWizardSession({ commitAccepted } = {}) {
  const onAccept = typeof commitAccepted === 'function' ? commitAccepted : () => {}

  let sessionGeneration = 0
  let attemptCounter = 0
  let currentAttempt = null
  let inputCounter = 0
  let currentInput = null
  const snapshots = new Map()
  let acceptedDraft = null

  const runKey = (owner, contributionId) => `${owner}\u0000${contributionId}`

  function reset() {
    currentAttempt = null
    currentInput = null
    snapshots.clear()
    acceptedDraft = null
  }

  // A new open cycle: a fresh session identity plus empty snapshot/attempt state.
  function beginSession() {
    sessionGeneration += 1
    reset()
    return sessionGeneration
  }

  // Close / confirmed discard / logout: nothing survives.
  function endSession() {
    sessionGeneration += 1
    reset()
  }

  // Back / breadcrumb / input unmount / availability loss / owner reload: the
  // current attempt stops being current and aborts its local request. Retained
  // snapshots and an already-accepted draft survive inside the open session.
  function invalidateAttempt() {
    if (currentAttempt) {
      currentAttempt.invalidated = true
      currentAttempt.controller.abort()
    }
    currentAttempt = null
  }

  function mountInput(owner, contributionId) {
    inputCounter += 1
    currentInput = { owner, contributionId, mountId: inputCounter }
    return { mountId: inputCounter }
  }

  function unmountInput(mountId) {
    if (currentInput && (mountId === undefined || currentInput.mountId === mountId)) {
      currentInput = null
    }
    invalidateAttempt()
  }

  function readSnapshot(owner, contributionId) {
    const stored = snapshots.get(runKey(owner, contributionId))
    if (stored === undefined) return null
    try {
      return clone(stored)
    } catch {
      return null
    }
  }

  // Persist a plugin-owned snapshot, bounded and detached. Only the currently
  // mounted input may write, so a callback from a replaced input cannot
  // overwrite a newer snapshot.
  function writeSnapshot(owner, contributionId, value) {
    if (!currentInput || currentInput.owner !== owner || currentInput.contributionId !== contributionId) {
      return false
    }
    if (!isPlainJsonObject(value)) return false
    let detached
    try {
      if (utf8Bytes(JSON.stringify(value)) > MAX_SNAPSHOT_BYTES) return false
      detached = clone(value)
    } catch {
      return false
    }
    snapshots.set(runKey(owner, contributionId), detached)
    return true
  }

  function beginAttempt() {
    // A new attempt supersedes and aborts any in-flight one.
    invalidateAttempt()
    attemptCounter += 1
    const controller = new AbortController()
    const generation = sessionGeneration
    const auth = { id: attemptCounter, generation, controller, invalidated: false }
    currentAttempt = auth

    const origin = currentInput
      ? { owner: currentInput.owner, contributionId: currentInput.contributionId }
      : null

    const facade = {
      id: auth.id,
      get signal() {
        return controller.signal
      },
      isCurrent() {
        return !auth.invalidated && auth.generation === sessionGeneration && currentAttempt === auth
      },
      // Success delivery. Validates atomically; on success consumes the attempt
      // and commits once, so a duplicate/late success is a no-op.
      acceptDraft(envelope) {
        if (!facade.isCurrent()) return { ok: false, reason: 'stale' }
        const result = normalizeHandoff(envelope)
        if (!result.ok) return result
        auth.invalidated = true
        currentAttempt = null
        acceptedDraft = result.data
        onAccept(result.data, origin)
        return { ok: true, data: result.data }
      },
      // Error delivery shares the same current-attempt check. A current error
      // consumes the attempt so a later success cannot commit; visible error
      // rendering is plugin-owned (the host adds no raw provider text).
      reportError(error) {
        if (!facade.isCurrent()) return { ok: false, reason: 'stale' }
        auth.invalidated = true
        currentAttempt = null
        return { ok: true, error }
      },
    }

    return facade
  }

  function hasAcceptedDraft() {
    return acceptedDraft !== null
  }

  function getAcceptedDraft() {
    if (acceptedDraft === null) return null
    try {
      return clone(acceptedDraft)
    } catch {
      return null
    }
  }

  return {
    beginSession,
    endSession,
    mountInput,
    unmountInput,
    readSnapshot,
    writeSnapshot,
    beginAttempt,
    invalidateAttempt,
    hasAcceptedDraft,
    getAcceptedDraft,
  }
}
