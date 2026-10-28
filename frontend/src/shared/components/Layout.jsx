import { useState, useEffect, useCallback } from 'react'
import { Outlet, useLocation } from 'react-router-dom'
import TopNav from './TopNav'
import Sidebar from './Sidebar'
import LoadingBar from './LoadingBar'
import { cn } from '../lib/utils'
import { Button } from './Button'
import { TemplateProvider } from './TemplateProvider'
import { LanguagePreviewProvider } from './LanguagePreviewProvider'
import { ActiveSchemaProvider } from './ActiveSchemaProvider'
import ActiveSchemaBootstrap from './ActiveSchemaBootstrap'
import { useTranslation } from 'react-i18next'

const SIDEBAR_PATH_PREFIXES = ['/search', '/browse/', '/object-types/']

export default function Layout() {
  const { t } = useTranslation()
  const { pathname } = useLocation()
  const routeHasSidebar = SIDEBAR_PATH_PREFIXES.some((prefix) => pathname.startsWith(prefix))

  // The mobile tree drawer closes on every navigation (e.g. picking an object type).
  useEffect(() => {
    setMobileTreeOpen(false)
  }, [pathname])
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem('indramesh_theme')
    if (stored) return stored === 'dark'
    return window.matchMedia('(prefers-color-scheme: dark)').matches
  })
  const [mobileTreeOpen, setMobileTreeOpen] = useState(false)
  const [fullWidth, setFullWidth] = useState(false)

  useEffect(() => {
    const root = document.documentElement
    if (dark) {
      root.classList.add('dark')
    } else {
      root.classList.remove('dark')
    }
    localStorage.setItem('indramesh_theme', dark ? 'dark' : 'light')
  }, [dark])

  const toggleDark = () => setDark((d) => !d)
  const handleSetFullWidth = useCallback((val) => setFullWidth(val), [])

  return (
    <TemplateProvider>
      <LanguagePreviewProvider>
      <ActiveSchemaProvider>
      <ActiveSchemaBootstrap />
      <div className="h-screen flex flex-col bg-background">
      <LoadingBar />
      {!fullWidth && <TopNav onToggleDark={toggleDark} />}
      <div className="flex flex-1 overflow-hidden">
        {routeHasSidebar && (
          <Sidebar
            className={cn(
              'flex-col shrink-0',
              fullWidth
                ? 'hidden'
                : mobileTreeOpen
                  ? 'flex fixed top-14 bottom-0 left-0 z-[36] max-w-[85vw] shadow-xl md:static md:z-auto md:max-w-none md:shadow-none'
                  : 'hidden md:flex'
            )}
          />
        )}
        {routeHasSidebar && !fullWidth && mobileTreeOpen && (
          <div
            className="md:hidden fixed inset-0 z-[35] bg-black/30"
            onClick={() => setMobileTreeOpen(false)}
            aria-hidden="true"
            data-testid="mobile-tree-backdrop"
          />
        )}
        {routeHasSidebar && !fullWidth && (
          <Button
            onClick={() => setMobileTreeOpen((o) => !o)}
            size="icon"
            variant="primary"
            leftIcon={mobileTreeOpen ? 'x' : 'list-tree'}
            aria-label={t('common.toggle_sidebar', 'Toggle sidebar')}
            aria-expanded={mobileTreeOpen}
            className="md:hidden fixed top-[4.5rem] right-4 z-[38] rounded-full shadow-lg"
            data-testid="mobile-tree-toggle"
          />
        )}
        <main className="flex-1 overflow-y-auto p-6"
          style={{
            background: 'hsl(var(--template-section-bg, var(--background)))',
            color: 'hsl(var(--template-section-fg, var(--foreground)))',
          }}
        >
          <div className="animate-page-in h-full">
            <Outlet context={{ fullWidth, setFullWidth: handleSetFullWidth }} />
          </div>
        </main>
      </div>
      </div>
      </ActiveSchemaProvider>
      </LanguagePreviewProvider>
    </TemplateProvider>
  )
}
