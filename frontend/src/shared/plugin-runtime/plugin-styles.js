// D-15 scoped plugin styles. The injector owns one stylesheet per
// (owner, styleId, root) identity, refcounts repeated mounts, dedupes by CSS
// content equality, diagnoses a conflicting reused id locally (never last-wins)
// and force-cleans only the owner generation it is told to release. It injects
// with textContent, never innerHTML, and never styles the document head
// globally.
//
// The document is injectable (R03-1) so node:test can exercise the whole
// lifecycle with a small fake; the default is the browser document.

function defaultRoot(doc) {
  return doc?.head ?? doc?.body ?? null
}

export function createStyleInjector({ document: doc = globalThis.document } = {}) {
  // A root element gets a stable key without leaking a DOM property onto it.
  const rootKeys = new WeakMap()
  let nextRootKey = 1
  // key -> { owner, styleId, css, element, root, refs }
  const records = new Map()
  // owner -> Set<element>. Dynamic sheets/portals the plugin's own code (e.g. a
  // third-party editor) creates outside this injector are adopted here so they
  // can be disposed by owner generation. Pre-existing host elements are never
  // in a plugin's adopted set.
  const ownedElements = new Map()

  function rootKey(root) {
    if (root && typeof root === 'object') {
      let key = rootKeys.get(root)
      if (key === undefined) {
        key = `root-${nextRootKey++}`
        rootKeys.set(root, key)
      }
      return key
    }
    return `root:${String(root ?? 'default')}`
  }

  function keyFor(owner, styleId, root) {
    return `${owner}\u0000${styleId}\u0000${rootKey(root)}`
  }

  function removeElement(record) {
    const parent = record.element?.parentNode
    if (parent && typeof parent.removeChild === 'function') parent.removeChild(record.element)
    else if (typeof record.element?.remove === 'function') record.element.remove()
  }

  function makeHandle(key, record) {
    let released = false
    return {
      ok: true,
      release() {
        // Idempotent: a second release is a no-op.
        if (released) return
        released = true
        const current = records.get(key)
        if (!current || current !== record) return
        current.refs -= 1
        if (current.refs <= 0) {
          removeElement(current)
          records.delete(key)
        }
      },
    }
  }

  function injectPluginStyles(pluginName, styleId, cssText, options = {}) {
    if (!pluginName || !styleId || typeof cssText !== 'string') {
      return { ok: false, reason: 'invalid_style', release: () => {} }
    }
    const root = options.root ?? defaultRoot(doc)
    if (!root || typeof root.appendChild !== 'function') {
      return { ok: false, reason: 'no_root', release: () => {} }
    }

    const key = keyFor(pluginName, styleId, root)
    const existing = records.get(key)
    if (existing) {
      // A reused id with conflicting content is a diagnosed local refusal; it
      // must never replace the first consumer's CSS.
      if (existing.css !== cssText) {
        return { ok: false, reason: 'style_conflict', release: () => {} }
      }
      existing.refs += 1
      return makeHandle(key, existing)
    }

    const element = doc.createElement('style')
    element.setAttribute('data-plugin-owner', pluginName)
    element.setAttribute('data-plugin-style', styleId)
    // textContent only — a plugin can never inject markup.
    element.textContent = cssText
    root.appendChild(element)

    const record = { owner: pluginName, styleId, css: cssText, element, root, refs: 1 }
    records.set(key, record)
    return makeHandle(key, record)
  }

  // Force-clean only this owner's sheets (disable/unload/version change). Other
  // owners' sheets, adopted dynamic elements and any pre-existing host styles
  // stay untouched.
  function releaseOwner(pluginName) {
    ;[...records.entries()].forEach(([key, record]) => {
      if (record.owner === pluginName) {
        removeElement(record)
        records.delete(key)
      }
    })
    const adopted = ownedElements.get(pluginName)
    if (adopted) {
      adopted.forEach((element) => removeElement({ element }))
      ownedElements.delete(pluginName)
    }
  }

  // Adopt a DOM element the plugin created itself (an editor's dynamic style
  // sheet or a portal root) so it participates in owner-generation cleanup. The
  // handle is idempotent and removes only the adopted element.
  function adoptElement(pluginName, element) {
    if (!pluginName || !element || typeof element !== 'object') {
      return { ok: false, reason: 'invalid_element', release: () => {} }
    }
    let set = ownedElements.get(pluginName)
    if (!set) {
      set = new Set()
      ownedElements.set(pluginName, set)
    }
    set.add(element)
    let released = false
    return {
      ok: true,
      release() {
        if (released) return
        released = true
        const current = ownedElements.get(pluginName)
        if (!current || !current.has(element)) return
        current.delete(element)
        removeElement({ element })
        if (current.size === 0) ownedElements.delete(pluginName)
      },
    }
  }

  function childrenOf(root) {
    return root && Array.isArray(root.children) ? root.children.slice() : []
  }

  // Snapshot the current children of a container so a later `adoptNewChildren`
  // can identify only the elements added in between. Layout-independent and
  // side-effect free.
  function snapshotChildren(root) {
    return childrenOf(root)
  }

  // Adopt every child added since `before`. Used after an editor initializes to
  // take ownership of the dynamic sheets/portals it created, without ever
  // touching elements that already existed (e.g. host editor styles).
  function adoptNewChildren(pluginName, root, before) {
    const seen = new Set(Array.isArray(before) ? before : [])
    return childrenOf(root)
      .filter((element) => !seen.has(element))
      .map((element) => adoptElement(pluginName, element))
  }

  return {
    injectPluginStyles,
    releaseOwner,
    adoptElement,
    adoptNewChildren,
    snapshotChildren,
  }
}

// Host default bound to the browser document. `releaseOwner` is the disposal
// entry point the provider calls when an owner leaves the active list.
export const { injectPluginStyles, releaseOwner } = createStyleInjector()
