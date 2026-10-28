import { useState, useEffect, useCallback, useRef } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../../shared/components/Button'
import X from 'lucide-react/dist/esm/icons/x'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'

const LIGHT_FIELDS = [
  { key: 'primary', dbField: 'primary_color' },
  { key: 'header', dbField: 'header_bg_color' },
  { key: 'sidebar', dbField: 'sidebar_bg_color' },
  { key: 'section', dbField: 'section_bg_color' },
  { key: 'surface', dbField: 'surface_bg_color' },
]

const HEX_PATTERN = /^#[0-9A-Fa-f]{6}$/

function colorLabel(key, t) {
  switch (key) {
    case 'primary': return t('settings.template.color.primary', 'Primary')
    case 'header': return t('settings.template.color.header', 'Header')
    case 'sidebar': return t('settings.template.color.sidebar', 'Sidebar')
    case 'section': return t('settings.template.color.section', 'Section')
    default: return t('settings.template.color.surface', 'Surface')
  }
}

function validateMode(modeData) {
  if (!modeData) return { valid: true, invalidFields: [] }
  const invalid = []
  for (const { key } of LIGHT_FIELDS) {
    const v = modeData[key]
    if (v !== null && v !== undefined && !HEX_PATTERN.test(v)) invalid.push(key)
  }
  return { valid: invalid.length === 0, invalidFields: invalid }
}

async function parseImportFile(file, t) {
  const text = await file.text()
  let json
  try {
    json = JSON.parse(text)
  } catch (e) {
    return { ok: false, error: interpolateDefault(t('settings.template.invalid_json', 'Invalid JSON: {{error}}'), { error: e.message }) }
  }
  if (typeof json !== 'object' || json === null || Array.isArray(json)) {
    return { ok: false, error: t('settings.template.invalid_file_shape', 'File must be a JSON object mapping schema IDs to colors') }
  }
  const keys = Object.keys(json)
  if (keys.length === 0) {
    return { ok: false, error: t('settings.template.empty_file', 'File contains no template entries') }
  }
  if (keys.length > 1) {
    return { ok: false, error: interpolateDefault(t('settings.template.single_schema_required', 'Single-schema import expects exactly 1 entry, got {{count}}. Use bulk import for multi-schema files.'), { count: keys.length }) }
  }
  const schemaId = keys[0]
  const entry = json[schemaId]
  if (typeof entry !== 'object' || entry === null) {
    return { ok: false, error: t('settings.template.entry_object_required', 'Template entry must be an object') }
  }
  const lightCheck = validateMode(entry.light)
  if (!lightCheck.valid) {
    return { ok: false, error: interpolateDefault(t('settings.template.invalid_light_hex', 'Invalid hex in light mode: {{fields}}'), { fields: lightCheck.invalidFields.join(', ') }) }
  }
  const darkCheck = validateMode(entry.dark)
  if (!darkCheck.valid) {
    return { ok: false, error: interpolateDefault(t('settings.template.invalid_dark_hex', 'Invalid hex in dark mode: {{fields}}'), { fields: darkCheck.invalidFields.join(', ') }) }
  }
  return { ok: true, schemaId, light: entry.light, dark: entry.dark, error: null }
}

function ColorRow({ label, currentHex, newHex, t }) {
  const changed = currentHex !== newHex
  return (
    <div className="grid grid-cols-[120px_1fr_auto_1fr] items-center gap-3 py-1.5" data-testid={`import-row-${label.toLowerCase()}`}>
      <span className="text-sm">{label}</span>
      <div className="flex items-center gap-2">
        <span className="h-5 w-5 rounded-sm border shrink-0" style={{ background: currentHex }} aria-label={interpolateDefault(t('settings.template.current_color', 'Current {{label}}: {{color}}'), { label, color: currentHex })} />
        <span className="font-mono text-xs text-muted-foreground">{currentHex}</span>
      </div>
      <span className="text-muted-foreground text-sm">&#8594;</span>
      <div className="flex items-center gap-2">
        <span className="h-5 w-5 rounded-sm border shrink-0" style={{ background: newHex }} aria-label={interpolateDefault(t('settings.template.new_color', 'New {{label}}: {{color}}'), { label, color: newHex })} />
        <span className={`font-mono text-xs ${changed ? 'text-foreground font-semibold' : 'text-muted-foreground'}`}>{newHex}</span>
      </div>
    </div>
  )
}

export default function ImportPreviewModal({ schema, file, onApply, onCancel, applying }) {
  const { t } = useTranslation()
  const [parsed, setParsed] = useState(null)
  const [parseError, setParseError] = useState(null)
  const dialogRef = useRef(null)

  useEffect(() => {
    if (!file) {
      setParsed(null)
      setParseError(null)
      return
    }
    let cancelled = false
    parseImportFile(file, t).then(result => {
      if (cancelled) return
      if (!result.ok) {
        setParsed(null)
        setParseError(result.error)
      } else {
        if (result.schemaId !== schema.id) {
          setParsed(null)
          setParseError(interpolateDefault(t('settings.template.different_schema', 'File is for a different schema ({{fileSchema}}). Current schema: {{currentSchema}}.'), { fileSchema: result.schemaId, currentSchema: schema.id }))
        } else {
          setParsed(result)
          setParseError(null)
        }
      }
    })
    return () => { cancelled = true }
  }, [file, schema.id, t])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape' && !applying) onCancel() }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [onCancel, applying])

  const canApply = parsed !== null && parsed !== undefined && !parseError

  const handleApply = useCallback(() => {
    if (!canApply) return
    onApply({ light: parsed.light, dark: parsed.dark })
  }, [canApply, parsed, onApply])

  const buildNewColors = (mode) => {
    const newMode = parsed?.[mode] || {}
    const result = {}
    for (const { key, dbField } of LIGHT_FIELDS) {
      result[key] = newMode[key] ?? schema[dbField]
    }
    return result
  }

  return createPortal(
    <div
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/50"
      role="dialog"
      aria-modal="true"
      aria-labelledby="import-modal-title"
      data-testid="template-import-modal"
      ref={dialogRef}
    >
      <div className="bg-background rounded-lg border shadow-lg max-w-2xl w-full mx-4 max-h-[80vh] overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b">
           <h2 id="import-modal-title" className="text-lg font-semibold">{t('settings.template.import_preview_title', 'Import Template Preview')}</h2>
          <Button
            variant="ghost"
            size="icon"
            onClick={onCancel}
            disabled={applying}
             aria-label={t('common.close', 'Close')}
            data-testid="template-import-modal-close"
          >
            <X size={18} />
          </Button>
        </div>

        <div className="p-4 space-y-4">
          {parseError && (
            <div className="rounded-md border border-destructive/30 bg-destructive/10 p-3 text-sm text-destructive" data-testid="template-import-error">
              {parseError}
            </div>
          )}

          {canApply && (
            <>
              <div>
                 <h3 className="text-sm font-semibold mb-2">{t('settings.template.light_mode', 'Light mode')}</h3>
                <div className="divide-y">
                   {LIGHT_FIELDS.map(({ key, dbField }) => (
                     <ColorRow
                       key={key}
                       label={colorLabel(key, t)}
                      currentHex={schema[dbField]}
                       newHex={buildNewColors('light')[key]}
                       t={t}
                    />
                  ))}
                </div>
              </div>
              <div>
                 <h3 className="text-sm font-semibold mb-2">{t('settings.template.dark_mode', 'Dark mode')}</h3>
                <div className="divide-y">
                   {LIGHT_FIELDS.map(({ key, dbField }) => {
                    const darkField = `dark_${dbField}`
                    return (
                      <ColorRow
                        key={key}
                         label={colorLabel(key, t)}
                        currentHex={schema[darkField]}
                         newHex={buildNewColors('dark')[key]}
                         t={t}
                      />
                    )
                  })}
                </div>
              </div>
              <p className="text-xs text-muted-foreground">
                 {t('settings.template.file_label', 'File:')} <span className="font-mono">{file?.name}</span>
              </p>
            </>
          )}

          {!parseError && !parsed && !file && (
             <p className="text-sm text-muted-foreground">{t('settings.template.no_file', 'No file selected.')}</p>
          )}
        </div>

        <div className="flex justify-end gap-2 p-4 border-t">
          <Button variant="outline" size="md" onClick={onCancel} disabled={applying} data-testid="template-import-cancel">
             {t('common.cancel', 'Cancel')}
          </Button>
          <Button
            variant="primary"
            size="md"
            onClick={handleApply}
            disabled={!canApply}
            loading={applying}
            data-testid="template-import-apply"
          >
             {t('settings.template.apply_changes', 'Apply Changes')}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}
