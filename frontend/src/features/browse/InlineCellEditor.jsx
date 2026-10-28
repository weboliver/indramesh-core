import React, { useState, useRef, useEffect } from 'react'
import DatePicker from './DatePicker'
import { Button } from '../../shared/components/Button'
import { getOptionValue, getOptionLabel, findOptionByValue } from '../../shared/lib/string-utils'
import { isValidUrl, isValidEmail } from '../../shared/lib/link-utils'
import TranslationPanel from '../../shared/components/TranslationPanel'
import { useTranslation } from 'react-i18next'

// 'select' intentionally excluded: extractTypedValue() returns the option's
// raw stored value/id for select attributes (no label lookup — that only
// happens in InlineCellValue's display layer), so translating "value" here
// silently overwrites it with untranslatable raw text that can no longer
// match any structure.options entry. Backend V-05 still permits it — the
// real fix is translating structure.options entries once via select_option
// translations in the attribute editor, not per object value.
const MULTILINGUAL_ELIGIBLE_TYPES = new Set(['text', 'status'])

function InlineCellEditor({ value, attrType, attribute, onSave, onCancel, objectId, allValues, onAddValue, onRemoveValue, valueId, schemaId, lang, readOnly = false, showInlineTranslateButton = true }) {
  const { t } = useTranslation()
  const [editValue, setEditValue] = useState(value ?? '')
  const [saving, setSaving] = useState(false)
  const [saveSuccess, setSaveSuccess] = useState(false)
  const [error, setError] = useState(null)
  const [searchQuery, setSearchQuery] = useState('')
  const [searchOpen, setSearchOpen] = useState(false)
  const [translationPanelOpen, setTranslationPanelOpen] = useState(false)
  const inputRef = useRef(null)

  // UX-05 entry point 5: ObjectValue translation editing — only offered when
  // this attribute's data_type is one of the translatable types and the
  // attribute itself is opted into multilingual (backend V-05 requires the
  // owning ObjectType to also be multilingual before attribute.multilingual
  // can be set true, so checking the attribute flag alone is sufficient here).
  const canTranslateValue = MULTILINGUAL_ELIGIBLE_TYPES.has(attrType) && !!attribute?.multilingual && !!valueId
  const translationBaseValue = value && typeof value === 'object'
    ? (value.name ?? value.value_simple ?? '')
    : (value ?? '')

  const mc = attribute?.maximum_cardinality ?? 1
  const isMulti = attrType === 'select' && (mc < 0 || mc > 1)
  const [addSearchQuery, setAddSearchQuery] = useState('')
  const [addSearchOpen, setAddSearchOpen] = useState(false)
  const [pendingRemove, setPendingRemove] = useState(new Set())
  const addInputRef = useRef(null)

  useEffect(() => {
    if (inputRef.current && attrType !== 'boolean') {
      inputRef.current.focus()
      if (inputRef.current.select) inputRef.current.select()
    }
  }, [attrType])

  const baseInputClass = `w-full rounded border px-2 py-1 text-sm bg-background ${error ? 'border-destructive' : 'border-input'} focus:outline-none focus:ring-1 focus:ring-primary`

  const handleSave = async (newValue) => {
    if (readOnly) return
    if (attrType === 'url' && newValue) {
      if (!isValidUrl(newValue)) {
        setError(t('browse.validation.valid_url', 'Enter a valid URL starting with http:// or https://'))
        return
      }
    }
    if (attrType === 'email' && newValue) {
      if (!isValidEmail(newValue)) {
        setError(t('browse.validation.valid_email', 'Enter a valid email address'))
        return
      }
    }
    setSaving(true)
    setError(null)
    try {
      await onSave(newValue)
      setSaveSuccess(true)
      setTimeout(() => setSaveSuccess(false), 1500)
    } catch (err) {
      setError(err?.response?.data?.detail || t('browse.errors.save_failed', 'Save failed'))
    } finally {
      setSaving(false)
    }
  }

  const handleKeyDown = (e) => {
    if (e.key === 'Enter') { e.preventDefault(); handleSave(editValue) }
    else if (e.key === 'Escape') { e.preventDefault(); onCancel() }
  }

  const handleBlur = () => {
    if (isMulti || attrType === 'select' || attrType === 'status') return
    if (saving) return
    // Moving focus into the translation panel's own inputs (a real input,
    // not a button we can guard with onMouseDown preventDefault) blurs this
    // field legitimately — don't let that trigger an unrelated base-value
    // autosave, whose onSuccess closes cell-edit mode and unmounts the
    // panel we just opened out from under the user.
    if (translationPanelOpen) return
    handleSave(editValue)
  }

  const handleAddValue = async (optionValue) => {
    if (readOnly) return
    if (onAddValue) {
      try {
        await onAddValue(attribute.id, optionValue)
      } catch {
        setError(t('browse.errors.update_value_failed', 'Failed to update value.'))
      }
    }
    setAddSearchQuery('')
    setAddSearchOpen(false)
  }

  const handleRemoveValue = async (valueId) => {
    setPendingRemove(prev => new Set(prev).add(valueId))
    if (onRemoveValue) {
      try {
        await onRemoveValue(valueId)
      } catch {
        setError(t('browse.errors.update_value_failed', 'Failed to update value.'))
        setPendingRemove(prev => { const next = new Set(prev); next.delete(valueId); return next })
      }
    }
  }

  const statusRow = (
    <div className="flex items-center gap-1.5 mt-1">
      {saving && <span className="text-xs text-muted-foreground">{t('common.saving', 'Saving...')}</span>}
      {saveSuccess && <span className="text-xs text-green-600 dark:text-green-400">{t('common.saved', '✓ Saved')}</span>}
      {error && <span className="text-xs text-destructive">{error}</span>}
      {canTranslateValue && showInlineTranslateButton && (
        <Button
          type="button"
          variant="ghost"
          size="icon"
          leftIcon="globe"
          aria-label={t('browse.values.translate', 'Translate value')}
          title={t('browse.values.translate', 'Translate value')}
          data-testid="lang-globe-value"
          disabled={readOnly}
          // Prevent the click from shifting focus off the active input — a
          // default mousedown-triggered blur fires handleBlur's autosave,
          // whose onSuccess (ObjectDetailPanel's updateMutation) closes the
          // cell edit mode and unmounts this component (and the panel we're
          // about to open) out from under the click.
          onMouseDown={(e) => e.preventDefault()}
          onClick={(e) => { e.stopPropagation(); setTranslationPanelOpen(true) }}
        />
      )}
      <Button type="button" variant="ghost" size="sm" disabled={readOnly} onClick={onCancel} className="ml-auto" aria-label={t('common.cancel_edit', 'Cancel edit')}>
        {t('common.cancel', 'Cancel')}
      </Button>
    </div>
  )

  const translationPanel = canTranslateValue ? (
    <TranslationPanel
      entityType="object_value"
      entityId={valueId}
      schemaId={schemaId}
      fields={[{ key: 'value' }]}
      fieldValues={{ value: translationBaseValue }}
      open={translationPanelOpen}
      onClose={() => setTranslationPanelOpen(false)}
      focusLang={lang}
    />
  ) : null

  if (attrType === 'boolean') {
    return (
      <div className="flex items-center gap-2" data-testid={`cell-editor-${objectId}-${attribute.id}`} onClick={e => e.stopPropagation()}>
        {/* Boolean switch intentionally NOT migrated to <Button> — custom switch UI with translate transform */}
        <button type="button" role="switch" aria-checked={editValue === true}
          disabled={readOnly}
          onClick={() => { const nv = editValue === true ? false : true; setEditValue(nv); handleSave(nv) }}
          className={`relative inline-flex h-4 w-8 shrink-0 cursor-pointer rounded-full transition-colors ${editValue === true ? 'bg-primary' : 'bg-input'}`}
        >
          <span className={`pointer-events-none inline-block h-3 w-3 rounded-full bg-white shadow transform ring-0 transition-transform mt-0.5 ${editValue === true ? 'translate-x-4' : 'translate-x-0.5'}`} />
        </button>
        {saving && <span className="text-xs text-muted-foreground">{t('common.saving', 'Saving...')}</span>}
        {saveSuccess && <span className="text-xs text-green-600">{t('common.saved_short', '✓')}</span>}
        {error && <span className="text-xs text-destructive">{error}</span>}
        <Button type="button" variant="ghost" size="sm" onClick={onCancel} aria-label={t('common.cancel_edit', 'Cancel edit')}>✕</Button>
      </div>
    )
  }

  if (attrType === 'datetime') {
    return (
      <div data-testid={`cell-editor-${objectId}-${attribute.id}`} onClick={e => e.stopPropagation()}>
        <div className="flex flex-col gap-2">
          <div className={readOnly ? 'pointer-events-none opacity-50' : ''}><DatePicker value={editValue} onChange={setEditValue} className={baseInputClass} small /></div>
          <div className="flex items-center gap-1">
            <Button type="button" variant="primary" size="sm" className="h-7 text-xs px-2" disabled={readOnly} onClick={() => handleSave(editValue)}>
              {t('common.save', 'Save')}
            </Button>
            {saving && <span className="text-xs text-muted-foreground">{t('common.saving', 'Saving...')}</span>}
            {saveSuccess && <span className="text-xs text-green-600 dark:text-green-400">{t('common.saved', '✓ Saved')}</span>}
            {error && <span className="text-xs text-destructive">{error}</span>}
            <Button type="button" variant="ghost" size="sm" className="h-7 text-xs px-2" disabled={readOnly} onClick={onCancel} aria-label={t('common.cancel_edit', 'Cancel edit')}>
              {t('common.cancel', 'Cancel')}
            </Button>
          </div>
        </div>
      </div>
    )
  }

  if (isMulti) {
    const selectOptions = attribute?.structure?.options || []
    const searchable = selectOptions.length > 20

    const selectedSimpleValues = new Set((allValues || []).map(v => String(v.value_simple)))
    const filtered = searchable
      ? selectOptions.filter((opt) => getOptionLabel(opt).toLowerCase().includes(addSearchQuery.toLowerCase()))
      : selectOptions
    const availableOptions = filtered.filter((opt, idx) => !selectedSimpleValues.has(String(getOptionValue(opt, idx))))

    const selectorElement = searchable ? (
      <div className="relative">
        <input
          ref={addInputRef}
          type="text"
          value={addSearchQuery}
          onChange={e => { setAddSearchQuery(e.target.value); setAddSearchOpen(true) }}
          onFocus={() => setAddSearchOpen(true)}
          placeholder={t('browse.references.search_to_add', 'Search to add...')}
          className="w-full rounded border px-2 py-1 text-sm bg-background border-input focus:outline-none focus:ring-1 focus:ring-primary"
          disabled={readOnly}
          onKeyDown={(e) => {
            if (e.key === 'Escape') { e.preventDefault(); onCancel() }
          }}
        />
        {addSearchOpen && (
          <div className="rounded-md border bg-background shadow-lg max-h-48 overflow-y-auto absolute z-10 w-full">
            {availableOptions.length === 0 && (
              <div className="px-4 py-2 text-sm text-muted-foreground">{t('browse.values.no_matches', 'No matches')}</div>
            )}
            {availableOptions.map((opt, idx) => {
              const optValue = getOptionValue(opt, selectOptions.indexOf(opt))
              return (
                <div
                  key={opt.id ?? optValue ?? idx}
                  onMouseDown={(e) => e.preventDefault()}
                  onClick={() => handleAddValue(optValue)}
                  className="px-4 py-2 text-sm hover:bg-accent cursor-pointer"
                >
                  {getOptionLabel(opt)}
                </div>
              )
            })}
          </div>
        )}
      </div>
    ) : (
      <select
        ref={addInputRef}
        size={Math.min(Math.max(availableOptions.length, 1), 8)}
        value=""
        onChange={(e) => {
          const selectedOpt = e.target.value
          if (selectedOpt) handleAddValue(selectedOpt)
          e.target.value = ''
        }}
        className="w-full rounded border px-2 py-1 text-sm bg-background border-input focus:outline-none focus:ring-1 focus:ring-primary"
        disabled={readOnly}
        onKeyDown={(e) => {
          if (e.key === 'Escape') { e.preventDefault(); onCancel() }
        }}
      >
        <option value="" disabled>{t('browse.values.select_to_add', 'Select to add...')}</option>
        {availableOptions.map((opt, idx) => {
          const optValue = getOptionValue(opt, idx)
          return (
            <option key={opt.id ?? optValue ?? idx} value={optValue}>{getOptionLabel(opt)}</option>
          )
        })}
      </select>
    )

    return (
      <div data-testid={`cell-editor-${objectId}-${attribute.id}`} onClick={e => e.stopPropagation()}>
        {(allValues || []).length > 0 && (
          <div className="flex flex-wrap gap-1 mb-2">
            {(allValues || []).map((v) => {
              const rawVal = v.value_simple || v.value_float || v.value_integer || v.value_boolean || v.value_datetime
              const opt = rawVal !== null && rawVal !== undefined ? findOptionByValue(selectOptions, rawVal) : null
              const isPending = pendingRemove.has(v.id)
              return (
                <span key={v.id} className={`inline-flex items-center gap-1 bg-muted rounded px-2 py-0.5 text-xs ${isPending ? 'opacity-50' : ''}`}>
                  {opt ? getOptionLabel(opt) : (rawVal !== null && rawVal !== undefined ? String(rawVal) : t('common.empty_value', '—'))}
                  <Button
                    type="button"
                    size="icon"
                    variant="ghost"
                    onClick={() => handleRemoveValue(v.id)}
                    disabled={isPending || readOnly}
                    className="h-4 w-4 ml-0.5 hover:text-destructive disabled:opacity-50"
                    leftIcon="x"
                    aria-label={t('browse.references.remove', 'Remove')}
                  />
                </span>
              )
            })}
          </div>
        )}
        {selectorElement}
        <div className="flex items-center gap-1 mt-1">
          {saving && <span className="text-xs text-muted-foreground">{t('common.saving', 'Saving...')}</span>}
          {saveSuccess && <span className="text-xs text-green-600 dark:text-green-400">{t('common.saved', '✓ Saved')}</span>}
          {error && <span className="text-xs text-destructive">{error}</span>}
          <Button type="button" variant="ghost" size="sm" disabled={readOnly} onClick={onCancel} className="ml-auto" aria-label={t('common.cancel_edit', 'Cancel edit')}>
            {t('common.cancel', 'Cancel')}
          </Button>
        </div>
      </div>
    )
  }

  let editor = null

  if (attrType === 'select') {
    const selectOptions = attribute?.structure?.options || []

    if (selectOptions.length <= 20) {
      editor = (
        <select
          ref={inputRef}
          value={editValue ?? ''}
          onChange={e => { setEditValue(e.target.value); handleSave(e.target.value) }}
          className={baseInputClass}
          disabled={readOnly}
        >
          <option value="">—</option>
          {selectOptions.map((opt, idx) => {
            const optValue = getOptionValue(opt, idx)
            return (
              <option key={opt.id ?? optValue ?? idx} value={optValue}>{getOptionLabel(opt)}</option>
            )
          })}
        </select>
      )
    } else {
      const filtered = selectOptions.filter((opt) => getOptionLabel(opt).toLowerCase().includes(searchQuery.toLowerCase()))
      editor = (
        <div className="relative">
          <input
            ref={inputRef}
            type="text"
            value={searchQuery}
            onChange={e => { setSearchQuery(e.target.value); setSearchOpen(true) }}
            onFocus={() => setSearchOpen(true)}
            placeholder={t('browse.values.search_options', 'Search options...')}
            className={baseInputClass}
            disabled={readOnly}
          />
          {searchOpen && (
            <div className="rounded-md border bg-background shadow-lg max-h-48 overflow-y-auto absolute z-10 w-full">
              {filtered.length === 0 && (
                <div className="px-3 py-2 text-sm text-muted-foreground">{t('browse.values.no_matches', 'No matches')}</div>
              )}
              {filtered.map((opt, idx) => {
                const optValue = getOptionValue(opt, selectOptions.indexOf(opt))
                return (
                  <div
                    key={opt.id ?? optValue ?? idx}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => { setSearchOpen(false); handleSave(optValue) }}
                    className="px-3 py-2 text-sm hover:bg-accent cursor-pointer"
                  >
                    {getOptionLabel(opt)}
                  </div>
                )
              })}
            </div>
          )}
        </div>
      )
    }
  } else if (attrType === 'status') {
    const statusOptions = attribute?.structure?.status_options || []
    const currentId = editValue && typeof editValue === 'object' ? editValue.id : editValue
    editor = (
      <select
        ref={inputRef}
        value={currentId ?? ''}
        onChange={e => {
          const opt = statusOptions.find(o => String(o.id) === e.target.value)
          if (opt) handleSave({ id: opt.id, name: opt.name })
          else handleSave(null)
        }}
        className={baseInputClass}
        disabled={readOnly}
      >
        <option value="">{t('browse.values.no_status', 'No status')}</option>
        {statusOptions.map((opt) => (
          <option key={opt.id} value={opt.id}>{opt.name}</option>
        ))}
      </select>
    )
  } else if (attrType === 'integer') {
    editor = (
      <input
        type="number"
        step="1"
        ref={inputRef}
        value={editValue ?? ''}
        onChange={e => setEditValue(e.target.value ? parseInt(e.target.value, 10) : null)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        className={baseInputClass}
        disabled={readOnly}
      />
    )
  } else if (attrType === 'double') {
    editor = (
      <input
        type="number"
        step="any"
        ref={inputRef}
        value={editValue ?? ''}
        onChange={e => setEditValue(e.target.value ? parseFloat(e.target.value) : null)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        className={baseInputClass}
        disabled={readOnly}
      />
    )
  } else if (attrType === 'url') {
    editor = (
      <input
        type="url"
        placeholder={t('browse.fields.url_placeholder', 'https://example.com')}
        ref={inputRef}
        value={editValue ?? ''}
        onChange={e => setEditValue(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        className={baseInputClass}
        disabled={readOnly}
      />
    )
  } else if (attrType === 'email') {
    editor = (
      <input
        type="email"
        placeholder={t('browse.fields.email_placeholder', 'name@example.com')}
        ref={inputRef}
        value={editValue ?? ''}
        onChange={e => setEditValue(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        className={baseInputClass}
        disabled={readOnly}
      />
    )
  } else if (attrType === 'textarea') {
    editor = (
      <textarea
        ref={inputRef}
        value={editValue ?? ''}
        onChange={e => setEditValue(e.target.value)}
        onBlur={handleBlur}
        onKeyDown={(e) => {
          if (e.key === 'Escape') {
            e.preventDefault()
            onCancel()
          }
        }}
        rows={6}
        className={baseInputClass + ' resize-vertical'}
        disabled={readOnly}
      />
    )
  } else {
    editor = (
      <input
        type="text"
        ref={inputRef}
        value={editValue ?? ''}
        onChange={e => setEditValue(e.target.value)}
        onKeyDown={handleKeyDown}
        onBlur={handleBlur}
        className={baseInputClass}
        disabled={readOnly}
      />
    )
  }

  return (
    <div data-testid={`cell-editor-${objectId}-${attribute.id}`} onClick={e => e.stopPropagation()}>
      {editor}
      {statusRow}
      {translationPanel}
    </div>
  )
}

export default React.memo(InlineCellEditor)
