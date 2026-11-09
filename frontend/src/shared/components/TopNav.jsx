import { useState, useRef, useEffect, useMemo } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useAuth } from '../hooks/use-auth'
import { Button } from './Button'
import { LucideIcon } from '../lib/lucide-icons'
import { Can, useCan } from './Can'
import { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator } from './DropdownMenu'
import { useTranslation } from 'react-i18next'
import { usePluginNavItems, usePluginText } from '../plugin-runtime/PluginProvider'
import { isPermitted } from '../plugin-runtime/contribution-utils'
import { PluginNavBadge } from '../plugin-runtime/PluginNavBadge'
import { mergePluginNavGroups } from './nav-groups'
import { cn } from '../lib/utils'
import { fetchAppVersion, formatVersionLabel } from '../lib/app-version'
import { getInitials } from '../lib/initials'

export default function TopNav({ onToggleDark }) {
  const { t } = useTranslation()
  const { logout, isAuthenticated, isAdmin, user, profile, can } = useAuth()
  const { pathname } = useLocation()
  const [openGroupId, setOpenGroupId] = useState(null)
  const [userMenuOpen, setUserMenuOpen] = useState(false)
  const [mobileNavOpen, setMobileNavOpen] = useState(false)
  // D-06/REL-01: `undefined` means "not read yet"; the label below renders
  // nothing for both `undefined` and a failed read, so the menu looks exactly
  // as before until a version is actually known.
  const [appVersion, setAppVersion] = useState(undefined)
  const userMenuRef = useRef(null)
  const mobileNavRef = useRef(null)

  const pluginNavItems = usePluginNavItems()
  const pluginText = usePluginText()
  // D-07/FEEXT-02: a nav item declaring a `permission` is hidden (not disabled)
  // for a user without it, in addition to the `plugin-runtime:use` gate. The
  // filter runs BEFORE mergePluginNavGroups so a plugin group whose items are
  // all hidden also hides its trigger (research Pitfall 9). A malformed
  // permission fails closed (isPermitted returns false).
  const visiblePluginNavItems = useMemo(
    () => pluginNavItems.filter((item) => isPermitted(item, can)),
    [pluginNavItems, can]
  )

  const canSeeUsers = useCan('users', 'read')
  const canSeeRoles = useCan('roles', 'read')
  const canSeeGroups = useCan('groups', 'read')
  const canSeeAuditLog = useCan('audit-log', 'read')
  const canSeePlugins = useCan('plugins', 'read')
  // CORESEAM-09/D-05: using plugin runtime surfaces is `plugin-runtime:use`,
  // deliberately separate from the admin-only `plugins:read` that gates the
  // management page and its Administration entry.
  const canUsePluginRuntime = useCan('plugin-runtime', 'use')

  const isActive = (path) => pathname.startsWith(path)

  // Combined active states for dropdown trigger highlighting (D-14):
  const isConfigActive = pathname.startsWith('/configuration') && pathname !== '/configuration/permissions'
  const isTasksActive = pathname.startsWith('/tasks') || pathname.startsWith('/schedules') || pathname.startsWith('/imports') || pathname.startsWith('/exports')
  // D-16: the Administration group also hosts plugin-contributed items (the
  // log-explorer entry), whose paths the static `isAdminActive` list cannot
  // know. Include every visible contributed Administration path so the trigger
  // highlights on /plugins/log-explorer/explorer too — matching the group's
  // canonical name the same way mergePluginNavGroups does.
  const contributedAdminPaths = visiblePluginNavItems
    .filter((item) => String(item.group ?? '').trim().toLowerCase() === 'administration')
    .map((item) => item.path)
    .filter((path) => typeof path === 'string' && path !== '')
  const isAdminActive =
    pathname.startsWith('/users') ||
    pathname.startsWith('/roles') ||
    pathname.startsWith('/groups') ||
    pathname === '/configuration/permissions' ||
    pathname.startsWith('/administration') ||
    contributedAdminPaths.some((path) => pathname.startsWith(path))

  const handleGroupOpenChange = (id, open) => {
    setOpenGroupId(open ? id : (prev) => (prev === id ? null : prev))
  }

  const handleGroupToggle = (id) => {
    setOpenGroupId((prev) => (prev === id ? null : id))
  }

  // D-07/PLUGFE-04: the static dropdowns are a data declaration merged with
  // plugin contributions at render time. `canonicalName` is the untranslated
  // match key — plugin `group` values never match the translated `label`.
  const baseGroups = [
    {
      id: 'tools',
      canonicalName: 'Tools',
      label: t('nav.tools', 'Tools'),
      triggerTestId: 'tools-menu-trigger',
      panelTestId: 'tools-menu-panel',
      triggerAriaLabel: t('nav.tools_menu', 'Tools menu'),
      isActive: isActive('/tools'),
      visible: true,
      toggleOnClick: false,
      items: [
        {
          testId: 'tools-menu-saved-queries',
          to: '/tools/saved-queries',
          icon: 'bookmark',
          label: t('nav.saved_queries', 'GraphQL Library'),
          can: { resource: 'saved-queries', action: 'read' },
        },
        {
          testId: 'tools-menu-translation',
          to: '/tools/translation',
          icon: 'languages',
          label: t('nav.translation', 'Translation'),
          can: { resource: 'translations', action: 'read' },
        },
      ],
    },
    {
      id: 'config',
      canonicalName: 'Configuration',
      label: t('nav.configuration', 'Configuration'),
      triggerTestId: 'config-menu-trigger',
      panelTestId: 'config-menu-panel',
      triggerAriaLabel: t('nav.configuration_menu', 'Configuration menu'),
      isActive: isConfigActive,
      visible: isAdmin,
      toggleOnClick: true,
      items: [
        {
          testId: 'config-menu-sources',
          to: '/configuration/sources',
          icon: 'globe',
          label: t('nav.sources', 'Sources'),
        },
        {
          testId: 'config-menu-targets',
          to: '/configuration/targets',
          icon: 'crosshair',
          label: t('nav.targets', 'Targets'),
          visible: isAdmin,
        },
        {
          testId: 'config-menu-schemas',
          to: '/configuration/schemas',
          icon: 'layers',
          label: t('nav.schemas', 'Schemas'),
        },
        {
          testId: 'config-menu-languages',
          to: '/configuration/languages',
          icon: 'languages',
          label: t('nav.languages', 'Languages'),
        },
        {
          testId: 'config-menu-service-accounts',
          to: '/configuration/service-accounts',
          icon: 'user-cog',
          label: t('nav.service_accounts', 'Service Accounts'),
        },
        {
          testId: 'config-menu-templates',
          to: '/configuration/templates',
          icon: 'file-text',
          label: t('nav.templates', 'Templates'),
        },
        {
          testId: 'config-menu-settings',
          to: '/configuration/settings',
          icon: 'cog',
          label: t('nav.settings', 'Settings'),
        },
      ],
    },
    {
      id: 'tasks',
      canonicalName: 'Integration',
      label: t('nav.integration', 'Integration'),
      triggerTestId: 'tasks-menu-trigger',
      panelTestId: 'tasks-menu-panel',
      triggerAriaLabel: t('nav.integration_menu', 'Integration menu'),
      isActive: isTasksActive,
      visible: true,
      toggleOnClick: false,
      items: [
        {
          testId: 'tasks-menu-tasks',
          to: '/tasks',
          icon: 'list-checks',
          label: t('nav.tasks', 'Tasks'),
        },
        {
          testId: 'tasks-menu-schedules',
          to: '/schedules',
          icon: 'clock',
          label: t('nav.schedules', 'Schedules'),
          visible: isAdmin,
        },
        {
          testId: 'tasks-menu-import',
          to: '/imports',
          icon: 'download',
          label: t('nav.import', 'Import'),
        },
        {
          testId: 'tasks-menu-export',
          to: '/exports',
          icon: 'upload',
          label: t('nav.export', 'Export'),
        },
      ],
    },
    {
      id: 'admin',
      canonicalName: 'Administration',
      label: t('nav.administration', 'Administration'),
      triggerTestId: 'admin-menu-trigger',
      panelTestId: 'admin-menu-panel',
      triggerAriaLabel: t('nav.administration_menu', 'Administration menu'),
      isActive: isAdminActive,
      visible: canSeeUsers || canSeeRoles || canSeeGroups || canSeeAuditLog || canSeePlugins,
      toggleOnClick: true,
      items: [
        {
          to: '/users',
          icon: 'users',
          label: t('nav.users', 'Users'),
          can: { resource: 'users', action: 'read' },
        },
        {
          to: '/roles',
          icon: 'shield',
          label: t('nav.roles', 'Roles'),
          can: { resource: 'roles', action: 'read' },
        },
        {
          to: '/groups',
          icon: 'users-round',
          label: t('nav.groups', 'Groups'),
          can: { resource: 'groups', action: 'read' },
        },
        {
          to: '/configuration/permissions',
          icon: 'key',
          label: t('nav.permissions', 'Permissions'),
          can: { resource: 'roles', action: 'read' },
        },
        {
          to: '/administration/audit-logs',
          icon: 'history',
          label: t('nav.audit_log', 'Audit Log'),
          can: { resource: 'audit-log', action: 'read' },
        },
        {
          testId: 'admin-menu-plugins',
          to: '/administration/plugins',
          icon: 'puzzle',
          label: t('nav.plugins', 'Plugins'),
          can: { resource: 'plugins', action: 'read' },
        },
      ],
    },
  ]

  const { groups, itemsByGroupId } = mergePluginNavGroups(baseGroups, visiblePluginNavItems)

  // The mobile menu closes on every navigation.
  useEffect(() => {
    setMobileNavOpen(false)
  }, [pathname])

  // D-06/REL-01: read the running version on the first user-menu open. The
  // read is lazy (never on page load) and silent: a failure leaves the state
  // unset, which renders no element at all — no empty row, no placeholder.
  // `app-version.js` caches the request per fetch implementation, so toggling
  // the menu issues at most one `/health` call per page load (T-318-07).
  useEffect(() => {
    if (!userMenuOpen || appVersion !== undefined) return undefined
    let cancelled = false
    fetchAppVersion()
      .then((value) => {
        if (!cancelled && value) setAppVersion(value)
      })
      .catch(() => {})
    return () => {
      cancelled = true
    }
  }, [userMenuOpen, appVersion])

  useEffect(() => {
    const handleKeyDown = (e) => {
      if (e.key === 'Escape') {
        setUserMenuOpen(false)
        setMobileNavOpen(false)
      }
    }
    const handleClickOutside = (e) => {
      if (userMenuRef.current && !userMenuRef.current.contains(e.target)) {
        setUserMenuOpen(false)
      }
      if (mobileNavRef.current && !mobileNavRef.current.contains(e.target)) {
        setMobileNavOpen(false)
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    document.addEventListener('mousedown', handleClickOutside)
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      document.removeEventListener('mousedown', handleClickOutside)
    }
  }, [])

  // Shared visibility rules for the desktop dropdowns and the mobile menu.
  // D-16: a static group hidden by its own rule still shows while it hosts a
  // visible contributed item — that is what keeps the Administration menu
  // reachable for a user whose only admin right is `system-logs:read` (its
  // static `visible` is computed before `mergePluginNavGroups` exists). A
  // contributed plugin group needs the runtime capability and at least one item.
  const isGroupShown = (group) => {
    const groupPluginItems = itemsByGroupId[group.id] ?? []
    const hasContributedItems = canUsePluginRuntime && groupPluginItems.length > 0
    if (group.id.startsWith('plugin-group-')) return hasContributedItems
    if (group.visible !== false) return true
    return hasContributedItems
  }

  // Empty until a version is known — the menu then renders no version row.
  const appVersionLabel = formatVersionLabel(appVersion)

  return (
    <header ref={mobileNavRef} className="sticky top-0 z-40 border-b bg-background" style={{ background: 'hsl(var(--template-header-bg, var(--background)))', color: 'hsl(var(--template-header-fg, var(--foreground)))' }}>
      <div className="flex h-14 items-center px-4 gap-4">
        <Button
          onClick={() => setMobileNavOpen((o) => !o)}
          size="icon"
          variant="ghost"
          leftIcon={mobileNavOpen ? 'x' : 'menu'}
          aria-label={t('nav.toggle_menu', 'Toggle navigation menu')}
          aria-expanded={mobileNavOpen}
          aria-controls="mobile-nav-panel"
          className="md:hidden shrink-0"
          data-testid="mobile-nav-trigger"
        />
        <Link to="/search" className="font-bold text-primary text-lg shrink-0">
          {t('auth.brand', 'IndraMesh Core')}
        </Link>

        <nav className="hidden md:flex items-center gap-1">
          {groups.map((group) => {
            const groupPluginItems = itemsByGroupId[group.id] ?? []
            const isPluginGroup = group.id.startsWith('plugin-group-')
            if (!isGroupShown(group)) return null
            const firstPluginPath = groupPluginItems[0]?.path
            const triggerActive = isPluginGroup
              ? Boolean(firstPluginPath) && pathname.startsWith(firstPluginPath)
              : group.isActive
            return (
              <DropdownMenu
                key={group.id}
                trigger={
                  <Button
                    onClick={group.toggleOnClick ? () => handleGroupToggle(group.id) : undefined}
                    variant={triggerActive ? 'secondary' : 'ghost'}
                    size="sm"
                    rightIcon="chevron-down"
                    aria-label={group.triggerAriaLabel ?? group.label}
                    data-testid={group.triggerTestId ?? group.testId}
                  >
                    {isPluginGroup ? (
                      <span className="max-w-[220px] truncate" title={group.label}>{group.label}</span>
                    ) : (
                      group.label
                    )}
                  </Button>
                }
                open={openGroupId === group.id}
                onOpenChange={(open) => handleGroupOpenChange(group.id, open)}
                data-testid={group.panelTestId}
                className={group.panelClassName}
              >
                {(group.items ?? []).map((item, index) => {
                  if (item.kind === 'separator') {
                    return <DropdownMenuSeparator key={`${group.id}-separator-${index}`} />
                  }
                  if (item.visible === false) return null
                  const entry = (
                    <DropdownMenuItem key={item.testId ?? item.to} asChild data-testid={item.testId}>
                      <Link to={item.to} onClick={() => handleGroupOpenChange(group.id, false)}>
                        {item.icon ? <LucideIcon name={item.icon} size={14} className="mr-0.5" /> : null}
                        {item.label}
                      </Link>
                    </DropdownMenuItem>
                  )
                  if (!item.can) return entry
                  return (
                    <Can key={`can-${item.testId ?? item.to}`} resource={item.can.resource} action={item.can.action}>
                      {entry}
                    </Can>
                  )
                })}
                {groupPluginItems.map((item) => {
                  const label = pluginText(item.pluginName, item.labelKey, item.label)
                  return (
                    <Can key={item.id} resource="plugin-runtime" action="use">
                      <DropdownMenuItem asChild data-testid={'plugin-nav-item-' + item.id}>
                        <Link to={item.path} onClick={() => handleGroupOpenChange(group.id, false)} className="flex w-full items-center">
                          {item.icon ? <LucideIcon name={item.icon} size={14} className="mr-0.5" /> : null}
                          <span className="max-w-[220px] truncate" title={label}>{label}</span>
                          <PluginNavBadge item={item} />
                        </Link>
                      </DropdownMenuItem>
                    </Can>
                  )
                })}
              </DropdownMenu>
            )
          })}
        </nav>

        <div className="ml-auto md:ml-0 flex items-center gap-2 shrink-0">
          <Button
            onClick={onToggleDark}
            size="icon"
            variant="ghost"
            leftIcon="moon"
             aria-label={t('common.toggle_dark_mode', 'Toggle dark mode')}
          />

          {isAuthenticated && user && (
            <div className="relative" ref={userMenuRef}>
              <button
                onClick={() => setUserMenuOpen((o) => !o)}
                className="flex items-center gap-2 rounded-md px-2 py-1 hover:bg-accent hover:text-accent-foreground transition-colors"
                 aria-label={t('common.user_menu', 'User menu')}
                data-testid="user-menu-trigger"
              >
                <div
                  className="h-7 w-7 rounded-full bg-primary text-primary-foreground flex items-center justify-center text-xs font-bold shrink-0"
                  aria-hidden="true"
                >
                  {getInitials({ ...(profile || {}), username: user.username })}
                </div>
                <span className="hidden sm:inline text-sm font-medium max-w-[120px] truncate">
                  {user.username}
                </span>
                <LucideIcon name="chevron-down" size={14} className="text-muted-foreground" />
              </button>

              {userMenuOpen && (
                <div className="absolute right-0 top-full mt-1 w-48 rounded-md border bg-card text-card-foreground shadow-lg z-50 overflow-hidden">
                  <Button
                    asChild
                    variant="ghost"
                    size="sm"
                    className="w-full justify-start rounded-none h-auto py-2"
                  >
                    <Link to="/profile" onClick={() => setUserMenuOpen(false)}>
                      <LucideIcon name="user" size={14} />
                       {t('common.profile', 'Profile')}
                    </Link>
                  </Button>
                  <hr className="border-t border-border" />
                  {appVersionLabel ? (
                    <div
                      data-testid="app-version"
                      className="px-3 py-2 text-xs text-muted-foreground"
                    >
                      {appVersionLabel}
                    </div>
                  ) : null}
                  <button
                    onClick={() => { void logout(); setUserMenuOpen(false); }}
                    data-testid="logout-button"
                    className="w-full flex items-center gap-2 px-3 py-2 text-xs font-medium text-foreground hover:bg-accent hover:text-accent-foreground transition-colors"
                  >
                    <LucideIcon name="log-out" size={14} />
                     {t('common.logout', 'Logout')}
                  </button>
                </div>
              )}
            </div>
          )}
        </div>
      </div>

      {mobileNavOpen && (
        <nav
          id="mobile-nav-panel"
          className="md:hidden max-h-[calc(100vh-3.5rem)] overflow-y-auto border-t bg-card text-card-foreground shadow-lg"
          aria-label={t('nav.mobile_menu', 'Navigation menu')}
          data-testid="mobile-nav-panel"
        >
          {groups.filter(isGroupShown).map((group) => {
            const groupPluginItems = itemsByGroupId[group.id] ?? []
            const linkClass = (to) => cn(
              'flex items-center gap-2 rounded-md px-3 py-2 text-sm hover:bg-accent hover:text-accent-foreground',
              isActive(to) && 'bg-muted font-semibold'
            )
            return (
              <div key={group.id} className="border-b px-2 py-2 last:border-b-0">
                <p className="px-3 pb-1 text-xs font-semibold uppercase tracking-wide text-muted-foreground">
                  {group.label}
                </p>
                {(group.items ?? []).map((item, index) => {
                  if (item.kind === 'separator' || item.visible === false) return null
                  const entry = (
                    <Link
                      key={item.testId ?? item.to ?? index}
                      to={item.to}
                      className={linkClass(item.to)}
                      data-testid={item.testId ? 'mobile-' + item.testId : undefined}
                    >
                      {item.icon ? <LucideIcon name={item.icon} size={14} /> : null}
                      {item.label}
                    </Link>
                  )
                  if (!item.can) return entry
                  return (
                    <Can key={`can-${item.testId ?? item.to}`} resource={item.can.resource} action={item.can.action}>
                      {entry}
                    </Can>
                  )
                })}
                {groupPluginItems.map((item) => {
                  const label = pluginText(item.pluginName, item.labelKey, item.label)
                  return (
                    <Can key={item.id} resource="plugin-runtime" action="use">
                      <Link
                        to={item.path}
                        className={linkClass(item.path)}
                        data-testid={'mobile-plugin-nav-item-' + item.id}
                      >
                        {item.icon ? <LucideIcon name={item.icon} size={14} /> : null}
                        <span className="truncate" title={label}>{label}</span>
                        <PluginNavBadge item={item} />
                      </Link>
                    </Can>
                  )
                })}
              </div>
            )
          })}
        </nav>
      )}
    </header>
  )
}
