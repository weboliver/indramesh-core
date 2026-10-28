import { useState } from 'react'
import DatePicker from './DatePicker'
import { Button } from '../../shared/components/Button'
import { EntityValuePicker } from '../../shared/components/EntityValuePicker'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { usePluginAttributeEditor } from '../../shared/plugin-runtime/PluginProvider'
import { PluginSlotBoundary } from '../../shared/plugin-runtime/PluginSlotBoundary'

export default function TypedField({ attribute, value, onChange, error }) {
  const { t } = useTranslation()
  const label = attribute.name || t('common.attribute', 'Attribute')
  const required = attribute.is_required || false
  const type = attribute.type || 'text'

  const [searchQuery, setSearchQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)

  const pluginEditor = usePluginAttributeEditor(type)
  const [PluginEditor] = useState(() => pluginEditor?.component ?? null)
  const [pluginEditorName] = useState(() => pluginEditor?.pluginName ?? null)
  const pluginEditorSlot = `attributeEditor:${type}`

  const baseInputClass = `w-full rounded-md border px-3 py-2 text-sm bg-background ${
    error ? 'border-destructive' : 'border-input'
  } focus:outline-none focus:ring-1 focus:ring-primary`

  const getOptionValue = (opt, idx) => (typeof opt === 'string' ? opt : (opt.value ?? opt.id ?? idx))
  const getOptionLabel = (opt) => (typeof opt === 'string' ? opt : (opt.label ?? opt.name ?? opt.value))

  const renderDefaultEditor = () => {
    switch (type) {
      case 'text':
        return (
          <input
            type="text"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value)}
            className={baseInputClass}
            aria-label={label}
          />
        )

      case 'integer':
        return (
          <input
            type="number"
            step="1"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value ? parseInt(e.target.value, 10) : null)}
            className={baseInputClass}
          />
        )

      case 'double':
        return (
          <input
            type="number"
            step="any"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value ? parseFloat(e.target.value) : null)}
            className={baseInputClass}
          />
        )

      case 'boolean':
        return (
          <label className="flex items-center gap-2 cursor-pointer">
            <button
              type="button"
              role="switch"
              aria-checked={value === true}
              onClick={() => onChange(value === true ? false : true)}
              className={`relative inline-flex h-5 w-9 shrink-0 cursor-pointer rounded-full border-2 border-transparent transition-colors ${
                value === true ? 'bg-primary' : 'bg-input'
              }`}
            >
              <span
                className={`pointer-events-none inline-block h-4 w-4 rounded-full bg-white shadow transform ring-0 transition-transform ${
                  value === true ? 'translate-x-4' : 'translate-x-0'
                }`}
              />
            </button>
            <span className="text-sm text-muted-foreground">{value === true ? t('common.yes', 'Yes') : t('common.no', 'No')}</span>
          </label>
        )

      case 'datetime':
        return (
          <DatePicker
            value={value}
            onChange={onChange}
            className={baseInputClass}
          />
        )

      case 'url':
        return (
          <input
            type="url"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value || null)}
            className={baseInputClass}
            placeholder={t('browse.fields.url_placeholder', 'https://example.com')}
            aria-label={label}
          />
        )

      case 'email':
        return (
          <input
            type="email"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value || null)}
            className={baseInputClass}
            placeholder={t('browse.fields.email_placeholder', 'name@example.com')}
            aria-label={label}
          />
        )

      case 'select': {
        const selectOptions = attribute?.structure?.options || []
        const maxCard = attribute?.maximum_cardinality ?? 1
        const isMulti = maxCard < 0 || maxCard > 1
        const selectedValues = isMulti ? (Array.isArray(value) ? value : (value ? [value] : [])) : null
        const searchable = selectOptions.length > 20

        const findOptionByValue = (v) =>
          selectOptions.find((opt, idx) => String(getOptionValue(opt, idx)) === String(v))

        const selectedSet = new Set((selectedValues || []).map(String))
        const filteredOptions = searchable
          ? selectOptions.filter((opt) => getOptionLabel(opt).toLowerCase().includes(searchQuery.toLowerCase()))
          : selectOptions
        const availableOptions = isMulti
          ? filteredOptions.filter((opt) => !selectedSet.has(String(getOptionValue(opt, selectOptions.indexOf(opt)))))
          : filteredOptions

        const addValue = (rawOptValue) => {
          const optValue = String(rawOptValue)
          if (isMulti) {
            if (!selectedValues.some((v) => String(v) === optValue)) {
              onChange([...selectedValues, optValue])
            }
          } else {
            onChange(optValue)
          }
          setSearchQuery('')
          setIsOpen(false)
          setActiveIndex(-1)
        }

        const removeValue = (rawOptValue) => {
          const optValue = String(rawOptValue)
          onChange(selectedValues.filter((v) => String(v) !== optValue))
        }

        const handleKeyDown = (e) => {
          if (!searchable) return
          if (e.key === 'ArrowDown') {
            e.preventDefault()
            setIsOpen(true)
            setActiveIndex((i) => Math.min(i + 1, availableOptions.length - 1))
          } else if (e.key === 'ArrowUp') {
            e.preventDefault()
            setActiveIndex((i) => Math.max(i - 1, 0))
          } else if (e.key === 'Enter') {
            e.preventDefault()
            const opt = availableOptions[activeIndex]
            if (opt) addValue(getOptionValue(opt, selectOptions.indexOf(opt)))
          } else if (e.key === 'Escape') {
            setIsOpen(false)
            setActiveIndex(-1)
          }
        }

        const selectedLabel = !isMulti && value !== null && value !== undefined
          ? (() => { const opt = findOptionByValue(value); return opt ? getOptionLabel(opt) : '' })()
          : ''

        const selectorElement = searchable ? (
          <div className="relative">
            <input
              type="text"
              value={isOpen ? searchQuery : (isMulti ? searchQuery : selectedLabel)}
              onChange={(e) => { setSearchQuery(e.target.value); setIsOpen(true); setActiveIndex(-1) }}
              onFocus={() => { setSearchQuery(''); setIsOpen(true) }}
              onBlur={() => setTimeout(() => setIsOpen(false), 150)}
              onKeyDown={handleKeyDown}
              placeholder={isMulti ? t('browse.references.search_to_add', 'Search to add...') : t('browse.values.search_options', 'Search options...')}
              className={baseInputClass}
              aria-label={label}
            />
            {isOpen && (
              <div className="rounded-md border bg-background shadow-lg max-h-48 overflow-y-auto absolute z-10 w-full">
                {availableOptions.length === 0 && (
                  <div className="px-3 py-2 text-sm text-muted-foreground">{t('browse.values.no_matches', 'No matches')}</div>
                )}
                {availableOptions.map((opt, idx) => {
                  const optValue = getOptionValue(opt, selectOptions.indexOf(opt))
                  return (
                    <div
                      key={opt.id ?? optValue ?? idx}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => addValue(optValue)}
                      className={`px-3 py-2 text-sm hover:bg-accent cursor-pointer ${idx === activeIndex ? 'bg-accent' : ''}`}
                    >
                      {getOptionLabel(opt)}
                    </div>
                  )
                })}
              </div>
            )}
            {!isMulti && !required && selectedLabel && (
              <Button
                type="button"
                variant="ghost"
                size="icon"
                leftIcon="x"
                aria-label={t('browse.values.clear_value', 'Clear value')}
                data-testid="clear-select-value"
                className="absolute right-1 top-1/2 -translate-y-1/2"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => {
                  onChange(null)
                  setSearchQuery('')
                  setIsOpen(false)
                  setActiveIndex(-1)
                }}
              />
            )}
          </div>
        ) : (
          <select
            value={isMulti ? '' : (value ?? '')}
            onChange={(e) => {
              if (e.target.value) addValue(e.target.value)
              // Not required → the empty option is a real, selectable "clear"
              // choice (matches InlineCellEditor's "—" pattern), not just an
              // inert placeholder.
              else if (!isMulti && !required) onChange(null)
            }}
            className={baseInputClass}
            aria-label={label}
          >
            <option value="" disabled={required}>
              {selectOptions.length > 0 ? (required ? t('browse.values.select_option', 'Select an option') : '—') : t('browse.values.no_options_available', 'No options available')}
            </option>
            {availableOptions.map((opt, idx) => (
              <option key={opt.id || idx} value={getOptionValue(opt, selectOptions.indexOf(opt))}>
                {getOptionLabel(opt)}
              </option>
            ))}
          </select>
        )

        if (isMulti) {
          return (
            <div>
              {selectedValues.length > 0 && (
                <div className="flex flex-wrap gap-1.5 mb-2">
                  {selectedValues.map((v, i) => {
                    const opt = findOptionByValue(v)
                    return (
                      <span key={i} className="inline-flex items-center gap-1 bg-muted rounded px-2 py-0.5 text-xs">
                        {opt ? getOptionLabel(opt) : v}
                        <Button
                          type="button"
                          size="icon"
                          variant="ghost"
                          onClick={() => removeValue(v)}
                          className="h-4 w-4 ml-0.5 hover:text-destructive"
                          leftIcon="x"
                          aria-label={t('browse.references.remove', 'Remove')}
                        />
                      </span>
                    )
                  })}
                </div>
              )}
              {selectorElement}
            </div>
          )
        }

        return selectorElement
      }

      case 'user':
      case 'group':
        return (
          <EntityValuePicker
            kind={type}
            value={value ?? null}
            onChange={onChange}
            label={label}
            error={error}
          />
        )

      case 'STATUS': {
        const statusOptions = attribute?.structure?.status_options || []
        const currentId = value && typeof value === 'object' ? (value.id || '') : (value || '')
        const currentName = value && typeof value === 'object' ? value.name : null
        // The current value may not correspond to any locally known Status (e.g. a
        // Jira status without a matching local Status row, id === ''). Surface it as
        // a synthetic, selected option instead of silently falling back to "No status"
        // and losing the current value from view.
        const isUnmatched = currentName && !statusOptions.some((s) => s.id === currentId)
        return (
          <select
            value={isUnmatched ? '__unmatched__' : currentId}
            onChange={(e) => {
              const selected = e.target.value
              if (!selected) return onChange(null)
              const opt = statusOptions.find((s) => s.id === selected)
              onChange(opt ? { id: opt.id, name: opt.name } : selected)
            }}
            className={baseInputClass}
          >
            <option value="">{t('browse.values.no_status', 'No status')}</option>
            {isUnmatched && (
              <option value="__unmatched__" disabled>{interpolateDefault(t('browse.values.unmatched', '{{name}} (unmatched)'), { name: currentName })}</option>
            )}
            {statusOptions.map((opt) => (
              <option key={opt.id} value={opt.id}>{opt.name}</option>
            ))}
          </select>
        )
      }

      case 'json':
        return (
          <textarea
            value={value ? JSON.stringify(value, null, 2) : ''}
            onChange={(e) => {
              try {
                onChange(JSON.parse(e.target.value))
              } catch {
                onChange(e.target.value)
              }
            }}
            className={baseInputClass + ' font-mono min-h-[120px]'}
            rows={5}
          />
        )

      case 'textarea':
        return (
          <textarea
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value || null)}
            className={baseInputClass + ' min-h-[120px]'}
            rows={6}
            aria-label={label}
          />
        )

      default:
        return (
          <input
            type="text"
            value={value ?? ''}
            onChange={(e) => onChange(e.target.value || null)}
            className={baseInputClass}
          />
        )
    }
  }

  const renderEditor = () => {
    if (!PluginEditor) return renderDefaultEditor()
    return (
      <PluginSlotBoundary
        slot={pluginEditorSlot}
        pluginName={pluginEditorName}
        fallback={renderDefaultEditor()}
      >
        <PluginEditor attribute={attribute} value={value} onChange={onChange} error={error} />
      </PluginSlotBoundary>
    )
  }

  return (
    <div className="space-y-1.5">
      <label className="flex items-center gap-1 text-sm font-medium text-foreground">
        {label}
        {required && <span className="text-destructive">*</span>}
      </label>
      {renderEditor()}
      {error && <p className="text-xs text-destructive">{error}</p>}
    </div>
  )
}
