import { createContext, useContext, useState, useEffect, useCallback } from 'react'
import { useTranslation } from 'react-i18next'
import api from './use-api'

const AuthContext = createContext(null)

export function AuthProvider({ children }) {
  const { t } = useTranslation()
  const [user, setUser] = useState(null)
  const [token, setToken] = useState(() => localStorage.getItem('indramesh_token'))
  const [loading, setLoading] = useState(true)
  const [permissions, setPermissions] = useState([])
  const [profile, setProfile] = useState(null)

  useEffect(() => {
    const fetchUser = async () => {
      if (!token) {
        setUser(null)
        setPermissions([])
        setProfile(null)
        setLoading(false)
        return
      }
      try {
        const userRes = await api.get('auth/me')
        setUser(userRes.data)

        const [permsRes, profileRes] = await Promise.all([
          api.get('auth/me/permissions').catch(() => null),
          api.get('users/me/profile').catch(() => null),
        ])
        setPermissions(permsRes ? permsRes.data : [])
        setProfile(profileRes ? profileRes.data : null)
      } catch {
        setUser(null)
        localStorage.removeItem('indramesh_token')
        setToken(null)
      } finally {
        setLoading(false)
      }
    }
    fetchUser()
  }, [token])

  const login = async (username, password) => {
    setLoading(true)
    try {
      const formData = new URLSearchParams()
      formData.append('username', username)
      formData.append('password', password)
      const res = await api.post('auth/login', formData.toString(), {
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
      })
      const { access_token } = res.data
      localStorage.setItem('indramesh_token', access_token)
      setToken(access_token)
    } catch (err) {
      setLoading(false)
      if (!err?.response?.data?.detail && err instanceof Error) {
        // Keep backend detail untouched; this is only the client-side fallback.
        err.response = {
          ...err.response,
          data: {
            ...err.response?.data,
            detail: t('auth.login_failed', 'Login failed'),
          },
        }
      }
      throw err
    }
  }

  const logout = async () => {
    // Best-effort server-side revocation first: a copied token must not outlive
    // the logout. A failure here (offline, expired token, 5xx) must never keep
    // the user logged in locally, so it is swallowed — local logout always wins.
    try {
      await api.post('auth/revoke')
    } catch {
      // ignored on purpose
    }
    localStorage.removeItem('indramesh_token')
    setToken(null)
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
