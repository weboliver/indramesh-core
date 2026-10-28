import { useState, useMemo, useEffect } from 'react'
import { useNavigate, Link, useSearchParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import api from '../../shared/hooks/use-api'
import { useSources } from '../../shared/hooks/use-sources'
import { useAttributeTypes } from '../../shared/hooks/use-attribute-types'
import { useCreateObjectType } from '../../shared/hooks/use-object-types'
import TypeSelector from '../../shared/components/TypeSelector'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import IconPicker from './IconPicker'
import { Button } from '../../shared/components/Button'
import { ErrorBoundary } from '../../shared/components/ErrorBoundary'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

const DEFAULT_ATTR = { name: '', typeValue: '', required: false, default: '', referencedObjectTypeId: null }

export default function CreateObjectTypePage() {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const [searchParams] = useSearchParams()

  // ObjectType fields
  const [name, setName] = useState('')
  const [sourceId, setSourceId] = useState('')
  const [schemaId, setSchemaId] = useState('')
  const [parentId, setParentId] = useState('')
  const [selectedIcon, setSelectedIcon] = useState(null)

  // Attribute rows
  const [attributes, setAttributes] = useState([])

  // Submission
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState('')

  // Data fetching
  const { data: sources = [], isLoading: sourcesLoading } = useSources()
  const { data: allSchemas = [], isLoading: schemasLoading } = useQuery({
    queryKey: ['schemas', { all: true }],
    queryFn: () => api.get('/schemas/', { params: { skip: 0, limit: 1000 } }).then(r => r.data),
  })
  const { data: attributeTypes = [] } = useAttributeTypes()
  const { data: allObjectTypes = [] } = useQuery({
    queryKey: ['object-types', { all: true }],
    queryFn: () => api.get('/object-types/', { params: { skip: 0, limit: 1000 } }).then(r => r.data),
  })
  const { data: icons = [] } = useQuery({
    queryKey: ['icons'],
    queryFn: () => api.get('/icons/', { params: { limit: 500 } }).then(r => r.data),
  })

  const createObjectType = useCreateObjectType()

  // Pre-select schema + parent from URL query params (set by sidebar/browse New ObjectType button)
  useEffect(() => {
    const preselectedSchemaId = searchParams.get('schemaId')
    if (preselectedSchemaId && allSchemas.length > 0) {
      const schema = allSchemas.find(s => s.id === preselectedSchemaId)
      if (schema) {
        setSourceId(schema.source_id)
        setSchemaId(schema.id)
      }
    }
    const preselectedParentId = searchParams.get('parentId')
    if (preselectedParentId && allObjectTypes.length > 0) {
      if (allObjectTypes.some(ot => ot.id === preselectedParentId)) {
        setParentId(preselectedParentId)
      }
    }
  }, [searchParams, allSchemas, allObjectTypes])

  // Filter schemas by selected source
  const schemas = useMemo(() => {
    if (!sourceId) return []
    return allSchemas.filter((s) => s.source_id === sourceId)
  }, [allSchemas, sourceId])

  // Filter object types by selected schema for parent picker
  const parentObjectTypes = useMemo(() => {
    if (!schemaId) return []
    return allObjectTypes.filter((ot) => ot.schema_id === schemaId)
  }, [allObjectTypes, schemaId])

  const iconNameToId = useMemo(() => {
    const map = {}
    for (const icon of icons) {
      if (icon.local_name) {
        map[icon.local_name] = icon.id
      }
    }
    return map
  }, [icons])

  function renderTreeOptions(types, parentIdVal = null, level = 0) {
    const children = types.filter(t => t.parent_id === parentIdVal)
    return children.map(child => [
      <option key={child.id} value={child.id} style={{ paddingLeft: `${level * 16}px` }}>
        {'→ '.repeat(level)}{child.name}
      </option>,
      ...renderTreeOptions(types, child.id, level + 1)
    ])
  }

  // Build type map: data_type -> attribute_type info
  const typeMap = useMemo(() => {
    const map = {}
    for (const at of attributeTypes) {
      map[at.data_type] = at
    }
    return map
  }, [attributeTypes])

  // Reset schema when source changes
  function handleSourceChange(newSourceId) {
    setSourceId(newSourceId)
    setSchemaId('')
  }

  // Add new attribute row
  function addAttribute() {
    setAttributes((prev) => [...prev, { ...DEFAULT_ATTR }])
  }

  // Remove attribute row
  function removeAttribute(index) {
    setAttributes((prev) => prev.filter((_, i) => i !== index))
  }

  // Update attribute field
  function updateAttribute(index, field, value) {
    setAttributes((prev) => {
      const updated = [...prev]
      updated[index] = { ...updated[index], [field]: value }
      return updated
    })
  }

  // Handle TypeSelector change for a specific attribute row
  function handleTypeChange(index, dataType) {
    if (dataType && dataType.startsWith('reference:')) {
      const otId = dataType.split(':')[1]
      setAttributes((prev) => {
        const updated = [...prev]
        updated[index] = { ...updated[index], typeValue: 'reference', referencedObjectTypeId: otId }
        return updated
      })
    } else {
      updateAttribute(index, 'typeValue', dataType)
      if (dataType !== 'reference') {
        updateAttribute(index, 'referencedObjectTypeId', null)
      }
    }
  }

  // Validate form
  function validate() {
    if (!name.trim()) return t('admin.object_type.name_required', 'ObjectType name is required')
    if (!sourceId) return t('admin.object_type.source_required', 'Please select a source')
    if (!schemaId) return t('admin.object_type.schema_required', 'Please select a schema')
    const duplicate = parentObjectTypes.some((ot) => ot.name.toLowerCase() === name.trim().toLowerCase())
    if (duplicate) return t('admin.object_type.name_duplicate', 'An ObjectType with this name already exists in this schema')
    for (let i = 0; i < attributes.length; i++) {
      if (!attributes[i].name.trim()) return interpolateDefault(t('admin.object_type.attribute_name_required', 'Attribute {{number}} requires a name'), { number: i + 1 })
      if (!attributes[i].typeValue) return interpolateDefault(t('admin.object_type.attribute_type_required', 'Attribute {{number}} requires a data type'), { number: i + 1 })
    }
    return null
  }

  // Submit handler
  async function handleSubmit(e) {
    e.preventDefault()
    setError('')

    const validationError = validate()
    if (validationError) {
      setError(validationError)
      return
    }

    setSubmitting(true)

    try {
      // 1. Create the ObjectType
      let resolvedIconId = null
      if (selectedIcon) {
        if (iconNameToId[selectedIcon]) {
          resolvedIconId = iconNameToId[selectedIcon]
        } else {
          try {
            const res = await api.post('/icons/', {
              schema_id: schemaId,
              name: selectedIcon,
              local_name: selectedIcon,
              url: `lucide:${selectedIcon}`,
              type: 'lucide',
            })
            resolvedIconId = res.data.id
          } catch {}
        }
      }

      const newOt = await createObjectType.mutateAsync({
        schema_id: schemaId,
        name: name.trim(),
        parent_id: parentId || null,
        sort: 0,
        icon_id: resolvedIconId,
      })

      // 2. Create each OTA
      let sort = 10
      for (const attr of attributes) {
        if (!attr.name.trim() || !attr.typeValue) continue

        // Find attribute_type_id from data_type
        const attrTypeInfo = typeMap[attr.typeValue]
        if (!attrTypeInfo) {
          console.error(`Unknown data type: ${attr.typeValue}`)
          continue
        }

        await api.post('/object-type-attributes/', {
          object_type_id: newOt.id,
          name: attr.name.trim(),
          attribute_type_id: attrTypeInfo.id,
          is_required: attr.required,
          default_value: attr.default || null,
          sort,
          referenced_object_type_id: attr.referencedObjectTypeId || null,
        })

        sort += 10
      }

      // 3. Redirect to the new ObjectType page
      navigate(`/browse/object-types/${newOt.id}`)
    } catch (err) {
      console.error('Failed to create ObjectType', err)
      const detail = err.response?.data?.detail
      setError(detail || err.message || t('admin.object_type.create_failed', 'Failed to create ObjectType'))
    } finally {
      setSubmitting(false)
    }
  }

  const isLoading = sourcesLoading || schemasLoading

  if (isLoading) {
    return <div className="flex h-full items-center justify-center text-muted-foreground">{t('common.loading', 'Loading...')}</div>
  }

  return (
    <ErrorBoundary>
      <div className="max-w-3xl mx-auto space-y-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-2 text-sm text-muted-foreground">
        <Link to="/browse/object-types" className="hover:text-foreground">{t('admin.object_type.object_types', 'Object Types')}</Link>
        <span>/</span>
        <span className="text-foreground">{t('admin.object_type.new_object_type', 'New Object Type')}</span>
      </div>

      <h1 className="text-2xl font-bold tracking-tight">{t('admin.object_type.create_title', 'Create Object Type')}</h1>

      {error && (
        <div className="rounded-md bg-destructive/10 border border-destructive/30 px-4 py-3 text-sm text-destructive">
          {error}
        </div>
      )}

      <form onSubmit={handleSubmit} className="space-y-6">
        {/* ObjectType fields */}
        <div className="rounded-lg border p-6 space-y-4">
          <h2 className="text-lg font-semibold">{t('common.details', 'Details')}</h2>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.name_required', 'Name *')}</label>
            <div className="flex items-center gap-2 mb-2">
              {selectedIcon && (
                <LucideIcon name={selectedIcon} size={24} className="shrink-0 text-muted-foreground" />
              )}
              <input
                type="text"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder={t('admin.object_type.name_placeholder', 'e.g. Server, Network Device')}
                className="flex-1 rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                required
              />
            </div>
            <IconPicker value={selectedIcon} onChange={setSelectedIcon} className="mt-2" />
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.source_required', 'Source *')}</label>
            <select
              value={sourceId}
              onChange={(e) => handleSourceChange(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              required
            >
              <option value="">{t('admin.object_type.select_source', 'Select a source...')}</option>
              {sources.map((source) => (
                <option key={source.id} value={source.id}>{source.name}</option>
              ))}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('common.schema_required', 'Schema *')}</label>
            <select
              value={schemaId}
              onChange={(e) => setSchemaId(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              required
              disabled={!sourceId}
            >
              {!sourceId ? (
                <option value="">{t('admin.object_type.select_source_first', 'Select a source first...')}</option>
              ) : schemas.length === 0 ? (
                <option value="">{t('admin.object_type.no_schemas', 'No schemas for this source')}</option>
              ) : (
                <>
                  <option value="">{t('admin.object_type.select_schema', 'Select a schema...')}</option>
                  {schemas.map((schema) => (
                    <option key={schema.id} value={schema.id}>{schema.name}</option>
                  ))}
                </>
              )}
            </select>
          </div>

          <div>
            <label className="block text-sm font-medium mb-1">{t('admin.object_type.parent_type', 'Parent Object Type')}</label>
            <select
              value={parentId}
              onChange={(e) => setParentId(e.target.value)}
              className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
              disabled={!schemaId}
            >
              <option value="">{t('admin.object_type.root_level', 'None (root level)')}</option>
              {parentObjectTypes.length > 0 && renderTreeOptions(parentObjectTypes)}
            </select>
          </div>
        </div>

        {/* Attribute rows */}
        <div className="rounded-lg border p-6 space-y-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">{t('common.attributes', 'Attributes')}</h2>
            <Button
              type="button"
              onClick={addAttribute}
              variant="primary"
              size="sm"
            >
              {t('admin.object_type.add_attribute', '+ Add Attribute')}
            </Button>
          </div>

          {attributes.length === 0 ? (
            <div className="rounded-lg border border-dashed p-8 text-center text-muted-foreground text-sm">
              {t('admin.object_type.no_attributes_hint', 'No attributes defined yet. Click "Add Attribute" to define the fields for this Object Type.')}
            </div>
          ) : (
            <div className="space-y-3">
              {attributes.map((attr, index) => (
                <div
                  key={index}
                  className="rounded-lg border p-4 space-y-3 bg-card"
                >
                  <div className="flex items-center justify-between">
                    <span className="text-xs font-medium text-muted-foreground uppercase tracking-wider">
                      {interpolateDefault(t('admin.object_type.attribute_number', 'Attribute {{number}}'), { number: index + 1 })}
                    </span>
                    <Button
                      type="button"
                      onClick={() => removeAttribute(index)}
                      variant="destructive"
                      size="sm"
                    >
                      {t('common.remove', 'Remove')}
                    </Button>
                  </div>

                  <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
                    <div>
                        <label className="block text-xs font-medium mb-1">{t('common.name_required', 'Name *')}</label>
                      <input
                        type="text"
                        value={attr.name}
                        onChange={(e) => updateAttribute(index, 'name', e.target.value)}
                          placeholder={t('admin.object_type.attribute_name_placeholder', 'Attribute name')}
                        className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                      />
                    </div>
                    <div>
                        <label className="block text-xs font-medium mb-1">{t('admin.object_type.data_type_required', 'Data Type *')}</label>
                      <TypeSelector
                        value={attr.typeValue}
                        onChange={(dt) => handleTypeChange(index, dt)}
                        objectTypes={allObjectTypes}
                        schemas={allSchemas}
                        currentSchemaId={schemaId}
                      />
                    </div>
                  </div>

                  <div className="flex items-center gap-6">
                    <label className="flex items-center gap-2 text-sm">
                      <input
                        type="checkbox"
                        checked={attr.required}
                        onChange={(e) => updateAttribute(index, 'required', e.target.checked)}
                        className="rounded border-input"
                      />
                      {t('common.required', 'Required')}
                    </label>
                    <div className="flex-1">
                        <label className="block text-xs font-medium mb-1">{t('common.default_value', 'Default Value')}</label>
                      <input
                        type="text"
                        value={attr.default}
                        onChange={(e) => updateAttribute(index, 'default', e.target.value)}
                        placeholder={t('common.no_default', 'No default')}
                        className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
                      />
                    </div>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>

        {/* Submit */}
        <div className="flex items-center gap-3">
          <Button
            type="submit"
            disabled={submitting || !name.trim()}
            variant="primary"
            size="lg"
            loading={submitting}
          >
            {submitting ? t('common.creating', 'Creating...') : t('admin.object_type.create_title', 'Create Object Type')}
          </Button>
          <Button asChild variant="ghost" size="sm">
            <Link to="/browse/object-types">
              {t('common.cancel', 'Cancel')}
            </Link>
          </Button>
        </div>
      </form>
    </div>
    </ErrorBoundary>
  )
}
