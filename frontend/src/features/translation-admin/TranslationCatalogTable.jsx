import { useCallback, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from '../../shared/components/Button'
import Table from '../../shared/components/Table'
import EmptyState from '../../shared/components/EmptyState'

// Wraps the machine-readable key only at `.` boundaries (never mid-word) by
// inserting zero-width spaces after every dot, matching the UI-SPEC's "wrap
// only at `.` boundaries; never truncate the only copy of a key" rule.
function wrapKeyForDisplay(key) {
  return key.split('.').join('.\u200b')
}

function fallbackBadgeText(t, fallbackSource) {
  if (fallbackSource === 'english') return t('translation_admin.fallback.english', 'English fallback')
  return t('translation_admin.fallback.code_default', 'Code default')
}

/**
 * Accessible read-only/editable Translation catalog table (D-01, D-05,
 * D-08..D-15, T-233-11) plus the neutral review projection (D-03, T-307-16).
 *
 * Browse mode is the existing manual editor, unchanged. Review mode narrows
 * the SAME table to the plugin-projected current block: draft cells show a
 * dashed border and an "Unsaved draft" chip, Enter/blur never commit, Reset is
 * absent and read-only users see static full-contrast text with no mutation
 * controls. The table itself knows no plugin API, no suggestion table and no
 * reason classifier — a row's `reasonText` is supplied by the plugin.
 */
export default function TranslationCatalogTable({
  rows,
  editorLanguage,
  editorLanguageName,
  hasUpdate,
  hasDelete,
  commitField,
  getFieldStatus,
  review = null,
  reviewReadonly = false,
  reviewBusy = false,
  onReviewChange,
  onReviewDiscard,
}) {
  const { t } = useTranslation()
  const [inputValues, setInputValues] = useState({})
  const [deletionRequired, setDeletionRequired] = useState({})
  const [resetKeys, setResetKeys] = useState({})
  const [discardState, setDiscardState] = useState({})
  const reviewInputRefs = useRef(new Map())
  const inReview = !!review
  const showActionsColumn = inReview ? !reviewReadonly : (hasUpdate || hasDelete)
  const displayRows = inReview ? review.rows : rows

  const fieldKey = useCallback((row) => `${row.id}:${editorLanguage}`, [editorLanguage])

  const getInputValue = useCallback((row) => {
    const key = fieldKey(row)
    if (key in inputValues) return inputValues[key]
    return row.stored_value ?? ''
  }, [inputValues, fieldKey])

  const setInputValue = useCallback((row, value) => {
    const key = fieldKey(row)
    setInputValues((prev) => ({ ...prev, [key]: value }))
    setDeletionRequired((prev) => {
      if (!prev[key]) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    setResetKeys((prev) => {
      if (!prev[key]) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
  }, [fieldKey])

  const handleCommit = useCallback((row) => {
    if (!hasUpdate) return
    const key = fieldKey(row)
    const currentValue = getInputValue(row)
    const committedValue = row.stored_value ?? ''
    // A pristine fallback field must not save/delete merely because it
    // receives and loses focus (UI-SPEC "Fallback, edit, auto-save...").
    if (currentValue === committedValue) return

    const trimmed = currentValue.trim()
    if (trimmed === '' && committedValue !== '' && !hasDelete) {
      setDeletionRequired((prev) => ({ ...prev, [key]: true }))
      return
    }
    setDeletionRequired((prev) => {
      if (!prev[key]) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    setResetKeys((prev) => {
      if (!prev[key]) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    commitField(row, editorLanguage, currentValue)
  }, [hasUpdate, hasDelete, fieldKey, getInputValue, commitField, editorLanguage])

  const handleReset = useCallback((row) => {
    if (!hasDelete) return
    const key = fieldKey(row)
    // Clear the field to '' immediately (D-10 "clears...immediately") rather
    // than deleting the override — deleting it would fall back to the still-
    // cached row.stored_value until the DELETE request resolves, flashing
    // the pre-reset text back into the input.
    setInputValues((prev) => ({ ...prev, [key]: '' }))
    setDeletionRequired((prev) => {
      if (!prev[key]) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    setResetKeys((prev) => ({ ...prev, [key]: true }))
    commitField(row, editorLanguage, '')
  }, [hasDelete, fieldKey, commitField, editorLanguage])

  // A review discard waits for the owner's confirmation; on success focus moves
  // to the next row's draft input, on failure the row stays with an error line.
  const handleReviewDiscard = useCallback(async (uiStringId) => {
    const ids = displayRows.map((row) => row.uiStringId)
    const nextId = ids[ids.indexOf(uiStringId) + 1] ?? null
    setDiscardState((prev) => ({ ...prev, [uiStringId]: 'pending' }))
    const confirmed = await onReviewDiscard?.(uiStringId)
    setDiscardState((prev) => {
      const next = { ...prev }
      if (confirmed === true) delete next[uiStringId]
      else next[uiStringId] = 'failed'
      return next
    })
    if (confirmed === true && nextId !== null) reviewInputRefs.current.get(nextId)?.focus()
  }, [displayRows, onReviewDiscard])

  const handleKeyDown = useCallback((event, row) => {
    if (event.key !== 'Enter') return
    event.preventDefault()
    handleCommit(row)
  }, [handleCommit])

  return (
    <>
      {inReview ? (
        <p className="mb-2 text-xs text-muted-foreground" data-testid="translation-review-instruction">
          {t('plugins.seams.translation.review_instruction', 'Edit suggestions as needed, then choose Apply translations. Pressing Enter does not save a suggestion.')}
        </p>
      ) : hasUpdate && rows.length > 0 ? (
        <p className="mb-2 text-xs text-muted-foreground" data-testid="translation-admin-save-hint">
          {t('plugins.seams.translation.admin_save_hint', 'Unsaved draft. Press Enter to save this translation.')}
        </p>
      ) : null}
      {displayRows.length === 0 ? (
        <EmptyState
          variant="no-results"
          title={t('translation_admin.table.no_match_title', 'No matching translations')}
          description={t('translation_admin.table.no_match_description', 'No keys or values match your search.')}
        />
      ) : (
      <Table tableClassName="min-w-[900px]" data-testid="translation-catalog-table">
        <thead>
          <tr className="border-b sticky top-0 bg-background">
            <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('translation_admin.table.namespace', 'Namespace')}</th>
            <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('translation_admin.table.key', 'Key')}</th>
            <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{t('translation_admin.table.code_default', 'Code default')}</th>
            <th scope="col" className="text-left px-4 py-3 text-sm font-medium">{editorLanguageName}</th>
            {showActionsColumn && (
              <th scope="col" className="text-right px-4 py-3 text-sm font-medium" data-testid="translation-actions-header">
                {t('translation_admin.table.actions', 'Actions')}
              </th>
            )}
          </tr>
        </thead>
        <tbody>
          {displayRows.map((row) => {
            if (inReview) {
              const draft = row.draft
              return (
                <tr key={row.uiStringId} className="border-b last:border-0 hover:bg-muted/30">
                  <td className="px-4 py-3 text-sm text-muted-foreground align-top">
                    {row.namespace ?? t('translation_admin.namespace_uncategorized', 'Uncategorized')}
                  </td>
                  <td className="px-4 py-3 text-xs font-mono align-top" style={{ wordBreak: 'break-word' }}>
                    {wrapKeyForDisplay(row.key)}
                  </td>
                  <td className="px-4 py-3 text-sm text-muted-foreground align-top">{row.codeDefault}</td>
                  <td className="px-4 py-3 text-sm align-top">
                    <div className="space-y-1">
                      {reviewReadonly ? (
                        <span data-testid={`translation-review-value-${row.uiStringId}`} className="text-sm text-foreground">
                          {draft?.value ?? ''}
                        </span>
                      ) : (
                        <input
                          type="text"
                          ref={(element) => {
                            if (element) reviewInputRefs.current.set(row.uiStringId, element)
                            else reviewInputRefs.current.delete(row.uiStringId)
                          }}
                          data-testid={`translation-review-input-${row.uiStringId}`}
                          aria-label={`${row.key} (${editorLanguageName})`}
                          value={draft?.value ?? ''}
                          readOnly={!draft}
                          onChange={(e) => onReviewChange?.(row.uiStringId, e.target.value)}
                          className={
                            'w-full rounded border px-2 py-1 text-sm bg-background focus:outline-none focus:ring-1 focus:ring-primary ' +
                            (draft ? 'border-dashed border-muted-foreground' : 'border-input')
                          }
                        />
                      )}
                      {draft && (
                        <span
                          data-testid={`translation-review-chip-${row.uiStringId}`}
                          className="inline-flex h-5 items-center rounded-full bg-muted px-2 text-xs text-muted-foreground"
                        >
                          {t('plugins.seams.translation.draft_marker', 'Unsaved draft')}
                        </span>
                      )}
                      {draft?.reasonText && (
                        <div role="alert" className="text-xs text-destructive">{draft.reasonText}</div>
                      )}
                    </div>
                  </td>
                  {showActionsColumn && (
                    <td className="px-4 py-3 text-right align-top">
                      {draft && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          data-testid={`translation-review-discard-${row.uiStringId}`}
                          disabled={reviewBusy || discardState[row.uiStringId] === 'pending'}
                          loading={discardState[row.uiStringId] === 'pending'}
                          onClick={() => handleReviewDiscard(row.uiStringId)}
                        >
                          {t('plugins.seams.translation.discard_suggestion', 'Discard suggestion')}
                        </Button>
                      )}
                      {discardState[row.uiStringId] === 'failed' && (
                        <div
                          role="alert"
                          className="mt-1 text-xs text-destructive text-left"
                          data-testid={`translation-review-discard-error-${row.uiStringId}`}
                        >
                          {t('plugins.seams.translation.discard_failed', 'The suggestion was not discarded. Try again.')}
                        </div>
                      )}
                    </td>
                  )}
                </tr>
              )
            }

            const key = fieldKey(row)
            const status = getFieldStatus(row.id, editorLanguage)
            const hasStoredValue = row.stored_value !== null && row.stored_value !== undefined
            const showFallbackBadge = !hasStoredValue

            return (
              <tr key={row.id} className="border-b last:border-0 hover:bg-muted/30">
                <td className="px-4 py-3 text-sm text-muted-foreground align-top">
                  {row.namespace ?? t('translation_admin.namespace_uncategorized', 'Uncategorized')}
                </td>
                <td className="px-4 py-3 text-xs font-mono align-top" style={{ wordBreak: 'break-word' }}>
                  {wrapKeyForDisplay(row.key)}
                </td>
                <td className="px-4 py-3 text-sm text-muted-foreground align-top">{row.default_text}</td>
                <td className="px-4 py-3 text-sm align-top">
                  {hasUpdate ? (
                    <div className="space-y-1">
                      <input
                        type="text"
                        data-testid={`translation-editor-input-${row.id}`}
                        aria-label={`${row.key} (${editorLanguageName})`}
                        value={getInputValue(row)}
                        placeholder={!hasStoredValue ? row.effective_value : undefined}
                        onChange={(e) => setInputValue(row, e.target.value)}
                        onBlur={() => handleCommit(row)}
                        onKeyDown={(e) => handleKeyDown(e, row)}
                        className="w-full rounded border px-2 py-1 text-sm bg-background border-input focus:outline-none focus:ring-1 focus:ring-primary placeholder:italic placeholder:text-muted-foreground"
                      />
                      {showFallbackBadge && (
                        <span
                          data-testid={`translation-fallback-badge-${row.id}`}
                          className="inline-flex items-center h-5 px-2 text-xs rounded-full bg-muted text-muted-foreground"
                        >
                          {fallbackBadgeText(t, row.fallback_source)}
                        </span>
                      )}
                      {deletionRequired[key] && (
                        <div
                          data-testid={`translation-deletion-required-${row.id}`}
                          className="text-xs text-muted-foreground"
                        >
                          {t('translation.delete_requires_permission', 'Deleting or resetting a translation requires delete permission.')}
                        </div>
                      )}
                      {status.status === 'error' && (
                        <div
                          role="alert"
                          data-testid={`translation-error-${row.id}`}
                          className="text-xs text-destructive"
                        >
                          {t('translation_admin.error.save_failed', "Couldn't save translation. Press Enter or move focus away to try again.")}
                        </div>
                      )}
                      {(status.status === 'saving' || status.status === 'saved') && (
                        <div
                          aria-live="polite"
                          data-testid={`translation-status-${row.id}`}
                          className="text-xs text-muted-foreground"
                        >
                          {status.status === 'saving'
                            ? t('translation_admin.status.saving', 'Saving…')
                            : (resetKeys[key]
                              ? t('translation_admin.reset_feedback', 'Translation reset to default.')
                              : t('translation_admin.status.saved', 'Saved'))}
                        </div>
                      )}
                    </div>
                  ) : (
                    <div className="space-y-1">
                      <span data-testid={`translation-value-cell-${row.id}`}>{row.effective_value}</span>
                      {showFallbackBadge && (
                        <span
                          data-testid={`translation-fallback-badge-${row.id}`}
                          className="ml-2 inline-flex items-center h-5 px-2 text-xs rounded-full bg-muted text-muted-foreground"
                        >
                          {fallbackBadgeText(t, row.fallback_source)}
                        </span>
                      )}
                      {hasDelete && (status.status === 'saving' || status.status === 'saved') && (
                        <div
                          aria-live="polite"
                          data-testid={`translation-status-${row.id}`}
                          className="text-xs text-muted-foreground"
                        >
                          {status.status === 'saving'
                            ? t('translation_admin.status.saving', 'Saving…')
                            : t('translation_admin.reset_feedback', 'Translation reset to default.')}
                        </div>
                      )}
                    </div>
                  )}
                </td>
                {showActionsColumn && (
                  <td className="px-4 py-3 text-right align-top">
                    {hasDelete && (
                      <Button
                        type="button"
                        variant="outline"
                        size="sm"
                        leftIcon="rotate-ccw"
                        data-testid={`translation-reset-${row.id}`}
                        onClick={() => handleReset(row)}
                      >
                        {t('translation_admin.reset', 'Reset to default')}
                      </Button>
                    )}
                  </td>
                )}
              </tr>
            )
          })}
        </tbody>
      </Table>
      )}
    </>
  )
}

