#!/usr/bin/env node

// Phase 232 task 2 — proves the singleton fallback/interpolation/plural
// contract (I18N-04/I18N-06) before any Profile UI or plural call site
// consumes it. Imports the real production singleton and exercises it with
// disposable `phase232.runtime.*` resources only; never touches the backend,
// localStorage, or locale JSON files.

import assert from 'node:assert/strict'
import { i18n, selectPluralKey } from '../src/shared/lib/i18n.js'

const NS = 'systemui'

// Test 1: a requested-language resource overrides English after
// addResourceBundle + changeLanguage.
i18n.addResourceBundle('en', NS, { 'phase232.runtime.greeting': 'Hello {{name}}' }, true, true)
await i18n.changeLanguage('en')
assert.equal(
  i18n.t('phase232.runtime.greeting', 'Hi {{name}}', { name: 'Ann' }),
  'Hello Ann'
)

i18n.addResourceBundle('de', NS, { 'phase232.runtime.greeting': 'Hallo {{name}}' }, true, true)
await i18n.changeLanguage('de')
assert.equal(
  i18n.t('phase232.runtime.greeting', 'Hi {{name}}', { name: 'Ann' }),
  'Hallo Ann'
)

// Test 2: missing and empty requested-language values resolve through
// English, then an explicit code default — never a raw key or empty string.
assert.equal(i18n.t('phase232.runtime.unknown_key', 'Code default'), 'Code default')

i18n.addResourceBundle('en', NS, { 'phase232.runtime.english_only': 'English only value' }, true, true)
assert.equal(
  i18n.t('phase232.runtime.english_only', 'Code default'),
  'English only value',
  'a key missing on the active (de) language must fall through to the English resource'
)

i18n.addResourceBundle('de', NS, { 'phase232.runtime.empty_value': '' }, true, true)
assert.equal(
  i18n.t('phase232.runtime.empty_value', 'Code default'),
  'Code default',
  'returnEmptyString: false must treat an empty translation as missing'
)

// Test 3: {{name}} interpolation escapes <script>-like input under the
// singleton's configured interpolation mode (escapeValue: true).
await i18n.changeLanguage('en')
i18n.addResourceBundle('en', NS, { 'phase232.runtime.escape_test': 'Hi {{name}}' }, true, true)
const escaped = i18n.t('phase232.runtime.escape_test', 'Hi {{name}}', {
  name: '<script>alert(1)</script>',
})
assert.ok(!escaped.includes('<script>'), 'raw <script> tag must not survive interpolation')
assert.ok(escaped.includes('&lt;script&gt;'), 'interpolated value must be HTML-escaped')

// Test 4: selectPluralKey('probe', 1) and selectPluralKey('probe', 2) choose
// the current language's categories while retaining the `_other` fallback
// for the Phase 231 two-key catalog convention.
i18n.addResourceBundle('en', NS, { 'phase232.runtime.probe_other': 'probe other' }, true, true)
assert.equal(
  selectPluralKey('phase232.runtime.probe', 1),
  'phase232.runtime.probe_other',
  'count=1 must fall back to _other when _one is not present in the catalog'
)

i18n.addResourceBundle('en', NS, { 'phase232.runtime.probe_one': 'probe one' }, true, true)
assert.equal(selectPluralKey('phase232.runtime.probe', 1), 'phase232.runtime.probe_one')
assert.equal(selectPluralKey('phase232.runtime.probe', 2), 'phase232.runtime.probe_other')

console.log('phase232 i18n runtime check passed: fallback chain, interpolation escaping, and plural selection verified')
