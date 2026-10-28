import { useState, useEffect, useMemo, useRef, Fragment } from 'react'
import { createPortal } from 'react-dom'
import { useNavigate } from 'react-router-dom'
import { Button } from '../../shared/components/Button'
import { useCreateSchemaTemplate } from '../../shared/hooks/use-create-schema-template'
import { useSources } from '../../shared/hooks/use-sources'
import { useActiveSchema } from '../../shared/components/ActiveSchemaProvider'
import SchemaTreeReview from './SchemaTreeReview'
import SourceSelect from './SourceSelect'
import SaveTemplateDialog from './SaveTemplateDialog'
import { showToast } from '../../shared/components/Toast'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { usePluginSchemaWizardEntries } from '../../shared/plugin-runtime/PluginProvider'
import { PluginContributionBoundary } from '../../shared/plugin-runtime/PluginSlotBoundary'
import { useCreateSchemaFromTree } from '../../shared/hooks/use-create-schema-from-tree'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { useCan } from '../../shared/components/Can'
import {
  createWizardSession,
  resolveReviewOrigin,
  reviewedDraftToTemplateData,
  reviewWriteRights,
} from './wizard-session'

// The committed Example reference registers a label-only input trigger (Plan 03
// registers `onSelect`, no data-carrying draft callback). The host supplies this
// small reference handoff so the consumer path is provable end-to-end; a real
// plugin input component calls `onDraft({ draft, warnings })` with its own tree.
const EXAMPLE_REFERENCE_HANDOFF = {
  draft: {
    description: null,
    statuses: [],
    object_types: [
      {
        name: 'Example asset',
        description: 'Example ObjectType from the reference draft',
        icon_key: 'box',
        attributes: [{ name: 'Hostname', type_string: 'string' }],
      },
    ],
  },
  warnings: ['Example reference draft — review the structure before creating.'],
}

// The Core generation path is gone (AIWIZ-02/S8): the wizard is always the
// plugin entry -> input -> Core review flow, so the breadcrumb path is always
// Input / Review / Finalize.
const STEP_ORDER = { entry: 0, 'plugin-input': 0, review: 1, finalize: 2 }
const CRUMB_INDEX = { 'plugin-input': 0, review: 1, finalize: 2 }

// The two lifecycle notices the entry step can show (S7/Carry-02). Keyed by the
// notice id so the reset/dismiss paths stay explicit.
const ENTRY_NOTICE = {
  input_deactivated: [
    'plugins.seams.wizard.input_deactivated',
    'The plugin was disabled. Choose another way to prepare your schema.',
  ],
  draft_retained: [
    'plugins.seams.wizard.draft_retained',
    'The plugin is no longer available. Your draft is kept; you can review and save it here.',
  ],
}

// R08-1: the strict Core save boundary reports a 422 `{path, reason}`, a 409
// name/key conflict, any other rejection, and a transport uncertainty
// (timeout/network) as four distinct outcomes. A confirmed rejection may say
// "nothing saved"; a transport uncertainty must not.
function classifyCreateError(error, t) {
  if (!error?.response) {
    return t('plugins.seams.wizard.create_unconfirmed', 'The response was interrupted. Check Schemas before trying to create again.')
  }
  const status = error.response.status
  const detail = error.response.data?.detail
  if (status === 422 && detail && typeof detail === 'object') {
    return interpolateDefault(
      t('plugins.seams.wizard.tree_invalid', 'Invalid schema at {{path}}: {{reason}}. Correct the draft or its selection before creating.'),
      { path: detail.path ?? '', reason: detail.reason ?? '' }
    )
  }
  if (status === 409) {
    return t('plugins.seams.wizard.name_conflict', 'The schema name or key conflicts with an existing schema. Choose another name; nothing was created.')
  }
  return t('plugins.seams.wizard.create_rejected', 'The schema could not be created. No partial schema was saved. Review the highlighted error and try again.')
}

export default function SchemaWizardModal({ isOpen, onClose, sourceId, onSelectStartFromScratch }) {
  const { t } = useTranslation()
  const navigate = useNavigate()
  const { activeSourceId } = useActiveSchema()
  const { data: sources = [] } = useSources(0, 1000)
  const [step, setStep] = useState('entry')

  const [generationResult, setGenerationResult] = useState(null)
  const [toggledIds, setToggledIds] = useState(new Set())
  const [expandedIds, setExpandedIds] = useState(new Set())
  const [selectedSourceId, setSelectedSourceId] = useState(null)
  const [schemaName, setSchemaName] = useState('')
  // The Core-final editable name is prefilled once from an accepted draft's
  // optional suggestion (D-04). The suggestion is kept so the helper line shows
  // only while the editable name still equals it.
  const [suggestedName, setSuggestedName] = useState('')
  const [isSaveTemplateOpen, setIsSaveTemplateOpen] = useState(false)
  const [creationError, setCreationError] = useState(null)
  const [templateSaveError, setTemplateSaveError] = useState(null)
  // S7 lifecycle notice shown above the entry choices (input_deactivated /
  // draft_retained). Null when there is nothing to report.
  const [entryNotice, setEntryNotice] = useState(null)

  // Plugin schemaWizardEntries consumer state (D-05/D-08). Only plain JSON plus
  // inert provenance strings survive a handoff — no plugin node, callback,
  // model/worker handle or stylesheet is retained in this Core draft.
  const [activePluginEntry, setActivePluginEntry] = useState(null)
  const [pluginProvenance, setPluginProvenance] = useState(null)
  const [pluginWarnings, setPluginWarnings] = useState([])
  const [pluginDraftError, setPluginDraftError] = useState(null)
  const [draftOrigin, setDraftOrigin] = useState('core')

  const saveTemplate = useCreateSchemaTemplate()
  const createSchemaFromTree = useCreateSchemaFromTree()
  const pluginWizardEntries = usePluginSchemaWizardEntries({ enabled: isOpen })

  // S6 defensive write gates (T-306-13). The wizard is reachable only through
  // ProtectedAdminRoute (superuser) today, so both rights are normally present;
  // the pure helper keeps the decision independent of the permission hook. The
  // server stays the authority. Both hooks run unconditionally (rules of hooks).
  const rights = reviewWriteRights(
    useCan('schemas', 'create'),
    useCan('schema-templates', 'create')
  )

  // Focus targets (S3/S6/S7). The review heading is focused on handoff; the
  // entry heading only after an input loss. Core renders no late result/error,
  // so nothing else moves focus.
  const entryHeadingRef = useRef(null)
  const reviewHeadingRef = useRef(null)
  const pendingEntryFocusRef = useRef(false)

  // Latest-ref pattern: mutation objects change identity whenever their state
  // flips (e.g. isPending during create) and `sourceId` is URL-derived — adding
  // them to the reset effect's deps would re-fire it mid-flow and wipe wizard
  // progress. Refs keep the effect on `[isOpen]` with fresh values.
  const createSchemaFromTreeRef = useRef(createSchemaFromTree)
  createSchemaFromTreeRef.current = createSchemaFromTree
  const saveTemplateRef = useRef(saveTemplate)
  saveTemplateRef.current = saveTemplate
  const sourceIdRef = useRef(sourceId)
  sourceIdRef.current = sourceId

  // One validated active-Source fallback per open: the URL-derived sourceId
  // stays first choice (D-01/D-02); activeSourceId only fills the still-empty
  // picker once it is confirmed to exist in the loaded source list (D-03).
  const activeFallbackRef = useRef(false)
  // Tracks a real user edit of the picker. The fallback effect must not read
  // `selectedSourceId` from its render closure — the reset-on-open effect
  // schedules setSelectedSourceId(null) in the same flush, so the closure sees
  // the previous session's value and would re-apply it (CR-01). Keying on this
  // ref instead preserves user edits without depending on state that is still
  // mid-reset.
  const userEditedSourceRef = useRef(false)

  // D-05/D-08 host seams. One ref-backed controller per open wizard session owns
  // the retained opaque input snapshots and the guarded attempt lifecycle. A
  // stable wrapper reads the latest commit handler from a ref so the controller
  // never captures a stale setter.
  const activePluginEntryRef = useRef(null)
  activePluginEntryRef.current = activePluginEntry
  const commitAcceptedRef = useRef(() => {})
  const sessionRef = useRef(null)
  if (sessionRef.current === null) {
    sessionRef.current = createWizardSession({
      commitAccepted: (data, origin) => commitAcceptedRef.current(data, origin),
    })
  }
  const session = sessionRef.current
  commitAcceptedRef.current = (data, origin) => {
    const entry = activePluginEntryRef.current
    setPluginDraftError(null)
    setEntryNotice(null)
    setGenerationResult(data.draft)
    const otNames = data.draft.object_types.map((ot) => ot.name)
    setToggledIds(new Set(otNames))
    setExpandedIds(new Set(otNames.slice(0, 1)))
    setPluginWarnings(data.warnings)
    setPluginProvenance({
      owner: origin?.owner ?? entry?.owner ?? null,
      contributionId: origin?.contributionId ?? entry?.id ?? null,
      label: entry?.displayLabel ?? null,
    })
    setDraftOrigin('plugin')
    setCreationError(null)
    // A name proposal is copied once, on a NEW accepted draft only; a
    // back/forward navigation never overwrites a user-edited name.
    const suggestion = typeof data.suggestedName === 'string' ? data.suggestedName : ''
    setSchemaName(suggestion)
    setSuggestedName(suggestion)
    setStep('review')
  }
  // A stable requestScope identity: the plugin input calls beginAttempt() to
  // receive its per-generation {signal,isCurrent,acceptDraft,reportError} facade.
  const requestScopeRef = useRef(null)
  if (requestScopeRef.current === null) {
    requestScopeRef.current = { beginAttempt: () => sessionRef.current.beginAttempt() }
  }
  const requestScope = requestScopeRef.current
  // The retained snapshot the plugin input restores from. useMemo keeps one
  // identity across re-renders (deduped by step+entry) and refreshes on re-entry.
  const pluginInputState = useMemo(() => {
    if (step !== 'plugin-input' || !activePluginEntry) return null
    return session.readSnapshot(activePluginEntry.owner, activePluginEntry.id)
  }, [step, activePluginEntry, session])

  useEffect(() => {
    if (isOpen) {
      // A new open cycle: fresh session identity, snapshots and attempt state.
      sessionRef.current.beginSession()
      setStep('entry')
      setGenerationResult(null)
      setToggledIds(new Set())
      setExpandedIds(new Set())
      activeFallbackRef.current = false
      userEditedSourceRef.current = false
      setSelectedSourceId(sourceIdRef.current ?? null)
      setSchemaName('')
      setSuggestedName('')
      setIsSaveTemplateOpen(false)
      setCreationError(null)
      setTemplateSaveError(null)
      setEntryNotice(null)
      createSchemaFromTreeRef.current.reset()
      saveTemplateRef.current.reset()
      setActivePluginEntry(null)
      setPluginProvenance(null)
      setPluginWarnings([])
      setPluginDraftError(null)
      setDraftOrigin('core')
    } else {
      // Close / confirmed discard / logout clears retained snapshots and any
      // accepted-draft identity. A later request cannot revive them.
      sessionRef.current.endSession()
    }
  }, [isOpen])

  // Entering the plugin-input step mounts the input and reads its retained
  // snapshot; leaving it unmounts and invalidates any in-flight attempt. The
  // snapshot itself survives within the open session (D-03); only close/discard
  // clears it (above).
  useEffect(() => {
    if (step !== 'plugin-input' || !activePluginEntry) return undefined
    sessionRef.current.mountInput(activePluginEntry.owner, activePluginEntry.id)
    return () => {
      sessionRef.current.unmountInput()
    }
  }, [step, activePluginEntry])

  // Availability / permission / owner-version loss while the input is open: the
  // entry is no longer offered, so the attempt is synchronously invalidated
  // (unmountInput aborts it), the host returns to the entry choices with the
  // input_deactivated notice and focuses the entry heading. The accepted draft
  // (if any) stays in the controller.
  useEffect(() => {
    if (step !== 'plugin-input' || !activePluginEntry) return
    const stillOffered = pluginWizardEntries.some(
      (entry) => entry.pluginName === activePluginEntry.pluginName && entry.id === activePluginEntry.id
    )
    if (stillOffered) return
    sessionRef.current.unmountInput()
    pendingEntryFocusRef.current = true
    setEntryNotice('input_deactivated')
    setStep('entry')
  }, [step, activePluginEntry, pluginWizardEntries])

  // Focus policy (S3/S6/S7): move focus to the review heading on handoff and to
  // the entry heading only after an input loss. No late plugin result/error is
  // rendered by Core, so nothing else moves focus.
  useEffect(() => {
    if (step === 'review') {
      reviewHeadingRef.current?.focus()
      return
    }
    if (step === 'entry' && pendingEntryFocusRef.current) {
      pendingEntryFocusRef.current = false
      entryHeadingRef.current?.focus()
    }
  }, [step])

  // Runs after the reset-on-open effect so its fallback wins the batched
  // commit: when the URL carries no sourceId and the user has not edited the
  // picker, pre-fill the validated active Source once sources are available.
  // Reading `selectedSourceId` here would see the pre-reset value (CR-01), so
  // user edits are tracked via `userEditedSourceRef` instead.
  useEffect(() => {
    if (!isOpen || activeFallbackRef.current) return
    if (sourceIdRef.current) {
      activeFallbackRef.current = true
      return
    }
    if (userEditedSourceRef.current) {
      activeFallbackRef.current = true
      return
    }
    if (!activeSourceId || !sources.some(s => s.id === activeSourceId)) return
    activeFallbackRef.current = true
    setSelectedSourceId(activeSourceId)
  }, [isOpen, sources, activeSourceId])

  const handleToggle = (otName) => {
    setToggledIds(prev => {
      const next = new Set(prev)
      if (next.has(otName)) next.delete(otName)
      else next.add(otName)
      return next
    })
  }

  const handleExpand = (otName) => {
    setExpandedIds(prev => {
      const next = new Set(prev)
      if (next.has(otName)) next.delete(otName)
      else next.add(otName)
      return next
    })
  }

  // An accepted plugin path is tracked by the inert origin state and the
  // accepted tree alone — never by the executable contribution component — so a
  // disabled/unloaded plugin cannot change the save branch (D-08).
  const isPluginPath = draftOrigin === 'plugin'
  const isPluginDraft = isPluginPath && !!generationResult
  const isCreating = createSchemaFromTree.isPending
  const canCreate = selectedSourceId && schemaName.trim() && toggledIds.size > 0 && !isCreating

  // The origin entry currently offered by the runtime (owner/id), or null when
  // it is gone (disabled plugin / lost right / owner reload). Drives the
  // "Back to input" vs "Back to entry choices" decision (S6).
  const originEntry = useMemo(
    () => resolveReviewOrigin(activePluginEntry, pluginWizardEntries),
    [activePluginEntry, pluginWizardEntries]
  )
  const canBackToInput = isPluginDraft && !!originEntry
  const showNameSuggestion = suggestedName !== '' && schemaName === suggestedName

  const handlePluginEntrySelect = (entry) => {
    setActivePluginEntry(entry)
    setDraftOrigin('plugin')
    setPluginDraftError(null)
    setStep('plugin-input')
  }

  const handlePluginBackToEntry = () => {
    // Synchronous invalidation before the step transition: any in-flight attempt
    // is aborted and can no longer mutate host state (D-08).
    sessionRef.current.invalidateAttempt()
    setPluginDraftError(null)
    setStep('entry')
  }

  // S6 review back: reacquire the origin entry by owner/id and restore its
  // retained snapshot with "Back to input"; when the origin is gone, fall back
  // to the entry choices with the draft-retained notice. The accepted draft and
  // the Core save path are untouched either way.
  const handleReviewBack = () => {
    setCreationError(null)
    if (canBackToInput) {
      setStep('plugin-input')
      return
    }
    setEntryNotice('draft_retained')
    setStep('entry')
  }

  // Backward-compatible wrappers for the legacy `onDraft`/`onSelect` handoff: a
  // handoff is delivered through a fresh current attempt, so the same
  // all-or-none validation and staleness guard apply as for requestScope.
  const handlePluginDraft = (handoff) => {
    const result = sessionRef.current.beginAttempt().acceptDraft(handoff)
    if (result.ok) return
    setPluginDraftError(interpolateDefault(
      t('plugins.seams.wizard.tree_invalid', 'Invalid schema at {{path}}: {{reason}}. Correct the draft or its selection before creating.'),
      { path: result.reason ?? '', reason: 'invalid handoff' }
    ))
  }

  // D-08: closing (or replacing) a nonempty accepted draft requires the shared
  // discard confirmation; the counteraction/Escape retain the draft. A close
  // while an attempt is pending has no accepted draft, so it takes the direct
  // abort path below with no confirmation.
  const requestClose = () => {
    if (isPluginDraft && !isCreating) {
      confirmDialog({
        title: t('plugins.seams.wizard.discard_action', 'Discard draft'),
        message: t('plugins.seams.wizard.discard_confirm', 'Discard this draft? Unsaved input in this wizard will be lost. Choose Keep reviewing to retain it.'),
        danger: true,
        confirmLabel: t('plugins.seams.wizard.discard_action', 'Discard draft'),
        cancelLabel: t('plugins.seams.wizard.discard_keep', 'Keep reviewing'),
      }).then((confirmed) => {
        if (confirmed) {
          // Synchronous teardown before the wizard closes.
          sessionRef.current.endSession()
          onClose()
        }
      })
      return
    }
    sessionRef.current.endSession()
    onClose()
  }

  const handleCreateSchema = async () => {
    if (!canCreate || !generationResult) return
    setCreationError(null)
    try {
      // R08-1: an accepted draft is always finalized through the strict Core
      // boundary, never the lenient atomic-create route, with the selected
      // ObjectTypes plus the Core-owned Source/name/description/statuses.
      const selectedOTs = generationResult.object_types.filter(ot => toggledIds.has(ot.name))
      const result = await createSchemaFromTree.mutateAsync({
        source_id: selectedSourceId,
        name: schemaName.trim(),
        description: generationResult.description ?? null,
        statuses: generationResult.statuses ?? [],
        object_types: selectedOTs,
      })
      const count = selectedOTs.length
      const defaultText = count === 1
        ? t('toasts.wizard.schema_created_one', 'Schema "{{name}}" created with {{count}} ObjectType.')
        : t('toasts.wizard.schema_created_other', 'Schema "{{name}}" created with {{count}} ObjectTypes.')
      showToast(interpolateDefault(defaultText, { name: result.name, count }), {
        type: 'success'
      })
      onClose()
      navigate(`/configuration/schemas/${result.id}`)
    } catch (err) {
      setCreationError(classifyCreateError(err, t))
    }
  }

  const handleSaveTemplate = async ({ name, description }) => {
    setTemplateSaveError(null)
    try {
      // The template dialog name/description are the library's own metadata; the
      // stored schema payload is Core-owned and uses the reviewed final name,
      // the selected ObjectTypes and the draft statuses only.
      const selectedOTs = (generationResult?.object_types ?? []).filter(ot => toggledIds.has(ot.name))
      const result = await saveTemplate.mutateAsync({
        name,
        description,
        category: null,
        template_data: reviewedDraftToTemplateData({
          schemaName,
          schemaDescription: generationResult?.description,
          objectTypes: selectedOTs,
          statuses: generationResult?.statuses ?? [],
        }),
      })
      showToast(interpolateDefault(t('toasts.wizard.template_saved', 'Template "{{name}}" saved.'), { name: result.name }), { type: 'success', duration: 4000 })
      setIsSaveTemplateOpen(false)
    } catch (err) {
      const detail = err?.response?.data?.detail || err.message || t('common.unknown_error', 'Unknown error')
      setTemplateSaveError(typeof detail === 'string' ? detail : JSON.stringify(detail))
    }
  }

  if (!isOpen) return null

  const currentStepIndex = STEP_ORDER[step] ?? -1
  const crumbSteps = ['plugin-input', 'review', 'finalize']

  return createPortal(
    <div
      className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center"
      onClick={requestClose}
      data-testid="schema-wizard-modal"
    >
      <div
        className="rounded-lg border bg-card p-6 w-full max-w-2xl space-y-6 shadow-lg max-h-[90vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between">
          <h2 ref={entryHeadingRef} tabIndex={-1} className="text-lg font-semibold outline-none">{t('wizard.title', 'New Schema')}</h2>
          <Button type="button" size="icon" variant="ghost" onClick={requestClose}
                  className="h-7 w-7 text-lg leading-none" aria-label={t('common.close', 'Close')}>×</Button>
        </div>

        {step !== 'entry' && (
          <div className="flex items-center gap-2 text-sm" data-testid="wizard-breadcrumbs">
            {crumbSteps.map((s, i) => {
              const isActive = step === s
              const isPast = currentStepIndex > CRUMB_INDEX[s]
              // Review is reachable from the input step while an accepted draft
              // exists, so the user can return without regenerating (S6).
              const isReachable = !isActive && (isPast || (s === 'review' && isPluginDraft))
              const label = {
                'plugin-input': t('plugins.seams.wizard.input_step', 'Input'),
                review: t('wizard.steps.review', 'Review'),
                finalize: t('wizard.steps.finalize', 'Finalize'),
              }[s]
              return (
                <Fragment key={s}>
                  {i > 0 && <span className="text-muted-foreground">→</span>}
                  <button
                    type="button"
                    onClick={() => {
                      if (!isReachable) return
                      if (s === 'review') {
                        // Any breadcrumb transition away from input invalidates
                        // the current attempt synchronously before the re-render.
                        sessionRef.current.invalidateAttempt()
                        setCreationError(null)
                        setStep('review')
                        return
                      }
                      if (s === 'plugin-input') {
                        handleReviewBack()
                      }
                    }}
                    disabled={!isReachable}
                    className={`px-2 py-1 rounded ${isActive ? 'bg-primary text-primary-foreground font-medium'
                      : isReachable ? 'text-foreground hover:underline cursor-pointer'
                      : 'text-muted-foreground cursor-not-allowed'}`}
                    data-testid={`breadcrumb-${s}`}
                  >
                    {label}
                  </button>
                </Fragment>
              )
            })}
          </div>
        )}

        {step === 'entry' && (
          <div className="space-y-4">
            {entryNotice && (
              <div
                className="rounded-md border bg-muted/40 p-3 text-sm text-muted-foreground break-words"
                role="status"
                data-testid="wizard-entry-notice"
              >
                {t(...ENTRY_NOTICE[entryNotice])}
              </div>
            )}
            <div>
              <Button type="button" variant="outline" size="lg" leftIcon="layers"
                      className="w-full h-auto min-h-10 whitespace-normal"
                      onClick={() => { onClose(); navigate('/configuration/templates') }}>
                {t('wizard.entry.use_saved_template', 'Use saved template')}
              </Button>
              <p className="text-sm text-muted-foreground mt-1">
                {t('wizard.entry.use_saved_template_description', 'Start from a previously saved template.')}
              </p>
            </div>
            <div>
              <Button type="button" variant="outline" size="lg" leftIcon="pen-line"
                      className="w-full h-auto min-h-10 whitespace-normal"
                      onClick={() => { onSelectStartFromScratch(); onClose() }}>
                {t('wizard.entry.start_from_scratch', 'Start from scratch')}
              </Button>
              <p className="text-sm text-muted-foreground mt-1">
                {t('wizard.entry.start_from_scratch_description', 'Create an empty schema and add ObjectTypes manually.')}
              </p>
            </div>

            {isPluginDraft && generationResult && (
              <div>
                <Button type="button" variant="primary" size="lg" leftIcon="database"
                        className="w-full h-auto min-h-10 whitespace-normal"
                        onClick={() => setStep('review')}
                        data-testid="plugin-wizard-review-cta">
                  {t('plugins.seams.wizard.review_cta', 'Review schema draft')}
                </Button>
              </div>
            )}

            {pluginWizardEntries.length > 0 && (
              <div className="space-y-4" data-testid="plugin-wizard-entries">
                {pluginWizardEntries.map((entry) => (
                  <div key={`${entry.pluginName}:${entry.id}`} className="min-w-0">
                    <PluginContributionBoundary
                      slot={`schemaWizardEntries:${entry.id}`}
                      pluginName={entry.pluginName}
                      label={entry.displayLabel}
                      owner={entry.owner}
                    >
                      <Button
                        type="button"
                        variant="outline"
                        size="lg"
                        leftIcon={entry.icon || undefined}
                        data-testid={`plugin-wizard-entry-${entry.pluginName}-${entry.id}`}
                        className="w-full h-auto min-h-10 justify-start text-left whitespace-normal"
                        onClick={() => handlePluginEntrySelect(entry)}
                      >
                        {entry.displayLabel}
                      </Button>
                    </PluginContributionBoundary>
                    {entry.displayDescription && (
                      <p
                        className="mt-1 text-sm text-muted-foreground break-words"
                        data-testid={`plugin-wizard-entry-description-${entry.pluginName}-${entry.id}`}
                      >
                        {entry.displayDescription}
                      </p>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {step === 'plugin-input' && activePluginEntry && (() => {
          const PluginInput = activePluginEntry.component
          return (
            <div className="space-y-5" data-testid="plugin-wizard-input-step">
              <h3 className="text-base font-semibold text-foreground break-words">{activePluginEntry.displayLabel}</h3>

              <PluginContributionBoundary
                slot={`schemaWizardEntries:${activePluginEntry.id}`}
                pluginName={activePluginEntry.pluginName}
                label={activePluginEntry.displayLabel}
                owner={activePluginEntry.owner}
              >
                <PluginInput
                  sourceId={selectedSourceId}
                  inputState={pluginInputState}
                  onInputStateChange={(value) =>
                    sessionRef.current.writeSnapshot(activePluginEntry.owner, activePluginEntry.id, value)
                  }
                  hasDraft={!!generationResult}
                  requestScope={requestScope}
                  onDraft={handlePluginDraft}
                  onBack={handlePluginBackToEntry}
                  onSelect={() => handlePluginDraft(EXAMPLE_REFERENCE_HANDOFF)}
                />
              </PluginContributionBoundary>

              {pluginDraftError && (
                <div className="rounded-md border border-destructive/30 bg-destructive/10 p-4 space-y-2" data-testid="plugin-draft-error">
                  <p className="text-sm text-destructive break-words">{pluginDraftError}</p>
                  <Button type="button" variant="outline" size="sm"
                          className="h-auto min-h-8 whitespace-normal px-4 py-2 font-semibold"
                          onClick={() => setPluginDraftError(null)}>
                    {t('plugins.seams.contribution.retry', 'Try again')}
                  </Button>
                </div>
              )}

              <div className="flex justify-between pt-2">
                <Button type="button" variant="outline" size="sm" onClick={handlePluginBackToEntry}>
                  {t('common.back', 'Back')}
                </Button>
              </div>
            </div>
          )
        })()}

        {step === 'review' && (
          <div className="space-y-5" data-testid="wizard-review-step">
            {!generationResult ? (
              <div className="space-y-2">
                <h3 className="text-base font-semibold text-foreground">
                  {t('plugins.seams.wizard.empty_heading', 'No schema draft available')}
                </h3>
                <p className="text-sm text-muted-foreground">
                  {t('plugins.seams.wizard.empty_body', 'Return to the entry choices and prepare a draft, or use a saved template.')}
                </p>
                <Button type="button" variant="outline" size="sm" onClick={() => setStep('entry')}>
                  {t('plugins.seams.wizard.back_to_entry', 'Back to entry choices')}
                </Button>
              </div>
            ) : (
              <>
                {/* Focus anchor for the handoff (S6): focus moves here, not to a
                    field, so the tree is announced before the name input. */}
                <h3 ref={reviewHeadingRef} tabIndex={-1} className="sr-only outline-none">
                  {t('wizard.steps.review', 'Review')}
                </h3>
                <div className="space-y-1.5">
                  <label htmlFor="wizard-schema-name" className="text-sm font-medium text-foreground">
                    {t('wizard.schema.name_label', 'Schema Name')} <span className="text-destructive">*</span>
                  </label>
                  <input
                    id="wizard-schema-name"
                    type="text"
                    value={schemaName}
                    onChange={e => setSchemaName(e.target.value)}
                    className={`w-full rounded-md border px-3 py-2 text-sm bg-background focus:outline-none focus:ring-2 focus:ring-primary focus:border-primary ${!schemaName.trim() ? 'border-destructive' : ''}`}
                    placeholder={t('wizard.schema.name_placeholder', 'Enter schema name')}
                    data-testid="wizard-schema-name-input"
                  />
                  {!schemaName.trim() && (
                    <p className="text-xs text-destructive">{t('wizard.schema.name_required', 'Schema name is required')}</p>
                  )}
                  {showNameSuggestion && (
                    <p className="text-xs text-muted-foreground" data-testid="wizard-name-suggestion">
                      {t('plugins.seams.wizard.name_suggestion', 'Suggested with the draft. You can change it.')}
                    </p>
                  )}
                </div>

                <SourceSelect
                  value={selectedSourceId}
                  onChange={(id) => {
                    userEditedSourceRef.current = true
                    setSelectedSourceId(id)
                  }}
                  disabled={isCreating}
                />

                <SchemaTreeReview
                  schemaData={generationResult}
                  toggledIds={toggledIds}
                  onToggle={handleToggle}
                  expandedIds={expandedIds}
                  onExpand={handleExpand}
                  warnings={pluginWarnings}
                  provenance={pluginProvenance}
                />

                {creationError && (
                  <div className="rounded-md bg-destructive/10 border border-destructive/30 p-3" data-testid="creation-error">
                    <p className="text-sm text-destructive mb-2 break-words">{creationError}</p>
                    <Button type="button" variant="primary" size="sm"
                            onClick={handleCreateSchema} disabled={!canCreate}>
                      {t('common.try_again', 'Try again')}
                    </Button>
                  </div>
                )}

                <div className="flex justify-between items-center pt-2 border-t">
                  <Button type="button" variant="outline" size="sm" onClick={handleReviewBack}>
                    {canBackToInput
                      ? t('plugins.seams.wizard.back_to_input', 'Back to input')
                      : t('plugins.seams.wizard.back_to_entry', 'Back to entry choices')}
                  </Button>
                  <div className="flex gap-2">
                    {rights.showSaveTemplate && (
                      <Button type="button" variant="secondary" size="sm"
                              onClick={() => setIsSaveTemplateOpen(true)}
                              disabled={isCreating}
                              leftIcon="save">
                        {t('wizard.template.save_action', 'Save as Template')}
                      </Button>
                    )}
                    {rights.showCreateSchema && (
                      <Button type="button" variant="primary" size="sm"
                              onClick={handleCreateSchema}
                              disabled={!canCreate}
                              leftIcon={isCreating ? undefined : 'check'}
                              data-testid="wizard-create-schema-button">
                        {isCreating ? t('common.creating', 'Creating...') : t('wizard.schema.create', 'Create Schema')}
                      </Button>
                    )}
                  </div>
                </div>

                <SaveTemplateDialog
                  isOpen={isSaveTemplateOpen}
                  onClose={() => { setIsSaveTemplateOpen(false); setTemplateSaveError(null) }}
                  onSave={handleSaveTemplate}
                  isSaving={saveTemplate.isPending}
                  error={templateSaveError}
                />
              </>
            )}
          </div>
        )}

        {step === 'finalize' && (
          <div className="text-center py-8 space-y-3" data-testid="finalize-step">
            <p className="text-sm text-muted-foreground">{t('wizard.finalize.redirecting', 'Redirecting to your new schema...')}</p>
          </div>
        )}
      </div>
    </div>,
    document.body
  )
}
