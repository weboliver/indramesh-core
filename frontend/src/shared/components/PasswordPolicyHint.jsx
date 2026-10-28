import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'

// One line naming the active password rules, shown under every password field
// that the policy checks (create user, reset, own password change).
export default function PasswordPolicyHint({ policy, className = '' }) {
  const { t } = useTranslation()
  const rules = [
    interpolateDefault(t('password_policy.rule.length', '{{min}}–{{max}} characters'), {
      min: policy.min_length,
      max: policy.max_length,
    }),
  ]
  if (policy.require_digit) rules.push(t('password_policy.rule.digit', 'a digit'))
  if (policy.require_uppercase) rules.push(t('password_policy.rule.uppercase', 'an uppercase letter'))
  if (policy.require_special) rules.push(t('password_policy.rule.special', 'a special character'))
  return (
    <p className={`text-xs text-muted-foreground ${className}`} data-testid="password-policy-hint">
      {interpolateDefault(t('password_policy.hint', 'Password requirements: {{rules}}'), {
        rules: rules.join(', '),
      })}
    </p>
  )
}
