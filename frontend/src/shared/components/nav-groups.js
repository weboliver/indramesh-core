export function slugifyGroupName(groupOrPluginName) {
  return String(groupOrPluginName ?? '')
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9]/g, '-')
}

export function mergePluginNavGroups(baseGroups, pluginItems) {
  const itemsByGroupId = {}
  const staticByMatchKey = new Map()
  baseGroups.forEach((group) => {
    staticByMatchKey.set(String(group.canonicalName ?? '').trim().toLowerCase(), group)
  })

  const pluginGroups = new Map()
  // WR-02: distinct labels can slugify to the same id (`"Demo Plugin"` vs
  // `"demo-plugin"`), which would merge two dropdowns under one id — duplicate
  // React keys and each dropdown rendering the other's items. Keep the ids (and
  // test ids) unique; the visible grouping is untouched.
  const usedGroupIds = new Set(baseGroups.map((group) => group.id))

  ;(pluginItems ?? []).forEach((item) => {
    const declaredGroup = typeof item?.group === 'string' ? item.group.trim() : ''
    const pluginName = item?.pluginName ?? ''
    const label = declaredGroup || pluginName
    const matchKey = label.trim().toLowerCase()

    const staticGroup = staticByMatchKey.get(matchKey)
    if (staticGroup) {
      itemsByGroupId[staticGroup.id] = [...(itemsByGroupId[staticGroup.id] ?? []), item]
      return
    }

    let group = pluginGroups.get(matchKey)
    if (!group) {
      const slug = slugifyGroupName(label)
      let uniqueSlug = slug
      let suffix = 2
      while (usedGroupIds.has(`plugin-group-${uniqueSlug}`)) {
        uniqueSlug = `${slug}-${suffix}`
        suffix += 1
      }
      usedGroupIds.add(`plugin-group-${uniqueSlug}`)
      group = {
        id: `plugin-group-${uniqueSlug}`,
        label,
        testId: `plugin-nav-group-${uniqueSlug}`,
        panelClassName: 'max-h-[60vh] overflow-y-auto',
        items: [],
        pluginNames: [],
      }
      pluginGroups.set(matchKey, group)
    }
    itemsByGroupId[group.id] = [...(itemsByGroupId[group.id] ?? []), item]
    if (pluginName && !group.pluginNames.includes(pluginName)) {
      group.pluginNames.push(pluginName)
    }
  })

  const orderedPluginGroups = [...pluginGroups.values()].sort((a, b) => {
    const aLabel = a.label.toLowerCase()
    const bLabel = b.label.toLowerCase()
    if (aLabel !== bLabel) return aLabel < bLabel ? -1 : 1
    const aName = a.pluginNames[0] ?? ''
    const bName = b.pluginNames[0] ?? ''
    if (aName === bName) return 0
    return aName < bName ? -1 : 1
  })

  return {
    groups: [...baseGroups, ...orderedPluginGroups],
    itemsByGroupId,
  }
}