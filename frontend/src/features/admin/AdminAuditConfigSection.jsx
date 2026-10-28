import { useState, useMemo } from 'react'
import { AUDIT_ENTITY_TYPES, ALWAYS_ON_ENTITY_TYPES } from '../../shared/constants/audit-entity-types'
import { useAuditLogConfig, useUpdateAuditLogConfig } from '../../shared/hooks/use-audit-logs'
import { useSystemSettings } from '../../shared/hooks/use-other-resources'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { Button } from '../../shared/components/Button'
import { cn } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'

const ENTITY_TYPES = AUDIT_ENTITY_TYPES.map(t => t.value)

// Kept in sync with HISTORY_ENTITY_TYPES in backend/app/services/audit_service.py —
// these three are locked on while the "History" switch in Configuration → Settings
// is active; they can only be turned off again by deactivating History there.
const HISTORY_ENTITY_TYPES = ['objects', 'object_values', 'object_refs']

function entityTypeLabel(type, t) {
  switch (type) {
    case 'sources': return t('admin.audit.entity.sources', 'Sources')
    case 'schemas': return t('admin.audit.entity.schemas', 'Schemas')
    case 'object_types': return t('admin.audit.entity.object_types', 'Object Types')
    case 'objects': return t('admin.audit.entity.objects', 'Objects')
    case 'object_values': return t('admin.audit.entity.object_values', 'Object Values')
    case 'object_refs': return t('admin.audit.entity.object_refs', 'Object Refs')
    case 'attribute_types': return t('admin.audit.entity.attribute_types', 'Attribute Types')
    case 'reference_types': return t('admin.audit.entity.reference_types', 'Reference Types')
    case 'statuses': return t('admin.audit.entity.statuses', 'Statuses')
    case 'icons': return t('admin.audit.entity.icons', 'Icons')
    case 'targets': return t('admin.audit.entity.targets', 'Targets')
    case 'service_accounts': return t('admin.audit.entity.service_accounts', 'Service Accounts')
    case 'schema_templates': return t('admin.audit.entity.schema_templates', 'Schema Templates')
    case 'import_definitions': return t('admin.audit.entity.import_definitions', 'Import Definitions')
    case 'export_definitions': return t('admin.audit.entity.export_definitions', 'Export Definitions')
    case 'tasks': return t('admin.audit.entity.tasks', 'Tasks')
    case 'audit_log_config': return t('admin.audit.entity.audit_log_config', 'Audit Log Config')
    case 'settings': return t('admin.audit.entity.settings', 'Settings')
    case 'permissions': return t('admin.audit.entity.permissions', 'Permissions')
    case 'users': return t('admin.audit.entity.users', 'Users')
    case 'roles': return t('admin.audit.entity.roles', 'Roles')
    case 'plugins': return t('admin.audit.entity.plugins', 'Plugins')
    default: return type
  }
}

export default function AdminAuditConfigSection() {
  const { t } = useTranslation()
  const [expanded, setExpanded] = useState(false)
  const [search, setSearch] = useState('')

  const { data: config, isLoading, isError, refetch } = useAuditLogConfig()
  const { data: settings = {} } = useSystemSettings()
  const mutation = useUpdateAuditLogConfig()
  const historyEnabled = settings.history_enabled === 'true'

  const filteredTypes = useMemo(() => {
    if (!search.trim()) return ENTITY_TYPES
    const q = search.toLowerCase()
    return ENTITY_TYPES.filter((type) =>
      entityTypeLabel(type, t).toLowerCase().includes(q)
    )
  }, [search, t])

  return (
    <div className="rounded-lg border" data-testid="audit-config-section">
      {/* Collapsible header button */}
      <button
        onClick={() => setExpanded(!expanded)}
        className="flex items-center gap-2 w-full px-4 py-2.5 text-sm font-semibold text-left hover:bg-accent/50 transition-colors"
        aria-expanded={expanded}
        data-testid="audit-config-toggle"
      >
        <LucideIcon name={expanded ? 'chevron-down' : 'chevron-right'} size={16} />
         {t('admin.audit.configuration_title', 'Audit Configuration')}
      </button>

      {/* Collapsible content — only visible when expanded */}
      {expanded && (
        <div className="border-t p-3 space-y-1 bg-card">
          {/* Search input */}
          <input
            type="text"
            value={search}
            onChange={(e) => setSearch(e.target.value)}
             placeholder={t('admin.audit.search_entity_types', 'Search entity types...')}
            className="w-full rounded-md border border-input bg-background px-3 py-1.5 text-sm placeholder:text-muted-foreground mb-2"
            data-testid="audit-config-search"
          />

          {/* Loading state: skeleton rows */}
          {isLoading && (
            <div className="space-y-1">
              {Array.from({ length: 5 }).map((_, i) => (
                <div key={i} className="h-8 rounded animate-shimmer motion-safe:animate-shimmer bg-muted/30" />
              ))}
            </div>
          )}

          {/* Error state */}
          {isError && !isLoading && (
            <div className="rounded-md bg-destructive/10 p-3 text-destructive text-sm">
               {t('admin.audit.load_failed', 'Failed to load audit configuration.')}{' '}
              <Button variant="link" className="p-0 h-auto" onClick={() => refetch()}>
                 {t('common.retry', 'Retry')}
              </Button>
            </div>
          )}

          {/* Empty search state */}
          {!isLoading && !isError && filteredTypes.length === 0 && (
           <p className="text-sm text-muted-foreground py-2">{t('admin.audit.no_matches', 'No entity types match your search')}</p>
          )}

          {/* Entity type toggle list */}
          {!isLoading && !isError && filteredTypes.map((type) => {
            const lockedByHistory = historyEnabled && HISTORY_ENTITY_TYPES.includes(type)
            const lockedAlwaysOn = ALWAYS_ON_ENTITY_TYPES.includes(type)
            const locked = lockedByHistory || lockedAlwaysOn
            const lockReason = lockedAlwaysOn
               ? t('admin.audit.always_on_reason', 'Always on — this entity type cannot be disabled')
               : t('admin.audit.history_locked_reason', 'Locked on by the History switch in Configuration → Settings')
            return (
              <div
                key={type}
                className="flex items-center justify-between py-1.5"
                data-testid={`audit-config-row-${type}`}
              >
                <span className="text-sm inline-flex items-center gap-1.5">
                   {entityTypeLabel(type, t)}
                  {locked && (
                    <span className="text-xs text-muted-foreground" title={lockReason}>
                       {lockedAlwaysOn ? t('admin.audit.always_on', '(always on)') : t('admin.audit.locked_by_history', '(locked by History)')}
                    </span>
                  )}
                </span>
                <button
                  role="switch"
                  aria-checked={config?.[type] ?? false}
                  onClick={() => {
                    if (!mutation.isPending && !locked) {
                      mutation.mutate({ entity_type: type, enabled: !(config?.[type] ?? false) })
                    }
                  }}
                  disabled={mutation.isPending || locked}
                  className={cn(
                    'relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2',
                    locked ? 'cursor-not-allowed opacity-60' : 'cursor-pointer',
                    (config?.[type] ?? false) ? 'bg-primary' : 'bg-input'
                  )}
                  data-testid={`audit-config-switch-${type}`}
                >
                  <span
                    className={cn(
                      'pointer-events-none inline-block h-4 w-4 rounded-full bg-background shadow transform ring-0 transition-transform',
                      (config?.[type] ?? false) ? 'translate-x-4' : 'translate-x-0'
                    )}
                  />
                </button>
              </div>
            )
          })}
        </div>
      )}
    </div>
  )
}
