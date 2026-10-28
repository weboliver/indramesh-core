import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import api from '../hooks/use-api'
import { useCan } from '../components/Can'
import { useAuth } from '../hooks/use-auth'
import { i18n } from '../lib/i18n'
import { createContributionRegistry } from './registry'
import { loadPluginBundle } from './loader'
import { setDeferredRuntimeSource } from './deferred-module'
import { clearTransientFaults, replacePluginFaults } from './faults'
import { LUCIDE_ICON_NAMES } from '../lib/lucide-icons'
import {
  CONNECTOR_ROLES,
  createOwnerGenerations,
  frameContribution,
  frameSchemaWizardEntry,
  isPermitted,
  pluginNamespace,
  selectVisibleEntries,
  sortByPluginName,
  sortContributions,
} from './contribution-utils'

const PluginContext = createContext(null)

// CORESEAM-09/D-05: bundle discovery is a RUNTIME capability, not the
// admin-only `plugins:read` the plugin management page uses. `plugins/runtime`
// lists ACTIVE plugins only and carries no admin detail. Exported so consumers
// that must not touch the admin list (e.g. PluginRouteResolver) share the exact
// same query state and cache key.
export function usePluginRuntimeList() {
  const canUseRuntime = useCan('plugin-runtime', 'use')
  return useQuery({
    queryKey: ['plugin-runtime'],
    queryFn: async () => (await api.get('plugins/runtime')).data,
    enabled: canUseRuntime,
  })
}

export function PluginProvider({ children }) {
  const { data } = usePluginRuntimeList()
  // Same check the list query enables on — needed as the effect's own guard so
  // a permission-less mount never schedules a bundle load.
  const canUseRuntime = useCan('plugin-runtime', 'use')

  const plugins = useMemo(() => (Array.isArray(data) ? data : []), [data])
  const activePlugins = useMemo(() => {
    const map = new Map()
    plugins.forEach((plugin) => map.set(plugin.name, plugin))
    return map
  }, [plugins])

  // D-14: publish the current runtime snapshot (owner -> { version, deferred
  // assets }) to the plain deferred-module helper so `loadPluginModule` can
  // resolve owner/version and restrict loads to declared assets without
  // importing this React context. Without this the helper resolves no runtime
  // and fails every deferred load closed as `undeclared_asset`.
  const deferredRuntime = useMemo(() => {
    const map = new Map()
    plugins.forEach((plugin) => {
      map.set(plugin.name, {
        version: plugin.version ?? null,
        deferred_assets: plugin.frontend?.deferred_assets ?? null,
      })
    })
    return map
  }, [plugins])

  useEffect(() => {
    setDeferredRuntimeSource(() => deferredRuntime)
    return () => setDeferredRuntimeSource(null)
  }, [deferredRuntime])

  const [runtimeStates, setRuntimeStates] = useState({})
  const [faults, setFaults] = useState({})
  const [, setRegistryVersion] = useState(0)
  const registryRef = useRef(null)
  if (registryRef.current === null) {
    registryRef.current = createContributionRegistry()
  }
  // WR-04: maps a plugin name to the `${name}@${version}` token that was last
  // started, so a manifest version bump re-requests the bundle with the new
  // `?v=` cache key in an open session instead of executing stale plugin code
  // until a full page reload.
  const startedRef = useRef(new Map())
  // WR-03: a retried plugin gets a fresh `attempt` so the loader imports a URL
  // the browser has not already cached as a failed module-map entry.
  const attemptsRef = useRef(new Map())
  // D-08/D-14: an owner generation guard. A bundle completion is only applied
  // while its captured generation is still current, so a late promise cannot
  // resurrect a contribution after the owner leaves the active list.
  const generationsRef = useRef(null)
  if (generationsRef.current === null) {
    generationsRef.current = createOwnerGenerations()
  }
  const [epoch, setEpoch] = useState(0)

  useEffect(() => {
    if (!canUseRuntime) return

    // D-08: an owner that left the active list drops its claims, runtime state,
    // faults and pending generation immediately in this render.
    const activeNames = new Set(plugins.map((plugin) => plugin.name))
    const departed = [...startedRef.current.keys()].filter((name) => !activeNames.has(name))
    if (departed.length > 0) {
      departed.forEach((name) => {
        startedRef.current.delete(name)
        attemptsRef.current.delete(name)
        generationsRef.current.invalidate(name)
        registryRef.current.unregister(name)
      })
      setRuntimeStates((prev) => {
        const next = { ...prev }
        departed.forEach((name) => { delete next[name] })
        return next
      })
      setFaults((prev) => {
        const next = { ...prev }
        departed.forEach((name) => { delete next[name] })
        return next
      })
      setRegistryVersion((value) => value + 1)
    }

    plugins.forEach((plugin) => {
      // `plugins/runtime` already filters to ACTIVE plugins, so the payload
      // carries no `state` field — a declared bundle is the whole precondition.
      if (!plugin.frontend?.bundle) return
      const startedKey = `${plugin.name}@${plugin.version ?? ''}`
      if (startedRef.current.get(plugin.name) === startedKey) return
      startedRef.current.set(plugin.name, startedKey)
      const generation = generationsRef.current.begin(plugin.name)
      setRuntimeStates((prev) => ({ ...prev, [plugin.name]: 'loading' }))
      loadPluginBundle(plugin, attemptsRef.current.get(plugin.name) ?? 0)
        .then((result) => {
          if (!generationsRef.current.isCurrent(plugin.name, generation)) return
          if (result.state === 'ready') {
            // FEEXT-04/D9: install the plugin's own JSON under a host-assigned
            // namespace. `systemui` is never passed, so a plugin can never
            // replace core copy. Invalid translations are a console warning only.
            const namespace = pluginNamespace(plugin.name)
            Object.entries(result.translations || {}).forEach(([lng, strings]) => {
              i18n.addResourceBundle(lng, namespace, strings, true, true)
            })
            ;(result.translationWarnings || []).forEach((warning) => {
              console.warn(`[plugin ${plugin.name}] translations: ${warning}`)
            })
            const { conflicts } = registryRef.current.register(plugin.name, result.contributions)
            setRegistryVersion((value) => value + 1)
            setRuntimeStates((prev) => ({ ...prev, [plugin.name]: 'ready' }))
            const rejectedConflicts = (conflicts || []).filter((entry) => entry.pluginName === plugin.name)
            // WR-08: a successful (re)load supersedes every earlier load/render
            // fault for this plugin — only deterministic registration outcomes
            // remain (`slot_conflict` and non-transient `entry_invalid`).
            setFaults((prev) =>
              replacePluginFaults(prev, plugin.name, [...rejectedConflicts, ...(result.rejected || [])])
            )
            return
          }
          setRuntimeStates((prev) => ({ ...prev, [plugin.name]: 'ui_fault' }))
          setFaults((prev) => ({
            ...prev,
            [plugin.name]: [
              ...(prev[plugin.name] || []),
              ...(result.faults || []).map((reason) => ({ kind: 'load', slot: null, reason })),
            ],
          }))
        })
        .catch((err) => {
          if (!generationsRef.current.isCurrent(plugin.name, generation)) return
          // WR-02: a throw in the success handler (i18n install, registration,
          // fault bookkeeping) would otherwise strand the plugin in `loading`
          // with no fault and no retry path. Record it as a load fault so the
          // runtime state and the admin view both show the failure.
          setRuntimeStates((prev) => ({ ...prev, [plugin.name]: 'ui_fault' }))
          setFaults((prev) => ({
            ...prev,
            [plugin.name]: [
              ...(prev[plugin.name] || []),
              { kind: 'load', slot: null, reason: `runtime: ${err?.message || err}` },
            ],
          }))
        })
    })
  }, [canUseRuntime, plugins, epoch])

  const clearFaults = useCallback((name) => {
    setFaults((prev) => clearTransientFaults(prev, name))
  }, [])

  const retry = useCallback((name) => {
    attemptsRef.current.set(name, (attemptsRef.current.get(name) ?? 0) + 1)
    clearFaults(name)
    startedRef.current.delete(name)
    setEpoch((value) => value + 1)
  }, [clearFaults])

  const reportFault = useCallback((name, fault) => {
    if (!name || !fault) return
    setFaults((prev) => ({ ...prev, [name]: [...(prev[name] || []), fault] }))
  }, [])

  const isApplicable = useCallback(
    (pluginName) =>
      // Every entry `plugins/runtime` returned is ACTIVE by construction, so
      // presence in the map is the active check (`plugin.state` no longer exists
      // in the runtime payload).
      runtimeStates[pluginName] === 'ready' && activePlugins.has(pluginName),
    [runtimeStates, activePlugins]
  )

  const contributions = registryRef.current.getContributions()

  const value = useMemo(
    () => ({ runtimeStates, faults, contributions, isApplicable, retry, reportFault, clearFaults }),
    [runtimeStates, faults, contributions, isApplicable, retry, reportFault, clearFaults]
  )

  return <PluginContext.Provider value={value}>{children}</PluginContext.Provider>
}

export function usePluginRuntime() {
  const context = useContext(PluginContext)
  if (!context) {
    throw new Error('usePluginRuntime must be used inside PluginProvider')
  }
  return context
}

export function usePluginNavItems() {
  const { contributions, isApplicable } = usePluginRuntime()
  return useMemo(
    () => (contributions.navItems || []).filter((item) => isApplicable(item.pluginName)),
    [contributions, isApplicable]
  )
}

export function usePluginPages() {
  const { contributions, isApplicable } = usePluginRuntime()
  return useMemo(
    () => (contributions.pages || []).filter((page) => isApplicable(page.pluginName)),
    [contributions, isApplicable]
  )
}

export function usePluginFaults() {
  return usePluginRuntime().faults
}

export function usePluginAttributeEditor(dataType) {
  const { contributions, isApplicable } = usePluginRuntime()
  return useMemo(
    () =>
      (contributions.attributeEditors || []).find(
        (entry) => entry.data_type === dataType && isApplicable(entry.pluginName)
      ) ?? null,
    [contributions, isApplicable, dataType]
  )
}

export function usePluginActionButtons(objectType) {
  const { contributions, isApplicable } = usePluginRuntime()
  const objectTypeId = objectType?.id ?? null
  const objectTypeName = objectType?.name ?? null
  return useMemo(
    () =>
      (contributions.actionButtons || []).filter(
        (entry) =>
          isApplicable(entry.pluginName) &&
          (entry.objectType === '*' ||
            (objectTypeId !== null && entry.objectType === objectTypeId) ||
            (objectTypeName !== null && entry.objectType === objectTypeName))
      ),
    [contributions, isApplicable, objectTypeId, objectTypeName]
  )
}

// Section families (objectDetailSections, schemaSettingsSections,
// objectTypeSettingsSections) share this hook: applicable + permitted entries,
// ordered by plugin name (D-02).
export function usePluginSections(family, context) {
  const { contributions, isApplicable } = usePluginRuntime()
  const { can } = useAuth()
  const candidates = useMemo(
    () =>
      sortByPluginName(
        (contributions[family] || []).filter(
          (entry) => isApplicable(entry.pluginName) && isPermitted(entry, can)
        )
      ),
    [contributions, isApplicable, can, family]
  )
  const availability = usePresenceAvailability(candidates, context)
  return useMemo(() => selectVisibleEntries(candidates, availability), [candidates, availability])
}

// D-06: the four shared action families (schemaWizardEntries,
// translationPanelActions, translationAdminActions, taskDetailActions) share one
// host hook. It returns applicable + permitted entries ordered by owner then
// stable id, each carrying the resolved visible label and host-stamped
// ownership. The separate attribution line is rendered by the host consumer
// from `owner`, so a plugin can never supply its own origin.
export function usePluginActionFamily(family) {
  const { contributions, isApplicable } = usePluginRuntime()
  const { can } = useAuth()
  const resolveLabel = usePluginText()
  return useMemo(
    () =>
      sortContributions(
        (contributions[family] || []).filter(
          (entry) => isApplicable(entry.pluginName) && isPermitted(entry, can)
        )
      ).map((entry) => frameContribution(entry, resolveLabel)),
    [contributions, isApplicable, can, family, resolveLabel]
  )
}

// D-12/Carry-02: the schema-wizard entry surface adds an optional async
// availability predicate on top of the shared action-family contract. Only
// already permitted + applicable entries are evaluated; availability fails
// closed until it settles; a throwing or rejecting predicate hides the entry;
// and any pending resolution is discarded when the open cycle, lifecycle, owner
// version or rights change. No polling and no provider probe happen here — the
// predicate owns whatever request it makes.
export function usePluginSchemaWizardEntries({ enabled = true } = {}) {
  const { contributions, isApplicable } = usePluginRuntime()
  const { can } = useAuth()
  const resolveLabel = usePluginText()

  const candidates = useMemo(
    () =>
      sortContributions(
        (contributions.schemaWizardEntries || []).filter(
          (entry) => isApplicable(entry.pluginName) && isPermitted(entry, can)
        )
      ),
    [contributions, isApplicable, can]
  )

  const [availability, setAvailability] = useState({})

  useEffect(() => {
    // Fail closed: a gated entry stays absent until its predicate resolves true.
    // Restarting from an empty map on every dependency change discards a
    // resolution captured under a previous open cycle / owner version / rights
    // set, so a late result can never resurrect a removed entry.
    setAvailability({})
    if (!enabled) return undefined
    let cancelled = false
    candidates.forEach((entry) => {
      if (typeof entry.isAvailable !== 'function') return
      let result
      try {
        result = entry.isAvailable()
      } catch {
        // A throwing predicate fails closed — the entry remains absent.
        return
      }
      Promise.resolve(result)
        .then((available) => {
          if (cancelled) return
          setAvailability((prev) => ({ ...prev, [schemaWizardEntryKey(entry)]: available === true }))
        })
        .catch(() => {
          if (cancelled) return
          setAvailability((prev) => ({ ...prev, [schemaWizardEntryKey(entry)]: false }))
        })
    })
    return () => {
      cancelled = true
    }
  }, [enabled, candidates])

  return useMemo(
    () =>
      candidates
        .filter((entry) => {
          if (typeof entry.isAvailable !== 'function') return true
          if (!enabled) return false
          return availability[schemaWizardEntryKey(entry)] === true
        })
        .map((entry) => frameSchemaWizardEntry(entry, resolveLabel, LUCIDE_ICON_NAMES)),
    [candidates, enabled, availability, resolveLabel]
  )
}

// D-16: the two translation action families and the schema settings section
// family may declare an optional async presence predicate. Only already
// permitted + applicable entries are evaluated; presence fails closed until it
// settles; a throwing or rejecting predicate hides the entry; and any pending
// resolution is discarded when the open cycle, owner version, rights or scope
// change. `onPresenceChanged` requests a re-evaluation of that owner's predicate
// without creating any host polling. An entry without a predicate is always
// visible, so existing contributions keep their exact behavior.
function usePresenceAvailability(candidates, context) {
  const contextKey = presenceContextKey(context)
  const contextRef = useRef(context)
  contextRef.current = context

  const generationsRef = useRef(null)
  if (generationsRef.current === null) {
    generationsRef.current = createOwnerGenerations()
  }

  const [availability, setAvailability] = useState({})

  useEffect(() => {
    // Fail closed: a gated entry stays absent until its predicate resolves true.
    // Restarting from an empty map on every dependency change discards a
    // resolution captured under a previous scope / owner version / rights set,
    // so a late result can never resurrect a removed entry.
    setAvailability({})
    let cancelled = false
    candidates.forEach((entry) => {
      if (typeof entry.isPresent !== 'function') return
      const token = generationsRef.current.begin(entry.pluginName)
      let result
      try {
        result = entry.isPresent(contextRef.current)
      } catch {
        // A throwing predicate fails closed — the entry remains absent.
        return
      }
      Promise.resolve(result)
        .then((present) => {
          if (cancelled || !generationsRef.current.isCurrent(entry.pluginName, token)) return
          setAvailability((prev) => ({ ...prev, [presenceEntryKey(entry)]: present === true }))
        })
        .catch(() => {
          if (cancelled || !generationsRef.current.isCurrent(entry.pluginName, token)) return
          setAvailability((prev) => ({ ...prev, [presenceEntryKey(entry)]: false }))
        })
    })
    return () => {
      cancelled = true
    }
  }, [candidates, contextKey])

  return availability
}

export function usePluginVisibleContributions(family, context) {
  const { contributions, isApplicable } = usePluginRuntime()
  const { can } = useAuth()
  const resolveLabel = usePluginText()

  const candidates = useMemo(
    () =>
      sortContributions(
        (contributions[family] || []).filter(
          (entry) => isApplicable(entry.pluginName) && isPermitted(entry, can)
        )
      ),
    [contributions, isApplicable, can, family]
  )

  const availability = usePresenceAvailability(candidates, context)

  return useMemo(
    () =>
      selectVisibleEntries(candidates, availability).map((entry) =>
        frameContribution(entry, resolveLabel)
      ),
    [candidates, availability, resolveLabel]
  )
}

function presenceEntryKey(entry) {
  return `${entry.pluginName}\u0000${entry.id}`
}

// A stable, order-independent string for the presence context so an equivalent
// context object does not re-trigger the predicate on every render while a real
// scope change (session/revision/entity) does.
function presenceContextKey(context) {
  if (context === null || context === undefined) return ''
  if (typeof context !== 'object') return String(context)
  return Object.keys(context)
    .sort()
    .map((key) => `${key}=${presenceContextKey(context[key])}`)
    .join('&')
}

function schemaWizardEntryKey(entry) {
  return `${entry.pluginName}\u0000${entry.id}`
}

// A2 ownership: a taskForms entry only applies to its own plugin's catalog
// types, so no plugin can replace a core or foreign form.
export function usePluginTaskForm(catalogEntry) {
  const { contributions, isApplicable } = usePluginRuntime()
  const tasktype = catalogEntry?.name ?? null
  const source = catalogEntry?.source ?? null
  const pluginName = catalogEntry?.plugin_name ?? null
  return useMemo(() => {
    if (source !== 'plugin' || pluginName === null) return null
    return (
      (contributions.taskForms || []).find(
        (entry) =>
          entry.tasktype === tasktype &&
          entry.pluginName === pluginName &&
          isApplicable(entry.pluginName)
      ) ?? null
    )
  }, [contributions, isApplicable, tasktype, source, pluginName])
}

// Applicable connector editors whose `roles` (default both) include `role`.
export function usePluginConnectorEditors(role) {
  const { contributions, isApplicable } = usePluginRuntime()
  const { can } = useAuth()
  return useMemo(
    () =>
      sortByPluginName(
        (contributions.connectorConfigEditors || []).filter(
          (entry) =>
            isApplicable(entry.pluginName) &&
            (entry.roles ?? CONNECTOR_ROLES).includes(role) &&
            isPermitted(entry, can)
        )
      ),
    [contributions, isApplicable, can, role]
  )
}

// Applicable item-badge resolvers that handle `kind`, ordered by plugin name.
export function usePluginItemBadgeResolvers(kind) {
  const { contributions, isApplicable } = usePluginRuntime()
  const { can } = useAuth()
  return useMemo(
    () =>
      sortByPluginName(
        (contributions.itemBadges || []).filter(
          (entry) =>
            isApplicable(entry.pluginName) &&
            entry.kinds.includes(kind) &&
            isPermitted(entry, can)
        )
      ),
    [contributions, isApplicable, can, kind]
  )
}

export function usePluginTranslation(pluginName) {
  return useTranslation(pluginNamespace(pluginName))
}

// Non-hook counterpart to `usePluginText` for descriptor callbacks that run
// outside the React tree but still need plugin-scoped copy — notably
// `navItems.badgeLabel(count)`, which the host calls as a plain function. The
// namespace is host-assigned, so a plugin can only ever read its own catalog
// and can never reach core `systemui`. The caller re-renders on language change
// through the host's own `useTranslation` subscription, so returning the
// current-language string here is sufficient.
export function pluginText(pluginName, key, options) {
  return i18n.getFixedT(null, pluginNamespace(pluginName))(key, options)
}

// Subscribes the caller to language changes (bare useTranslation call) and
// returns a stable resolver for a plugin key with the contribution's label as
// the default. The callback is not named `t`, so the catalog extractor never
// mistakes plugin keys for core keys.
export function usePluginText() {
  useTranslation()
  return useCallback(
    (pluginName, key, fallback) =>
      key
        ? i18n.getFixedT(null, pluginNamespace(pluginName))(key, { defaultValue: fallback })
        : fallback,
    []
  )
}

export function usePluginSettingsSections() {
  const { contributions, isApplicable } = usePluginRuntime()
  return useMemo(() => {
    const sections = (contributions.settingsSections || []).filter((entry) =>
      isApplicable(entry.pluginName)
    )
    return [...sections].sort((a, b) => {
      if (a.pluginName === b.pluginName) return 0
      return a.pluginName < b.pluginName ? -1 : 1
    })
  }, [contributions, isApplicable])
}