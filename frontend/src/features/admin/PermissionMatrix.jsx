import { Fragment, useEffect, useMemo, useRef, useState } from 'react'
import { useQueries } from '@tanstack/react-query'
import { Link, useSearchParams } from 'react-router-dom'
import api from '../../shared/hooks/use-api'
import { usePermissions } from '../../shared/hooks/use-permissions'
import { useRoles, useAssignPermission, useRemovePermission } from '../../shared/hooks/use-roles'
import { Can, useCan } from '../../shared/components/Can'
import PageHeader from '../../shared/components/PageHeader'
import Table from '../../shared/components/Table'
import TableToolbar from '../../shared/components/TableToolbar'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import EmptyState from '../../shared/components/EmptyState'
import TableSkeleton from '../../shared/components/TableSkeleton'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { cn } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { filterPermissionGroups } from './permission-matrix-search'

function cellState(rolePermissions, permissionId) {
  const rp = rolePermissions?.find(p => p.id === permissionId)
  if (!rp) return 'unassigned'
  return rp.is_deny ? 'denied' : 'granted'
}

function nextState(current) {
  if (current === 'unassigned') return 'granted'
  if (current === 'granted') return 'denied'
  return 'unassigned'
}

// Stable id linking a disabled owner's explanation to the related cells via
// aria-describedby (303-UI-SPEC: disabled ownership is described, never the
// reason an assignment control is disabled).
function disabledNoticeId(permissionId) {
  return `permission-owner-disabled-${permissionId}`
}

const CELL_STYLES = {
  unassigned: 'bg-muted text-muted-foreground',
  granted: 'bg-primary text-primary-foreground',
  denied: 'bg-destructive text-destructive-foreground',
}

const LEGEND_ITEMS = [
  { state: 'unassigned' },
  { state: 'granted' },
  { state: 'denied' },
]

function legendLabel(state, t) {
  switch (state) {
    case 'granted': return t('admin.permissions.legend.granted', 'Granted')
    case 'denied': return t('admin.permissions.legend.denied', 'Denied (overrides grants from other roles)')
    default: return t('admin.permissions.legend.unassigned', 'Not assigned')
  }
}

function PermissionMatrixGrid() {
  const { t } = useTranslation()
  const [searchParams, setSearchParams] = useSearchParams()
  const highlightRoleId = searchParams.get('role')
  const canManage = useCan('roles', 'manage')
  const columnRefs = useRef({})
  const [pendingCell, setPendingCell] = useState(null)
  const [cellError, setCellError] = useState(null)
  const [collapsedGroups, setCollapsedGroups] = useState(new Set())
  const collapsedSeeded = useRef(false)

  const { data: catalog, isLoading: catalogLoading, isError: catalogError, refetch: refetchCatalog } = usePermissions()
  const { data: rolesData, isLoading: rolesLoading, isError: rolesError, refetch: refetchRoles } = useRoles(0, 50, { includeTotal: true })
  const roles = rolesData?.items || []

  const roleQueries = useQueries({
    queries: roles.map(role => ({
      queryKey: ['roles', role.id],
      queryFn: () => api.get(`/roles/${role.id}`).then(r => r.data),
      enabled: !!role.id,
    })),
  })

  const assignPermission = useAssignPermission()
  const removePermission = useRemovePermission()

  const groups = useMemo(() => {
    if (!catalog) return []
    const sorted = [...catalog].sort((a, b) =>
      a.resource.localeCompare(b.resource) || a.action.localeCompare(b.action)
    )
    const map = new Map()
    for (const permission of sorted) {
      if (!map.has(permission.resource)) map.set(permission.resource, [])
      map.get(permission.resource).push(permission)
    }
    return Array.from(map.entries()).map(([resource, permissions]) => ({ resource, permissions }))
  }, [catalog])

  // ~21 resource groups / ~86 rows is a lot to scan at once — collapse every
  // group by default on first load so the page opens to an overview, not a
  // wall of rows. Only seeded once; user-driven expand/collapse afterward
  // must not get stomped by a later catalog refetch.
  useEffect(() => {
    if (!collapsedSeeded.current && groups.length > 0) {
      setCollapsedGroups(new Set(groups.map(g => g.resource)))
      collapsedSeeded.current = true
    }
  }, [groups])

  // D-02 standard search adapter (surface 05, existing client search adapted
  // to the standard control): URL-synced q drives the in-memory group filter,
  // preserving the matrix's sticky/border geometry and local filtering.
  const q = searchParams.get('q') || ''
  const setQ = (value, { replace = false } = {}) => {
    setSearchParams(prev => {
      const next = new URLSearchParams(prev)
      if (value) next.set('q', value)
      else next.delete('q')
      return next
    }, { replace })
  }

  const trimmedSearch = q.trim().toLowerCase()
  const filteredGroups = useMemo(() => filterPermissionGroups(groups, q), [groups, q])

  function toggleGroup(resource) {
    setCollapsedGroups(prev => {
      const next = new Set(prev)
      if (next.has(resource)) next.delete(resource)
      else next.add(resource)
      return next
    })
  }

  function expandAll() {
    setCollapsedGroups(new Set())
  }

  function collapseAll() {
    setCollapsedGroups(new Set(groups.map(g => g.resource)))
  }

  useEffect(() => {
    if (highlightRoleId && columnRefs.current[highlightRoleId]) {
      columnRefs.current[highlightRoleId].scrollIntoView({ behavior: 'smooth', inline: 'center', block: 'nearest' })
    }
  }, [highlightRoleId, roles.length])

  async function handleCellClick(roleId, permissionId, currentState) {
    if (!canManage) return
    const next = nextState(currentState)
    const key = `${roleId}:${permissionId}`
    setPendingCell(key)
    setCellError(null)
    try {
      if (next === 'unassigned') {
        await removePermission.mutateAsync({ roleId, permissionId })
      } else {
        await assignPermission.mutateAsync({ roleId, permissionId, isDeny: next === 'denied' })
      }
    } catch (err) {
      // A failed mutation must not fail silently. The query cache is untouched
      // on error, so the cell keeps its last confirmed state; a nearby local
      // alert (UI-SPEC "Matrix cell mutation failure") names the failure.
      setCellError({
        roleId,
        permissionId,
        detail: err?.response?.data?.detail || null,
      })
    } finally {
      setPendingCell(null)
    }
  }

  if (catalogLoading || rolesLoading) {
    return <TableSkeleton rows={8} cols={5} />
  }

  if (catalogError || rolesError) {
    // Existing full-page error treatment with a query retry (UI-SPEC: initial
    // catalogue/roles failure, not a per-column state).
    return (
      <EmptyState
        variant="error"
        description={t('plugins.seams.matrix.load_failed', 'Permissions could not be loaded. Try again.')}
        onButtonClick={() => {
          if (catalogError) refetchCatalog()
          if (rolesError) refetchRoles()
        }}
      />
    )
  }

  if (groups.length === 0 || roles.length === 0) {
    return (
      <EmptyState
        variant="empty"
         title={t('common.empty.no_data_title', 'No data yet')}
         description={t('admin.permissions.no_data_description', 'There are no permissions or roles to display.')}
      />
    )
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-5 rounded-lg border bg-muted/30 px-3 py-2.5 text-xs" data-testid="permission-legend">
         {LEGEND_ITEMS.map(({ state }) => (
          <div key={state} className="flex items-center gap-2">
            <span className={cn('inline-flex h-5 w-5 shrink-0 items-center justify-center rounded', CELL_STYLES[state])}>
              {state === 'granted' && <LucideIcon name="check" size={12} />}
              {state === 'denied' && <LucideIcon name="x" size={12} />}
              {state === 'unassigned' && <span aria-hidden="true">–</span>}
            </span>
             <span className="text-muted-foreground">{legendLabel(state, t)}</span>
          </div>
        ))}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <TableToolbar
          urlQ={q}
          setQ={setQ}
          placeholder={t('admin.permissions.search_placeholder', 'Search by resource, action, description or origin')}
          ariaLabel={t('admin.permissions.search_label', 'Filter permissions')}
          searchTestId="permission-search"
          className="flex-1"
        />
        <button
          type="button"
          onClick={expandAll}
          data-testid="expand-all-groups"
          className="rounded-md border px-3 py-2 text-xs font-medium hover:bg-muted"
        >
           {t('admin.permissions.expand_all', 'Expand all')}
        </button>
        <button
          type="button"
          onClick={collapseAll}
          data-testid="collapse-all-groups"
          className="rounded-md border px-3 py-2 text-xs font-medium hover:bg-muted"
        >
           {t('admin.permissions.collapse_all', 'Collapse all')}
        </button>
      </div>

      {cellError && (
        <div
          role="alert"
          data-testid="permission-cell-error"
          className="flex flex-wrap items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/5 px-3 py-2 text-xs text-destructive"
        >
          <LucideIcon name="alert-triangle" size={14} className="shrink-0" />
          <span className="min-w-0 break-words">
            {t('plugins.seams.matrix.cell_failed', 'The permission was not changed. Try this cell again.')}
          </span>
          {cellError.detail && (
            <span className="min-w-0 break-words text-destructive/80">{cellError.detail}</span>
          )}
          <button
            type="button"
            onClick={() => setCellError(null)}
            className="ml-auto rounded border border-destructive/40 px-2 py-1 hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
          >
            {t('common.dismiss', 'Dismiss')}
          </button>
        </div>
      )}

      {filteredGroups.length === 0 ? (
        <EmptyState
          variant="empty"
           title={t('admin.permissions.no_matching_title', 'No matching permissions')}
           description={t('admin.permissions.no_matching_description', 'No permissions match this resource, action, description or origin search. Clear or change the search.')}
        />
      ) : (
        <Table
          tableClassName="min-w-full border-separate border-spacing-0 text-sm"
          data-testid="permission-matrix"
        >
          {/* border-separate (not border-collapse) — Chromium renders sticky
              positioned <td>/<th> incorrectly (ghost/duplicated content on
              horizontal scroll) when combined with border-collapse. */}
          <thead>
            <tr>
              {/* Fixed width (not auto) so the sticky column has a known,
                  stable footprint — role headers below use that same
                  number as scroll-margin-left so scrollIntoView never
                  scrolls a column half-behind this sticky edge. */}
              <th scope="col" className="sticky left-0 top-0 z-20 w-64 min-w-64 max-w-64 border-b border-r bg-background px-3 py-2.5 text-left font-semibold">
                 {t('admin.permissions.permission', 'Permission')}
              </th>
              {roles.map(role => (
                <th
                  key={role.id}
                  scope="col"
                  ref={el => { columnRefs.current[role.id] = el }}
                  className={cn(
                    'sticky top-0 z-10 w-24 min-w-24 max-w-24 scroll-ml-64 overflow-hidden border-b bg-background px-1.5 py-2.5 text-center font-semibold align-top',
                    highlightRoleId === role.id && 'border-b-2 border-b-primary bg-primary/15'
                  )}
                >
                    <div className="flex min-w-0 flex-col items-center gap-1">
                      <span className="line-clamp-2 break-words leading-tight" title={role.name}>{role.name}</span>
                      {role.is_system && (
                        <LucideIcon
                          name="shield"
                          size={12}
                          className="shrink-0 text-muted-foreground"
                           aria-label={t('admin.roles.system_role_label', 'System role — cannot be deleted')}
                        />
                      )}
                      {highlightRoleId === role.id && (
                         <span className="text-[10px] font-semibold uppercase text-primary">{t('common.selected', 'Selected')}</span>
                      )}
                    </div>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {filteredGroups.map(group => {
                const isCollapsed = !trimmedSearch && collapsedGroups.has(group.resource)
                return (
                  <Fragment key={group.resource}>
                    <tr>
                      <td colSpan={roles.length + 1} className="sticky left-0 z-10 border-b bg-muted/60 p-0">
                        <button
                          type="button"
                          onClick={() => toggleGroup(group.resource)}
                          data-testid={`group-toggle-${group.resource}`}
                          className="flex w-full items-center gap-2 px-3 py-2 text-left text-xs font-semibold uppercase tracking-wide text-muted-foreground hover:bg-muted"
                        >
                          <LucideIcon name={isCollapsed ? 'chevron-right' : 'chevron-down'} size={12} className="shrink-0" />
                          {group.resource}
                          <span className="normal-case font-normal text-muted-foreground/70">({group.permissions.length})</span>
                        </button>
                      </td>
                    </tr>
                    {!isCollapsed && group.permissions.map(permission => {
                      const ownerDisabled = permission.owner_state === 'disabled'
                      const ownerUnavailable = permission.owner_state === 'unavailable'
                      const ownerName = permission.owner_label || permission.owner_plugin
                      return (
                        <tr key={permission.id} className="h-14 border-b last:border-b-0">
                          <td className="sticky left-0 z-10 w-64 min-w-64 max-w-64 border-r bg-background px-3 py-2 align-middle">
                            <div className="break-words font-medium">{permission.action}</div>
                            {permission.description && (
                              <div className="break-words text-xs leading-snug text-muted-foreground">{permission.description}</div>
                            )}
                            <div className="mt-1 min-w-0 break-words text-[11px] leading-snug text-muted-foreground">
                              {permission.owner_plugin ? (
                                <span>{interpolateDefault(t('plugins.sections.provided_by', 'Provided by plugin "{{plugin}}"'), { plugin: ownerName })}</span>
                              ) : (
                                <span>{t('plugins.seams.ownership.core', 'Core')}</span>
                              )}
                              {ownerUnavailable && (
                                <span className="ml-1 font-medium">
                                  <span aria-hidden="true" className="mr-1">·</span>
                                  {t('plugins.seams.ownership.unavailable', 'Plugin unavailable')}
                                </span>
                              )}
                              {ownerDisabled && (
                                <span className="ml-1 font-medium">
                                  <span aria-hidden="true" className="mr-1">·</span>
                                  {t('plugins.seams.ownership.disabled', 'Plugin disabled')}
                                </span>
                              )}
                            </div>
                            {ownerDisabled && (
                              <p id={disabledNoticeId(permission.id)} className="mt-0.5 text-[11px] leading-snug text-muted-foreground">
                                {t('plugins.seams.ownership.disabled_explanation', 'Assignments are kept. These features are unavailable until the plugin is active.')}
                              </p>
                            )}
                          </td>
                          {roles.map((role, idx) => {
                            const roleQuery = roleQueries[idx]
                            const rolePermissions = roleQuery?.data?.permissions
                            const state = cellState(rolePermissions, permission.id)
                            const pendingKey = `${role.id}:${permission.id}`
                            const isPending = pendingCell === pendingKey
                            const disabled = isPending || !canManage
                            const loadFailedLabel = t('plugins.seams.matrix.load_failed', 'Permissions could not be loaded. Try again.')
                            const roleCellLabel = interpolateDefault(t('admin.permissions.cell_label', '{{permission}} for {{role}}: {{state}}'), { permission: permission.name, role: role.name, state })
                            const describedBy = ownerDisabled ? disabledNoticeId(permission.id) : undefined
                            return (
                              <td
                                key={role.id}
                                aria-busy={roleQuery?.isLoading ? 'true' : undefined}
                                className={cn(
                                  'w-24 min-w-24 max-w-24 text-center align-middle',
                                  highlightRoleId === role.id && 'bg-primary/5'
                                )}
                              >
                                {roleQuery?.isLoading ? (
                                  <button
                                    type="button"
                                    disabled
                                    aria-busy="true"
                                    aria-label={interpolateDefault(t('admin.permissions.cell_label', '{{permission}} for {{role}}: {{state}}'), { permission: permission.name, role: role.name, state: t('common.loading', 'Loading...') })}
                                    className="inline-flex h-9 w-9 cursor-progress items-center justify-center rounded bg-muted/60"
                                  >
                                    <span aria-hidden="true" className="h-4 w-4 animate-pulse rounded bg-muted-foreground/30" />
                                  </button>
                                ) : roleQuery?.isError ? (
                                  <button
                                    type="button"
                                    onClick={() => roleQuery.refetch()}
                                    aria-label={loadFailedLabel}
                                    title={loadFailedLabel}
                                    className="inline-flex h-9 w-9 items-center justify-center rounded border border-destructive/40 text-destructive hover:bg-destructive/10 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
                                  >
                                    <LucideIcon name="refresh-cw" size={14} />
                                  </button>
                                ) : (
                                  <button
                                    type="button"
                                    disabled={disabled}
                                    onClick={() => handleCellClick(role.id, permission.id, state)}
                                    aria-label={roleCellLabel}
                                    aria-describedby={describedBy}
                                    className={cn(
                                      'inline-flex h-9 w-9 items-center justify-center rounded transition-colors',
                                      CELL_STYLES[state],
                                      !disabled && 'hover:opacity-80',
                                      disabled && 'cursor-not-allowed opacity-60'
                                    )}
                                  >
                                    {state === 'granted' && <LucideIcon name="check" size={14} />}
                                    {state === 'denied' && <LucideIcon name="x" size={14} />}
                                    {state === 'unassigned' && <span aria-hidden="true">–</span>}
                                  </button>
                                )}
                              </td>
                            )
                          })}
                        </tr>
                      )
                    })}
                  </Fragment>
                )
              })}
            </tbody>
          </Table>
      )}
    </div>
  )
}

export default function PermissionMatrix() {
  const { t } = useTranslation()
  const canView = useCan('roles', 'read')

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Link to="/roles" className="hover:text-foreground">{t('admin.roles.title', 'Roles')}</Link>
        <span>/</span>
         <span className="text-foreground">{t('common.permissions', 'Permissions')}</span>
      </div>
       <PageHeader title={t('common.permissions', 'Permissions')} icon="key" />
      <ErrorBoundary>
        <Can resource="roles" action="read">
          <PermissionMatrixGrid />
        </Can>
        {!canView && (
          <EmptyState
            variant="error"
             title={t('common.access_denied', 'Access denied')}
             description={t('admin.permissions.access_denied_description', 'You do not have permission to view the permission matrix.')}
          />
        )}
      </ErrorBoundary>
    </div>
  )
}
