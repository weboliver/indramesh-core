export const SLOT_CONFLICT_KEYS = {
  pages: 'path',
  navItems: 'id',
  attributeEditors: 'data_type',
  // WR-01: `objectType` is a match predicate, not a uniqueness claim — the
  // render path (usePluginActionButtons) returns every matching entry, so two
  // plugins may contribute buttons to the same ObjectType (including '*').
  // Ownership is claimed per contribution id, like navItems/settingsSections.
  actionButtons: 'id',
  settingsSections: 'id',
  taskForms: 'tasktype',
  connectorConfigEditors: 'connector_type',
  objectDetailSections: 'id',
  schemaSettingsSections: 'id',
  objectTypeSettingsSections: 'id',
  itemBadges: 'id',
  schemaWizardEntries: 'id',
  translationPanelActions: 'id',
  translationAdminActions: 'id',
  taskDetailActions: 'id',
}

const SLOT_FAMILIES = Object.keys(SLOT_CONFLICT_KEYS)

export function createContributionRegistry() {
  const registered = new Map()
  let contributions = {}
  let conflicts = []

  function rebuild() {
    const next = {}
    const claimed = {}
    const rejected = []

    SLOT_FAMILIES.forEach((family) => {
      next[family] = []
      claimed[family] = new Map()
    })

    registered.forEach((pluginContributions, pluginName) => {
      SLOT_FAMILIES.forEach((family) => {
        const entries = pluginContributions[family]
        if (!Array.isArray(entries)) return
        const keyField = SLOT_CONFLICT_KEYS[family]
        entries.forEach((entry) => {
          const key = entry?.[keyField]
          if (key === undefined || key === null) {
            next[family].push({ ...entry, pluginName })
            return
          }
          if (claimed[family].has(key)) {
            rejected.push({
              kind: 'slot_conflict',
              family,
              slot: `${family}:${key}`,
              pluginName,
              reason: `slot_conflict:${family}:${key}`,
            })
            return
          }
          claimed[family].set(key, pluginName)
          next[family].push({ ...entry, pluginName })
        })
      })
    })

    contributions = next
    conflicts = rejected
  }

  function register(pluginName, pluginContributions) {
    registered.set(pluginName, pluginContributions || {})
    rebuild()
    return { contributions, conflicts }
  }

  // D-08: an owner that leaves the active list drops its claims so a later
  // plugin may claim the freed slot. A no-op for an unknown owner.
  function unregister(pluginName) {
    if (registered.delete(pluginName)) rebuild()
    return { contributions, conflicts }
  }

  function getContributions() {
    return contributions
  }

  function getConflicts() {
    return conflicts
  }

  return { register, unregister, getContributions, getConflicts }
}