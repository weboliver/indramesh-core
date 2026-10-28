import { useState, useEffect } from 'react'
import { useParams, Link, useNavigate } from 'react-router-dom'
import { useObject, useDeleteObject } from '../../shared/hooks/use-objects'
import { useObjectType } from '../../shared/hooks/use-object-types'
import { useObjectValues } from '../../shared/hooks/use-object-values'
import { useObjectRefs } from '../../shared/hooks/use-object-refs'
import { useObjectTypeAttributes } from '../../shared/hooks/use-other-resources'
import { useAttributeTypes } from '../../shared/hooks/use-attribute-types'
import { useActiveLanguage } from '../../shared/hooks/use-active-language'
import { useLanguages, useSchemaLanguages } from '../../shared/hooks/use-languages'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useAuth } from '../../shared/hooks/use-auth'
import { Can } from '../../shared/components/Can'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { PluginSectionRegion } from '../../shared/plugin-runtime/PluginSections'
import EditObjectModal from './EditObjectModal'
import TypedValue from './TypedValue'
import ObjectTypeBreadcrumb from './ObjectTypeBreadcrumb'
import TableSkeleton from '../../shared/components/TableSkeleton'
import { extractTypedValue } from '../../shared/lib/value-utils'
import PermissionsDialog from '../permissions/PermissionsDialog'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { showToast } from '../../shared/components/Toast'

export default function ObjectDetailPage() {
  const { t } = useTranslation()
  const { objectId } = useParams()
  const { data: systemLanguageList } = useLanguages()
  // Bootstrap fetch (unlang) to discover the object's ObjectType/schema before
  // the active language can be resolved — this page redirects to
  // ObjectTypeObjects immediately once obj loads, so the extra round trip is
  // cheap and only ever observed on a direct-link/hard-refresh visit.
  const { data: objBootstrap } = useObject(objectId)
  const { data: objectType } = useObjectType(objBootstrap?.object_type_id)
  const { data: schemaLanguageList } = useSchemaLanguages(objectType?.schema_id)
  const { lang } = useActiveLanguage({ schemaLanguages: schemaLanguageList, systemLanguages: systemLanguageList })

  const { data: obj, isLoading } = useObject(objectId, lang)
  const deleteObject = useDeleteObject()
  const { data: values = [] } = useObjectValues(objectId, lang)
  const { data: refs = [] } = useObjectRefs(objectId)
  const { data: attributes = [], isLoading: attributesLoading } = useObjectTypeAttributes(obj?.object_type_id)
  const { data: attributeTypes = [], isLoading: attributeTypesLoading } = useAttributeTypes()
  const [editOpen, setEditOpen] = useState(false)
  const [permDialogOpen, setPermDialogOpen] = useState(false)
  const [showHidden, setShowHidden] = useState(true)
  const navigate = useNavigate()
  const { isAdmin } = useAuth()

  useEffect(() => {
    if (obj?.object_type_id) {
      navigate(`/browse/object-types/${obj.object_type_id}?select=${objectId}`, { replace: true })
    }
  }, [obj?.object_type_id, navigate, objectId])

  const typeMap = {}
  for (const at of attributeTypes) {
    typeMap[at.id] = at.data_type
  }

  if (isLoading) return (
    <div className="space-y-6">
      <div className="flex items-center gap-2 text-sm">
        <div className="h-4 w-16 rounded bg-muted animate-skeleton" />
        <span className="text-muted-foreground">/</span>
        <div className="h-4 w-24 rounded bg-muted animate-skeleton" />
      </div>
      <TableSkeleton rows={6} cols={1} />
      <TableSkeleton rows={6} cols={1} />
    </div>
  )
  if (!obj) return <div className="text-destructive p-6">{t('browse.detail.object_not_found', 'Object not found')}</div>

  const handleSaved = () => {
    window.location.reload()
  }

  const handleDelete = async () => {
    if (await confirmDialog({ message: t('browse.objects.delete_confirm', 'Delete this object?'), danger: true })) {
      try {
        await deleteObject.mutateAsync(obj.id)
        navigate(`/browse/object-types/${obj.object_type_id}`)
      } catch (err) {
        // 290 D-15: a 409 means the type is still externally managed — tell
        // the user that detaching in the connector is the way out.
        showToast(
          err?.response?.status === 409
            ? t('common.delete_externally_managed', 'Externally managed — detach in the connector before deleting.')
            : (err?.response?.data?.detail || t('common.error_generic', 'Something went wrong. Please try again.')),
          { type: 'error' }
        )
      }
    }
  }

  return (
    <ErrorBoundary>
      <div className="space-y-6">
      <ObjectTypeBreadcrumb objectTypeId={obj.object_type_id} trailing={obj.name} />

      <div className="rounded-lg border p-6 space-y-4">
        <div className="flex items-center justify-between">
          <h1 className="text-2xl font-bold tracking-tight">{obj.name}</h1>
          <div className="flex gap-2">
            <Button type="button" variant="primary" size="sm" onClick={() => setEditOpen(true)}>
              {t('common.edit', 'Edit')}
            </Button>
            <Can resource="roles" action="manage">
              <Button type="button" variant="outline" size="sm" onClick={() => setPermDialogOpen(true)}>
                {t('common.permissions', 'Permissions')}
              </Button>
            </Can>
            <Button type="button" variant="destructive" size="sm" onClick={handleDelete}>
              {t('common.delete', 'Delete')}
            </Button>
          </div>
        </div>

      </div>

      <div className="rounded-lg border p-6 space-y-3">
        <div className="flex items-center justify-between">
          <h2 className="text-lg font-semibold" data-testid="attributes-section">{selectPluralKey('browse.detail.attributes_references', values.length + refs.length) === 'browse.detail.attributes_references_one'
             ? interpolateDefault(t('browse.detail.attributes_references_one', 'Attributes & References ({{count}})'), { count: values.length + refs.length })
             : interpolateDefault(t('browse.detail.attributes_references_other', 'Attributes & References ({{count}})'), { count: values.length + refs.length })}</h2>
          {isAdmin && (
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => setShowHidden(prev => !prev)}
              leftIcon={showHidden ? 'eye-off' : 'eye'}
            >
              {showHidden ? t('browse.detail.hide_hidden', 'Hide Hidden') : t('browse.detail.show_hidden', 'Show Hidden')}
            </Button>
          )}
        </div>
        {values.length === 0 && refs.length === 0 ? (
          <p className="text-sm text-muted-foreground">{t('browse.detail.no_attributes_references', 'No attributes or references.')}</p>
        ) : (
          <div className="rounded-lg border">
            <table className="w-full">
              <thead>
                <tr className="border-b">
                  <th className="text-left px-4 py-3 text-sm font-medium">{t('common.attribute', 'Attribute')}</th>
                  <th className="text-left px-4 py-3 text-sm font-medium">{t('common.value', 'Value')}</th>
                </tr>
              </thead>
              <tbody>
                {(() => {
                  // Deduplicate: one row per attribute (multi-select has multiple ObjectValues)
                  const seenAttrIds = new Set()
                  const dedupedValues = []
                  for (const v of values) {
                    const attr = attributes.find(a => a.id === v.object_type_attribute_id)
                    if (attr?.is_hidden && !isAdmin) continue
                    if (attr?.is_hidden && !showHidden) continue
                    if (!seenAttrIds.has(v.object_type_attribute_id)) {
                      seenAttrIds.add(v.object_type_attribute_id)
                      dedupedValues.push(v)
                    }
                  }
                  return dedupedValues.map((v) => {
                    const attr = attributes.find(a => a.id === v.object_type_attribute_id)
                    const attrType = attr ? (typeMap[attr.attribute_type_id] || attr.type || 'text') : 'text'
                    const allValuesForAttr = values.filter(v2 => v2.object_type_attribute_id === v.object_type_attribute_id)
                    const mc = attr?.maximum_cardinality ?? 1
                    const isMultiSelect = attrType === 'select' && (mc < 0 || mc > 1) && allValuesForAttr.length > 1
                    return (
                      <tr key={v.object_type_attribute_id} className={`border-b last:border-0 text-sm ${attr?.is_hidden ? 'opacity-50' : ''}`} data-testid="attribute-row">
                        <td className="px-4 py-3 text-muted-foreground">
                          <span className="inline-flex items-center gap-1">
                            {v.attribute_name || v.object_type_attribute_id}
                            {attr?.is_inherited && (
                              <>
                                <LucideIcon name="layers" size={14} className="text-muted-foreground" />
                                 <span className="text-xs text-muted-foreground/60">{interpolateDefault(t('browse.attributes.from_type', '(from {{name}})'), { name: attr.defining_object_type_name })}</span>
                              </>
                            )}
                            {attr?.is_hidden && (
                              <LucideIcon name="eye-off" size={14} className="text-muted-foreground" title={t('browse.attributes.hidden', 'Hidden attribute')} />
                            )}
                            {attr?.is_unique && (
                              <LucideIcon name="key" size={14} className="text-muted-foreground" title={t('browse.attributes.unique', 'Unique attribute')} />
                            )}
                          </span>
                        </td>
                        <td className="px-4 py-3">
                          {isMultiSelect ? (
                            <div className="flex flex-wrap gap-1">
                              {allValuesForAttr.map((mv) => {
                                const rawVal = mv.value_simple
                                const options = attr?.structure?.options || []
                                const match = options.find((opt, idx) => {
                                  const optValue = typeof opt === 'string' ? opt : (opt.value ?? opt.id ?? idx)
                                  return String(optValue) === String(rawVal)
                                })
                                const label = match ? (typeof match === 'string' ? match : (match.label ?? match.name ?? match.value)) : rawVal
                                return (
                                  <span key={mv.id} className="bg-muted rounded px-2 py-0.5 text-xs font-medium">
                                    {label ?? '—'}
                                  </span>
                                )
                              })}
                            </div>
                          ) : (
                            <TypedValue
                              rawValue={extractTypedValue(v, attrType) ?? '-'}
                              attrType={attrType}
                              attribute={attr}
                              typeMap={typeMap}
                              attributeTypes={attributeTypes}
                            />
                          )}
                        </td>
                      </tr>
                    )
                  })
                })()}
                {(() => {
                  const grouped = {}
                  for (const r of refs) {
                    const key = r.from_attribute_name || 'References'
                    if (!grouped[key]) grouped[key] = []
                    grouped[key].push(r)
                  }
                  return Object.entries(grouped).map(([attrName, refList]) => (
                    <tr key={attrName} className="border-b last:border-0 text-sm bg-blue-50/50 dark:bg-blue-950/20" data-testid="attribute-row">
                      <td className="px-4 py-3">
                        <span className="inline-flex items-center gap-1 rounded bg-blue-100 dark:bg-blue-900 px-1.5 py-0.5 text-xs font-medium text-blue-700 dark:text-blue-300 mr-1">
                           {t('browse.references.ref_badge', 'Ref')}
                        </span>
                        <span className="text-muted-foreground">{attrName}</span>
                      </td>
                      <td className="px-4 py-3 space-y-1">
                        {refList.map((r) => (
                          <div key={r.id}>
                            <Link to={`/browse/objects/${r.to_object_id}`} className="text-primary hover:underline">
                              {r.to_object_name || r.to_object_id}
                            </Link>
                          </div>
                        ))}
                      </td>
                    </tr>
                  ))
                })()}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* D-01/D-02: inline plugin sections after the last core card (the
          attributes/references card) and before the dialogs; the shared region
          renders nothing at zero contributions, so this page is unchanged then. */}
      <PluginSectionRegion
        family="objectDetailSections"
        headingLevel="h3"
        gapClass="space-y-4"
        componentProps={{ object: obj }}
      />

      <EditObjectModal
        object={obj}
        attributes={attributes}
        labelAttributeId={objectType?.label_attribute_id}
        attributeTypes={attributeTypes}
        attributeTypesLoading={attributeTypesLoading}
        attributesLoading={attributesLoading}
        values={values}
        refs={refs}
        open={editOpen}
        onClose={() => setEditOpen(false)}
        onSaved={handleSaved}
        isCreate={false}
      />
      <PermissionsDialog
        entityType="object"
        recordId={objectId}
        open={permDialogOpen}
        onClose={() => setPermDialogOpen(false)}
      />
    </div>
    </ErrorBoundary>
  )
}
