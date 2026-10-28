import { useState } from 'react'
import api from '../../shared/hooks/use-api'
import { useAuth } from '../../shared/hooks/use-auth'
import { Button } from '../../shared/components/Button'
import { useCan } from '../../shared/components/Can'
import { useGroups } from '../../shared/hooks/use-groups'
import { useRoles } from '../../shared/hooks/use-roles'
import { useTranslation } from 'react-i18next'
import GroupAssignmentList from './GroupAssignmentList'
import RoleAssignmentList from './RoleAssignmentList'
import PasswordPolicyHint from '../../shared/components/PasswordPolicyHint'
import { usePasswordPolicy } from '../../shared/hooks/use-password-policy'
import { passwordViolations } from '../../shared/lib/password-policy'

// Create mode has no user id yet, so roles and memberships are collected here
// and assigned by the caller right after the user exists. Same row markup as
// RoleAssignmentList/GroupAssignmentList, so create and edit look alike.
function NewUserPicker({ items, selected, onChange, testId }) {
  if (items.length === 0) return null
  return (
    <div className="space-y-2" data-testid={testId}>
      {items.map((item) => (
        <label
          key={item.id}
          className="flex items-center gap-2 p-2 border rounded-md text-sm font-normal cursor-pointer hover:bg-muted/50 transition-colors"
        >
          <input
            type="checkbox"
            checked={selected.includes(item.id)}
            onChange={(e) => onChange(e.target.checked
              ? [...selected, item.id]
              : selected.filter((id) => id !== item.id))}
            data-testid={`${testId}-${item.id}`}
          />
          <span className="font-semibold">{item.name}</span>
        </label>
      ))}
    </div>
  )
}

// Superuser-only: sets another account's password; the backend ends that
// user's sessions. Its own button, so it never rides on the form's Save.
function PasswordResetSection({ userId }) {
  const { t } = useTranslation()
  const policy = usePasswordPolicy()
  const [newPassword, setNewPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [result, setResult] = useState(null)

  const handleReset = async () => {
    setBusy(true)
    setResult(null)
    try {
      await api.post(`/users/${userId}/reset-password`, { new_password: newPassword })
      setNewPassword('')
      setResult('done')
    } catch {
      setResult('failed')
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="flex flex-wrap items-center gap-2">
      <PasswordPolicyHint policy={policy} className="basis-full" />
      <input
        value={newPassword}
        onChange={(e) => { setNewPassword(e.target.value); setResult(null) }}
        placeholder={t('profile.new_password', 'New Password')}
        type="password"
        autoComplete="new-password"
        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground flex-1 min-w-[200px]"
        data-testid="user-form-reset-password-input"
      />
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={handleReset}
        disabled={busy || passwordViolations(newPassword, policy).length > 0}
        loading={busy}
        data-testid="user-form-reset-password"
      >
        {t('admin.users.reset_password', 'Reset password')}
      </Button>
      {result === 'done' && (
        <span className="text-sm text-green-600" role="status">
          {t('admin.users.reset_password_done', "Password reset. The user's sessions have ended.")}
        </span>
      )}
      {result === 'failed' && (
        <span className="text-sm text-destructive" role="alert">
          {t('admin.users.reset_password_failed', 'The password could not be reset.')}
        </span>
      )}
    </div>
  )
}

function AssignmentSection({ title, children }) {
  return (
    <div className="basis-full space-y-2 pt-2">
      <h3 className="text-sm font-semibold">{title}</h3>
      {children}
    </div>
  )
}

export default function UserForm({
  mode = 'create',
  initialValues,
  onSubmit,
  onCancel,
  disableIsActive = false,
  disableIsSuperuser = false,
  showGroups = false,
  isSelf = false,
  currentIsSuperuser = false,
}) {
  const { t } = useTranslation()
  const { user: currentUser } = useAuth()
  const policy = usePasswordPolicy()
  const canResetPassword = mode === 'edit' && !!initialValues?.id
    && !!currentUser?.is_superuser && currentUser.id !== initialValues.id
  const canManageGroups = useCan('groups', 'manage')
  const canManageRoles = useCan('users', 'manage')
  const [groupIds, setGroupIds] = useState([])
  const [roleIds, setRoleIds] = useState([])
  const pickersEnabled = showGroups && mode === 'create'
  const { data: groupData } = useGroups('', null, 0, 100, { enabled: pickersEnabled && canManageGroups })
  const { data: roleData } = useRoles(0, 100, { includeTotal: true, enabled: pickersEnabled && canManageRoles })
  // WR-04: the backend's effective role count (direct + group-inherited) is
  // what the self-lockout guard authorizes against, as on the detail page.
  const assignedRoles = initialValues?.roles || []
  const lastRoleGuard = isSelf && !currentIsSuperuser
    && (initialValues?.effective_role_count ?? assignedRoles.length) <= 1
  const [username, setUsername] = useState(initialValues?.username || '')
  const [email, setEmail] = useState(initialValues?.email || '')
  const [password, setPassword] = useState('')
  const [isActive, setIsActive] = useState(initialValues?.is_active ?? true)
  const [isSuperuser, setIsSuperuser] = useState(initialValues?.is_superuser ?? false)
  const [saving, setSaving] = useState(false)
  const passwordInvalid = mode === 'create' && passwordViolations(password, policy).length > 0

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      const values = mode === 'create'
        ? { username, email, password, is_active: isActive, is_superuser: isSuperuser }
        : { username, email, is_active: isActive, is_superuser: isSuperuser }
      await onSubmit(values, { groupIds, roleIds })
    } finally {
      setSaving(false)
    }
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-wrap gap-2 p-3 border rounded-md bg-muted/30" data-testid="user-form">
      <input
        value={username}
        onChange={(e) => setUsername(e.target.value)}
         placeholder={t('common.username', 'Username')}
        required
        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground flex-1 min-w-[150px]"
        data-testid="user-form-username"
      />
      <input
        value={email}
        onChange={(e) => setEmail(e.target.value)}
         placeholder={t('common.email', 'Email')}
        type="email"
        required
        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground flex-1 min-w-[200px]"
        data-testid="user-form-email"
      />
      {mode === 'create' && (
        <input
          value={password}
          onChange={(e) => setPassword(e.target.value)}
           placeholder={t('auth.password', 'Password')}
          type="password"
          required
          className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground flex-1 min-w-[150px]"
          data-testid="user-form-password"
        />
      )}
      <label className="flex items-center gap-1.5 text-sm font-normal px-1">
        <input
          type="checkbox"
          checked={isActive}
          disabled={disableIsActive}
          onChange={(e) => setIsActive(e.target.checked)}
          data-testid="user-form-is-active"
        />
         {t('common.status.active', 'Active')}
      </label>
      <label className="flex items-center gap-1.5 text-sm font-normal px-1">
        <input
          type="checkbox"
          checked={isSuperuser}
          disabled={disableIsSuperuser}
          onChange={(e) => setIsSuperuser(e.target.checked)}
          data-testid="user-form-is-superuser"
        />
         {t('admin.users.superuser', 'Superuser')}
      </label>
      <Button type="submit" variant="primary" size="sm" disabled={saving || passwordInvalid} loading={saving}>
         {mode === 'create' ? t('common.create', 'Create') : t('common.save', 'Save')}
      </Button>
      <Button type="button" onClick={onCancel} variant="outline" size="sm" disabled={saving}>
         {t('common.cancel', 'Cancel')}
      </Button>
      {mode === 'create' && <PasswordPolicyHint policy={policy} className="basis-full" />}
      {canResetPassword && (
        <AssignmentSection title={t('admin.users.reset_password', 'Reset password')}>
          <PasswordResetSection userId={initialValues.id} />
        </AssignmentSection>
      )}
      {showGroups && mode === 'edit' && initialValues?.id && (
        <>
          <AssignmentSection title={t('admin.users.roles', 'Roles')}>
            <RoleAssignmentList
              userId={initialValues.id}
              assignedRoleNames={assignedRoles}
              canManage={canManageRoles}
              disableRemoveGuard={lastRoleGuard}
            />
          </AssignmentSection>
          <AssignmentSection title={t('admin.users.groups', 'Groups')}>
            <GroupAssignmentList
              userId={initialValues.id}
              canManage={canManageGroups}
              isSelf={isSelf}
              isSuperuser={currentIsSuperuser}
            />
          </AssignmentSection>
        </>
      )}
      {pickersEnabled && canManageRoles && (
        <AssignmentSection title={t('admin.users.roles', 'Roles')}>
          <NewUserPicker items={roleData?.items || []} selected={roleIds} onChange={setRoleIds} testId="user-form-roles" />
        </AssignmentSection>
      )}
      {pickersEnabled && canManageGroups && (
        <AssignmentSection title={t('admin.users.groups', 'Groups')}>
          <NewUserPicker items={groupData?.items || []} selected={groupIds} onChange={setGroupIds} testId="user-form-groups" />
        </AssignmentSection>
      )}
    </form>
  )
}
