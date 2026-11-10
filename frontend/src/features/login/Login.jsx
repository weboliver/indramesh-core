import { useEffect, useRef, useState } from 'react'
import { useNavigate, useSearchParams } from 'react-router-dom'
import { useAuth } from '../../shared/hooks/use-auth'
import { Button } from '../../shared/components/Button'
import { useTranslation } from 'react-i18next'
import { SLOW_LOGIN_HINT_MS, NOTICE_DEFAULTS, noticeKeyForSessionParam } from './login-states'

export default function LoginPage() {
  const { t } = useTranslation()
  const [username, setUsername] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(false)
  const [slowHint, setSlowHint] = useState(false)
  const slowHintTimer = useRef(null)
  const { login } = useAuth()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  const noticeKey = noticeKeyForSessionParam(searchParams.get('session'))

  // The hint belongs to one pending submit; unmounting the form (successful
  // login, route change) must never leave the timer behind.
  useEffect(() => () => {
    if (slowHintTimer.current !== null) {
      clearTimeout(slowHintTimer.current)
    }
  }, [])

  // One copy source for the three failure states: the raw backend `detail`
  // never reaches the screen, and an unknown account and a wrong password
  // render the identical string (UI-SPEC §2.3).
  const errorMessageFor = (err) => {
    const status = err?.response?.status
    if (status === 401) {
      return t('auth.invalid_credentials', 'Username or password is incorrect.')
    }
    if (status === 429) {
      return t('auth.too_many_attempts', 'Too many sign-in attempts. Wait a moment and try again.')
    }
    return t('auth.login_failed', 'Login failed')
  }

  const handleSubmit = async (e) => {
    e.preventDefault()

    const stopSlowHintTimer = () => {
      if (slowHintTimer.current !== null) {
        clearTimeout(slowHintTimer.current)
        slowHintTimer.current = null
      }
    }

    setError('')
    setSlowHint(false)
    setLoading(true)
    stopSlowHintTimer()
    slowHintTimer.current = setTimeout(() => setSlowHint(true), SLOW_LOGIN_HINT_MS)

    try {
      await login(username, password)
      navigate('/search')
    } catch (err) {
      setError(errorMessageFor(err))
    } finally {
      stopSlowHintTimer()
      setSlowHint(false)
      setLoading(false)
    }
  }

  return (
    <div className="min-h-screen flex items-center justify-center bg-background">
      <div className="w-full max-w-md space-y-8 p-8">
        <div className="text-center">
          <h1 className="text-3xl font-bold tracking-tight text-primary">{t('auth.brand', 'IndraMesh Core')}</h1>
          <p className="mt-2 text-sm text-muted-foreground">
             {t('auth.subtitle', 'Asset Database Management')}
          </p>
        </div>

        <form onSubmit={handleSubmit} className="mt-8 space-y-6">
          {noticeKey && (
            <div
              role="status"
              data-testid="login-notice"
              className="rounded-md border border-border bg-muted p-4 text-sm text-foreground"
            >
              {t(noticeKey, NOTICE_DEFAULTS[noticeKey])}
            </div>
          )}

          <div className="space-y-4">
            <div>
              <label htmlFor="username" className="block text-sm font-medium text-foreground">
                 {t('common.username', 'Username')}
              </label>
              <input
                id="username"
                type="text"
                value={username}
                onChange={(e) => setUsername(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-foreground shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary sm:text-sm"
                 placeholder={t('auth.username_placeholder', 'admin')}
                required
              />
            </div>
            <div>
              <label htmlFor="password" className="block text-sm font-medium text-foreground">
                 {t('auth.password', 'Password')}
              </label>
              <input
                id="password"
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
                className="mt-1 block w-full rounded-md border border-input bg-background px-3 py-2 text-foreground shadow-sm placeholder:text-muted-foreground focus:border-primary focus:outline-none focus:ring-1 focus:ring-primary sm:text-sm"
                 placeholder={t('auth.password_placeholder', '••••••••')}
                required
              />
            </div>
          </div>

          {error && (
            <div
              className="rounded-md bg-destructive/10 p-3 text-sm text-destructive"
              data-testid="login-error"
              role="alert"
            >
              {error}
            </div>
          )}

          <Button
            type="submit"
            disabled={loading}
            variant="primary"
            size="md"
            className="w-full"
          >
             {loading ? t('auth.signing_in', 'Signing in...') : t('auth.sign_in', 'Sign in')}
          </Button>

          {slowHint && (
            <p
              className="text-xs text-muted-foreground text-center"
              role="status"
              data-testid="login-slow-hint"
            >
              {t('auth.slow_login_hint', 'Please wait…')}
            </p>
          )}
        </form>
      </div>
    </div>
  )
}
