import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useAuth } from './use-auth'
import { useLanguages } from './use-languages'
import api from './use-api'
import { i18n } from '../lib/i18n'
import { showToast } from '../components/Toast'

const UiLanguageContext = createContext(null)

const DICTIONARY_STALE_TIME = 5 * 60 * 1000

function fetchUiDictionary(code) {
  return api.get('ui-translations/dictionary', { params: { lang: code } }).then((response) => response.data)
}

// D-02/D-04/D-05/D-06/D-12/D-14/D-15/D-16 — this provider owns only the
// profile-selected UI language. It never imports useActiveLanguage or reads
// `?lang=`; the schema/data-language switcher stays fully independent.
export function UiLanguageProvider({ children }) {
  const { t } = useTranslation()
  const { profile, isAuthenticated } = useAuth()
  // Gate on auth: this provider wraps the whole app including Login (D-04),
  // and an ungated list call fires unauthenticated on first mount. With 321-07
  // that 401 no longer hard-reloads the page — the interceptor refreshes once
  // and retries, and the boot path skips the refresh on `/login` — but the
  // request would still be a round trip that can only fail, so keep the gate.
  const { data: languagesData } = useLanguages(0, 200, { enabled: isAuthenticated })
  const queryClient = useQueryClient()
  const [runtimeLanguage, setRuntimeLanguage] = useState(i18n.resolvedLanguage || i18n.language || 'en')
  const requestIdRef = useRef(0)
  const failedDictionaryCodeRef = useRef(null)

  const activeLanguages = useMemo(
    () => (Array.isArray(languagesData) ? languagesData : []).filter((language) => language.is_active),
    [languagesData]
  )

  // Monotonic request id guards against a stale dictionary fetch/activation
  // completing after a newer one was already requested (D-12).
  const activateUiLanguage = useCallback(async (code) => {
    const requestId = ++requestIdRef.current

    if (!code || code === 'en') {
      failedDictionaryCodeRef.current = null
      await i18n.changeLanguage('en')
      if (requestIdRef.current !== requestId) return
      setRuntimeLanguage('en')
      return
    }

    try {
      const dictionary = await queryClient.fetchQuery({
        queryKey: ['ui-strings', code],
        queryFn: () => fetchUiDictionary(code),
        staleTime: DICTIONARY_STALE_TIME,
      })
      if (requestIdRef.current !== requestId) return
      i18n.addResourceBundle(code, 'systemui', dictionary.strings, true, true)
      await i18n.changeLanguage(code)
      if (requestIdRef.current !== requestId) return
      failedDictionaryCodeRef.current = null
      setRuntimeLanguage(code)
    } catch {
      if (requestIdRef.current !== requestId) return
      // Retain English (or the already-loaded English bundle); the saved
      // profile preference is left untouched so a later retry/reactivation
      // can still restore it (D-14/D-15).
      if (failedDictionaryCodeRef.current !== code) {
        failedDictionaryCodeRef.current = code
        showToast(
          t('profile.ui_language_dictionary_failed', "Couldn't load interface translations. The interface is shown in English."),
          { type: 'error' }
        )
      }
    }
  }, [queryClient, t])

  useEffect(() => {
    if (!profile) return
    const code = profile.ui_language || 'en'
    const isActive = code === 'en' || activeLanguages.some((language) => language.code === code)
    activateUiLanguage(isActive ? code : 'en')
    // activateUiLanguage is stable across renders (only queryClient/t change
    // identity, neither of which should re-trigger this on every profile poll).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [profile, activeLanguages])

  const isLanguageActive = useCallback(
    (code) => activeLanguages.some((language) => language.code === code),
    [activeLanguages]
  )

  const value = useMemo(() => ({
    activeLanguages,
    runtimeLanguage,
    activateUiLanguage,
    isLanguageActive,
  }), [activeLanguages, runtimeLanguage, activateUiLanguage, isLanguageActive])

  return (
    <UiLanguageContext.Provider value={value}>
      {children}
    </UiLanguageContext.Provider>
  )
}

export function useUiLanguage() {
  return useContext(UiLanguageContext)
}
