import { createContext, useContext, useCallback } from 'react'
import { hexToHsl } from '../lib/hex-to-hsl'

function contrastFg(hex) {
  const r = parseInt(hex.slice(1, 3), 16) / 255
  const g = parseInt(hex.slice(3, 5), 16) / 255
  const b = parseInt(hex.slice(5, 7), 16) / 255
  const l = (Math.max(r, g, b) + Math.min(r, g, b)) / 2
  return l < 0.5 ? '0 0% 98%' : '0 0% 4%'
}

const FIELD_MAP = [
  ['primary_color', 'dark_primary_color', '--template-primary'],
  ['header_bg_color', 'dark_header_bg_color', '--template-header-bg'],
  ['sidebar_bg_color', 'dark_sidebar_bg_color', '--template-sidebar-bg'],
  ['section_bg_color', 'dark_section_bg_color', '--template-section-bg'],
  ['surface_bg_color', 'dark_surface_bg_color', '--template-surface-bg'],
]

const TemplateContext = createContext({
  setTemplate: () => {},
  clearTemplate: () => {},
})

export function useTemplate() {
  return useContext(TemplateContext)
}

export function TemplateProvider({ children }) {
  const setTemplate = useCallback((schema) => {
    let styleEl = document.getElementById('template-colors')
    if (!styleEl) {
      styleEl = document.createElement('style')
      styleEl.id = 'template-colors'
      document.head.appendChild(styleEl)
    }
    const hf = contrastFg(schema.header_bg_color)
    const sf = contrastFg(schema.sidebar_bg_color)
    const smf = sf === '0 0% 98%' ? '0 0% 65%' : '215.4 16.3% 46.9%'
    const secf = contrastFg(schema.section_bg_color)
    const surf = contrastFg(schema.surface_bg_color)
    const darkHf = contrastFg(schema.dark_header_bg_color)
    const darkSf = contrastFg(schema.dark_sidebar_bg_color)
    const darkSmf = darkSf === '0 0% 98%' ? '0 0% 65%' : '215.4 16.3% 46.9%'
    const darkSecf = contrastFg(schema.dark_section_bg_color)
    const darkSurf = contrastFg(schema.dark_surface_bg_color)
    const lightRules = FIELD_MAP.map(([lightField, , cssVar]) =>
      `  ${cssVar}: ${hexToHsl(schema[lightField])};`
    ).join('\n') + [
      `  --card: ${hexToHsl(schema.surface_bg_color)};`,
      `  --card-foreground: ${surf};`,
      `  --template-header-fg: ${hf};`,
      `  --template-sidebar-fg: ${sf};`,
      `  --template-sidebar-muted-fg: ${smf};`,
      `  --template-section-fg: ${secf};`,
      `  --template-surface-fg: ${surf};`,
    ].map(r => '\n' + r).join('')
    const darkRules = FIELD_MAP.map(([, darkField, cssVar]) =>
      `  ${cssVar}: ${hexToHsl(schema[darkField])};`
    ).join('\n') + [
      `  --card: ${hexToHsl(schema.dark_surface_bg_color)};`,
      `  --card-foreground: ${darkSurf};`,
      `  --template-header-fg: ${darkHf};`,
      `  --template-sidebar-fg: ${darkSf};`,
      `  --template-sidebar-muted-fg: ${darkSmf};`,
      `  --template-section-fg: ${darkSecf};`,
      `  --template-surface-fg: ${darkSurf};`,
    ].map(r => '\n' + r).join('')
    styleEl.textContent = `:root {\n${lightRules}\n}\n.dark {\n${darkRules}\n}`
  }, [])

  const clearTemplate = useCallback(() => {
    const styleEl = document.getElementById('template-colors')
    if (styleEl) styleEl.remove()
  }, [])

  return (
    <TemplateContext.Provider value={{ setTemplate, clearTemplate }}>
      {children}
    </TemplateContext.Provider>
  )
}
