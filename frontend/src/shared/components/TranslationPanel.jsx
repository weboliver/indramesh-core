import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useSchema } from '../hooks/use-schemas'
import { useSchemaLanguages } from '../hooks/use-languages'
import { invalidateTranslatedEntity, useTranslations, useUpsertTranslation } from '../hooks/use-translations'
import { Button } from './Button'
import LoadingSpinner from './LoadingSpinner'
import { showToast } from './Toast'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../lib/i18n-defaults'
import { PHASE231_TRANSLATION_ENTITY_LABELS } from '../lib/phase231-dynamic-contract'
import { usePluginVisibleContributions } from '../plugin-runtime/PluginProvider'
import { PluginContributionBoundary } from '../plugin-runtime/PluginSlotBoundary'
import { useCan } from './Can'
import { translationPairKey } from '../plugin-runtime/contribution-utils'
import { createTranslationDraftSession } from '../plugin-runtime/translation-draft-session'

// D-01/D-04/D-15: the panel is a genuinely neutral manual editor plus a host for
// protected contributor drafts. It knows no plugin API URL, suggestion table or
// LLM. A contribution delivers plain-JSON proposals through `acceptDraft`; the
// host merges them against the latest synchronous state and routes a marked
// pair's Save through the contributor's registered protected commit. A cleared
// pair loses its marker but stays touched. Manual, non-draft Save keeps the
// existing Core upsert.
export default function TranslationPanel({
  entityType,
  entityId,
  schemaId,
  fields = [],
  fieldValues = {},
  translations: translationsProp,
  open,
  onClose,
  focusLang,
}) {
  const { t } = useTranslation()
  const inputRefs = useRef({})
  const [savingKey, setSavingKey] = useState(null)
  const [savedKey, setSavedKey] = useState(null)
  const [inputValues, setInputValues] = useState({})
  const [fieldStatus, setFieldStatus] = useState({})
  const [, setDraftsVersion] = useState(0)

  const [touchedPairs, setTouchedPairs] = useState(new Set())
  const [handoffSummary, setHandoffSummary] = useState(null)
  const [presenceRevision, setPresenceRevision] = useState(0)

  const canWrite = useCan('translations', 'update')

  const scopeCounterRef = useRef(0)
  const [session, setSession] = useState(() =>
    createTranslationDraftSession({ sessionId: 'panel-init', scopeRevision: 0 })
  )
  const [requestScope, setRequestScope] = useState(() => {
    const controller = new AbortController()
    controller.abort()
    return { isCurrent: () => false, signal: controller.signal }
  })

  // D-16: the panel action family may declare an optional async presence
  // predicate. `presenceRevision` lets an owner request a re-evaluation of its
  // own predicate without any host polling.
  const panelActions = usePluginVisibleContributions('translationPanelActions', {
    entityType,
    entityId,
    schemaId,
    sessionId: session.sessionId,
    scopeRevision: session.scopeRevision,
    presenceRevision,
  })

  const { data: schema } = useSchema(schemaId)
  const { data: schemaLanguages, isLoading: languagesLoading } = useSchemaLanguages(schemaId)
  const {
    data: translationsRaw,
    isLoading: translationsLoading,
    refetch: refetchTranslations,
  } = useTranslations(entityType, entityId, {
    enabled: open && !translationsProp && !!entityType && !!entityId,
  })
  const upsertTranslation = useUpsertTranslation()
  const queryClient = useQueryClient()

  const defaultLanguageCode = schema?.default_language_code

  const languages = useMemo(
    () => (Array.isArray(schemaLanguages) ? schemaLanguages : []),
    [schemaLanguages]
  )

  const targetLanguages = useMemo(
    () => languages.filter((language) => language.code !== defaultLanguageCode),
    [languages, defaultLanguageCode]
  )

  const normalizedTranslations = useMemo(() => {
    if (translationsProp) return translationsProp
    const map = {}
    for (const row of (translationsRaw || [])) {
      if (!map[row.language_code]) map[row.language_code] = {}
      map[row.language_code][row.field] = row.value
    }
    return map
  }, [translationsProp, translationsRaw])

  // A new entity or a reopened panel starts a fresh draft session: a proposal
  // can never be accepted or rejected by another entity's edit history, and a
  // late answer from the previous scope is fenced out by session/revision.
  useEffect(() => {
    setTouchedPairs(new Set())
    setHandoffSummary(null)
    setFieldStatus({})
    setInputValues({})
    if (!open) return undefined

    const controller = new AbortController()
    scopeCounterRef.current += 1
    const revision = scopeCounterRef.current
    const next = createTranslationDraftSession({
      sessionId: `panel-${revision}`,
      scopeRevision: revision,
    })
    const unsubscribe = next.subscribe(() => setDraftsVersion((value) => value + 1))
    setSession(next)
    setRequestScope({
      isCurrent: () => next.isCurrent({ sessionId: next.sessionId, scopeRevision: next.scopeRevision }) && !controller.signal.aborted,
      signal: controller.signal,
    })
    return () => {
      controller.abort()
      unsubscribe()
    }
  }, [open, entityType, entityId])

  // Plugin disappearance: an owner that left the panel action family drops
  // exactly its marked drafts and registered handlers. Non-draft manual input
  // and other owners' drafts are preserved.
  const knownOwnersRef = useRef(new Set())
  useEffect(() => {
    const currentOwners = new Set(panelActions.map((entry) => entry.pluginName))
    knownOwnersRef.current.forEach((owner) => {
      if (currentOwners.has(owner)) return
      // A dropped owner's delivered value must leave the field as well: the
      // marked pair is gone, so keeping its text in `inputValues` would let the
      // plain manual upsert path persist an unattributable AI draft (WR-01).
      const droppedKeys = session
        .getDrafts()
        .filter((draft) => draft.owner === owner)
        .map((draft) => `${draft.language}:${draft.field}`)
      session.unregisterOwner(owner)
      if (droppedKeys.length > 0) {
        setInputValues((prev) => {
          const next = { ...prev }
          droppedKeys.forEach((key) => {
            delete next[key]
          })
          return next
        })
      }
    })
    knownOwnersRef.current = currentOwners
  }, [panelActions, session])

  useEffect(() => {
    if (!open || !focusLang || focusLang === defaultLanguageCode) return
    const firstFieldKey = fields[0]?.key
    if (!firstFieldKey) return
    const ref = inputRefs.current[`${focusLang}:${firstFieldKey}`]
    if (ref) ref.focus()
  }, [open, focusLang, defaultLanguageCode, fields])

  useEffect(() => {
    if (!open) return undefined
    function handleKeyDown(e) {
      if (e.key === 'Escape') onClose?.()
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [open, onClose])

  const getInputValue = useCallback((lang, fieldKey) => {
    const key = `${lang}:${fieldKey}`
    if (key in inputValues) return inputValues[key]
    return normalizedTranslations?.[lang]?.[fieldKey] ?? ''
  }, [inputValues, normalizedTranslations])

  // The current synchronous snapshot of every allowed pair and its emptiness.
  // Never synthesized from `fieldValues`; it reflects the host's own state.
  const buildSnapshot = useCallback(() => {
    const latest = {}
    const allowed = new Set()
    const eligible = []
    for (const field of fields) {
      latest[field.key] = {}
      for (const language of targetLanguages) {
        const pairKey = translationPairKey(field.key, language.code)
        const key = `${language.code}:${field.key}`
        const current = key in inputValues
          ? inputValues[key]
          : (normalizedTranslations?.[language.code]?.[field.key] ?? '')
        latest[field.key][language.code] = current
        allowed.add(pairKey)
        if (current === '' && !touchedPairs.has(pairKey)) {
          eligible.push({ field: field.key, language: language.code })
        }
      }
    }
    return { latest, allowed, eligible }
  }, [fields, targetLanguages, inputValues, normalizedTranslations, touchedPairs])

  const setInputValue = (lang, fieldKey, value) => {
    const key = `${lang}:${fieldKey}`
    setInputValues((prev) => ({ ...prev, [key]: value }))
    // Type-then-clear counts as touched: once the user has edited a pair in this
    // session it must never be overwritten by a plugin draft.
    setTouchedPairs((prev) => {
      const next = new Set(prev)
      next.add(translationPairKey(fieldKey, lang))
      return next
    })
    setFieldStatus((prev) => {
      if (!(key in prev)) return prev
      const next = { ...prev }
      delete next[key]
      return next
    })
    // Clearing a drafted field by typing removes the marker; it stays touched.
    if (value === '' && session.hasDraft(fieldKey, lang)) {
      session.clearDraft(fieldKey, lang)
    } else if (value !== '') {
      session.setDraftValue(fieldKey, lang, value)
    }
  }

  // The host applies a contributor delivery against the latest state and shows
  // the existing neutral filled/kept summary. It performs no save/upsert.
  const handleAcceptDraft = (owner, payload = {}) => {
    const { latest, allowed } = buildSnapshot()
    const result = session.acceptDraft({
      owner,
      sessionId: payload.sessionId,
      scopeRevision: payload.scopeRevision,
      proposals: payload.proposals,
      latest,
      touched: touchedPairs,
      allowed,
    })
    const proposalCount = Array.isArray(payload.proposals) ? payload.proposals.length : 0
    if (result.applied.length > 0) {
      // The accepted pair becomes a real per-pair draft: its value must reach
      // the field input immediately, so the visible text and the protected
      // draft identity never drift apart. This is a host-side write, not a user
      // edit, so the pair stays eligible for the draft path (it is not touched).
      setInputValues((prev) => {
        const next = { ...prev }
        for (const pair of result.applied) {
          next[`${pair.language}:${pair.field}`] = pair.value
        }
        return next
      })
      const filled = interpolateDefault(
        t(
          result.applied.length === 1
            ? 'plugins.seams.translation.handoff_filled_one'
            : 'plugins.seams.translation.handoff_filled_other',
          '{{count}} draft fields filled'
        ),
        { count: result.applied.length }
      )
      const kept = interpolateDefault(
        t(
          result.skipped.length === 1
            ? 'plugins.seams.translation.handoff_kept_one'
            : 'plugins.seams.translation.handoff_kept_other',
          '{{count}} fields kept unchanged'
        ),
        { count: result.skipped.length }
      )
      setHandoffSummary(`${filled}; ${kept}. ${t(
        'plugins.seams.translation.handoff_save_hint',
        'Save each field to keep the draft.'
      )}`)
    } else if (proposalCount > 0) {
      setHandoffSummary(t(
        'plugins.seams.translation.nothing_filled',
        'No empty, unchanged fields remain. Existing translations and your edits were kept.'
      ))
    }
    return result
  }

  const handleSave = async (lang, fieldKey) => {
    const key = `${lang}:${fieldKey}`
    const value = getInputValue(lang, fieldKey)
    setSavingKey(key)
    try {
      if (session.hasDraft(fieldKey, lang)) {
        const outcome = await session.commit(fieldKey, lang, value)
        if (outcome.status === 'written') {
          await refetchTranslations?.()
          invalidateTranslatedEntity(queryClient, entityType, entityId)
          setInputValues((prev) => {
            const next = { ...prev }
            delete next[key]
            return next
          })
          setSavedKey(key)
          showToast(t('toasts.translation_saved', 'Translation saved'))
          setTimeout(() => setSavedKey((current) => (current === key ? null : current)), 1500)
        } else if (outcome.status === 'skipped') {
          await refetchTranslations?.()
          setInputValues((prev) => {
            const next = { ...prev }
            delete next[key]
            return next
          })
          setFieldStatus((prev) => ({ ...prev, [key]: 'skipped' }))
        } else {
          setFieldStatus((prev) => ({ ...prev, [key]: 'invalid' }))
        }
        return
      }
      await upsertTranslation.mutateAsync({
        translation_type: entityType,
        entity_id: entityId,
        language_code: lang,
        field: fieldKey,
        value,
      })
      setSavedKey(key)
      showToast(t('toasts.translation_saved', 'Translation saved'))
      setTimeout(() => setSavedKey((current) => (current === key ? null : current)), 1500)
    } catch {
      // Carry-01: fixed, classified copy — never the raw server detail.
      showToast(t('toasts.translation_save_failed', 'Failed to save translation'), { type: 'error' })
    } finally {
      setSavingKey((current) => (current === key ? null : current))
    }
  }

  const handleDiscardDraft = async (lang, fieldKey) => {
    const key = `${lang}:${fieldKey}`
    try {
      const ok = await session.discard(fieldKey, lang)
      if (ok) {
        setInputValues((prev) => {
          const next = { ...prev }
          delete next[key]
          return next
        })
        inputRefs.current[key]?.focus()
      }
    } catch {
      showToast(
        t('plugins.seams.translation.discard_failed', 'The draft was not discarded. Try again.'),
        { type: 'error' }
      )
    }
  }

  const handlePresenceChanged = useCallback(() => setPresenceRevision((value) => value + 1), [])

  const entityLabelConfig = PHASE231_TRANSLATION_ENTITY_LABELS[entityType]
  const entityLabel = entityLabelConfig ? t(entityLabelConfig.key, entityLabelConfig.defaultText) : entityType

  if (!open) return null

  const eligiblePairs = buildSnapshot().eligible

  const buildActionProps = (entry) => {
    const owner = entry.pluginName
    return {
      entityType,
      entityId,
      schemaId,
      fields,
      fieldValues,
      targetLanguages,
      translations: normalizedTranslations,
      sessionId: session.sessionId,
      scopeRevision: session.scopeRevision,
      allowedFields: fields.map((field) => field.key),
      eligiblePairs,
      canWrite,
      requestScope,
      registerDraftHandlers: (handlers) =>
        session.registerDraftHandlers(owner, handlers, { sourceLabel: entry.displayLabel }),
      acceptDraft: (payload) => handleAcceptDraft(owner, payload),
      getCurrentEligiblePairs: () => buildSnapshot().eligible,
      onPresenceChanged: handlePresenceChanged,
      disabled: targetLanguages.length === 0,
    }
  }

  return (
    <div
      role="dialog"
       aria-label={t('common.translations', 'Translations')}
      data-testid="translation-panel"
      className="fixed inset-y-0 right-0 z-50 w-[420px] max-w-[85vw] bg-card border-l shadow-lg flex flex-col"
    >
      <div className="px-4 py-3 border-b flex items-center justify-between">
         <h2 className="text-sm font-semibold">{interpolateDefault(
           t('common.entity_translations', '{{entity}} Translations'),
           { entity: entityLabel }
         )}</h2>
        <Button
          type="button"
          variant="ghost"
          size="icon"
          leftIcon="x"
           aria-label={t('common.close', 'Close')}
          data-testid="translation-panel-close"
          onClick={onClose}
        />
      </div>
      <div className="flex-1 overflow-y-auto px-4 py-4">
        {panelActions.length > 0 && (
          <div className="mb-4 space-y-2" data-testid="translation-panel-actions">
            <div className="flex flex-wrap items-start gap-2">
              {panelActions.map((entry) => {
                const PanelAction = entry.component
                return (
                  <div key={`${entry.pluginName}:${entry.id}`} className="min-w-0">
                    <PluginContributionBoundary
                      slot={`translationPanelActions:${entry.id}`}
                      pluginName={entry.pluginName}
                      label={entry.displayLabel}
                      owner={entry.owner}
                    >
                      <PanelAction {...buildActionProps(entry)} />
                    </PluginContributionBoundary>
                  </div>
                )
              })}
            </div>
            {targetLanguages.length === 0 && (
              <p className="text-xs text-muted-foreground break-words">
                {t('plugins.seams.translation.no_target_languages', 'No target languages available. Configure the schema languages before requesting translations.')}
              </p>
            )}
            {handoffSummary && (
              <p className="text-xs text-muted-foreground" aria-live="polite" data-testid="translation-handoff-summary">
                {handoffSummary}
              </p>
            )}
          </div>
        )}
        {(languagesLoading || translationsLoading) && (
          <div className="flex justify-center py-8">
            <LoadingSpinner size={24} />
          </div>
        )}
        {!languagesLoading && fields.map((field) => (
          <div key={field.key} className="mb-6">
            <h3 className="text-sm font-semibold mb-3">{field.label || field.key}</h3>
            <div className="space-y-3">
              <div>
                 <label className="text-xs text-muted-foreground block mb-1">
                    {interpolateDefault(t('common.default_language', 'Default ({{language}})'), { language: defaultLanguageCode || '—' })}
                </label>
                <input
                  type="text"
                  readOnly
                  value={fieldValues[field.key] ?? ''}
                  className="w-full rounded border px-2 py-1 text-sm bg-muted text-muted-foreground border-input cursor-not-allowed"
                />
              </div>
              {targetLanguages.map((lang) => {
                const key = `${lang.code}:${field.key}`
                const hasValue = !!normalizedTranslations?.[lang.code]?.[field.key]
                const draft = session.hasDraft(field.key, lang.code)
                  ? session.getDraft(field.key, lang.code)
                  : null
                const status = fieldStatus[key]
                const chipId = `translation-draft-chip-${lang.code}-${field.key}`
                const statusId = `${chipId}-status`
                const describedBy = [draft ? chipId : null, status ? statusId : null]
                  .filter(Boolean)
                  .join(' ')
                return (
                  <div key={lang.code}>
                    <label className="text-xs text-muted-foreground block mb-1">
                      {lang.name} ({lang.code}){!hasValue && ' — falling back'}
                      {draft && (
                        <span
                          id={chipId}
                          data-testid={`translation-draft-chip-${lang.code}-${field.key}`}
                          className="ml-2 inline-flex h-5 items-center rounded-full bg-muted px-2 align-middle text-xs text-muted-foreground"
                        >
                          {t('plugins.seams.translation.draft_marker', 'Unsaved draft')}
                          <span className="sr-only">
                            {interpolateDefault(
                              t('plugins.seams.translation.draft_source', 'Draft from {{source}}'),
                              { source: draft.sourceLabel || draft.owner }
                            )}
                          </span>
                        </span>
                      )}
                    </label>
                    <div className="flex items-center gap-1.5">
                      <input
                        ref={(el) => { inputRefs.current[key] = el }}
                        type="text"
                        data-testid={`translation-input-${lang.code}-${field.key}`}
                        value={getInputValue(lang.code, field.key)}
                        onChange={(e) => setInputValue(lang.code, field.key, e.target.value)}
                        placeholder={fieldValues[field.key] ?? ''}
                        aria-describedby={describedBy || undefined}
                        readOnly={!!draft && !canWrite}
                        aria-readonly={draft && !canWrite ? 'true' : undefined}
                        className={
                          'w-full rounded border px-2 py-1 text-sm focus:outline-none focus:ring-1 focus:ring-primary placeholder:italic placeholder:text-muted-foreground ' +
                          (draft
                            ? 'border-dashed border-muted-foreground bg-background'
                            : 'bg-background border-input')
                        }
                      />
                      {draft && canWrite && (
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          leftIcon="x"
                          aria-label={interpolateDefault(
                            t('plugins.seams.translation.discard_draft', 'Discard draft for {{language}}'),
                            { language: lang.name }
                          )}
                          data-testid={`discard-draft-${lang.code}-${field.key}`}
                          onClick={() => handleDiscardDraft(lang.code, field.key)}
                        />
                      )}
                      {!(draft && !canWrite) && (
                        <Button
                          type="button"
                          variant="outline"
                          size="sm"
                          data-testid={`translation-save-${lang.code}-${field.key}`}
                          disabled={savingKey === key}
                          loading={savingKey === key}
                          onClick={() => handleSave(lang.code, field.key)}
                        >
                           {interpolateDefault(
                             t('common.save_language', 'Save {{language}}'),
                             { language: lang.name }
                           )}
                        </Button>
                      )}
                    </div>
                    {draft && !canWrite && (
                      <p className="mt-1 text-xs text-muted-foreground">
                        {t('plugins.seams.translation.draft_read_only', 'Draft only. Saving translations requires permission to edit them.')}
                      </p>
                    )}
                    {status === 'skipped' && (
                      <p id={statusId} role="status" className="mt-1 text-xs">
                        {t('plugins.seams.translation.protected_save_skipped', 'Not saved. This field was filled in the meantime; the stored translation is shown.')}
                      </p>
                    )}
                    {status === 'invalid' && (
                      <p id={statusId} role="alert" className="mt-1 text-xs text-destructive">
                        {t('plugins.seams.translation.protected_save_invalid', 'The draft was not saved. Review the value and try again.')}
                      </p>
                    )}
                    {savedKey === key && (
                       <span className="text-xs text-green-600 dark:text-green-400">{t('common.saved', '✓ Saved')}</span>
                    )}
                  </div>
                )
              })}
            </div>
          </div>
        ))}
      </div>
    </div>
  )
}
