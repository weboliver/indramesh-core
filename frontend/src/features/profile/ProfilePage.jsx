import { useEffect, useState } from 'react'
import { useAuth } from '../../shared/hooks/use-auth'
import { useUiLanguage } from '../../shared/hooks/use-ui-language'
import api from '../../shared/hooks/use-api'
import { showToast } from '../../shared/components/Toast'
import { Button } from '../../shared/components/Button'
import { Can } from '../../shared/components/Can'
import PasswordPolicyHint from '../../shared/components/PasswordPolicyHint'
import { usePasswordPolicy } from '../../shared/hooks/use-password-policy'
import { passwordViolations } from '../../shared/lib/password-policy'
import { getInitials } from '../../shared/lib/initials'
import { clearToken } from '../../shared/hooks/session-token'
import ApiClientsSection from './ApiClientsSection'
import { useTranslation } from 'react-i18next'

export default function ProfilePage() {
  const { t } = useTranslation()
  const { profile, setProfile, user } = useAuth()
  const { activeLanguages, isLanguageActive } = useUiLanguage()

  // Profile form state
  const [firstName, setFirstName] = useState(profile?.first_name || '')
  const [lastName, setLastName] = useState(profile?.last_name || '')
  const [uiLanguage, setUiLanguage] = useState(profile?.ui_language || 'en')
  const [profileSaving, setProfileSaving] = useState(false)
  const [profileError, setProfileError] = useState(null)

  // Keep the form synchronized with the authoritative server profile,
  // including right after our own PATCH response flows through setProfile
  // (D-11) — never a redundant profile GET.
  useEffect(() => {
    if (!profile) return
    setFirstName(profile.first_name || '')
    setLastName(profile.last_name || '')
    setUiLanguage(profile.ui_language || 'en')
  }, [profile])

  // Password form state
  const [currentPassword, setCurrentPassword] = useState('')
  const [newPassword, setNewPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [passwordSaving, setPasswordSaving] = useState(false)
  const [passwordError, setPasswordError] = useState(null)
  const passwordPolicy = usePasswordPolicy()

  const handleProfileSubmit = async (e) => {
    e.preventDefault()
    setProfileError(null)
    setProfileSaving(true)

    try {
      const data = {
        // Send '' (not null) so an intentionally cleared name actually
        // clears — the backend treats an explicit null as "field not sent,
        // leave as-is" (see update_profile's `is not None` checks), so null
        // silently no-oped on clearing a name. A key that is not sent at all
        // is left untouched server-side, which is what keeps the stored
        // avatar value alive across repeated profile saves.
        first_name: firstName,
        last_name: lastName,
      }
      // Only send ui_language for a changed, active selection (D-14/T-232-05)
      // — an unchanged unavailable preference must never be resent, or a
      // name-only save could overwrite/reject the retained value.
      const uiLanguageChanged = uiLanguage !== (profile?.ui_language || 'en')
      if (uiLanguageChanged && (uiLanguage === 'en' || isLanguageActive(uiLanguage))) {
        data.ui_language = uiLanguage
      }
      const res = await api.patch('/users/me/profile', data)
      // setProfile alone drives runtime activation (D-10/D-11/T-232-06):
      // UiLanguageProvider's own profile-driven effect calls activateUiLanguage
      // once `profile` updates here, under the same requestId guard (D-12) —
      // calling it again from this component would just race a duplicate
      // dictionary fetch against itself.
      setProfile(res.data)
      showToast(t('toasts.profile.updated', 'Profile updated'))
    } catch (err) {
      const detail = err?.response?.data?.detail
      setProfileError(typeof detail === 'string' ? detail : t('profile.update_failed', 'Failed to update profile'))
    } finally {
      setProfileSaving(false)
    }
  }

  const handlePasswordSubmit = async (e) => {
    e.preventDefault()
    setPasswordError(null)

    if (passwordViolations(newPassword, passwordPolicy).length > 0) {
      setPasswordError(t('password_policy.not_met', 'The password does not meet the requirements.'))
      return
    }

    if (newPassword !== confirmPassword) {
      setPasswordError(t('profile.password_mismatch', 'Passwords do not match'))
      return
    }

    setPasswordSaving(true)

    try {
      await api.post('/users/me/change-password', {
        current_password: currentPassword,
        new_password: newPassword,
        confirm_password: confirmPassword,
      })
      // A password change bumps the token epoch, which ends this very session
      // (SESS-02). Clear the store and land on the notice that names the real
      // cause — "your session expired" right after a deliberate action would be
      // the wrong message (UI-SPEC §2.4). The fields are cleared by the unmount.
      clearToken()
      // A hard landing, not a router push: with the token cleared the still
      // mounted layout's ProtectedRoute would render `<Navigate to="/login">`
      // and replace this URL, dropping `?session=` (and with it the notice the
      // user needs). A document load also takes the login page's cold-start
      // path, which skips the pointless refresh.
      window.location.assign('/login?session=password-changed')
    } catch (err) {
      const detail = err?.response?.data?.detail
      setPasswordError(typeof detail === 'string' ? detail : t('profile.password_change_failed', 'Failed to change password'))
    } finally {
      setPasswordSaving(false)
    }
  }

  const previewProfile = {
    first_name: firstName || profile?.first_name,
    last_name: lastName || profile?.last_name,
    // The profile payload carries no username, so the chain falls through to
    // the authenticated user object — otherwise a user with neither name
    // would see `?` instead of their username's initial.
    username: profile?.username || user?.username,
  }

  // Only flag as unavailable once active-language data has actually
  // resolved — otherwise the still-loading state would falsely render the
  // disabled placeholder for a code that simply hasn't loaded yet (D-05).
  const uiLanguageUnavailable =
    !!uiLanguage && uiLanguage !== 'en' && activeLanguages.length > 0 && !isLanguageActive(uiLanguage)

  return (
    <div className="max-w-2xl mx-auto space-y-8">
      <h1 className="text-2xl font-bold">{t('profile.title', 'My Profile')}</h1>

      {/* Profile Fields Section */}
      <div className="rounded-lg border p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold">{t('profile.details_title', 'Profile Details')}</h2>
          <div
            className="rounded-full bg-primary text-primary-foreground flex items-center justify-center text-xs font-bold shrink-0"
            style={{ width: 40, height: 40 }}
            aria-hidden="true"
          >
            {getInitials(previewProfile)}
          </div>
        </div>

        <form onSubmit={handleProfileSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">{t('profile.first_name', 'First Name')}</label>
            <input
              value={firstName}
              onChange={(e) => setFirstName(e.target.value)}
              placeholder={t('profile.first_name_placeholder', 'First name')}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm w-full"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('profile.last_name', 'Last Name')}</label>
            <input
              value={lastName}
              onChange={(e) => setLastName(e.target.value)}
              placeholder={t('profile.last_name_placeholder', 'Last name')}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm w-full"
            />
          </div>

          <div>
            <label htmlFor="ui-language" className="block text-sm font-medium mb-1">{t('profile.ui_language', 'UI language')}</label>
            <select
              id="ui-language"
              name="ui_language"
              value={uiLanguage}
              onChange={(e) => setUiLanguage(e.target.value)}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm w-full"
            >
              {uiLanguageUnavailable && (
                <option value={uiLanguage} disabled>
                  {t('profile.ui_language_unavailable_option', '{{code}} (unavailable)', { code: uiLanguage })}
                </option>
              )}
              {(activeLanguages.some((language) => language.code === 'en')
                ? activeLanguages
                : [{ code: 'en', name: 'English' }, ...activeLanguages]
              ).map((language) => (
                <option key={language.code} value={language.code}>
                  {`${language.name} (${language.code})`}
                </option>
              ))}
            </select>
            <p className="text-xs text-muted-foreground mt-1">
              {uiLanguageUnavailable
                ? t('profile.ui_language_unavailable_help', 'This language is unavailable. The interface is using English until it becomes available.')
                : t('profile.ui_language_help', 'Changes apply after you save your profile.')}
            </p>
          </div>

          {profileError && (
            <p className="text-sm text-destructive">{profileError}</p>
          )}

          <Button type="submit" variant="primary" size="sm" loading={profileSaving}>
            {t('common.save_changes', 'Save Changes')}
          </Button>
        </form>
      </div>

      {/* Change Password Section */}
      <div className="rounded-lg border p-6 space-y-4">
        <h2 className="text-lg font-semibold">{t('profile.change_password_title', 'Change Password')}</h2>

        <form onSubmit={handlePasswordSubmit} className="space-y-4">
          <div>
            <label className="block text-sm font-medium mb-1">{t('profile.current_password', 'Current Password')}</label>
            <input
              type="password"
              value={currentPassword}
              onChange={(e) => setCurrentPassword(e.target.value)}
              placeholder={t('profile.current_password_placeholder', 'Current password')}
              required
              className="rounded-md border border-input bg-background px-3 py-2 text-sm w-full"
            />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('profile.new_password', 'New Password')}</label>
            <input
              type="password"
              value={newPassword}
              onChange={(e) => setNewPassword(e.target.value)}
              required
              minLength={passwordPolicy.min_length}
              maxLength={passwordPolicy.max_length}
              className="rounded-md border border-input bg-background px-3 py-2 text-sm w-full"
            />
            <PasswordPolicyHint policy={passwordPolicy} className="mt-1" />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('profile.confirm_password', 'Confirm New Password')}</label>
            <input
              type="password"
              value={confirmPassword}
              onChange={(e) => setConfirmPassword(e.target.value)}
              placeholder={t('profile.confirm_password_placeholder', 'Confirm new password')}
              required
              className="rounded-md border border-input bg-background px-3 py-2 text-sm w-full"
            />
          </div>

          {passwordError && (
            <p className="text-sm text-destructive">{passwordError}</p>
          )}

          <Button type="submit" variant="primary" size="sm" loading={passwordSaving}>
            {t('profile.change_password', 'Change Password')}
          </Button>
        </form>
      </div>

      {/* API Tokens Section — only visible with api-clients:read permission */}
      <Can resource="api-clients" action="read">
        <ApiClientsSection />
      </Can>
    </div>
  )
}
