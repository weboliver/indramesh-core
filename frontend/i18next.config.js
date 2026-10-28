import { defineConfig } from 'i18next-cli'

export default defineConfig({
  locales: ['en'],
  extract: {
    input: ['src/**/*.{js,jsx,ts,tsx}'],
    output: 'locales/{{language}}/systemui.json',
    defaultNS: 'systemui',
    keySeparator: false,
    nsSeparator: false,
    primaryLanguage: 'en',
    sort: true,
    // Plural variants are selected explicitly with Intl.PluralRules in source;
    // the catalog keys already include the required _one/_other suffixes.
    disablePlurals: true,
  },
  lint: {
    checkInterpolationParams: true,
    checkConcatenation: 'error',
    checkPunctuationConcatenation: 'error',
    ignoredAttributes: [
      'className',
      'data-testid',
      'id',
      'name',
      'role',
      'value',
    ],
    ignoredTags: ['code', 'pre'],
  },
})
