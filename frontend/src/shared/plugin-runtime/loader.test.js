import { describe, it } from 'node:test'
import assert from 'node:assert/strict'

import api from '../hooks/use-api.js'
import { clearToken, setToken } from '../hooks/session-token.js'
import * as loader from './loader.js'

const { buildPluginBundleUrl, createBundleLoader } = loader

const plugin = {
  name: 'example', version: '0.3.0',
  frontend: { bundle: 'static/example.js', extension_points: ['pages'] },
}

describe('credentialed plugin bundle loader', () => {
  it('uses the shared client to request text at the versioned URL with a header, never a token in the URL', async () => {
    assert.equal(typeof createBundleLoader, 'function', 'credentialed loader factory is required')
    const originalAdapter = api.defaults.adapter
    const token = 'secret-test-token'
    // The bearer comes from the shared in-memory store — never from web
    // storage and never from the URL (D-01/T-321-02).
    setToken(token)
    let seen
    api.defaults.adapter = async (config) => {
      seen = config
      return { data: 'export default { pages: [] }', status: 200, statusText: 'OK', headers: {}, config }
    }
    try {
      const load = createBundleLoader({ importModule: async () => ({ default: { pages: [] } }) })
      assert.equal((await load(plugin, 2)).state, 'ready')
      assert.equal(api.getUri(seen), buildPluginBundleUrl(plugin, 2))
      assert.equal(seen.headers.Authorization, `Bearer ${token}`)
      assert.equal(seen.headers.get('Cache-Control'), 'no-store')
      assert.equal(seen.responseType, 'text')
      assert.equal(seen.baseURL, '')
      assert.ok(api.getUri(seen).includes('&ce=2&attempt=2'))
      assert.ok(!api.getUri(seen).includes(token), 'the token must never travel in a URL')
    } finally {
      api.defaults.adapter = originalAdapter
      clearToken()
    }
  })

  it('requests the bundle with a no-store policy and a cache-epoch URL so no stored response can outlive authorization', async () => {
    const originalAdapter = api.defaults.adapter
    const token = 'epoch-test-token'
    setToken(token)
    let seen
    api.defaults.adapter = async (config) => {
      seen = config
      return { data: 'export default { pages: [] }', status: 200, statusText: 'OK', headers: {}, config }
    }
    try {
      const load = createBundleLoader({ importModule: async () => ({ default: { pages: [] } }) })
      assert.equal((await load(plugin, 0)).state, 'ready')
      const url = api.getUri(seen)
      assert.equal(url, buildPluginBundleUrl(plugin, 0))
      assert.ok(url.includes('?v=0.3.0&ce=2'))
      assert.ok(!url.includes(token), 'the token must never travel in a URL')
      assert.equal(seen.headers.get('Cache-Control'), 'no-store')

      seen = undefined
      assert.deepEqual(await load({ name: 'frontend-less', frontend: null }), {
        state: 'ready', contributions: {}, faults: [],
        rejected: [], translations: {}, translationWarnings: [],
      })
      assert.equal(seen, undefined)
    } finally {
      api.defaults.adapter = originalAdapter
      clearToken()
    }
  })

  it('imports the fetched source and validates the declared descriptor', async () => {
    let source
    const load = createBundleLoader({
      requestBundle: async () => 'export default { pages: [] }',
      importModule: async (text) => { source = text; return { default: { pages: [] } } },
    })
    assert.deepEqual(await load(plugin), {
      state: 'ready',
      contributions: { pages: [] },
      faults: [],
      rejected: [],
      translations: {},
      translationWarnings: [],
    })
    assert.equal(source, 'export default { pages: [] }')
  })

  it('imports a text/javascript object URL and revokes it after the module resolves', async () => {
    const create = URL.createObjectURL
    const revoke = URL.revokeObjectURL
    let created
    let revoked
    URL.createObjectURL = (blob) => {
      created = blob
      // Node cannot import blob: URLs; a data: module exercises the same import step.
      return 'data:text/javascript,export%20default%20%7B%20pages%3A%20%5B%5D%20%7D'
    }
    URL.revokeObjectURL = (url) => { revoked = url }
    try {
      const load = createBundleLoader({ requestBundle: async () => 'export default { pages: [] }' })
      assert.equal((await load(plugin)).state, 'ready')
      assert.equal(created.type, 'text/javascript')
      assert.equal(await created.text(), 'export default { pages: [] }')
      assert.equal(revoked, 'data:text/javascript,export%20default%20%7B%20pages%3A%20%5B%5D%20%7D')
    } finally {
      URL.createObjectURL = create
      URL.revokeObjectURL = revoke
    }
  })

  it('maps an HTTP rejection to a bundle fault including its status', async () => {
    const load = createBundleLoader({ requestBundle: async () => {
      throw Object.assign(new Error('Unauthorized'), { response: { status: 401 } })
    } })
    assert.match((await load(plugin)).faults[0], /^bundle: .*401/)
  })

  it('maps import failures to bundle faults without throwing', async () => {
    const load = createBundleLoader({
      requestBundle: async () => 'bad source',
      importModule: async () => { throw new Error('bad module') },
    })
    assert.deepEqual(await load(plugin), {
      state: 'ui_fault', contributions: {}, faults: ['bundle: bad module'],
      rejected: [], translations: {}, translationWarnings: [],
    })
  })

  it('resolves frontend-less plugins without requesting or importing', async () => {
    const fail = () => { throw new Error('unexpected request') }
    const load = createBundleLoader({ requestBundle: fail, importModule: fail })
    assert.deepEqual(await load({ name: 'server-only', frontend: null }), {
      state: 'ready', contributions: {}, faults: [],
      rejected: [], translations: {}, translationWarnings: [],
    })
  })

  it('retains undeclared and malformed descriptor faults', async () => {
    const requestBundle = async () => 'source'
    for (const [descriptor, reason] of [
      [{ navItems: [] }, 'undeclared: navItems'],
      [{ pages: {} }, 'malformed: pages'],
    ]) {
      const load = createBundleLoader({ requestBundle, importModule: async () => ({ default: descriptor }) })
      assert.deepEqual((await load(plugin)).faults, [reason])
    }
  })

  it('records an entry_invalid fault per malformed entry without disabling the bundle', async () => {
    const descriptor = {
      itemBadges: [
        { id: 'ok', kinds: ['object'], resolve: () => null },
        { id: 'broken', kinds: ['object'] },
      ],
    }
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: descriptor }),
    })
    const result = await load({
      name: 'badges', version: '1', frontend: { bundle: 'x.js', extension_points: ['itemBadges'] },
    })

    assert.equal(result.state, 'ready')
    assert.equal(result.contributions.itemBadges.length, 1)
    assert.equal(result.contributions.itemBadges[0].id, 'ok')
    assert.deepEqual(result.faults, [])
    assert.equal(result.rejected.length, 1)
    assert.equal(result.rejected[0].kind, 'entry_invalid')
    assert.equal(result.rejected[0].family, 'itemBadges')
    assert.equal(result.rejected[0].slot, 'itemBadges:broken')
    assert.match(result.rejected[0].reason, /^entry_invalid:itemBadges:/)
  })

  it('drops an invalid navItems entry without disabling the bundle', async () => {
    const descriptor = { navItems: [{ id: 'good' }, { id: 'broken', badge: 'nope' }] }
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: descriptor }),
    })
    const result = await load({
      name: 'nav', version: '1', frontend: { bundle: 'x.js', extension_points: ['navItems'] },
    })

    assert.equal(result.state, 'ready')
    assert.deepEqual(result.contributions.navItems.map((entry) => entry.id), ['good'])
    assert.equal(result.rejected.length, 1)
    assert.equal(result.rejected[0].slot, 'navItems:broken')
  })

  it('drops a schemaWizardEntries entry without a valid operation permission', async () => {
    const descriptor = {
      schemaWizardEntries: [
        { id: 'ok', label: 'OK', permission: 'example-tools:read', component: () => null },
        { id: 'no-perm', label: 'No permission', component: () => null },
      ],
    }
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: descriptor }),
    })
    const result = await load({
      name: 'wizard',
      version: '1',
      frontend: { bundle: 'x.js', extension_points: ['schemaWizardEntries'] },
    })

    assert.equal(result.state, 'ready')
    assert.deepEqual(
      result.contributions.schemaWizardEntries.map((entry) => entry.id),
      ['ok']
    )
    assert.equal(result.rejected.length, 1)
    assert.equal(result.rejected[0].kind, 'entry_invalid')
    assert.equal(result.rejected[0].slot, 'schemaWizardEntries:no-perm')
  })

  it('validates the remaining three shared action families per entry', async () => {
    const valid = (id) => ({ id, label: id, permission: 'example-tools:read', component: () => null })
    const descriptor = {
      translationPanelActions: [valid('panel-ok'), { id: 'panel-bad', label: 'Bad', component: () => null }],
      translationAdminActions: [valid('admin-ok'), { id: 'admin-bad', label: 'Bad', permission: 'nope', component: () => null }],
      taskDetailActions: [valid('task-ok'), { id: 'task-bad', label: '', permission: 'example-tools:read', component: () => null }],
    }
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: descriptor }),
    })
    const result = await load({
      name: 'actions',
      version: '1',
      frontend: {
        bundle: 'x.js',
        extension_points: ['translationPanelActions', 'translationAdminActions', 'taskDetailActions'],
      },
    })

    assert.equal(result.state, 'ready')
    assert.deepEqual(result.contributions.translationPanelActions.map((entry) => entry.id), ['panel-ok'])
    assert.deepEqual(result.contributions.translationAdminActions.map((entry) => entry.id), ['admin-ok'])
    assert.deepEqual(result.contributions.taskDetailActions.map((entry) => entry.id), ['task-ok'])
    assert.deepEqual(
      result.rejected.map((entry) => entry.slot),
      ['translationPanelActions:panel-bad', 'translationAdminActions:admin-bad', 'taskDetailActions:task-bad']
    )
  })

  it('returns validated translations and their warnings', async () => {
    const descriptor = { translations: { en: { hello: 'Hello' }, de: 'nope' } }
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: descriptor }),
    })
    const result = await load({
      name: 'i18n', version: '1', frontend: { bundle: 'x.js', extension_points: [] },
    })

    assert.deepEqual(result.translations, { en: { hello: 'Hello' } })
    assert.equal(result.translationWarnings.length, 1)
  })

  it('yields empty translations without a warning when the descriptor omits them', async () => {
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: {} }),
    })
    const result = await load({
      name: 'plain', version: '1', frontend: { bundle: 'x.js', extension_points: [] },
    })

    assert.deepEqual(result.translations, {})
    assert.deepEqual(result.translationWarnings, [])
    assert.deepEqual(result.rejected, [])
  })

  it('still faults the whole bundle for an undeclared new family', async () => {
    const load = createBundleLoader({
      requestBundle: async () => 'source',
      importModule: async () => ({ default: { itemBadges: [] } }),
    })
    const result = await load({
      name: 'badges', version: '1', frontend: { bundle: 'x.js', extension_points: [] },
    })

    assert.equal(result.state, 'ui_fault')
    assert.match(result.faults[0], /undeclared:/)
    assert.match(result.faults[0], /itemBadges/)
    assert.deepEqual(result.rejected, [])
    assert.deepEqual(result.translations, {})
    assert.deepEqual(result.translationWarnings, [])
  })
})

// CLEAN-03 absence (D-06): only runtime-listed plugins are loaded. The eager
// list filter lives in PluginProvider.jsx (`plugins/runtime` returns ACTIVE
// plugins only) and is React-bound, and the deferred path's runtime-snapshot
// restriction is already pinned in deferred-module.test.js ("drops a completion
// whose owner left the runtime"). This case pins the loader half that IS
// observable here: when a disabled plugin's asset request is refused by the
// plugin-state gate (503), the loader applies NO contributions and reports a
// bundle fault — a disabled plugin can never leave a dead page, nav item or
// section behind.
describe('first-party plugin absence contributes nothing (CLEAN-03/D-06)', () => {
  const FIRST_PARTY_OWNERS = [
    'graphql-studio',
    'log-explorer',
    'ai-schema-wizard',
    'ai-translation',
  ]

  it('applies no contributions and reports a bundle fault when the disabled plugin asset answers 503', async () => {
    for (const name of FIRST_PARTY_OWNERS) {
      const load = createBundleLoader({
        requestBundle: async () => {
          throw Object.assign(new Error('Service Unavailable'), {
            response: { status: 503 },
          })
        },
      })

      const result = await load({
        name,
        version: '1.0.0',
        frontend: {
          bundle: 'static/plugin.js',
          extension_points: ['pages', 'navItems', 'settingsSections'],
        },
      })

      assert.equal(result.state, 'ui_fault', `${name} must not report a ready bundle`)
      assert.deepEqual(result.contributions, {}, `${name} contributed UI while disabled`)
      assert.match(result.faults[0], /503/)
    }
  })
})
