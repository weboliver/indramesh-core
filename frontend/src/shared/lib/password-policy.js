// Mirrors backend/app/services/password_policy_service.py so a form can show
// what is missing before it submits; the backend stays the authority.
export const PASSWORD_MAX_BYTES = 72
export const PASSWORD_MIN_LENGTH_FLOOR = 8

export const DEFAULT_PASSWORD_POLICY = {
  min_length: PASSWORD_MIN_LENGTH_FLOOR,
  max_length: PASSWORD_MAX_BYTES,
  require_digit: false,
  require_special: false,
  require_uppercase: false,
}

export function passwordViolations(password, policy = DEFAULT_PASSWORD_POLICY) {
  const value = password || ''
  const chars = [...value]
  const violations = []
  if (chars.length < policy.min_length) violations.push('min_length')
  if (chars.length > policy.max_length) violations.push('max_length')
  if (new TextEncoder().encode(value).length > PASSWORD_MAX_BYTES) violations.push('max_bytes')
  if (policy.require_digit && !/\p{Nd}/u.test(value)) violations.push('digit')
  if (policy.require_uppercase && !/\p{Lu}/u.test(value)) violations.push('uppercase')
  if (policy.require_special && chars.every((ch) => /[\p{L}\p{N}]/u.test(ch))) violations.push('special')
  return violations
}
