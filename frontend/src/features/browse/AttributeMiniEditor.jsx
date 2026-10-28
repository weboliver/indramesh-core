import { useState, useEffect, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../../shared/components/Button'
import TypeSelector from '../../shared/components/TypeSelector'
import TranslationPanel from '../../shared/components/TranslationPanel'
import { useUpdateObjectTypeAttribute, useCreateObjectTypeAttribute, useReferenceTypes, useStatuses } from '../../shared/hooks/use-other-resources'
import { useClassificationFamilies } from '../../shared/hooks/use-classification'
import { useTranslations } from '../../shared/hooks/use-translations'
import { showToast } from '../../shared/components/Toast'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { confirmDialog } from '../../shared/components/ConfirmDialog'

const MULTILINGUAL_ELIGIBLE_TYPES = ['text', 'select', 'status']

function getAttrTypeValue(attr, attributeTypes) {
  if (!attr) return ''
  const dataType = attr.attribute_type?.data_type
    || attributeTypes.find(at => at.id === attr.attribute_type_id)?.data_type
    || ''
  if (dataType === 'reference' && attr.referenced_object_type_id) {
    return `reference:${attr.referenced_object_type_id}`
  }
  return dataType
}

function attrTypeIdForValue(value, attributeTypes) {
  const found = attributeTypes.find(at => at.data_type === value)
  return found?.id || null
}

export default function AttributeMiniEditor({ attribute, attributeTypes, objectTypes, referenceObjectTypes = [], schemaId, schemas = [], open, onClose, onSaved, onCreated, isCreate = false, nextSort = 0 }) {
  const { t, i18n } = useTranslation()
  const updateAttribute = useUpdateObjectTypeAttribute()
  const createAttribute = useCreateObjectTypeAttribute()
  const { data: referenceTypes = [], isLoading: rtLoading, isError: rtError } = useReferenceTypes(0, 50, schemaId)
  const { data: schemaStatuses = [] } = useStatuses(schemaId, 0, 200)
  // Catalogue labels are resolved in the UI language, so a German session sees
  // "Vertraulich" while writing the same stable label id (D-13).
  const { data: classificationFamilies = [], isError: classificationError } = useClassificationFamilies(i18n.resolvedLanguage || 'en')

  const [activeTab, setActiveTab] = useState('General')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState(null)
  const [form, setForm] = useState({})
  const [dragOptionIndex, setDragOptionIndex] = useState(null)
  const [dragOverOptionIndex, setDragOverOptionIndex] = useState(null)
  const [panelOpen, setPanelOpen] = useState(false)
  const [translatingOptionId, setTranslatingOptionId] = useState(null)
  const newOptionRef = useRef(null)

  const dataType = attributeTypes.find(at => at.id === form.attribute_type_id)?.data_type
    || attribute?.attribute_type?.data_type
    || ''
  const parentObjectType = (objectTypes || []).find(ot => ot.id === attribute?.object_type_id)
  const parentMultilingual = parentObjectType?.multilingual || false
  const multilingualEligible = MULTILINGUAL_ELIGIBLE_TYPES.includes(dataType)
  // Labels sorted by position so a ranked family reads from least to strictest
  // (D-12); a family the operator emptied yields an empty list, not an error.
  function labelsOfFamily(key) {
    const family = classificationFamilies.find(item => item.key === key)
    return [...(family?.labels || [])].sort((a, b) => a.position - b.position)
  }
  const confidentialityLabels = labelsOfFamily('confidentiality')
  const exportControlLabels = labelsOfFamily('export_control')

  const { data: existingTranslations = [] } = useTranslations('object_type_attribute', attribute?.id, {
    enabled: open && multilingualEligible && !!form.multilingual && !isCreate,
  })

  async function handleMultilingualToggle(checked) {
    if (!parentMultilingual) return
    if (!checked && form.multilingual) {
      const count = existingTranslations.length
      const message = selectPluralKey('browse.attribute.disable_multilingual', count) === 'browse.attribute.disable_multilingual_one'
        ? interpolateDefault(t('browse.attribute.disable_multilingual_one', 'Disable multilingual?\n\nThis will hide {{count}} existing translation. They are kept, not deleted, and will reappear if you re-enable it.'), { count })
        : interpolateDefault(t('browse.attribute.disable_multilingual_other', 'Disable multilingual?\n\nThis will hide {{count}} existing translations. They are kept, not deleted, and will reappear if you re-enable it.'), { count })
      const confirmed = await confirmDialog({ message, danger: false })
      if (!confirmed) return
      updateField('multilingual', false)
      return
    }
    if (checked) {
      updateField('multilingual', true)
      showToast(t('browse.attribute.multilingual_enabled', 'Multilingual enabled. You can now translate name and description.'))
      return
    }
    updateField('multilingual', false)
  }

  useEffect(() => {
    if (attribute && open) {
      setForm({
        name: attribute.name || '',
        description: attribute.description || '',
        attribute_type_id: attribute.attribute_type_id || '',
        is_required: attribute.is_required || false,
        minimum_cardinality: attribute.minimum_cardinality ?? 0,
        maximum_cardinality: attribute.maximum_cardinality ?? -1,
        is_unique: attribute.is_unique || false,
        is_hidden: attribute.is_hidden || false,
        default_value: attribute.default_value || '',
        referenced_object_type_id: attribute.referenced_object_type_id || null,
        reference_type_id: attribute.reference_type_id || null,
        multilingual: attribute.multilingual || false,
        // Keep the option's stable `id` (Phase 223) alongside its value —
        // object_values.value_simple and select_option translations both
        // reference it. Flattening to bare strings here previously meant
        // ANY save (even unrelated fields) resubmitted the options as
        // id-less strings, which ensure_option_ids() treats as brand-new
        // options and mints fresh random ids for — silently orphaning every
        // existing object value that referenced the old ids.
        options: (attribute.structure?.options || []).map(opt =>
          typeof opt === 'string'
            ? { id: null, value: opt }
            : { id: opt.id ?? null, value: opt.value ?? opt.label ?? '' }
        ),
        statusFilterIds: (attribute.structure?.status_options || []).map(o => o.id),
        format: attribute.structure?.format || 'simpletext',
        confidentiality_id: attribute.confidentiality?.label_id || '',
        personal_data: attribute.personal_data || false,
        export_control_ids: (attribute.export_control || []).map(label => label.label_id),
      })
      setError(null)
      setActiveTab('General')
      setTranslatingOptionId(null)
    }
  }, [attribute, open])

  function updateField(field, value) {
    setForm(prev => ({ ...prev, [field]: value }))
  }

  function addOption() {
    setForm(prev => ({ ...prev, options: [...(prev.options || []), { id: null, value: '' }] }))
    setTimeout(() => newOptionRef.current?.focus(), 0)
  }

  function updateOption(index, value) {
    setForm(prev => {
      const opts = [...(prev.options || [])]
      opts[index] = { ...opts[index], value }
      return { ...prev, options: opts }
    })
  }

  async function removeOption(index) {
    const label = (form.options && form.options[index]?.value) || '(empty)'
    if (!(await confirmDialog({ message: interpolateDefault(t('browse.attribute.delete_option', 'Delete option "{{label}}"?'), { label }), danger: true }))) return
    setForm(prev => {
      const opts = [...(prev.options || [])]
      opts.splice(index, 1)
      return { ...prev, options: opts }
    })
  }

  function toggleStatusFilter(statusId) {
    setForm(prev => {
      const current = prev.statusFilterIds || []
      const next = current.includes(statusId)
        ? current.filter(id => id !== statusId)
        : [...current, statusId]
      return { ...prev, statusFilterIds: next }
    })
  }

  function toggleExportControl(labelId) {
    setForm(prev => {
      const current = prev.export_control_ids || []
      const next = current.includes(labelId)
        ? current.filter(id => id !== labelId)
        : [...current, labelId]
      return { ...prev, export_control_ids: next }
    })
  }

  function handleOptionDragStart(index) {
    setDragOptionIndex(index)
  }

  function handleOptionDragOver(e, index) {
    e.preventDefault()
    setDragOverOptionIndex(index)
  }

  function handleOptionDragLeave() {
    setDragOverOptionIndex(null)
  }

  function handleOptionDrop(index) {
    const from = dragOptionIndex
    setDragOverOptionIndex(null)
    if (from === null || from === index) return
    setForm(prev => {
      const opts = [...(prev.options || [])]
      const [moved] = opts.splice(from, 1)
      opts.splice(index, 0, moved)
      return { ...prev, options: opts }
    })
    setDragOptionIndex(null)
  }

  async function handleSave() {
    if (isCreate && (!form.name?.trim() || !form.attribute_type_id)) return
    setSaving(true)
    setError(null)
    try {
      if (isCreate) {
        const newOptions = (form.options || [])
          .filter(o => o.value)
          .map((o, idx) => (o.id ? { id: o.id, value: o.value, position: idx } : { value: o.value, position: idx }))
        const newStatusIds = form.statusFilterIds || []
        const newStatusOptions = schemaStatuses
          .filter(s => newStatusIds.includes(s.id))
          .map(s => ({
            id: s.id,
            name: s.name,
            color: s.color,
            status_category: s.status_category,
          }))

        const payload = {
          object_type_id: attribute.object_type_id,
          name: form.name.trim(),
          attribute_type_id: form.attribute_type_id,
          description: form.description || null,
          is_required: form.is_required,
          is_unique: form.is_unique,
          is_hidden: form.is_hidden,
          default_value: form.default_value || null,
          referenced_object_type_id: form.referenced_object_type_id || null,
          reference_type_id: form.reference_type_id || null,
          minimum_cardinality: form.minimum_cardinality,
          maximum_cardinality: form.maximum_cardinality,
          multilingual: form.multilingual,
          sort: nextSort,
          confidentiality: form.confidentiality_id || null,
          personal_data: form.personal_data || false,
          export_control: form.export_control_ids || [],
        }
        if (newOptions.length > 0 || newStatusOptions.length > 0) {
          payload.structure = {
            ...(newOptions.length > 0 ? { options: newOptions } : {}),
            ...(newStatusOptions.length > 0 ? { status_options: newStatusOptions } : {}),
          }
        }
        if (dataType === 'textarea' && form.format && form.format !== 'simpletext') {
          payload.structure = { ...(payload.structure || {}), format: form.format }
        }

        const created = await createAttribute.mutateAsync(payload)
        onSaved?.()
        onCreated?.(created)
        return
      }

      const payload = {}
      if (form.name !== attribute.name) payload.name = form.name
      if (form.description !== (attribute.description || '')) payload.description = form.description || null
      if (form.is_required !== attribute.is_required) payload.is_required = form.is_required
      if (form.is_unique !== attribute.is_unique) payload.is_unique = form.is_unique
      if (form.is_hidden !== attribute.is_hidden) payload.is_hidden = form.is_hidden
      if (form.referenced_object_type_id !== (attribute.referenced_object_type_id || null)) payload.referenced_object_type_id = form.referenced_object_type_id
      if (form.reference_type_id !== (attribute.reference_type_id || null)) payload.reference_type_id = form.reference_type_id || null
      if (form.minimum_cardinality !== (attribute.minimum_cardinality ?? 0)) payload.minimum_cardinality = form.minimum_cardinality
      if (form.maximum_cardinality !== (attribute.maximum_cardinality ?? -1)) payload.maximum_cardinality = form.maximum_cardinality
      if (form.default_value !== (attribute.default_value || '')) payload.default_value = form.default_value || null
      if (form.attribute_type_id !== attribute.attribute_type_id) payload.attribute_type_id = form.attribute_type_id
      if (form.multilingual !== (attribute.multilingual || false)) payload.multilingual = form.multilingual
      // Classification is diffed field by field: an unchanged save sends none of
      // them, so another editor's untouched family is never overwritten (CLASS-03).
      if ((form.confidentiality_id || '') !== (attribute.confidentiality?.label_id || '')) {
        payload.confidentiality = form.confidentiality_id || null
      }
      if ((form.personal_data || false) !== (attribute.personal_data || false)) {
        payload.personal_data = form.personal_data || false
      }
      const currentExportIds = (attribute.export_control || []).map(label => label.label_id)
      const formExportIds = form.export_control_ids || []
      // Order-insensitive compare: the multi-select has no meaningful order.
      if (JSON.stringify([...formExportIds].sort()) !== JSON.stringify([...currentExportIds].sort())) {
        payload.export_control = formExportIds
      }

      const currentStructure = attribute.structure || {}
      // Recompute position from array order (drag-reorder never wrote a
      // position field per-row); drop blank rows the user left empty;
      // preserve existing ids verbatim so ensure_option_ids() treats
      // unchanged options as no-ops instead of minting fresh ids for them.
      const newOptions = (form.options || [])
        .filter(o => o.value)
        .map((o, idx) => (o.id ? { id: o.id, value: o.value, position: idx } : { value: o.value, position: idx }))
      const currentComparable = (currentStructure.options || []).map((o, idx) =>
        (o.id ? { id: o.id, value: o.value, position: idx } : { value: o.value, position: idx })
      )
      if (JSON.stringify(newOptions) !== JSON.stringify(currentComparable)) {
        payload.structure = { ...currentStructure, options: newOptions }
      }

      const currentStatusIds = (currentStructure.status_options || []).map(o => o.id)
      const newStatusIds = form.statusFilterIds || []
      if (JSON.stringify([...currentStatusIds].sort()) !== JSON.stringify([...newStatusIds].sort())) {
        const newStatusOptions = schemaStatuses
          .filter(s => newStatusIds.includes(s.id))
          .map(s => ({
            id: s.id,
            name: s.name,
            color: s.color,
            status_category: s.status_category,
          }))
        payload.structure = { ...(payload.structure || currentStructure), status_options: newStatusOptions }
      }

      if ((form.format || 'simpletext') !== (currentStructure.format || 'simpletext')) {
        payload.structure = {
          ...(payload.structure || currentStructure),
          format: form.format || 'simpletext',
        }
      }

      if (Object.keys(payload).length === 0) {
        onClose()
        return
      }

      await updateAttribute.mutateAsync({ id: attribute.id, ...payload })
      onSaved?.()
      onClose()
    } catch (err) {
      setError(t('browse.errors.save_try_again', 'Failed to save. Try again.'))
      console.error('AttributeMiniEditor save failed:', err)
    } finally {
      setSaving(false)
    }
  }

  if (!open) return null

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center"
      onKeyDown={(e) => {
        if (e.key === 'Escape') onClose()
        if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) handleSave()
      }}
    >
      <div
        className="fixed inset-0 bg-black/50"
        onClick={onClose}
        aria-hidden="true"
      />

      <div
        role="dialog"
        aria-label={isCreate ? t('browse.attributes.new', 'New Attribute') : interpolateDefault(t('browse.attribute.edit_aria', 'Edit attribute: {{name}}'), { name: attribute?.name || '' })}
        className="relative z-50 w-full max-w-lg max-h-[85vh] flex flex-col rounded-lg border bg-card shadow-lg"
      >
        <div className="flex items-center justify-between border-b bg-muted/30 px-4 py-3">
        <h2 className="text-lg font-semibold">{isCreate ? t('browse.attributes.new', 'New Attribute') : interpolateDefault(t('browse.attribute.edit_title', 'Edit: {{name}}'), { name: attribute?.name })}</h2>
          <Button
            type="button"
            size="icon"
            variant="ghost"
            onClick={onClose}
            className="h-7 w-7 hover:bg-muted"
            leftIcon="x"
            aria-label={t('common.close_editor', 'Close editor')}
          />
        </div>

        <div className="flex border-b" role="tablist">
          {(() => {
             const tabs = ['General', 'Constraints', 'Display']
             if (dataType === 'select') tabs.push('Options')
             if (dataType === 'textarea') tabs.push('Format')
            return tabs
          })().map((tab) => (
            // Tab buttons intentionally NOT migrated to <Button> — custom tab UI with active-state border.
            <button
              key={tab}
              role="tab"
              aria-selected={activeTab === tab}
              onClick={() => setActiveTab(tab)}
              className={`flex-1 px-3 py-2 text-sm font-medium border-b-2 transition-colors ${
                activeTab === tab
                  ? 'text-primary border-primary'
                  : 'text-muted-foreground border-transparent hover:text-foreground'
              }`}
            >
              {tab}
            </button>
          ))}
        </div>

        <div className="flex-1 overflow-y-auto px-4 py-4 space-y-4">
          {activeTab === 'General' && (
            <>
              <div className="space-y-1">
              <label className="text-sm font-medium">{t('common.name', 'Name')}</label>
                <input
                  type="text"
                  value={form.name}
                  onChange={(e) => updateField('name', e.target.value)}
                  className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                />
              </div>

              <div className="space-y-1">
              <label className="text-sm font-medium">{t('browse.attribute.data_type', 'Data Type')}</label>
                <TypeSelector
                  value={getAttrTypeValue(attribute, attributeTypes)}
                  onChange={(val) => {
                    if (val && val.startsWith('reference:')) {
                      const refOtId = val.split(':')[1]
                      const refType = attributeTypes.find(at => at.data_type === 'reference')
                      if (refType) updateField('attribute_type_id', refType.id)
                      updateField('referenced_object_type_id', refOtId || null)
                    } else {
                      const typeId = attrTypeIdForValue(val, attributeTypes)
                      if (typeId) updateField('attribute_type_id', typeId)
                      updateField('referenced_object_type_id', null)
                    }
                  }}
                  objectTypes={referenceObjectTypes}
                  schemas={schemas}
                  currentSchemaId={schemaId}
                />
              </div>

              {(() => {
                if (dataType !== 'reference') return null
                if (rtError) return <p className="text-xs text-destructive">{t('browse.attribute.reference_types_failed', 'Could not load reference types')}</p>
                return (
                  <div className="space-y-1">
                    <label className="text-sm font-medium">{t('browse.attribute.reference_type', 'Reference Type')}</label>
                    <p className="text-xs text-muted-foreground">{t('browse.attribute.reference_type_hint', 'Categorises the relationship this attribute represents')}</p>
                    <select
                      value={form.reference_type_id || ''}
                      onChange={(e) => updateField('reference_type_id', e.target.value || null)}
                      className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                    >
                      {rtLoading ? (
                        <option disabled>{t('common.loading', 'Loading…')}</option>
                      ) : (
                        <>
                          <option value="">{t('common.none', '— None —')}</option>
                          {referenceTypes.map(rt => (
                            <option key={rt.id} value={rt.id}>{rt.reference}</option>
                          ))}
                        </>
                      )}
                    </select>
                  </div>
                )
              })()}

              <div className="space-y-1">
              <label className="text-sm font-medium">{t('common.description', 'Description')}</label>
                <textarea
                  value={form.description}
                  onChange={(e) => updateField('description', e.target.value)}
                  rows={3}
                placeholder={t('browse.attribute.description_placeholder', 'Describe this attribute...')}
                  className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary resize-vertical"
                />
              </div>

              {multilingualEligible && (
                <div className="space-y-1">
                  <label className="flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={form.multilingual}
                      disabled={!parentMultilingual}
                      onChange={(e) => handleMultilingualToggle(e.target.checked)}
                      className={!parentMultilingual ? 'opacity-50 cursor-not-allowed' : ''}
                    />
                    {t('common.multilingual', 'Multilingual')}
                  </label>
                  {!parentMultilingual ? (
                    <p className="text-xs text-muted-foreground">{t('browse.attribute.enable_object_type_first', 'Enable multilingual on the ObjectType first')}</p>
                  ) : (
                    <p className="text-xs text-muted-foreground">{t('browse.attribute.multilingual_hint', "Allow translating this attribute's name and description")}</p>
                  )}
                  {!isCreate && form.multilingual && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      leftIcon="globe"
                    aria-label={t('browse.attribute.translate_name_description', 'Translate name and description')}
                      data-testid="lang-globe-attribute"
                      onClick={() => setPanelOpen(true)}
                    >
                    {t('browse.attribute.translate', 'Translate')}
                    </Button>
                  )}
                </div>
              )}

              <div className="border-t pt-3 mt-2 space-y-2">
                <h5 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1">
                  {t('browse.attribute.classification', 'Classification')}
                </h5>
                {classificationError ? (
                  <p className="text-xs text-destructive">{t('browse.attribute.classification_load_failed', 'Could not load classification labels')}</p>
                ) : (
                  <>
                    <div className="space-y-1">
                      <label className="text-sm font-medium">{t('browse.attribute.confidentiality', 'Confidentiality')}</label>
                      <select
                        data-testid="attr-confidentiality-select"
                        value={form.confidentiality_id || ''}
                        onChange={(e) => updateField('confidentiality_id', e.target.value)}
                        className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                      >
                        <option value="">{t('browse.attribute.classification_not_classified', 'Not classified')}</option>
                        {confidentialityLabels.map(label => (
                          <option key={label.id} value={label.id}>{label.name}</option>
                        ))}
                      </select>
                    </div>

                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        data-testid="attr-personal-data-toggle"
                        checked={form.personal_data || false}
                        onChange={(e) => updateField('personal_data', e.target.checked)}
                        className="rounded border-input"
                      />
                      {t('browse.attribute.personal_data', 'Personal data')}
                    </label>

                    <div className="space-y-1">
                      <label className="text-sm font-medium">{t('browse.attribute.export_control', 'Export control')}</label>
                      <div className="max-h-40 overflow-y-auto rounded border border-input divide-y">
                        {exportControlLabels.map(label => (
                          <label key={label.id} className="flex items-center gap-2 px-2 py-1.5 text-sm hover:bg-muted/50 cursor-pointer">
                            <input
                              type="checkbox"
                              data-testid={`attr-export-control-${label.key}`}
                              checked={(form.export_control_ids || []).includes(label.id)}
                              onChange={() => toggleExportControl(label.id)}
                              className="rounded border-input"
                            />
                            <span className="truncate">{label.name}</span>
                          </label>
                        ))}
                      </div>
                    </div>
                  </>
                )}
              </div>
            </>
          )}

          {activeTab === 'Constraints' && (() => {
            const supportsCardinality = dataType === 'select' || dataType === 'reference' || dataType === 'status'
            return (
              <>
                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.is_required}
                    onChange={(e) => updateField('is_required', e.target.checked)}
                    className="rounded border-input"
                  />
                  {t('common.required', 'Required')}
                </label>

                {supportsCardinality && (
                  <div className="grid grid-cols-2 gap-3">
                    <div className="space-y-1">
                        <label className="text-sm font-medium">{t('common.minimum', 'Min')}</label>
                      <input
                        type="number"
                        value={form.minimum_cardinality}
                        onChange={(e) => updateField('minimum_cardinality', parseInt(e.target.value) || 0)}
                        min={0}
                        className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                      />
                    </div>
                    <div className="space-y-1">
                        <label className="text-sm font-medium">{t('common.maximum', 'Max')}</label>
                      <input
                        type="number"
                        value={form.maximum_cardinality}
                        onChange={(e) => {
                          const val = parseInt(e.target.value)
                          updateField('maximum_cardinality', isNaN(val) ? -1 : val)
                        }}
                        min={-1}
                        className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                      />
                        <p className="text-xs text-muted-foreground">{t('browse.attribute.unlimited_hint', '-1 = unlimited')}</p>
                    </div>
                  </div>
                )}

                <label className="flex items-center gap-2 text-sm">
                  <input
                    type="checkbox"
                    checked={form.is_unique}
                    onChange={(e) => updateField('is_unique', e.target.checked)}
                    className="rounded border-input"
                  />
                  {t('browse.attribute.unique_value', 'Unique value')}
                </label>

                {/* Status filter — only for STATUS type attributes */}
                {dataType === 'status' && attribute?.object_type_id && (
                  <div className="border-t pt-3 mt-2 space-y-2">
                    <h5 className="text-xs font-semibold text-muted-foreground uppercase tracking-wider mb-1">
                      {t('browse.attribute.status_filter', 'Status Filter')}
                    </h5>
                    <p className="text-xs text-muted-foreground">
                      {t('browse.attribute.status_filter_hint', 'Only checked statuses are offered in selection dropdowns for this attribute — an empty selection means no status can be chosen at all.')}
                    </p>
                    {schemaStatuses.length === 0 ? (
                      <p className="text-xs text-muted-foreground italic">{t('browse.attribute.no_statuses', 'No statuses defined in this schema yet.')}</p>
                    ) : (
                      <>
                        <div className="flex gap-3 text-xs">
                          <button
                            type="button"
                            className="text-primary hover:underline"
                            onClick={() => updateField('statusFilterIds', schemaStatuses.map(s => s.id))}
                          >
                            {t('common.select_all', 'Select all')}
                          </button>
                          <button
                            type="button"
                            className="text-primary hover:underline"
                            onClick={() => updateField('statusFilterIds', [])}
                          >
                            {t('common.clear', 'Clear')}
                          </button>
                        </div>
                        <div className="max-h-40 overflow-y-auto rounded border border-input divide-y">
                          {schemaStatuses.map(s => (
                            <label key={s.id} className="flex items-center gap-2 px-2 py-1.5 text-sm hover:bg-muted/50 cursor-pointer">
                              <input
                                type="checkbox"
                                checked={(form.statusFilterIds || []).includes(s.id)}
                                onChange={() => toggleStatusFilter(s.id)}
                                className="rounded border-input"
                              />
                              {s.color && (
                                <span
                                  className="h-2.5 w-2.5 rounded-full shrink-0"
                                  style={{ backgroundColor: s.color }}
                                  aria-hidden="true"
                                />
                              )}
                              <span className="truncate">{s.name}</span>
                            </label>
                          ))}
                        </div>
                        <p className="text-xs text-muted-foreground">
                          {selectPluralKey('browse.attribute.statuses_selected', (form.statusFilterIds || []).length) === 'browse.attribute.statuses_selected_one'
                             ? interpolateDefault(t('browse.attribute.statuses_selected_one', '{{count}} of {{total}} status selected.'), { count: (form.statusFilterIds || []).length, total: schemaStatuses.length })
                             : interpolateDefault(t('browse.attribute.statuses_selected_other', '{{count}} of {{total}} statuses selected.'), { count: (form.statusFilterIds || []).length, total: schemaStatuses.length })}
                        </p>
                      </>
                    )}
                  </div>
                )}
              </>
            )
          })()}

          {activeTab === 'Display' && (
            <>
              <label className="flex items-center gap-2 text-sm">
                <input
                  type="checkbox"
                  checked={form.is_hidden}
                  onChange={(e) => updateField('is_hidden', e.target.checked)}
                  className="rounded border-input"
                />
                  {t('common.hidden', 'Hidden')}
              </label>

              <div className="space-y-1">
                  <label className="text-sm font-medium">{t('common.default_value', 'Default Value')}</label>
                <input
                  type="text"
                  value={form.default_value}
                  onChange={(e) => updateField('default_value', e.target.value)}
                  placeholder={t('common.no_default', 'No default')}
                  className="w-full rounded border border-input bg-background px-2 py-1.5 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                />
              </div>
            </>
          )}

          {activeTab === 'Options' && (
            <div className="space-y-2">
              {multilingualEligible && parentMultilingual && !form.multilingual && (
                <p className="text-xs text-muted-foreground">
                  {t('browse.attribute.enable_multilingual_options', 'Enable Multilingual on the General tab to translate these options')}
                </p>
              )}
              {(form.options || []).length === 0 ? (
                <div className="text-center py-4">
                  <p className="text-sm font-medium text-muted-foreground">{t('browse.attribute.no_options', 'No options')}</p>
                  <p className="text-xs text-muted-foreground mt-1">{t('browse.attribute.add_options_hint', 'Add options for users to select from this attribute.')}</p>
                </div>
              ) : (
                <div className="space-y-1">
                  {(form.options || []).map((opt, idx) => (
                    <div
                      key={idx}
                      draggable
                      onDragStart={() => handleOptionDragStart(idx)}
                      onDragOver={(e) => handleOptionDragOver(e, idx)}
                      onDragLeave={handleOptionDragLeave}
                      onDrop={() => handleOptionDrop(idx)}
                      onDragEnd={() => { setDragOptionIndex(null); setDragOverOptionIndex(null) }}
                      className={`flex items-center gap-2 rounded px-2 py-1 ${
                        dragOverOptionIndex === idx ? 'border-t-2 border-t-primary' : 'border border-transparent'
                      }`}
                    >
                      <button
                        type="button"
                        className="opacity-40 hover:opacity-100 cursor-grab active:cursor-grabbing shrink-0"
                        aria-label={t('browse.attribute.drag_reorder', 'Drag to reorder')}
                        tabIndex={0}
                      >
                        <svg width="16" height="16" viewBox="0 0 16 16" fill="currentColor">
                          <circle cx="5" cy="4" r="1.5" />
                          <circle cx="11" cy="4" r="1.5" />
                          <circle cx="5" cy="8" r="1.5" />
                          <circle cx="11" cy="8" r="1.5" />
                          <circle cx="5" cy="12" r="1.5" />
                          <circle cx="11" cy="12" r="1.5" />
                        </svg>
                      </button>
                      <input
                        ref={idx === (form.options || []).length - 1 ? newOptionRef : null}
                        type="text"
                        value={opt.value}
                        onChange={(e) => updateOption(idx, e.target.value)}
                        placeholder={t('browse.attribute.option_label', 'Option label')}
                        className="flex-1 rounded border border-input bg-background px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                      />
                      {parentMultilingual && form.multilingual && opt.id && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          leftIcon="globe"
                          className="shrink-0"
                          aria-label={interpolateDefault(t('browse.attribute.translate_option', 'Translate option {{value}}'), { value: opt.value })}
                          data-testid={`lang-globe-option-${idx}`}
                          onClick={() => setTranslatingOptionId(opt.id)}
                        />
                      )}
                      {parentMultilingual && form.multilingual && !opt.id && (
                        <p className="text-xs text-muted-foreground shrink-0">{t('browse.attribute.save_to_translate', 'Save to translate')}</p>
                      )}
                      <Button
                        type="button"
                        variant="destructive"
                        size="sm"
                        onClick={() => removeOption(idx)}
                        className="shrink-0"
                      >
                        {t('browse.attribute.delete_option_button', 'Delete Option')}
                      </Button>
                    </div>
                  ))}
                </div>
              )}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={addOption}
              >
                {t('browse.attribute.add_option', '+ Add Option')}
              </Button>
            </div>
          )}

          {activeTab === 'Format' && (
            <fieldset className="space-y-3">
              <legend className="text-sm font-medium">{t('browse.attribute.format', 'Format')}</legend>
              <p className="text-xs text-muted-foreground">
                {t('browse.attribute.format_hint', 'Format affects display only; values are always edited as plain text.')}
              </p>
              {[
                ['simpletext', t('browse.attribute.format_simpletext', 'Plain text')],
                ['markdown', t('browse.attribute.format_markdown', 'Markdown')],
                ['html', t('browse.attribute.format_html', 'HTML')],
              ].map(([value, label]) => (
                <label key={value} className="flex items-center gap-2 text-sm">
                  <input
                    type="radio"
                    name="textarea-format"
                    value={value}
                    checked={(form.format || 'simpletext') === value}
                    onChange={(e) => updateField('format', e.target.value)}
                  />
                  {label}
                </label>
              ))}
            </fieldset>
          )}
        </div>

        <div className="border-t px-4 py-3 space-y-2">
          {error && (
            <p className="text-sm text-destructive">{error}</p>
          )}
          <div className="flex gap-2 justify-end">
            <Button
              type="button"
              variant="ghost"
              size="sm"
              onClick={onClose}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              type="button"
              variant="primary"
              size="sm"
              onClick={handleSave}
              disabled={saving || (isCreate && (!form.name?.trim() || !form.attribute_type_id))}
              loading={saving}
            >
              {isCreate ? t('common.create', 'Create') : t('common.save_changes', 'Save changes')}
            </Button>
          </div>
        </div>
      </div>
      <TranslationPanel
        entityType="object_type_attribute"
        entityId={attribute?.id}
        schemaId={schemaId}
        fields={[{ key: 'name' }, { key: 'description' }]}
        fieldValues={{ name: form.name, description: form.description }}
        open={panelOpen}
        onClose={() => setPanelOpen(false)}
      />
      <TranslationPanel
        entityType="select_option"
        entityId={translatingOptionId}
        schemaId={schemaId}
        fields={[{ key: 'value', label: 'Label' }]}
        fieldValues={{ value: (form.options || []).find(o => o.id === translatingOptionId)?.value ?? '' }}
        open={!!translatingOptionId}
        onClose={() => setTranslatingOptionId(null)}
      />
    </div>,
    document.body
  )
}
