// Pure contract helpers for the plugin runtime. This module must stay free of
// React and of runtime singletons so node:test can import it directly.

// `resource:action` is the only accepted permission shape. A value that does
// not split into exactly two non-empty parts is malformed.
export function parsePermission(value) {
  if (typeof value !== 'string') return null
  const parts = value.split(':')
  return parts.length === 2 && parts[0] && parts[1]
    ? { resource: parts[0], action: parts[1] }
    : null
}

// Undeclared permission means "no gate". A present but malformed permission
// fails closed (hidden), matching the nav/section hiding rule.
export function isPermitted(entry, can) {
  if (entry?.permission === undefined) return true
  const parsed = parsePermission(entry.permission)
  if (!parsed) return false
  return can(parsed.resource, parsed.action)
}

// D-16: filter entries by the host's resolved presence state. An entry without
// an isPresent predicate is always visible; a gated entry is visible only when
// the host resolved its predicate to exactly `true`. A missing, false or
// unsettled resolution hides it (fail closed), so an empty contribution renders
// no wrapper, row gap or section frame. The availability map is keyed by the
// runtime-stamped owner/id.
export function selectVisibleEntries(entries, availability = {}) {
  return (Array.isArray(entries) ? entries : []).filter((entry) => {
    if (typeof entry?.isPresent !== 'function') return true
    return availability[`${entry.pluginName}\u0000${entry.id}`] === true
  })
}

// Stable alphabetical order by plugin name, base-insensitive. Entries from the
// same plugin keep their original (descriptor) order.
export function sortByPluginName(entries) {
  return [...entries].sort((a, b) =>
    a.pluginName.localeCompare(b.pluginName, undefined, { sensitivity: 'base' })
  )
}

// Locale-independent lexical comparison (codepoint order). Using the runtime
// default locale (localeCompare) would let the order drift with the host
// language, which D-06 forbids.
function compareStable(a, b) {
  if (a === b) return 0
  return a < b ? -1 : 1
}

// D-06: the four shared action families order by owner plugin name first, then
// stable contribution id. Fixed Core entries are prepended by the host consumer,
// never here. Ordering is never by translated label, load completion or fetch
// order.
export function sortContributions(entries) {
  return [...entries].sort((a, b) => {
    const owner = compareStable(a?.pluginName ?? '', b?.pluginName ?? '')
    if (owner !== 0) return owner
    return compareStable(String(a?.id ?? ''), String(b?.id ?? ''))
  })
}

// D-06: the host resolves the visible label from the plugin's own namespace and
// stamps ownership from the registry-assigned pluginName. A plugin can never
// supply its own origin, and the separate attribution line uses that stamped
// owner. `resolveLabel(owner, key, fallback)` is the host resolver.
export function frameContribution(entry, resolveLabel) {
  const key = entry?.labelKey
  const resolved =
    key && typeof resolveLabel === 'function'
      ? resolveLabel(entry.pluginName, key, entry.label)
      : entry?.label
  return {
    ...entry,
    displayLabel: resolved ?? entry?.label ?? entry?.pluginName,
    owner: entry?.pluginName,
  }
}

// The schemaWizardEntries descriptor may carry an optional owner-namespaced
// description and an optional icon. `displayDescription` is resolved through the
// SAME owner namespace resolver as `displayLabel`; the icon is validated against
// the host's known lucide names at the host boundary. `iconNames` is injected so
// this stays React-free for node tests. An unknown icon is dropped (icon = null)
// without rejecting the entry; a descriptor without these fields stays
// label-only and fully compatible.
export function frameSchemaWizardEntry(entry, resolveLabel, iconNames) {
  const base = frameContribution(entry, resolveLabel)
  const descriptionKey = entry?.descriptionKey
  const resolvedDescription =
    descriptionKey && typeof resolveLabel === 'function'
      ? resolveLabel(entry.pluginName, descriptionKey, entry.description)
      : entry?.description
  const knownIcons = Array.isArray(iconNames) ? iconNames : []
  const icon =
    isNonEmptyString(entry?.icon) && knownIcons.includes(entry.icon) ? entry.icon : null
  return {
    ...base,
    displayDescription: resolvedDescription ?? null,
    icon,
  }
}

// The item kinds an itemBadges contribution may resolve, and the connector
// roles a connectorConfigEditors entry may apply to.
export const ITEM_BADGE_KINDS = ['object', 'comment', 'file', 'attribute']
export const CONNECTOR_ROLES = ['source', 'target']

// The host assigns the i18n namespace from the registered plugin identity, so
// a plugin can never choose a namespace or write into core `systemui`.
export function pluginNamespace(pluginName) {
  return 'plugin-' + pluginName
}

function isPlainObject(value) {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function isNonEmptyString(value) {
  return typeof value === 'string' && value.length > 0
}

const SECTION_FAMILIES = [
  'objectDetailSections',
  'schemaSettingsSections',
  'objectTypeSettingsSections',
]

// Carry-02/D-06: the four shared action families share one descriptor shape and
// one required operation-permission rule. Older families keep their optional
// permission semantics; these four fail closed on a missing/malformed one.
export const ACTION_FAMILIES = [
  'schemaWizardEntries',
  'translationPanelActions',
  'translationAdminActions',
  'taskDetailActions',
]

// Returns null when the entry is valid for its family, otherwise a short reason
// string the loader prefixes into an `entry_invalid` fault. Unknown families
// are accepted unchanged (v19 leniency).
export function validateContributionEntry(family, entry) {
  if (!isPlainObject(entry)) return 'entry is not an object'

  if (family === 'taskForms') {
    if (!isNonEmptyString(entry.id)) return 'missing id'
    if (!isNonEmptyString(entry.tasktype)) return 'missing tasktype'
    if (typeof entry.component !== 'function') return 'component is not a function'
    return null
  }

  if (family === 'connectorConfigEditors') {
    if (!isNonEmptyString(entry.id)) return 'missing id'
    if (!isNonEmptyString(entry.connector_type)) return 'missing connector_type'
    if (!isNonEmptyString(entry.label)) return 'empty label'
    if (typeof entry.component !== 'function') return 'component is not a function'
    if (entry.roles !== undefined) {
      if (!Array.isArray(entry.roles) || entry.roles.length === 0) {
        return 'roles must be a non-empty array of source or target'
      }
      if (!entry.roles.every((role) => CONNECTOR_ROLES.includes(role))) {
        return 'roles must be a non-empty array of source or target'
      }
    }
    // R2-01: an optional post-Core-save contribution hook. When present it must
    // be a function — the host awaits its promise and reports its rejection, so
    // a non-function would strand a two-store save silently.
    if (entry.afterCoreSave !== undefined && typeof entry.afterCoreSave !== 'function') {
      return 'afterCoreSave is not a function'
    }
    return null
  }

  if (SECTION_FAMILIES.includes(family)) {
    if (!isNonEmptyString(entry.id)) return 'missing id'
    if (!isNonEmptyString(entry.label)) return 'empty label'
    if (typeof entry.component !== 'function') return 'component is not a function'
    if (entry.labelKey !== undefined && !isNonEmptyString(entry.labelKey)) return 'empty labelKey'
    if (entry.permission !== undefined && parsePermission(entry.permission) === null) {
      return 'permission is not resource:action'
    }
    // D-16: only the schema settings section family may carry an optional async
    // presence predicate; other section families keep their existing contract.
    if (family === 'schemaSettingsSections' && entry.isPresent !== undefined) {
      if (typeof entry.isPresent !== 'function') return 'isPresent is not a function'
    }
    return null
  }

  if (family === 'itemBadges') {
    if (!isNonEmptyString(entry.id)) return 'missing id'
    if (typeof entry.resolve !== 'function') return 'resolve is not a function'
    if (!Array.isArray(entry.kinds) || entry.kinds.length === 0) {
      return 'kinds must be a non-empty subset of object, comment, file, attribute'
    }
    if (!entry.kinds.every((kind) => ITEM_BADGE_KINDS.includes(kind))) {
      return 'kinds must be a non-empty subset of object, comment, file, attribute'
    }
    return null
  }

  if (family === 'navItems') {
    if (entry.badge !== undefined && typeof entry.badge !== 'function') return 'badge is not a function'
    if (entry.badgeLabel !== undefined && typeof entry.badgeLabel !== 'function') {
      return 'badgeLabel is not a function'
    }
    if (entry.permission !== undefined && parsePermission(entry.permission) === null) {
      return 'permission is not resource:action'
    }
    if (entry.labelKey !== undefined && !isNonEmptyString(entry.labelKey)) return 'empty labelKey'
    return null
  }

  // UI-SPEC rule 6: the four new families require a valid operation permission.
  // A missing or malformed permission is invalid and hidden, never an implicit
  // grant. The host applies the existing Core read/write gate for the operation.
  if (ACTION_FAMILIES.includes(family)) {
    if (!isNonEmptyString(entry.id)) return 'missing id'
    if (!isNonEmptyString(entry.label)) return 'empty label'
    if (typeof entry.component !== 'function') return 'component is not a function'
    if (entry.labelKey !== undefined && !isNonEmptyString(entry.labelKey)) return 'empty labelKey'
    if (parsePermission(entry.permission) === null) return 'permission is not resource:action'
    // D-12/Carry-02: the wizard entry surface alone may declare an optional async
    // availability predicate plus an owner-namespaced description and an icon.
    // Other action families keep their existing contract unchanged.
    if (family === 'schemaWizardEntries') {
      if (entry.isAvailable !== undefined && typeof entry.isAvailable !== 'function') {
        return 'isAvailable is not a function'
      }
      if (entry.description !== undefined && typeof entry.description !== 'string') {
        return 'description is not a string'
      }
      if (entry.descriptionKey !== undefined && !isNonEmptyString(entry.descriptionKey)) {
        return 'empty descriptionKey'
      }
      if (entry.icon !== undefined && typeof entry.icon !== 'string') {
        return 'icon is not a string'
      }
    }
    // D-16: the two translation action families may declare an optional async
    // presence predicate; other action families keep their existing contract.
    if (
      (family === 'translationPanelActions' || family === 'translationAdminActions') &&
      entry.isPresent !== undefined
    ) {
      if (typeof entry.isPresent !== 'function') return 'isPresent is not a function'
    }
    return null
  }

  return null
}

// Keeps only `{ <lng>: { <key>: string } }` with non-empty string keys that do
// not contain ':' (i18next's namespace separator is not overridden in the
// host). Every dropped value produces one warning and is not a UI fault.
export function validateTranslations(value) {
  const bundles = {}
  const warnings = []

  if (value === undefined || value === null) return { bundles, warnings }
  if (!isPlainObject(value)) {
    warnings.push('translations must be an object of { <language>: { <key>: string } }')
    return { bundles, warnings }
  }

  Object.entries(value).forEach(([lng, strings]) => {
    if (!isPlainObject(strings)) {
      warnings.push(`${lng}: not an object of string values`)
      return
    }
    const clean = {}
    Object.entries(strings).forEach(([key, text]) => {
      if (!isNonEmptyString(key) || key.includes(':')) {
        warnings.push(`${lng}.${key}: key must be a non-empty string without ":"`)
        return
      }
      if (typeof text !== 'string') {
        warnings.push(`${lng}.${key}: value must be a string`)
        return
      }
      clean[key] = text
    })
    bundles[lng] = clean
  })

  return { bundles, warnings }
}

// D-04/D-05 value table: no pill for anything that is not a positive integer;
// exact up to 99; `99+` above.
export function normalizeNavBadge(value) {
  if (typeof value !== 'number' || !Number.isInteger(value) || value <= 0) return null
  return value >= 100 ? '99+' : String(value)
}

function cleanBadge(badge) {
  const clean = { label: badge.label }
  ;['icon', 'title', 'labelKey', 'titleKey'].forEach((field) => {
    if (typeof badge[field] === 'string') clean[field] = badge[field]
  })
  return clean
}

// Normalizes a resolver return (`Map` or plain object) into requested-id ->
// cleaned badge array. A malformed top-level return is `{ ok: false }`; badges
// without a non-empty string label are dropped; ids not requested are ignored.
export function normalizeBadgeMap(result, ids) {
  let entries
  if (result instanceof Map) entries = [...result.entries()]
  else if (isPlainObject(result)) entries = Object.entries(result)
  else return { ok: false, byId: new Map() }

  const allowed = new Set(ids)
  const byId = new Map()
  entries.forEach(([id, badges]) => {
    if (!allowed.has(id) || !Array.isArray(badges)) return
    const clean = badges
      .filter((badge) => badge && typeof badge.label === 'string' && badge.label !== '')
      .map(cleanBadge)
    if (clean.length > 0) byId.set(id, clean)
  })
  return { ok: true, byId }
}

// An item shows at most `max` badges; the rest collapse into one overflow pill.
export function splitItemBadges(badges, max = 2) {
  const list = Array.isArray(badges) ? badges : []
  return { visible: list.slice(0, max), hidden: list.slice(max) }
}

// D-08/D-14: an owner generation guard. A deferred load or async contribution
// captures the generation it started in; when the owner is disabled, unloaded or
// version-bumped the generation advances and the captured completion is stale and
// must be dropped, so a late promise can never resurrect a removed contribution.
export function createOwnerGenerations() {
  const generations = new Map()

  function advance(owner) {
    const next = (generations.get(owner) ?? 0) + 1
    generations.set(owner, next)
    return next
  }

  return {
    // Start a new generation for an owner and return the token the caller holds.
    begin: advance,
    // Advance past the current generation (disable/unload/version change).
    invalidate: advance,
    current(owner) {
      return generations.get(owner) ?? 0
    },
    isCurrent(owner, generation) {
      return generations.get(owner) === generation
    },
  }
}

// The key a `field`/`language` pair uses for touched/allowed tracking.
export function translationPairKey(field, language) {
  return `${field}\u0000${language}`
}

// D-07 pure draft-merge. `proposals` is a list of `{ field, language, value }`.
// `context` carries the LATEST Core state (`latest` as `{ field: { language:
// value } }`), the touched pair keys (`touched`) and, optionally, the allowed
// pair keys (`allowed`). It returns the pairs to apply plus the pairs kept
// (existing/touched) and rejected (unknown/duplicate/malformed); it performs no
// persistence and never mutates its inputs. A pair that was typed and then
// cleared is present in `touched`, so it is skipped like any other touched pair.
export function mergeTranslationDraft(proposals, context = {}) {
  const latest = context.latest ?? {}
  const touched = context.touched ?? new Set()
  const allowed = context.allowed ?? null
  const applied = []
  const skipped = []
  const rejected = []

  if (!Array.isArray(proposals)) return { applied, skipped, rejected }

  // Detect duplicate pairs that disagree before applying anything. A pair that
  // appears twice with the same value is treated as one proposal.
  const grouped = new Map()
  proposals.forEach((proposal) => {
    if (!isPlainObject(proposal)) return
    const { field, language } = proposal
    if (!isNonEmptyString(field) || !isNonEmptyString(language)) return
    const pairKey = translationPairKey(field, language)
    if (!grouped.has(pairKey)) grouped.set(pairKey, [])
    grouped.get(pairKey).push(proposal)
  })
  const conflicting = new Set()
  grouped.forEach((list, pairKey) => {
    if (new Set(list.map((proposal) => proposal.value)).size > 1) conflicting.add(pairKey)
  })

  const handled = new Set()
  proposals.forEach((proposal) => {
    if (!isPlainObject(proposal)) {
      rejected.push({ reason: 'invalid_proposal' })
      return
    }
    const { field, language, value } = proposal
    if (!isNonEmptyString(field) || !isNonEmptyString(language) || typeof value !== 'string') {
      rejected.push({ field, language, reason: 'invalid_proposal' })
      return
    }
    const pairKey = translationPairKey(field, language)
    if (handled.has(pairKey)) return
    handled.add(pairKey)
    if (conflicting.has(pairKey)) {
      rejected.push({ field, language, reason: 'duplicate_conflict' })
      return
    }
    if (allowed && !allowed.has(pairKey)) {
      rejected.push({ field, language, reason: 'not_allowed' })
      return
    }
    if (touched.has(pairKey)) {
      skipped.push({ field, language, reason: 'touched' })
      return
    }
    const current = latest?.[field]?.[language]
    if (current !== undefined && current !== null && String(current).trim() !== '') {
      skipped.push({ field, language, reason: 'existing' })
      return
    }
    applied.push({ field, language, value })
  })

  return { applied, skipped, rejected }
}

// T-307-15: normalize a contributor's plain-JSON proposal list before the pure
// draft merge. Only `{ field, language, value }` with non-empty string
// field/language and a string value survive; an optional `reference` must be an
// opaque non-empty string or finite number. A function, object, empty string or
// serialized URL as a reference is rejected — a contribution can never smuggle
// an executable or a later session through a proposal field.
export function normalizeDraftProposals(proposals) {
  const accepted = []
  const rejected = []
  if (!Array.isArray(proposals)) return { accepted, rejected }

  proposals.forEach((proposal) => {
    if (!isPlainObject(proposal)) {
      rejected.push({ reason: 'invalid_proposal' })
      return
    }
    const { field, language, value, reference } = proposal
    if (!isNonEmptyString(field) || !isNonEmptyString(language) || typeof value !== 'string') {
      rejected.push({ field, language, reason: 'invalid_proposal' })
      return
    }
    const hasReference = reference !== undefined
    const validReference =
      isNonEmptyString(reference) ||
      (typeof reference === 'number' && Number.isFinite(reference))
    if (hasReference && !validReference) {
      rejected.push({ field, language, reason: 'invalid_reference' })
      return
    }
    const clean = { field, language, value }
    if (hasReference) clean.reference = reference
    accepted.push(clean)
  })

  return { accepted, rejected }
}