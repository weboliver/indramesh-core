import { useEffect, useMemo, useRef, useState } from 'react'
import { cn } from '../lib/utils'
import { CATEGORIES, DATA_TYPE_LABELS } from '../lib/data-types'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { Button } from './Button'

const VALUE_TO_CATEGORY = {}
for (const cat of CATEGORIES) {
  for (const t of cat.types) {
    VALUE_TO_CATEGORY[t.value] = cat.id
  }
  if (cat.directValue) {
    VALUE_TO_CATEGORY[cat.directValue] = cat.id
  }
}

const CAT_ROW_CLASS = 'w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary'

export default function TypeSelector({ value, onChange, className, objectTypes = [], schemas = [], currentSchemaId }) {
  const { t } = useTranslation()
  const isRefWithId = value?.startsWith('reference:')
  const rawValue = isRefWithId ? 'reference' : (value || '')
  const initialCategory = rawValue ? (VALUE_TO_CATEGORY[rawValue] || 'default') : ''
  const [selectedCategory, setSelectedCategory] = useState(initialCategory)
  const [selectedType, setSelectedType] = useState(rawValue)
  const [selectedObjectTypeId, setSelectedObjectTypeId] = useState(
    isRefWithId ? value.split(':')[1] : ''
  )

  const currentCategory = CATEGORIES.find((c) => c.id === selectedCategory)

  function handleCategoryChange(catId) {
    setSelectedCategory(catId)
    setSelectedType('')
    setSelectedObjectTypeId('')

    const cat = CATEGORIES.find((c) => c.id === catId)
    if (!cat) {
      onChange('')
      return
    }

    if (cat.directValue && cat.types.length === 0) {
      setSelectedType(cat.directValue)
      onChange(cat.directValue)
    } else {
      onChange('')
    }
  }

  function handleTypeChange(typeValue) {
    setSelectedType(typeValue)
    if (typeValue === 'reference') {
      onChange(typeValue)
    } else {
      setSelectedObjectTypeId('')
      onChange(typeValue)
    }
  }

  function handleObjectTypeChange(otId) {
    setSelectedObjectTypeId(otId)
    onChange(otId ? `reference:${otId}` : 'reference')
  }

  const referenceObjectTypes = objectTypes

  const schemaNameById = useMemo(() => {
    return schemas.reduce((acc, schema) => {
      acc[schema.id] = schema.name
      return acc
    }, {})
  }, [schemas])

  const catRowClass = CAT_ROW_CLASS

  return (
    <div className={cn('space-y-2', className)}>
      {/* First level: category */}
      <select
        value={selectedCategory}
        onChange={(e) => handleCategoryChange(e.target.value)}
        className={catRowClass}
      >
        <option value="" disabled>{t('common.select_type_category', 'Select type category...')}</option>
        {CATEGORIES.map((cat) => (
          <option key={cat.id} value={cat.id}>{cat.label}</option>
        ))}
      </select>

      {/* Second level: specific type (when category has sub-types) */}
      {currentCategory && currentCategory.types.length > 0 && (
        <select
          value={selectedType}
          onChange={(e) => handleTypeChange(e.target.value)}
          className={catRowClass}
        >
          <option value="" disabled>{t('common.select_type', 'Select type...')}</option>
          {currentCategory.types.map((t) => (
            <option key={t.value} value={t.value}>{t.label}</option>
          ))}
        </select>
      )}

      {/* ObjectType picker for reference type */}
      {selectedType === 'reference' && (
        <ReferenceObjectTypePicker
          objectTypes={referenceObjectTypes}
          schemaNameById={schemaNameById}
          value={selectedObjectTypeId}
          onChange={handleObjectTypeChange}
          currentSchemaId={currentSchemaId}
        />
      )}

      {/* Info display for direct-selection categories */}
      {currentCategory && currentCategory.directValue && currentCategory.types.length === 0 && (
        <p className="text-xs text-muted-foreground px-1">
          {interpolateDefault(
            t('common.type_value', 'Type: {{value}}'),
            { value: DATA_TYPE_LABELS[currentCategory.directValue] || currentCategory.directValue }
          )}
        </p>
      )}
    </div>
  )
}

// Searchable, schema-annotated target-ObjectType combobox for the Reference
// data type — mirrors EntityValuePicker's combined display+search shape
// (collapsed "Selected: X" + Clear once chosen, otherwise a filter input with
// a dropdown of matching rows) instead of the flat unsearchable `<select>`
// this replaces.
function ReferenceObjectTypePicker({ objectTypes, schemaNameById, value, onChange, currentSchemaId }) {
  const { t } = useTranslation()
  const [query, setQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const containerRef = useRef(null)

  useEffect(() => {
    function handleClickOutside(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false)
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    const matches = objectTypes.filter((ot) => {
      if (!q) return true
      const schemaName = schemaNameById[ot.schema_id] || ''
      return ot.name.toLowerCase().includes(q) || schemaName.toLowerCase().includes(q)
    })
    return matches
      .slice()
      .sort((a, b) => {
        const schemaA = schemaNameById[a.schema_id] || ''
        const schemaB = schemaNameById[b.schema_id] || ''
        const schemaCompare = schemaA.localeCompare(schemaB)
        if (schemaCompare !== 0) return schemaCompare
        return a.name.localeCompare(b.name)
      })
      .slice(0, 20)
  }, [objectTypes, schemaNameById, query])

  const selectedObjectType = objectTypes.find((ot) => ot.id === value)
  const selectedSchemaName = selectedObjectType ? schemaNameById[selectedObjectType.schema_id] : undefined
  const selectedLabel = selectedObjectType
    ? (selectedSchemaName && selectedObjectType.schema_id !== currentSchemaId
        ? interpolateDefault(t('browse.references.name_with_schema', '{{schema}} - {{name}}'), { schema: selectedSchemaName, name: selectedObjectType.name })
        : selectedObjectType.name)
    : (value ? `${value.slice(0, 8)}...` : '')

  function selectItem(ot) {
    onChange(ot.id)
    setQuery('')
    setIsOpen(false)
    setActiveIndex(-1)
  }

  function handleKeyDown(e) {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setIsOpen(true)
      setActiveIndex((i) => Math.min(i + 1, filtered.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const ot = filtered[activeIndex]
      if (ot) selectItem(ot)
    } else if (e.key === 'Escape') {
      setIsOpen(false)
      setActiveIndex(-1)
    }
  }

  if (value) {
    return (
      <div className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2">
        <span className="text-sm flex-1 truncate text-primary">
          {interpolateDefault(t('browse.references.selected', 'Selected: {{value}}'), { value: selectedLabel })}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          onClick={() => onChange('')}
          className="hover:text-destructive"
        >
          {t('browse.references.clear', 'Clear')}
        </Button>
      </div>
    )
  }

  return (
    <div ref={containerRef} className="relative">
      <input
        type="text"
        value={query}
        onChange={(e) => { setQuery(e.target.value); setIsOpen(true); setActiveIndex(-1) }}
        onFocus={() => setIsOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder={t('common.select_target_object_type', 'Select target ObjectType...')}
        className={CAT_ROW_CLASS}
        data-testid="object-type-reference-picker"
      />
      {isOpen && (
        <div className="absolute z-10 w-full rounded-md border bg-background shadow-lg max-h-48 overflow-y-auto">
          {filtered.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">{t('browse.references.no_results', 'No results')}</p>
          ) : (
            filtered.map((ot, idx) => (
              <div
                key={ot.id}
                data-testid="object-type-reference-picker-row"
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => selectItem(ot)}
                className={`px-3 py-2 cursor-pointer hover:bg-accent ${idx === activeIndex ? 'bg-accent' : ''}`}
              >
                <div className="text-sm font-normal text-foreground">{ot.name}</div>
                {schemaNameById[ot.schema_id] && (
                  <div className="text-xs text-muted-foreground">{schemaNameById[ot.schema_id]}</div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}
