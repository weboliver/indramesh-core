import i18next from 'i18next'
import { initReactI18next } from 'react-i18next'

// Phase 232 — the only module in the app that calls i18next.init(). Every
// other consumer imports the singleton exported below. Runtime dictionaries
// are installed later via i18next.addResourceBundle + changeLanguage
// (use-ui-language.jsx); this module only establishes the English code-default
// baseline so the app can render before authentication/profile resolution.
i18next.use(initReactI18next).init({
  lng: 'en',
  fallbackLng: 'en',
  defaultNS: 'systemui',
  keySeparator: false,
  returnEmptyString: false,
  initImmediate: false,
  interpolation: {
    escapeValue: true,
  },
  react: {
    useSuspense: false,
    bindI18nStore: 'added',
  },
  resources: {
    en: {
      systemui: {},
    },
  },
})

// Phase 231's catalog only ever emits `_one`/`_other` suffixed keys (D: plural
// expansion is disabled, see phase231-plural-audit.mjs). selectPluralKey still
// asks Intl.PluralRules for the language-correct category (e.g. German/French
// `few`/`many` categories do not exist in the two-key catalog) and falls back
// to `_other` whenever the resolved category has no matching resource key.
export function selectPluralKey(baseKey, count) {
  const language = i18next.resolvedLanguage || i18next.language || 'en'
  const category = new Intl.PluralRules(language).select(count)
  const candidate = `${baseKey}_${category}`
  return i18next.exists(candidate) ? candidate : `${baseKey}_other`
}

export const i18n = i18next
