import { createPortal } from 'react-dom'
import Loader2 from 'lucide-react/dist/esm/icons/loader-2'
import { useQueryClient } from '@tanstack/react-query'
import { Button } from '../../shared/components/Button'
import { useRoles } from '../../shared/hooks/use-roles'
import {
  useEffectivePermissions,
  useDirectRecordPermissions,
  useCreateRecordPermission,
  useDeleteRecordPermission,
} from '../../shared/hooks/use-record-permissions'
import { RecordPermissionForm } from './RecordPermissionForm'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { PHASE231_PERMISSION_ENTITY_LABELS } from '../../shared/lib/phase231-dynamic-contract'

function extractItems(data) {
  if (Array.isArray(data)) return data
  return data?.items || []
}

function mergeWithDirectIds(effectiveItems, directItems) {
  return effectiveItems.map(item => {
    if (!item.is_direct) return item
    const match = directItems.find(
      d => d.role_id === item.role_id && d.action === item.action && d.is_deny === item.is_deny
    )
    return match ? { ...item, id: match.id } : item
  })
}

function entityTypeLabel(entityType, t) {
  const label = PHASE231_PERMISSION_ENTITY_LABELS[entityType]
  return label ? t(label.key, label.defaultText) : entityType
}

function PermissionsDialog({ entityType, recordId, open, onClose }) {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const effectiveQuery = useEffectivePermissions(entityType, recordId)
  const directQuery = useDirectRecordPermissions(entityType, recordId)
  const rolesQuery = useRoles(0, 100)
  const createRecordPermission = useCreateRecordPermission()
  const deleteRecordPermission = useDeleteRecordPermission()

  if (!open) return null

  const roles = extractItems(rolesQuery.data)
  const effectiveItems = extractItems(effectiveQuery.data)
  const directItems = extractItems(directQuery.data)
  const existingItems = mergeWithDirectIds(effectiveItems, directItems)

   const entityLabel = entityTypeLabel(entityType, t)

  const invalidateDependentLists = () => {
    queryClient.invalidateQueries({ queryKey: ['objects'] })
    queryClient.invalidateQueries({ queryKey: ['object-type-attributes'] })
  }

  const handleCreate = (data) =>
    createRecordPermission.mutateAsync(data).then((res) => { invalidateDependentLists(); return res })
  const handleDelete = (id) =>
    deleteRecordPermission.mutateAsync(id).then((res) => { invalidateDependentLists(); return res })

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="relative z-50 w-full max-w-lg rounded-lg border bg-card p-6 shadow-lg space-y-4"
        onClick={e => e.stopPropagation()}
      >
        <div className="space-y-1">
           <h3 className="text-lg font-semibold">{t('common.permissions', 'Permissions')}</h3>
          <p className="text-sm text-muted-foreground">
             {recordId ? interpolateDefault(t('permissions.for_record', 'Permissions for {{entity}} ({{id}})'), { entity: entityLabel, id: recordId }) : interpolateDefault(t('permissions.for_entity', 'Permissions for {{entity}}'), { entity: entityLabel })}
          </p>
        </div>

        {effectiveQuery.isLoading && (
          <div className="flex items-center justify-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="animate-spin" size={18} />
             {t('permissions.loading', 'Loading permissions...')}
          </div>
        )}

        {effectiveQuery.isError && (
          <p className="text-sm text-destructive">
             {t('permissions.load_failed', 'Failed to load permissions. Please try again.')}
          </p>
        )}

        {!effectiveQuery.isLoading && !effectiveQuery.isError && (
          <RecordPermissionForm
            entityType={entityType}
            recordId={recordId}
            roles={roles}
            existingItems={existingItems}
            onCreate={handleCreate}
            onDelete={handleDelete}
          />
        )}

        <div className="flex justify-end pt-2">
          <Button type="button" variant="outline" size="sm" onClick={onClose}>
             {t('common.close', 'Close')}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}

export default PermissionsDialog
