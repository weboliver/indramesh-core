import { describe, it, after } from 'node:test'
import assert from 'node:assert/strict'

import { i18n } from './i18n.js'

// A provisional runtime dictionary is installed under the code the backend
// stores (`pt-br`), mirroring `use-ui-language.jsx` → addResourceBundle(code).
// `lowerCaseLng: true` is what makes i18next look the bundle up under the
// same lowercase code instead of its canonical `pt-BR` form.
describe('i18n hyphenated language resolution', () => {
  after(async () => {
    await i18n.changeLanguage('en')
  })

  it('resolves a resource bundle registered under a lowercase hyphenated code', async () => {
    i18n.addResourceBundle('pt-br', 'systemui', { 'probe.key': 'Tradução' }, true, true)

    await i18n.changeLanguage('pt-br')
    assert.equal(i18n.t('probe.key', 'English'), 'Tradução')

    // A canonicalised input (`pt-BR`) must land on the same lowercase bundle.
    await i18n.changeLanguage('pt-BR')
    assert.equal(i18n.t('probe.key', 'English'), 'Tradução')
  })
})
