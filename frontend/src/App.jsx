import React, { Suspense } from 'react'
import { Routes, Route, Navigate, useParams, useLocation, useNavigate } from 'react-router-dom'
import { useAuth } from './shared/hooks/use-auth'
import { useCan } from './shared/components/Can'
import { usePluginRuntimeList } from './shared/plugin-runtime/PluginProvider'
import Layout from './shared/components/Layout'
import LoadingSpinner from './shared/components/LoadingSpinner'
import EmptyState from './shared/components/EmptyState'
import { ToastContainer } from './shared/components/Toast'
import { ConfirmDialogRoot } from './shared/components/ConfirmDialog'
import { useTranslation } from 'react-i18next'

const LoginPage = React.lazy(() => import('./features/login/Login'))
const ObjectDetailPage = React.lazy(() => import('./features/browse/ObjectDetail'))
const ObjectTypeObjectsPage = React.lazy(() => import('./features/browse/ObjectTypeObjects'))
const ProfilePage = React.lazy(() => import('./features/profile/ProfilePage'))
const SchemaSettingsPage = React.lazy(() => import('./features/settings/SchemaSettings'))
const SettingsPage = React.lazy(() => import('./features/settings/Settings'))
const TemplateLibraryPage = React.lazy(() => import('./features/wizard/TemplateLibraryPage'))
const CrossEntitySearchPage = React.lazy(() => import('./features/search/CrossEntitySearchPage'))
const SearchPage = React.lazy(() => import('./features/search/SearchPage'))
const TaskListPage = React.lazy(() => import('./features/tasks/TaskList'))
const TaskDetailPage = React.lazy(() => import('./features/tasks/TaskDetail'))
const ScheduleListPage = React.lazy(() => import('./features/schedules/ScheduleList'))
const CreateObjectTypePage = React.lazy(() => import('./features/object-type-admin/CreateObjectType'))
const EditObjectTypePage = React.lazy(() => import('./features/object-type-admin/EditObjectType'))
const UserListPage = React.lazy(() => import('./features/admin/UserList'))
const UserDetailPage = React.lazy(() => import('./features/admin/UserDetail'))
const RoleListPage = React.lazy(() => import('./features/admin/RoleList'))
const RoleDetailPage = React.lazy(() => import('./features/admin/RoleDetail'))
const GroupListPage = React.lazy(() => import('./features/admin/GroupList'))
const GroupDetailPage = React.lazy(() => import('./features/admin/GroupDetail'))
const PermissionMatrixPage = React.lazy(() => import('./features/admin/PermissionMatrix'))
const TargetListPage = React.lazy(() => import('./features/targets/TargetListPage'))
const TargetDetailPage = React.lazy(() => import('./features/targets/TargetDetailPage'))
const SourcesPage = React.lazy(() => import('./features/settings/SourcesPage'))
const SchemasListPage = React.lazy(() => import('./features/settings/SchemasListPage'))
const ServiceAccountsPage = React.lazy(() => import('./features/settings/ServiceAccountsPage'))
const ImportListPage = React.lazy(() => import('./features/imports/ImportListPage'))
const ExportListPage = React.lazy(() => import('./features/exports/ExportListPage'))
const AuditLogPage = React.lazy(() => import('./features/admin/AuditLogPage'))
const LanguagesAdminPage = React.lazy(() => import('./features/settings/LanguagesAdminPage'))
const SavedQueriesPage = React.lazy(() => import('./features/saved-queries/SavedQueriesPage'))
const TranslationAdminPage = React.lazy(() => import('./features/translation-admin/TranslationAdminPage'))
const PluginRouteResolver = React.lazy(() => import('./features/plugins/PluginRouteResolver'))
const PluginsAdminPage = React.lazy(() => import('./features/plugins/PluginsAdminPage'))

function ProtectedRoute({ children }) {
  const { isAuthenticated, loading } = useAuth()
  const { t } = useTranslation()
  if (loading) return <div className="flex h-screen items-center justify-center text-muted-foreground">{t('common.loading', 'Loading...')}</div>
  if (!isAuthenticated) return <Navigate to="/login" replace />
  return children
}

function ProtectedAdminRoute({ children }) {
  const { isAuthenticated, loading, isAdmin } = useAuth()
  const { t } = useTranslation()
  if (loading) return <div className="flex h-screen items-center justify-center text-muted-foreground">{t('common.loading', 'Loading...')}</div>
  if (!isAuthenticated) return <Navigate to="/login" replace />
  if (!isAdmin) return <Navigate to="/browse/object-types" replace />
  return children
}

// Route-level guard for permission-gated (not superuser-only) admin pages —
// ProtectedAdminRoute checks is_superuser, which would wrongly lock out an
// Editor-role user who legitimately has e.g. users:read. Redirects instead
// of rendering the page's own <Can> "Access denied" fallback in place, so
// direct navigation to a gated URL doesn't leave a stale admin URL visible.
function ProtectedPermissionRoute({ resource, action, children }) {
  const { isAuthenticated, loading } = useAuth()
  const allowed = useCan(resource, action)
  const { t } = useTranslation()
  if (loading) return <div className="flex h-screen items-center justify-center text-muted-foreground">{t('common.loading', 'Loading...')}</div>
  if (!isAuthenticated) return <Navigate to="/login" replace />
  if (!allowed) return <Navigate to="/browse/object-types" replace />
  return children
}

function RedirectWithParams({ to }) {
  const params = useParams()
  const target = Object.entries(params).reduce(
    (path, [key, value]) => path.replace(`:${key}`, value ?? ''),
    to
  )
  return <Navigate to={target} replace />
}

// 304 D-12/GQL-05: tools/graphql is a redirect-only route. While the plugin
// runtime discovery is unresolved the full-page spinner shows (no wrong-surface
// flash); an active graphql-studio plugin redirects to its studio page, otherwise
// the Core saved-queries page is the deterministic target.
function GraphQLRedirect() {
  const canUseRuntime = useCan('plugin-runtime', 'use')
  const { data, isPending } = usePluginRuntimeList()
  if (!canUseRuntime) return <Navigate to="/tools/saved-queries" replace />
  if (isPending) return <LoadingSpinner />
  const active = (Array.isArray(data) ? data : []).some((plugin) => plugin.name === 'graphql-studio')
  return <Navigate to={active ? '/plugins/graphql-studio/studio' : '/tools/saved-queries'} replace />
}

// 305 D-16/LOGX-05: the Core System Logs viewer was extracted into the
// `log-explorer` plugin, so `/administration/system-logs` is redirect-only.
// `usePluginRuntimeList` is `enabled: canUseRuntime`, so its query never leaves
// `isPending` for a user without `plugin-runtime:use` — the capability check
// must come before the pending check (same order as GraphQLRedirect), or that
// user would spin forever instead of seeing the notice. While discovery is
// unresolved a full-page spinner shows (no notice flash); an active plugin plus
// `system-logs:read` replaces the old URL with the plugin explorer (query
// string preserved); every other outcome is the Core notice, whose
// `Open plugins` action is exposed to `plugins:read` holders only.
function SystemLogsRedirect() {
  const { t } = useTranslation()
  const canUseRuntime = useCan('plugin-runtime', 'use')
  const canReadSystemLogs = useCan('system-logs', 'read')
  const canSeePlugins = useCan('plugins', 'read')
  const { data, isPending } = usePluginRuntimeList()
  const navigate = useNavigate()
  const location = useLocation()

  const notice = (
    <EmptyState
      variant="empty"
      data-testid="system-logs-plugin-inactive"
      title={t('admin.system_logs.plugin_inactive_heading', 'System logs are not available')}
      description={t(
        'admin.system_logs.plugin_inactive_body',
        'System logs are provided by the log-explorer plugin, which is not active. An administrator can activate it under Administration, Plugins.'
      )}
      buttonLabel={canSeePlugins ? t('admin.system_logs.open_plugins', 'Open plugins') : undefined}
      onButtonClick={canSeePlugins ? () => navigate('/administration/plugins') : undefined}
    />
  )

  if (!canUseRuntime) return notice
  if (isPending) return <LoadingSpinner />

  const active = (Array.isArray(data) ? data : []).some((plugin) => plugin.name === 'log-explorer')
  if (active && canReadSystemLogs) {
    return <Navigate to={`/plugins/log-explorer/explorer${location.search}`} replace />
  }
  return notice
}

export default function App() {
  return (
    <>
      <Routes>
        <Route path="/login" element={
          <Suspense fallback={<LoadingSpinner />}>
            <LoginPage />
          </Suspense>
        } />
        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Layout />
            </ProtectedRoute>
          }
        >
          <Route index element={<Navigate to="/search" replace />} />
          <Route path="browse" element={<Navigate to="/browse/object-types" replace />} />
          <Route path="browse/sources" element={<Navigate to="/configuration/sources" replace />} />
          <Route path="browse/sources/:sourceId" element={<Navigate to="/configuration/sources" replace />} />
          <Route path="browse/schemas/:schemaId" element={<RedirectWithParams to="/configuration/schemas/:schemaId" />} />
          <Route path="browse/object-types" element={
            <Suspense fallback={<LoadingSpinner />}>
              <ObjectTypeObjectsPage />
            </Suspense>
          } />
          <Route path="browse/object-types/:objectTypeId" element={
            <Suspense fallback={<LoadingSpinner />}>
              <ObjectTypeObjectsPage />
            </Suspense>
          } />
          <Route path="object-types/new" element={
            <Suspense fallback={<LoadingSpinner />}>
              <CreateObjectTypePage />
            </Suspense>
          } />
          <Route path="object-types/:id/edit" element={
            <Suspense fallback={<LoadingSpinner />}>
              <EditObjectTypePage />
            </Suspense>
          } />
          <Route path="browse/objects/:objectId" element={
            <Suspense fallback={<LoadingSpinner />}>
              <ObjectDetailPage />
            </Suspense>
          } />
          <Route path="profile" element={
            <Suspense fallback={<LoadingSpinner />}>
              <ProfilePage />
            </Suspense>
          } />
          <Route path="search/cross-entity" element={
            <Suspense fallback={<LoadingSpinner />}>
              <CrossEntitySearchPage />
            </Suspense>
          } />
          <Route path="search" element={
            <Suspense fallback={<LoadingSpinner />}>
              <SearchPage />
            </Suspense>
          } />
          <Route path="tools/graphql" element={<GraphQLRedirect />} />
          <Route path="tools/translation" element={
            <ProtectedPermissionRoute resource="translations" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <TranslationAdminPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="tools/saved-queries" element={
            <ProtectedPermissionRoute resource="saved-queries" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <SavedQueriesPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="tasks" element={
            <Suspense fallback={<LoadingSpinner />}>
              <TaskListPage />
            </Suspense>
          } />
          <Route path="tasks/:taskId" element={
            <Suspense fallback={<LoadingSpinner />}>
              <TaskDetailPage />
            </Suspense>
          } />
          <Route path="schedules" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <ScheduleListPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/settings" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <SettingsPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/sources" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <SourcesPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/schemas" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <SchemasListPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/languages" element={
            <ProtectedPermissionRoute resource="languages" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <LanguagesAdminPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="configuration/schemas/:schemaId" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <SchemaSettingsPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/templates" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <TemplateLibraryPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/targets" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <TargetListPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/targets/:targetId" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <TargetDetailPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/service-accounts" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <ServiceAccountsPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="configuration/permissions" element={
            <ProtectedPermissionRoute resource="roles" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <PermissionMatrixPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="settings" element={<Navigate to="/configuration/settings" replace />} />
          <Route path="settings/schemas/:schemaId" element={<RedirectWithParams to="/configuration/schemas/:schemaId" />} />
          <Route path="settings/sources/:sourceId" element={<Navigate to="/configuration/sources" replace />} />
          <Route path="settings/templates" element={<Navigate to="/configuration/templates" replace />} />
          <Route path="settings/targets" element={<Navigate to="/configuration/targets" replace />} />
          <Route path="settings/permissions" element={<Navigate to="/configuration/permissions" replace />} />
          <Route path="imports" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <ImportListPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="exports" element={
            <ProtectedAdminRoute>
              <Suspense fallback={<LoadingSpinner />}>
                <ExportListPage />
              </Suspense>
            </ProtectedAdminRoute>
          } />
          <Route path="users" element={
            <ProtectedPermissionRoute resource="users" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <UserListPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="users/:id" element={
            <ProtectedPermissionRoute resource="users" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <UserDetailPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="roles" element={
            <ProtectedPermissionRoute resource="roles" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <RoleListPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="roles/:id" element={
            <ProtectedPermissionRoute resource="roles" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <RoleDetailPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="groups" element={
            <ProtectedPermissionRoute resource="groups" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <GroupListPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="groups/:id" element={
            <ProtectedPermissionRoute resource="groups" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <GroupDetailPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="administration/system-logs" element={<SystemLogsRedirect />} />
          <Route path="administration/audit-logs" element={
            <ProtectedPermissionRoute resource="audit-log" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <AuditLogPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="administration/plugins" element={
            <ProtectedPermissionRoute resource="plugins" action="read">
              <Suspense fallback={<LoadingSpinner />}>
                <PluginsAdminPage />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
          <Route path="plugins/:pluginName/*" element={
            <ProtectedPermissionRoute resource="plugin-runtime" action="use">
              <Suspense fallback={<LoadingSpinner />}>
                <PluginRouteResolver />
              </Suspense>
            </ProtectedPermissionRoute>
          } />
        </Route>
        <Route path="*" element={<Navigate to="/search" replace />} />
      </Routes>
      <ToastContainer />
      <ConfirmDialogRoot />
    </>
  )
}
