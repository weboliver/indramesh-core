export { Button } from '../../shared/components/Button'
export { default as Table } from '../../shared/components/Table'
export { default as PageHeader } from '../../shared/components/PageHeader'
export { DropdownMenu, DropdownMenuItem, DropdownMenuSeparator } from '../../shared/components/DropdownMenu'
export { showToast } from '../../shared/components/Toast'
export { cn } from '../../shared/lib/utils'
// Phase 304 (GQL-06/D-18): the plugin editor shows a production-only warning. A
// plugin reads the host's baked build flag by identity (never its own copy), so
// `environment=production` is the single source of truth. This is an identity
// re-export like every other name above: it does not change the index.html
// import-map hash (frontend/index.html lists paths only) -- see test_plugin_csp.py.
export { IS_PRODUCTION } from '../../shared/lib/env'
export { LucideIcon } from '../../shared/lib/lucide-icons'
export { useTranslation } from 'react-i18next'

// FEEXT-03: identity re-exports only (`export { } from`), never local copies —
// a plugin and the host share one module instance of the axios client, the
// confirm-dialog root, the auth context and the QueryClient. The import map and
// the CSP hash stay unchanged because this file is served/built as its own
// entry regardless of how many names it re-exports.
export { default as api, serializeRepeatedParams } from '../../shared/hooks/use-api'
export { confirmDialog, alertDialog } from '../../shared/components/ConfirmDialog'
export { Can, useCan } from '../../shared/components/Can'
export { extractErrorMessage } from '../../shared/lib/utils'
export { interpolateDefault } from '../../shared/lib/i18n-defaults'
export { pluginNamespace } from './contribution-utils'
export { usePluginTranslation, pluginText } from './PluginProvider'
export { injectPluginStyles, releaseOwner, createStyleInjector } from './plugin-styles'
export { loadPluginModule, createDeferredModuleLoader, setDeferredRuntimeSource } from './deferred-module'
export { FormField, TextInput, NumberInput, SelectInput, CheckboxField } from '../../shared/components/FormControls'
export { default as TableSkeleton } from '../../shared/components/TableSkeleton'
export { PaginationBar } from '../../shared/components/PaginationBar'
export { default as EmptyState } from '../../shared/components/EmptyState'
export { useSources, useSource } from '../../shared/hooks/use-sources'
export { useSchemas, useSchema } from '../../shared/hooks/use-schemas'
export { useObjectTypes, useObjectType } from '../../shared/hooks/use-object-types'
export { useTargets, useTarget } from '../../shared/hooks/use-targets'
export { useObjects, useObject } from '../../shared/hooks/use-objects'
export { useObjectTypeAttributes, useTasks, useTask, useServiceAccounts } from '../../shared/hooks/use-other-resources'
export { useTaskTypes } from '../../shared/hooks/use-task-types'
// Phase 305 (LOGX-02/LOGX-03, FEEXT-03 identity re-export): the explorer rows and
// axis use the host's row-time formatters so log lines share
// `settings.default_timezone` with the Core task log. A missing named export here
// would fail the whole plugin module at runtime (the import map serves this file
// as an ES module), so these stay identity re-exports — never a plugin-local copy.
export { useFormatDate, useFormatTime, useFormatDateTime } from '../../shared/hooks/use-format-date'