import { useCallback, useMemo } from 'react'
import { useSearchParams } from 'react-router-dom'
import i18next from 'i18next'
import { interpolateDefault } from '../lib/i18n-defaults'

function t(key, defaultValue, variables = {}) {
  return i18next.isInitialized ? i18next.t(key, defaultValue, variables) : defaultValue
}

// D-06: schema-context pages (schemaLanguages provided and non-empty) use the
// schema's enabled languages; non-schema pages (or schemas with no enabled
// languages configured) fall back to all active system languages.
//
// Both input lists are filtered to `is_active` here, in this one shared
// place, rather than trusting each caller to pre-filter: `useSchemaLanguages`
// intentionally returns deactivated-but-still-enabled languages too (D-04 —
// the Schema Settings tab needs them to render its "flagged, disabled"
// rows), and callers of this hook pass that raw list straight through. A
// system-wide deactivation on the Languages admin page must disappear from
// every language *selector* (this switcher) even though it stays visible,
// greyed out, in the Schema Settings tab itself.
function resolveLanguageList({ schemaLanguages, systemLanguages, schemaDefaultCode } = {}) {
  const schemaList = (Array.isArray(schemaLanguages) ? schemaLanguages : []).filter(l => l.is_active)
  const systemList = (Array.isArray(systemLanguages) ? systemLanguages : []).filter(l => l.is_active)
  // schemaDefaultCode is accepted for interface completeness (callers may pass
  // the schema's default language code alongside its enabled list) but does
  // not affect which list is returned — it's a resolution hint for consumers.
  void schemaDefaultCode
  return schemaList.length > 0 ? schemaList : systemList
}

export function getLanguageName(code, languageList) {
  if (!code) return ''
  const list = Array.isArray(languageList) ? languageList : []
  const entry = list.find(l => l.code === code)
  if (!entry) return code
  const translated = t('common.language.label', '{{name}} ({{code}})')
  return interpolateDefault(translated, { name: entry.name, code: entry.code })
}

// D-05..D-08: URL-only persistence — `?lang=` is the sole source of truth for
// the active display language. No sessionStorage/localStorage is ever used.
export function useActiveLanguage({ schemaLanguages, systemLanguages, schemaDefaultCode } = {}) {
  const [searchParams, setSearchParams] = useSearchParams()

  const languageList = useMemo(
    () => resolveLanguageList({ schemaLanguages, systemLanguages, schemaDefaultCode }),
    [schemaLanguages, systemLanguages, schemaDefaultCode]
  )

  const isSchemaContext = Array.isArray(schemaLanguages) && schemaLanguages.length > 0

  const lang = useMemo(() => {
    const raw = searchParams.get('lang') || null
    if (!raw) return null
    const valid = languageList.some(l => l.code === raw)
    return valid ? raw : null
  }, [searchParams, languageList])

  const setLang = useCallback((code) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (code) {
        next.set('lang', code)
      } else {
        next.delete('lang')
      }
      return next
    }, { replace: true })
  }, [setSearchParams])

  return { lang, setLang, languageList, isSchemaContext }
}
