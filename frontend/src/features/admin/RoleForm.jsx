import { useState } from 'react'
import { Button } from '../../shared/components/Button'
import { useTranslation } from 'react-i18next'

export default function RoleForm({ mode = 'create', initialValues, onSubmit, onCancel }) {
  const { t } = useTranslation()
  const [name, setName] = useState(initialValues?.name || '')
  const [description, setDescription] = useState(initialValues?.description || '')
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      await onSubmit({ name, description: description || undefined })
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-2 p-3 border rounded-md bg-muted/30"
      data-testid={mode === 'create' ? 'create-role-form' : 'edit-role-form'}
    >
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
         placeholder={t('common.name', 'Name')}
        required
        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground font-normal"
        data-testid="role-name-input"
      />
      <textarea
        value={description}
        onChange={(e) => setDescription(e.target.value)}
         placeholder={t('common.description_optional', 'Description (optional)')}
        rows={2}
        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground font-normal"
        data-testid="role-description-input"
      />
      <div className="flex gap-2 justify-end">
        <Button type="button" onClick={onCancel} variant="outline" size="sm" disabled={saving}>
           {t('common.cancel', 'Cancel')}
        </Button>
        <Button type="submit" variant="primary" size="sm" disabled={saving} loading={saving}>
           {mode === 'create' ? t('common.create', 'Create') : t('common.save', 'Save')}
        </Button>
      </div>
    </form>
  )
}
