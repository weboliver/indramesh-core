import { useState, useEffect, useCallback, useRef } from 'react'
import { HexColorPicker } from 'react-colorful'
import { useUpdateSchema } from '../../shared/hooks/use-schemas'
import { useTemplate } from '../../shared/components/TemplateProvider'
import { hexToHsl } from '../../shared/lib/hex-to-hsl'
import { cn } from '../../shared/lib/utils'
import { Button } from '../../shared/components/Button'
import ImportPreviewModal from './ImportPreviewModal'
import { useTemplateImportSingle } from '../../shared/hooks/use-template-export'
import api from '../../shared/hooks/use-api'
import { useTranslation } from 'react-i18next'

const COLOR_FIELDS = [
  { key: 'primary' },
  { key: 'header' },
  { key: 'sidebar' },
  { key: 'section' },
  { key: 'surface' },
]

function colorLabel(key, t) {
  switch (key) {
    case 'primary': return t('settings.template.color.primary_full', 'Primary Color')
    case 'header': return t('settings.template.color.header_full', 'Header Background')
    case 'sidebar': return t('settings.template.color.sidebar_full', 'Sidebar Background')
    case 'section': return t('settings.template.color.section_full', 'Section Background')
    default: return t('settings.template.color.surface_full', 'Surface/Card Background')
  }
}

function TemplatePreview({ colors, mode }) {
  const c = colors[mode]
  return (
    <div className="border rounded-md p-4" style={{ background: `hsl(${hexToHsl(c.section)})` }}>
      <div className="h-10 rounded-sm mb-2" style={{ background: `hsl(${hexToHsl(c.header)})` }} />
      <div className="flex gap-2" style={{ minHeight: '96px' }}>
        <div className="w-16 rounded-sm shrink-0" style={{ background: `hsl(${hexToHsl(c.sidebar)})` }} />
        <div className="flex-1 rounded-sm p-3" style={{ background: `hsl(${hexToHsl(c.surface)})` }}>
          <div className="w-16 h-7 rounded-sm" style={{ background: `hsl(${hexToHsl(c.primary)})` }} />
        </div>
      </div>
    </div>
  )
}

export default function TemplateTab({ schema }) {
  const { t } = useTranslation()
  const [activeMode, setActiveMode] = useState('light')
  const [colors, setColors] = useState({
    light: {
      primary: schema.primary_color,
      header: schema.header_bg_color,
      sidebar: schema.sidebar_bg_color,
      section: schema.section_bg_color,
      surface: schema.surface_bg_color,
    },
    dark: {
      primary: schema.dark_primary_color,
      header: schema.dark_header_bg_color,
      sidebar: schema.dark_sidebar_bg_color,
      section: schema.dark_section_bg_color,
      surface: schema.dark_surface_bg_color,
    },
  })
  const [error, setError] = useState(null)
  const [isSaving, setIsSaving] = useState(false)

  useEffect(() => {
    setColors({
      light: { primary: schema.primary_color, header: schema.header_bg_color, sidebar: schema.sidebar_bg_color, section: schema.section_bg_color, surface: schema.surface_bg_color },
      dark: { primary: schema.dark_primary_color, header: schema.dark_header_bg_color, sidebar: schema.dark_sidebar_bg_color, section: schema.dark_section_bg_color, surface: schema.dark_surface_bg_color },
    })
    setError(null)
  }, [schema.id, schema.primary_color, schema.header_bg_color, schema.sidebar_bg_color, schema.section_bg_color, schema.surface_bg_color, schema.dark_primary_color, schema.dark_header_bg_color, schema.dark_sidebar_bg_color, schema.dark_section_bg_color, schema.dark_surface_bg_color])

  const updateSchema = useUpdateSchema()
  const { setTemplate } = useTemplate()
  const importSingle = useTemplateImportSingle()
  const fileInputRef = useRef(null)
  const [importFile, setImportFile] = useState(null)
  const [importError, setImportError] = useState(null)
  const [isImporting, setIsImporting] = useState(false)

  const handleSave = useCallback(async () => {
    setIsSaving(true)
    setError(null)
    try {
      const updated = await updateSchema.mutateAsync({
        id: schema.id,
        primary_color: colors.light.primary,
        header_bg_color: colors.light.header,
        sidebar_bg_color: colors.light.sidebar,
        section_bg_color: colors.light.section,
        surface_bg_color: colors.light.surface,
        dark_primary_color: colors.dark.primary,
        dark_header_bg_color: colors.dark.header,
        dark_sidebar_bg_color: colors.dark.sidebar,
        dark_section_bg_color: colors.dark.section,
        dark_surface_bg_color: colors.dark.surface,
      })
      setTemplate(updated)
    } catch {
       setError(t('settings.template.save_failed', 'Failed to save template colors. Please try again.'))
    } finally {
      setIsSaving(false)
    }
  }, [colors, schema.id, t, updateSchema, setTemplate])

  const handleExport = useCallback(async () => {
    try {
      const response = await api.get(`/schemas/${schema.id}/template`, { responseType: 'blob' })
      const cd = response.headers['content-disposition'] || ''
      const filename = cd.match(/filename="([^"]+)"/)?.[1] || `schema-template.json`
      const url = URL.createObjectURL(new Blob([response.data], { type: 'application/json' }))
      const a = document.createElement('a')
      a.href = url
      a.download = filename
      a.click()
      URL.revokeObjectURL(url)
    } catch {
       setError(t('settings.template.export_failed', 'Export failed. Please try again.'))
    }
  }, [schema.id, t])

  const handleImportClick = useCallback(() => {
    setImportError(null)
    fileInputRef.current?.click()
  }, [])

  const handleFileChange = useCallback((e) => {
    const file = e.target.files?.[0]
    if (file) {
      setImportFile(file)
      setImportError(null)
    }
    e.target.value = ''
  }, [])

  const handleImportApply = useCallback(async ({ light, dark }) => {
    setIsImporting(true)
    setImportError(null)
    try {
      const updated = await importSingle.mutateAsync({ schemaId: schema.id, light, dark })
      setImportFile(null)
      setColors({
        light: {
          primary: updated.primary_color,
          header: updated.header_bg_color,
          sidebar: updated.sidebar_bg_color,
          section: updated.section_bg_color,
          surface: updated.surface_bg_color,
        },
        dark: {
          primary: updated.dark_primary_color,
          header: updated.dark_header_bg_color,
          sidebar: updated.dark_sidebar_bg_color,
          section: updated.dark_section_bg_color,
          surface: updated.dark_surface_bg_color,
        },
      })
    } catch (e) {
       setImportError(e.response?.data?.detail || t('settings.template.import_failed', 'Import failed. Please check the file and try again.'))
    } finally {
      setIsImporting(false)
    }
  }, [importSingle, schema.id, t])

  const handleImportCancel = useCallback(() => {
    setImportFile(null)
    setImportError(null)
  }, [])

  return (
    <div>
      <div className="border-b border-border">
        <nav className="flex gap-6">
          <button
            onClick={() => setActiveMode('light')}
            className={cn('pb-3 text-sm font-medium transition-colors', activeMode === 'light' ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground')}
          >
             {t('settings.template.light', 'Light')}
          </button>
          <button
            onClick={() => setActiveMode('dark')}
            className={cn('pb-3 text-sm font-medium transition-colors', activeMode === 'dark' ? 'border-b-2 border-primary text-foreground' : 'text-muted-foreground hover:text-foreground')}
          >
             {t('settings.template.dark', 'Dark')}
          </button>
        </nav>
      </div>

      <div className="flex flex-col md:flex-row gap-6 items-start mt-6">
        <div className="flex-1 space-y-6">
           {COLOR_FIELDS.map(({ key }) => (
             <div key={key} className="space-y-1.5">
               <label className="text-sm font-medium">{colorLabel(key, t)}</label>
              <HexColorPicker
                color={colors[activeMode][key]}
                onChange={(newColor) => {
                  setColors(prev => ({ ...prev, [activeMode]: { ...prev[activeMode], [key]: newColor } }))
                  setError(null)
                }}
              />
              <p className="text-xs text-muted-foreground font-mono">{colors[activeMode][key]}</p>
            </div>
          ))}
        </div>

        <div className="w-72 shrink-0 sticky top-4 space-y-4">
          <TemplatePreview colors={colors} mode={activeMode} />
          <Button loading={isSaving} onClick={handleSave} data-testid="template-save-button" className="w-full">
             {t('common.save_changes', 'Save Changes')}
          </Button>
          <Button
            variant="outline"
            size="md"
            leftIcon="download"
            onClick={handleExport}
            data-testid="template-export-button"
            className="w-full"
          >
             {t('common.export', 'Export')}
          </Button>
          <Button
            variant="outline"
            size="md"
            leftIcon="upload"
            onClick={handleImportClick}
            data-testid="template-import-button"
            className="w-full"
          >
             {t('common.import', 'Import')}
          </Button>
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            onChange={handleFileChange}
            className="hidden"
            data-testid="template-import-file-input"
          />
          {error && <p className="text-sm text-destructive">{error}</p>}
          {importError && <p className="text-sm text-destructive">{importError}</p>}
        </div>
      </div>

      {importFile && (
        <ImportPreviewModal
          schema={schema}
          file={importFile}
          applying={isImporting}
          onApply={handleImportApply}
          onCancel={handleImportCancel}
        />
      )}
    </div>
  )
}
