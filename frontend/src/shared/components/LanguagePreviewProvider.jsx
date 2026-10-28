import { createContext, useContext, useState, useCallback, useMemo } from 'react'

const LanguagePreviewContext = createContext(null)

export function LanguagePreviewProvider({ children }) {
  const [previewLang, setPreviewLangState] = useState(() => new URLSearchParams(window.location.search).get('lang'))

  const setPreviewLang = useCallback((code) => setPreviewLangState(code), [])
  const resetPreview = useCallback(() => setPreviewLangState(null), [])

  const value = useMemo(() => ({ previewLang, setPreviewLang, resetPreview }), [previewLang, setPreviewLang, resetPreview])

  return (
    <LanguagePreviewContext.Provider value={value}>
      {children}
    </LanguagePreviewContext.Provider>
  )
}

export function useLanguagePreview() {
  const ctx = useContext(LanguagePreviewContext)
  if (!ctx) throw new Error('useLanguagePreview must be used within a LanguagePreviewProvider')
  return ctx
}
