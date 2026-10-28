import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router-dom'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { I18nextProvider } from 'react-i18next'
import { i18n } from './shared/lib/i18n'
import { AuthProvider } from './shared/hooks/use-auth'
import { UiLanguageProvider } from './shared/hooks/use-ui-language'
import { useWebVitals } from './shared/hooks/use-web-vitals'
import { PluginProvider } from './shared/plugin-runtime/PluginProvider'
import App from './App'

import './index.css'

function AppWithVitals({ children }) {
  useWebVitals()
  return <>{children}</>
}

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <I18nextProvider i18n={i18n}>
      <BrowserRouter>
        <QueryClientProvider client={queryClient}>
          <AuthProvider>
            <UiLanguageProvider>
              <PluginProvider>
                <AppWithVitals>
                  <App />
                </AppWithVitals>
              </PluginProvider>
            </UiLanguageProvider>
          </AuthProvider>
        </QueryClientProvider>
      </BrowserRouter>
    </I18nextProvider>
  </React.StrictMode>
)
