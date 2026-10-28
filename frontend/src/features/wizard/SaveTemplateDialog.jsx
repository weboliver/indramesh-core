import { useState } from 'react'
import { createPortal } from 'react-dom'
import { Button } from '../../shared/components/Button'
import { useTranslation } from 'react-i18next'

export default function SaveTemplateDialog({ isOpen, onClose, onSave, isSaving, error }) {
  const { t } = useTranslation()
  const [templateName, setTemplateName] = useState('')
  const [description, setDescription] = useState('')   // REQUIRED — both states needed

  if (!isOpen) return null

  const handleSave = () => {
    onSave({ name: templateName.trim(), description: description.trim() || null })
  }

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-[60] flex items-center justify-center"
      onClick={onClose}
    >
      <div
        className="rounded-lg border bg-card p-6 w-full max-w-md space-y-4 shadow-lg"
        onClick={e => e.stopPropagation()}
      >
         <h3 className="text-lg font-semibold">{t('wizard.template.save_title', 'Save as Template')}</h3>

        <div className="space-y-1.5">
          <label htmlFor="template-name" className="text-sm font-medium">
             {t('wizard.template.name_label', 'Template name')} <span className="text-destructive">*</span>
          </label>
          <input
            id="template-name"
            type="text"
            value={templateName}
            onChange={e => setTemplateName(e.target.value)}
             placeholder={t('wizard.template.name_placeholder', 'e.g., IT Asset Database')}
            className="w-full rounded-md border px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
            autoFocus
            data-testid="template-name-input"
          />
        </div>

        <div className="space-y-1.5">
          <label htmlFor="template-desc" className="text-sm font-medium">
             {t('wizard.template.description_label', 'Description')} <span className="text-muted-foreground font-normal">{t('wizard.template.optional', '(optional)')}</span>
          </label>
          <textarea
            id="template-desc"
            value={description}
            onChange={e => setDescription(e.target.value)}
            rows={3}
             placeholder={t('wizard.template.description_placeholder', 'Brief description of this template...')}
            className="w-full rounded-md border px-3 py-2 text-sm bg-background resize-none focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary"
            data-testid="template-desc-textarea"
          />
        </div>

        {error && (
          <p className="text-sm text-destructive">{error}</p>
        )}

        <div className="flex justify-end gap-3 pt-2">
          <Button type="button" variant="outline" size="sm" onClick={onClose} disabled={isSaving}>
             {t('common.cancel', 'Cancel')}
          </Button>
          <Button
            type="button"
            variant="primary"
            size="sm"
            onClick={handleSave}
            disabled={!templateName.trim() || isSaving}
            leftIcon={isSaving ? undefined : 'save'}
            data-testid="template-save-button"
          >
             {isSaving ? t('common.saving', 'Saving...') : t('common.save', 'Save')}
          </Button>
        </div>
      </div>
    </div>,
    document.body
  )
}
