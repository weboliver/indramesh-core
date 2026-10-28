/**
 * PermissionMatrix client search (Phase 252, UX-06; origin-aware Phase 303).
 *
 * Pure form of the matrix's existing in-memory filter (surface 05, "existing
 * client search adapted to the standard control"): a group survives only while
 * at least one of its permissions still matches the trimmed, lowercased query
 * across resource, action, name, description, or the displayed origin — `Core`
 * or the plugin display label / stable-name fallback (303-UI-SPEC, E1). Kept
 * pure so the migration to the shared BrowseSearchInput control is
 * unit-testable.
 */

/**
 * The displayed origin of a permission (UI-SPEC "Display label falls back to
 * the stable plugin name"): `Core` when no plugin owns it, otherwise the
 * provided-by label. Deliberately locale-independent so search matches the
 * stable identity, not the translated copy.
 */
export function permissionOrigin(permission) {
  if (!permission || !permission.owner_plugin) {
    return { isCore: true, label: 'Core' }
  }
  return {
    isCore: false,
    label: permission.owner_label || permission.owner_plugin,
  }
}

export function filterPermissionGroups(groups, query) {
  const trimmed = (query || '').trim().toLowerCase()
  if (!trimmed) return groups
  return groups
    .map((group) => ({
      resource: group.resource,
      permissions: group.permissions.filter((permission) =>
        group.resource.toLowerCase().includes(trimmed) ||
        permission.action.toLowerCase().includes(trimmed) ||
        permission.name.toLowerCase().includes(trimmed) ||
        (permission.description || '').toLowerCase().includes(trimmed) ||
        permissionOrigin(permission).label.toLowerCase().includes(trimmed)
      ),
    }))
    .filter((group) => group.permissions.length > 0)
}
