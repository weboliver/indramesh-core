import { useAuth } from '../hooks/use-auth'

/**
 * useCan(resource, action) — imperative permission check.
 * Returns true only when the permission `${resource}:${action}` is
 * present in the user's loaded permission set. Returns false
 * while loading (D-05 fail-closed) and when the permission is absent.
 *
 * Single resource+action pair only per D-02 — no anyOf/multi support.
 */
export function useCan(resource, action) {
  const { can } = useAuth()
  // can() already returns false while loading (D-05) — no need to re-check
  // loading here too; trust AuthProvider as the single source of truth.
  return can(resource, action)
}

/**
 * <Can resource="sources" action="edit"> — declarative permission gate.
 * Renders {children} only when the user has the required permission.
 * Renders null when:
 *   - loading is true (D-05 fail-closed, same as missing permission)
 *   - user lacks the required permission
 *   - user is not authenticated (useAuth().can() handles this)
 *
 * No fallback prop (D-01) — add only when a concrete use case requires
 * a non-null fallback (e.g. disabled button variant of a permitted action).
 * Single resource+action pair only (D-02).
 */
export function Can({ resource, action, children }) {
  const allowed = useCan(resource, action)
  if (!allowed) return null
  return children
}
