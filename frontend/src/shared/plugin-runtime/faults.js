// WR-08: runtime fault bookkeeping for the plugin provider.
//
// `load` and `slot_render` faults describe one failed attempt. A retry starts a
// new attempt, and a successful (re)load supersedes every earlier transient
// fault — otherwise the admin page keeps showing an error for a plugin that has
// recovered. `slot_conflict` is a deterministic registration outcome, not a
// transient failure, so it is preserved until a fresh registration reports it
// again.

const TRANSIENT_FAULT_KINDS = new Set(['load', 'slot_render'])

export function clearTransientFaults(faults, pluginName) {
  const entries = faults?.[pluginName]
  if (!entries) return faults

  const kept = entries.filter((entry) => !TRANSIENT_FAULT_KINDS.has(entry?.kind))
  if (kept.length === entries.length) return faults

  const next = { ...faults }
  if (kept.length === 0) delete next[pluginName]
  else next[pluginName] = kept
  return next
}

export function replacePluginFaults(faults, pluginName, entries) {
  const next = { ...(faults || {}) }
  if (!entries || entries.length === 0) delete next[pluginName]
  else next[pluginName] = entries
  return next
}