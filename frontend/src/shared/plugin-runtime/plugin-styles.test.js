import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import { createStyleInjector } from './plugin-styles.js'

// A tiny fake document so the whole style lifecycle is exercisable without
// jsdom (R03-1). The innerHTML setter throws, proving the injector never
// injects markup.
function fakeDocument() {
  const head = {
    children: [],
    appendChild(element) {
      element.parentNode = head
      head.children.push(element)
      return element
    },
    removeChild(element) {
      head.children = head.children.filter((child) => child !== element)
      element.parentNode = null
      return element
    },
  }

  function createElement() {
    const element = {
      parentNode: null,
      attributes: {},
      textContent: '',
      setAttribute(name, value) {
        this.attributes[name] = value
      },
    }
    Object.defineProperty(element, 'innerHTML', {
      get() {
        return undefined
      },
      set() {
        throw new Error('innerHTML must never be used')
      },
    })
    return element
  }

  return { head, createElement }
}

function fakeRoot() {
  const root = {
    children: [],
    appendChild(element) {
      element.parentNode = root
      root.children.push(element)
      return element
    },
    removeChild(element) {
      root.children = root.children.filter((child) => child !== element)
      element.parentNode = null
      return element
    },
  }
  return root
}

describe('injectPluginStyles (D-15 scoped style lifecycle)', () => {
  it('injects one stylesheet with textContent and dedupes identical identity/content', () => {
    const doc = fakeDocument()
    const injector = createStyleInjector({ document: doc })

    const first = injector.injectPluginStyles('alpha', 'editor', '.x{color:red}', { root: doc.head })
    const second = injector.injectPluginStyles('alpha', 'editor', '.x{color:red}', { root: doc.head })

    assert.equal(first.ok, true)
    assert.equal(second.ok, true)
    assert.equal(doc.head.children.length, 1)
    assert.equal(doc.head.children[0].textContent, '.x{color:red}')
    assert.equal(doc.head.children[0].attributes['data-plugin-owner'], 'alpha')
    assert.equal(doc.head.children[0].attributes['data-plugin-style'], 'editor')
  })

  it('refcounts repeated mounts and removes the stylesheet on the last release', () => {
    const doc = fakeDocument()
    const injector = createStyleInjector({ document: doc })

    const handles = [
      injector.injectPluginStyles('alpha', 'editor', '.x{}', { root: doc.head }),
      injector.injectPluginStyles('alpha', 'editor', '.x{}', { root: doc.head }),
    ]
    assert.equal(doc.head.children.length, 1)

    handles[0].release()
    handles[0].release() // idempotent
    assert.equal(doc.head.children.length, 1)

    handles[1].release()
    assert.equal(doc.head.children.length, 0)
  })

  it('refuses a reused style id with conflicting content instead of replacing it', () => {
    const doc = fakeDocument()
    const injector = createStyleInjector({ document: doc })

    injector.injectPluginStyles('alpha', 'editor', '.x{color:red}', { root: doc.head })
    const conflict = injector.injectPluginStyles('alpha', 'editor', '.x{color:blue}', { root: doc.head })

    assert.equal(conflict.ok, false)
    assert.equal(conflict.reason, 'style_conflict')
    assert.equal(doc.head.children.length, 1)
    assert.equal(doc.head.children[0].textContent, '.x{color:red}')
  })

  it('force-cleans only the released owner and leaves other owners untouched', () => {
    const doc = fakeDocument()
    const injector = createStyleInjector({ document: doc })

    injector.injectPluginStyles('alpha', 'a-style', '.a{}', { root: doc.head })
    injector.injectPluginStyles('beta', 'b-style', '.b{}', { root: doc.head })

    injector.releaseOwner('alpha')

    assert.equal(doc.head.children.length, 1)
    assert.equal(doc.head.children[0].attributes['data-plugin-owner'], 'beta')
  })

  it('scopes sheets per root so the same identity in another root is a separate sheet', () => {
    const doc = fakeDocument()
    const otherRoot = fakeRoot()
    const injector = createStyleInjector({ document: doc })

    injector.injectPluginStyles('alpha', 'editor', '.x{}', { root: doc.head })
    injector.injectPluginStyles('alpha', 'editor', '.x{}', { root: otherRoot })

    assert.equal(doc.head.children.length, 1)
    assert.equal(otherRoot.children.length, 1)
  })

  it('refuses injection without a usable root', () => {
    const injector = createStyleInjector({ document: undefined })
    const result = injector.injectPluginStyles('alpha', 'editor', '.x{}')

    assert.equal(result.ok, false)
    assert.equal(result.reason, 'no_root')
    assert.equal(typeof result.release, 'function')
  })
})
