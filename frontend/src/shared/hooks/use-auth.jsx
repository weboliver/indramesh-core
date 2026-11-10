import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import api from './use-api'
import { clearToken, getToken, refreshOnce, setToken, subscribe } from './session-token'

const AuthContext = createContext(null)

// 1.0.0 kept a 30-day access token in browser storage under this key. D-10
// removes it once on boot: after the upgrade the stale token must not stay
// script-readable in the browser. This is the only storage access left in the
// app, and it is a removal — never a read.
const LEGACY_TOKEN_KEY = 'indramesh_token'

function purgeLegacyToken() {
  try {
    window.localStorage.removeItem(LEGACY_TOKEN_KEY)
  } catch {
    // Storage disabled or unavailable — there is nothing to purge.
  }
}

export function AuthProvider({ children }) {
  const [user, setUser] = useState(null)
  const [loading, setLoading] = useState(true)
  const [permissions, setPermissions] = useState([])
  const [profile, setProfile] = useState(null)
  // Derived from the store, never a second source of truth (RESEARCH Pattern 1).
  const [token, setTokenState] = useState(() => getToken())

  useEffect(() => subscribe(setTokenState), [])

  // Boot: purge the 1.0.0 leftover, then restore the session from the refresh
  // cookie with one silent refresh. `loading` stays true across it, so
  // ProtectedRoute keeps rendering the existing loading screen and the login
  // page never flashes (UI-SPEC §2.1). On `/login` the refresh is skipped — a
  // pointless 401 the page does not need.
  useEffect(() => {
    let cancelled = false

    const restoreSession = async () => {
      purgeLegacyToken()

      const onLoginRoute =
        typeof window !== 'undefined' && window.location?.pathname === '/login'
      if (!onLoginRoute) {
        try {
          await refreshOnce(() =>
            api
              .post('auth/refresh', null, { _skipAuthRetry: true })
              .then((res) => res.data.access_token)
          )
        } catch {
          // No cookie, or the refresh was refused. With no JS-readable session
          // state the app cannot tell "never signed in" from "cookie gone", so
          // it just stays signed out — no notice, no error (UI-SPEC §2.2).
        }
      }

      if (cancelled) return
      // A restored token is picked up by the token effect below, which loads
      // the user and then releases `loading`.
      if (!getToken()) setLoading(false)
    }

    restoreSession()
    return () => {
      cancelled = true
    }
  }, [])

  // Whoever holds a token (boot refresh or a fresh login) loads the identity
  // plus its permissions and profile.
  useEffect(() => {
    if (!token) {
      setUser(null)
      setPermissions([])
      setProfile(null)
      return undefined
    }

    let cancelled = false
    const loadIdentity = async () => {
      try {
        const userRes = await api.get('auth/me')
        if (cancelled) return
        setUser(userRes.data)

        const [permsRes, profileRes] = await Promise.all([
          api.get('auth/me/permissions').catch(() => null),
          api.get('users/me/profile').catch(() => null),
        ])
        if (cancelled) return
        setPermissions(permsRes ? permsRes.data : [])
        setProfile(profileRes ? profileRes.data : null)
      } catch {
        // The interceptor already dealt with a refused refresh; here the token
        // is unusable, so stay signed out rather than render a half session.
        if (cancelled) return
        setUser(null)
        setPermissions([])
        setProfile(null)
      } finally {
        if (!cancelled) setLoading(false)
      }
    }

    loadIdentity()
    return () => {
      cancelled = true
    }
  }, [token])

  const login = async (username, password) => {
    setLoading(true)
    try {
      const formData = new URLSearchParams()
      formData.append('username', username)
      formData.append('password', password)
      // `_skipAuthRetry`: a wrong-password 401 is the form's business, not a
      // session end — it must never start a refresh and a navigation.
      const res = await api.post('auth/login', formData.toString(), {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        _skipAuthRetry: true,
      })
      setToken(res.data.access_token)
    } catch (err) {
      // No `detail` fabrication: the login screen maps the status to its own
      // copy, so the raw backend text never reaches the UI (UI-SPEC §2.3).
      setLoading(false)
      throw err
    }
  }

  const logout = async () => {
    // Best-effort server-side revocation first: the cookie must not outlive the
    // logout. A failure here (offline, expired token, 5xx) must never keep the
    // user logged in locally, so it is swallowed — local logout always wins.
    // `_skipAuthRetry` keeps a failed sign-out from triggering a refresh whose
    // failure would then navigate to the session-expired notice.
    try {
      await api.post('auth/logout', null, { _skipAuthRetry: true })
    } catch {
      // ignored on purpose
    }
    clearToken()
    setUser(null)
    setPermissions([])
    setProfile(null)
  }

  const can = useCallback((resource, action) => {
    if (loading) return false
    return permissions.includes(`${resource}:${action}`)
  }, [permissions, loading])

  return (
    <AuthContext.Provider
      value={{
        user,
        token,
        login,
        logout,
        loading,
        isAuthenticated: !!token,
        isAdmin: user?.is_superuser === true,
        can,
        permissions,
        profile,
        setProfile,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)
