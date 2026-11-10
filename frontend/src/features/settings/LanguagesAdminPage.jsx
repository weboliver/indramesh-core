import { useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import { useLanguages, useUpdateLanguage, useCreateLanguage, useDeleteLanguage } from '../../shared/hooks/use-languages'
import PageHeader from '../../shared/components/PageHeader'
import TableSkeleton from '../../shared/components/TableSkeleton'
import EmptyState from '../../shared/components/EmptyState'
import { Button } from '../../shared/components/Button'
import { FormField, TextInput, CheckboxField } from '../../shared/components/FormControls'
import { useCan } from '../../shared/components/Can'
import { showToast } from '../../shared/components/Toast'
import { cn } from '../../shared/lib/utils'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { useLanguageCoverage } from './use-language-coverage'

const DEACTIVATE_IMPACT_MESSAGE =
  'Deactivating hides existing translations and disables the language in schemas until reactivated.'

// Byte-identical to the backend's `_LANGUAGE_CODE_PATTERN` (D-05): the browser
// only mirrors the authoritative server-side validation.
const LANGUAGE_CODE_PATTERN =
  /^[a-z]{2,3}(-[a-z]{4})?(-([a-z]{2}|[0-9]{3}))?(-([a-z0-9]{5,8}|[0-9][a-z0-9]{3}))*$/

export default function LanguagesAdminPage() {
  const { t } = useTranslation()
  const queryClient = useQueryClient()
  const { data: languages = [], isLoading, isError, refetch } = useLanguages()
  const updateLanguage = useUpdateLanguage()
  const createLanguage = useCreateLanguage()
  const deleteLanguage = useDeleteLanguage()
  const canCreate = useCan('languages', 'create')
  const canCoverage = useCan('translations', 'read')
  const canDeleteLanguages = useCan('languages', 'delete')

  const [panelOpen, setPanelOpen] = useState(false)
  const [code, setCode] = useState('')
  const [name, setName] = useState('')
  const [activateNow, setActivateNow] = useState(true)
  const [codeError, setCodeError] = useState(null)
  const [nameError, setNameError] = useState(null)
  const [pendingDeleteId, setPendingDeleteId] = useState(null)

  const hasActiveLanguage = languages.some((language) => language.is_active)
  const editorLanguage = languages.find((language) => language.is_active)?.code || 'en'
  const { coverage } = useLanguageCoverage(editorLanguage, canCoverage && hasActiveLanguage)

  const skeletonCols = 2 + (canCoverage ? 1 : 0) + (canDeleteLanguages ? 1 : 0)

  const rowGridClass = cn(
    'flex flex-wrap items-center gap-x-4 gap-y-2 px-4 py-3 sm:grid sm:items-center',
    canCoverage && canDeleteLanguages && 'sm:grid-cols-[minmax(0,1fr)_11rem_2.25rem_auto]',
    canCoverage && !canDeleteLanguages && 'sm:grid-cols-[minmax(0,1fr)_11rem_2.25rem]',
    !canCoverage && canDeleteLanguages && 'sm:grid-cols-[minmax(0,1fr)_2.25rem_auto]',
    !canCoverage && !canDeleteLanguages && 'sm:grid-cols-[minmax(0,1fr)_2.25rem]'
  )

  const focusField = (id) => {
    document.getElementById(id)?.focus()
  }

  const refreshCoverage = () => queryClient.invalidateQueries({ queryKey: ['language-coverage'] })

  const handleToggle = async (language) => {
    const activating = !language.is_active

    if (!activating) {
      const confirmed = await confirmDialog({
        message: interpolateDefault(t('settings.languages.confirm_deactivate', 'This language is enabled in schema(s) and has translation(s). {{impact}}'), { impact: t('settings.languages.deactivate_impact', DEACTIVATE_IMPACT_MESSAGE) }),
        danger: false,
      })
      if (!confirmed) return
    }

    updateLanguage.mutate(
      { id: language.id, is_active: activating },
      {
        onSuccess: () => {
          refreshCoverage()
          showToast(
             activating
               ? interpolateDefault(t('toasts.languages.activated', '{{name}} activated.'), { name: language.name })
               : interpolateDefault(t('toasts.languages.deactivated', '{{name}} deactivated.'), { name: language.name }),
            { type: 'success' }
          )
        },
        onError: () => {
           showToast(t('toasts.languages.update_failed', 'Failed to update language.'), { type: 'error' })
        },
      }
    )
  }

  const resetPanel = () => {
    setPanelOpen(false)
    setCode('')
    setName('')
    setActivateNow(true)
    setCodeError(null)
    setNameError(null)
  }

  const handleCreate = (event) => {
    event.preventDefault()

    const normalizedCode = code.trim().toLowerCase()
    const trimmedName = name.trim()
    const nextCodeError = LANGUAGE_CODE_PATTERN.test(normalizedCode) ? null : t('settings.languages.create_invalid_code', 'Enter a code of two or three letters, optionally with a region (for example pt-BR).')
    const nextNameError = trimmedName ? null : t('settings.languages.name_required', 'Enter a display name.')

    setCodeError(nextCodeError)
    setNameError(nextNameError)

    if (nextCodeError) {
      focusField('language-code')
      return
    }
    if (nextNameError) {
      focusField('language-name')
      return
    }

    createLanguage.mutate(
      { code: normalizedCode, name: trimmedName, is_active: activateNow },
      {
        onSuccess: () => {
          resetPanel()
          refreshCoverage()
          showToast(interpolateDefault(t('settings.languages.created', '{{name}} added.'), { name: trimmedName }))
          focusField('languages-add')
        },
        onError: (error) => {
          const status = error?.response?.status
          if (status === 409) {
            setCodeError(interpolateDefault(t('settings.languages.create_duplicate', 'The language code "{{code}}" already exists.'), { code: normalizedCode }))
            focusField('language-code')
            return
          }
          if (status === 422) {
            setCodeError(t('settings.languages.create_invalid_code', 'Enter a code of two or three letters, optionally with a region (for example pt-BR).'))
            focusField('language-code')
            return
          }
          showToast(t('settings.languages.create_failed', 'The language could not be created. Try again.'), { type: 'error' })
        },
      }
    )
  }

  const handleDelete = async (language) => {
    const confirmed = await confirmDialog({
      message: interpolateDefault(t('settings.languages.delete_confirm', 'Delete the language "{{name}}"? This cannot be undone.'), { name: language.name }),
      danger: true,
      confirmLabel: t('common.delete', 'Delete'),
    })
    if (!confirmed) return

    setPendingDeleteId(language.id)
    deleteLanguage.mutate(language.id, {
      onSuccess: () => {
        refreshCoverage()
        showToast(interpolateDefault(t('settings.languages.deleted', '{{name}} deleted.'), { name: language.name }))
      },
      onError: (error) => {
        const status = error?.response?.status
        showToast(
          status === 409
            ? t('settings.languages.delete_referenced', 'The language is still enabled in one or more schemas. Remove it there first, then delete it.')
            : t('settings.languages.delete_failed', 'The language could not be deleted. Try again.'),
          { type: 'error' }
        )
      },
      onSettled: () => setPendingDeleteId(null),
    })
  }

  return (
    <div className="space-y-6" data-testid="languages-admin-page">
      <PageHeader
         title={t('settings.languages.title', 'Languages')}
         description={t('settings.languages.description', 'Manage which languages are available system-wide. Languages enabled here can be selected per schema.')}
      >
        {canCreate && (
          <Button
            id="languages-add"
            variant="primary"
            size="sm"
            leftIcon="plus"
            data-testid="languages-add"
            onClick={() => setPanelOpen((open) => !open)}
          >
            {t('settings.languages.add', 'Add language')}
          </Button>
        )}
      </PageHeader>

      {canCreate && panelOpen && (
        <form
          data-testid="languages-create-form"
          className="rounded-md border bg-muted/30 p-4 space-y-4"
          onSubmit={handleCreate}
          noValidate
        >
          <div className="space-y-1">
            <h2 className="text-sm font-semibold">{t('settings.languages.create_title', 'New language')}</h2>
            <p className="text-sm text-muted-foreground">{t('settings.languages.create_description', 'The language is created without a bundled catalog. Untranslated text stays in English until it is translated.')}</p>
          </div>

          <div className="grid gap-4 md:grid-cols-2">
            <FormField
              id="language-code"
              label={t('settings.languages.code_label', 'Language code')}
              required
              help={t('settings.languages.code_help', 'Two or three lowercase letters, optionally with a region: sv or pt-BR.')}
              error={codeError}
            >
              <TextInput
                id="language-code"
                data-testid="language-code-input"
                value={code}
                onChange={(event) => setCode(event.target.value)}
                maxLength={10}
                autoComplete="off"
                autoCapitalize="none"
                spellCheck={false}
                autoFocus
                placeholder={t('settings.languages.code_placeholder', 'e.g. sv or pt-BR')}
                help={t('settings.languages.code_help', 'Two or three lowercase letters, optionally with a region: sv or pt-BR.')}
                error={codeError}
              />
            </FormField>

            <FormField
              id="language-name"
              label={t('settings.languages.name_label', 'Display name (English)')}
              required
              help={t('settings.languages.name_help', 'This name appears in every language selector.')}
              error={nameError}
            >
              <TextInput
                id="language-name"
                data-testid="language-name-input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                maxLength={100}
                autoComplete="off"
                placeholder={t('settings.languages.name_placeholder', 'e.g. Swedish')}
                help={t('settings.languages.name_help', 'This name appears in every language selector.')}
                error={nameError}
              />
            </FormField>
          </div>

          <CheckboxField
            id="language-active"
            data-testid="language-active-checkbox"
            label={t('settings.languages.active_label', 'Activate language immediately')}
            help={t('settings.languages.active_help', 'The language appears in all selectors right away. Untranslated text falls back to English.')}
            checked={activateNow}
            onChange={(event) => setActivateNow(event.target.checked)}
          />

          <div className="flex justify-end gap-2">
            <Button
              type="button"
              variant="outline"
              size="sm"
              data-testid="language-create-cancel"
              disabled={createLanguage.isPending}
              onClick={resetPanel}
            >
              {t('common.cancel', 'Cancel')}
            </Button>
            <Button
              type="submit"
              variant="primary"
              size="sm"
              data-testid="language-create-submit"
              loading={createLanguage.isPending}
              disabled={createLanguage.isPending}
            >
              {t('settings.languages.create_submit', 'Create language')}
            </Button>
          </div>
        </form>
      )}

      {isLoading && <TableSkeleton rows={5} cols={skeletonCols} />}

      {isError && !isLoading && (
        <EmptyState variant="error" onButtonClick={() => refetch()} />
      )}

      {!isLoading && !isError && languages.length === 0 && (
        <EmptyState
           title={t('settings.languages.empty_title', 'No languages available')}
           description={t('settings.languages.empty_description', 'System languages have not been seeded. Contact your administrator.')}
        />
      )}

      {!isLoading && !isError && languages.length > 0 && (
        <div className="bg-card border rounded-lg divide-y">
          {languages.map((language) => (
            <div
              key={language.id}
              className={cn(rowGridClass, !language.is_active && 'opacity-60')}
              data-testid={`lang-row-${language.code}`}
            >
              <span className="min-w-0 break-words text-sm">
                {language.name} <span className="text-muted-foreground">({language.code})</span>
              </span>

              {canCoverage && (
                language.is_active && coverage[language.code] ? (
                  <Link
                    to="/tools/translation"
                    data-testid={`lang-coverage-${language.code}`}
                    className="text-sm text-primary hover:underline whitespace-nowrap sm:text-right"
                  >
                    <span className="sr-only">{interpolateDefault(t('settings.languages.coverage_open', 'Open translations for {{name}}'), { name: language.name })}: </span>
                    {interpolateDefault(t('translation_admin.coverage.count', '{{translated}} / {{total}} translated'), { translated: coverage[language.code].translated, total: coverage[language.code].total })}
                  </Link>
                ) : (
                  <span className="text-sm text-muted-foreground sm:text-right">—</span>
                )
              )}

              <button
                role="switch"
                aria-checked={language.is_active}
                 aria-label={interpolateDefault(t('settings.languages.toggle_label', '{{name}} language toggle'), { name: language.name })}
                onClick={() => !updateLanguage.isPending && handleToggle(language)}
                disabled={updateLanguage.isPending}
                className={cn(
                  'relative inline-flex h-5 w-9 shrink-0 rounded-full border-2 border-transparent transition-colors focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-primary focus-visible:ring-offset-2 cursor-pointer',
                  language.is_active ? 'bg-primary' : 'bg-input'
                )}
                data-testid={`lang-toggle-${language.code}`}
              >
                <span
                  className={cn(
                    'pointer-events-none inline-block h-4 w-4 rounded-full bg-background shadow transform ring-0 transition-transform',
                    language.is_active ? 'translate-x-4' : 'translate-x-0'
                  )}
                />
              </button>

              {canDeleteLanguages && language.is_bundled === false && (
                <Button
                  variant="destructive"
                  size="sm"
                  data-testid={`lang-delete-${language.code}`}
                  aria-label={interpolateDefault(t('settings.languages.delete_label', 'Delete {{name}}'), { name: language.name })}
                  loading={deleteLanguage.isPending && pendingDeleteId === language.id}
                  disabled={deleteLanguage.isPending && pendingDeleteId === language.id}
                  onClick={() => handleDelete(language)}
                >
                  {t('common.delete', 'Delete')}
                </Button>
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
