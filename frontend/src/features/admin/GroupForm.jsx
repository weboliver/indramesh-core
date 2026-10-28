import { useState } from 'react'
import { Button } from '../../shared/components/Button'
import { useTranslation } from 'react-i18next'

export default function GroupForm({ mode = 'create', initialValues, onSubmit, onCancel }) {
  const { t } = useTranslation()
  const [name, setName] = useState(initialValues?.name || '')
  const [saving, setSaving] = useState(false)

  const handleSubmit = async (e) => {
    e.preventDefault()
    setSaving(true)
    try {
      await onSubmit({ name })
    } finally {
      setSaving(false)
    }
  }

  return (
    <form
      onSubmit={handleSubmit}
      className="flex flex-col gap-2 p-3 border rounded-md bg-muted/30"
      data-testid={mode === 'create' ? 'create-group-form' : 'edit-group-form'}
    >
      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder={t('common.name', 'Name')}
        required
        className="rounded-md border border-input bg-background px-3 py-1.5 text-sm text-foreground font-normal"
        data-testid="group-name-input"
      />
      <div className="flex gap-2 justify-end">
        <Button type="button" onClick={onCancel} variant="outline" size="sm" disabled={saving}>
          {t('common.cancel', 'Cancel')}
        </Button>
        <Button type="submit" variant="primary" size="sm" disabled={saving} loading={saving}>
          {mode === 'create' ? t('admin.groups.create_submit', 'Create Group') : t('admin.groups.save_submit', 'Save Group')}
        </Button>
      </div>
    </form>
  )
}