import api from '../hooks/use-api.js'
import { validateContributionEntry, validateTranslations } from './contribution-utils.js'

const SLOT_FAMILIES = [
  'pages',
  'navItems',
  'attributeEditors',
  'actionButtons',
  'settingsSections',
  'taskForms',
  'connectorConfigEditors',
  'objectDetailSections',
  'schemaSettingsSections',
  'objectTypeSettingsSections',
  'itemBadges',
  'schemaWizardEntries',
  'translationPanelActions',
  'translationAdminActions',
  'taskDetailActions',
]

// Families whose entries are validated and dropped individually. pages,
// attributeEditors, actionButtons and settingsSections keep the v19 leniency.
const PER_ENTRY_VALIDATED_FAMILIES = [
  'navItems',
  'taskForms',
  'connectorConfigEditors',
  'objectDetailSections',
  'schemaSettingsSections',
  'objectTypeSettingsSections',
  'itemBadges',
  'schemaWizardEntries',
  'translationPanelActions',
  'translationAdminActions',
  'taskDetailActions',
]

const BUNDLE_CACHE_EPOCH = '2'

function fault(reason) {
  return {
    state: 'ui_fault',
    contributions: {},
    faults: [reason],
    rejected: [],
    translations: {},
    translationWarnings: [],
  }
}

export function validateDescriptor(descriptor, extensionPoints) {
  if (descriptor === null || typeof descriptor !== 'object' || Array.isArray(descriptor)) {
    return fault('descriptor: default export is not an object')
  }

  const declared = new Set(Array.isArray(extensionPoints) ? extensionPoints : [])
  const undeclared = SLOT_FAMILIES.filter(
    (family) => descriptor[family] !== undefined && !declared.has(family)
  )
  if (undeclared.length > 0) {
    return fault(`undeclared: ${undeclared.join(', ')}`)
  }

  // WR-01: the binding contract is bidirectional (D-06) — a manifest that
  // declares a family the bundle never registers is as much a mismatch as the
  // reverse, and otherwise hides a misconfigured manifest behind a `ready`
  // state. Families unknown to the host are ignored (v19 forward leniency).
  const declaredButMissing = [...declared].filter(
    (family) => SLOT_FAMILIES.includes(family) && descriptor[family] === undefined
  )
  if (declaredButMissing.length > 0) {
    return fault(`unregistered: ${declaredButMissing.join(', ')}`)
  }

  const malformed = SLOT_FAMILIES.filter(
    (family) => descriptor[family] !== undefined && !Array.isArray(descriptor[family])
  )
  if (malformed.length > 0) {
    return fault(`malformed: ${malformed.join(', ')}`)
  }

  const contributions = {}
  const rejected = []
  SLOT_FAMILIES.forEach((family) => {
    if (!Array.isArray(descriptor[family])) return
    if (!PER_ENTRY_VALIDATED_FAMILIES.includes(family)) {
      contributions[family] = descriptor[family]
      return
    }
    // A malformed entry is dropped individually and recorded as a
    // non-transient `entry_invalid` fault; the rest of the bundle still loads.
    contributions[family] = descriptor[family].filter((entry, index) => {
      const detail = validateContributionEntry(family, entry)
      if (detail === null) return true
      rejected.push({
        kind: 'entry_invalid',
        family,
        slot: `${family}:${entry?.id ?? index}`,
        reason: `entry_invalid:${family}:${detail}`,
      })
      return false
    })
  })

  const { bundles, warnings } = validateTranslations(descriptor.translations)
  return {
    state: 'ready',
    contributions,
    faults: [],
    rejected,
    translations: bundles,
    translationWarnings: warnings,
  }
}

/**
 * The asset request URL for a plugin bundle. `?v=` is the manifest version
  * cache key; `ce` is the build-time loader cache epoch, bumped only when the
  * delivery/authorization contract changes. It retires public entries from
  * earlier epochs. `attempt` distinguishes retries. Imports use fresh blob URLs.
 */
export function buildPluginBundleUrl(plugin, attempt = 0) {
  const base =
    `/api/v1/plugins/${encodeURIComponent(plugin.name)}/assets/${plugin.frontend.bundle}` +
    `?v=${encodeURIComponent(plugin.version)}&ce=${BUNDLE_CACHE_EPOCH}`
  return attempt > 0 ? `${base}&attempt=${encodeURIComponent(attempt)}` : base
}

async function requestBundle(url) {
  const { data } = await api.get(url, {
    baseURL: '', responseType: 'text', headers: { 'Cache-Control': 'no-store' },
  })
  return data
}

async function importModule(source) {
  const blobUrl = URL.createObjectURL(new Blob([source], { type: 'text/javascript' }))
  try {
    return await import(/* @vite-ignore */ blobUrl)
  } finally {
    URL.revokeObjectURL(blobUrl)
  }
}

export function createBundleLoader({ requestBundle: fetchBundle = requestBundle, importModule: importBundle = importModule } = {}) {
  return async (plugin, attempt = 0) => {
    const frontend = plugin?.frontend
    if (!frontend?.bundle) {
      return {
        state: 'ready',
        contributions: {},
        faults: [],
        rejected: [],
        translations: {},
        translationWarnings: [],
      }
    }

    try {
      const source = await fetchBundle(buildPluginBundleUrl(plugin, attempt))
      const mod = await importBundle(source)
      return validateDescriptor(mod.default, frontend.extension_points)
    } catch (err) {
      const status = err?.response?.status
      return fault(`bundle: ${status ? `${status} ` : ''}${err?.message || err}`)
    }
  }
}

export const loadPluginBundle = createBundleLoader()
