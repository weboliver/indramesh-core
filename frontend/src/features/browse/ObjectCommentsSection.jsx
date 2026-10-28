import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import api from '../../shared/hooks/use-api'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Can } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { PluginItemBadges, usePluginItemBadges } from '../../shared/plugin-runtime/PluginItemBadges'

// D-09: the 64,000-character maximum, enforced client-side before any
// request AND server-side by the ObjectCommentCreate field_validator — the
// server's Python code-point count is authoritative (edge row COMMENT-01
// encoding); this constant only pre-gates the UI.
const MAX_COMMENT_LENGTH = 64000

// Single source of truth for the comments cache-key shape. The query, the
// placeholder-scoping predicate, and every invalidation derive from these
// helpers, so the cross-Object guard can never silently desync from the key it
// inspects if that shape ever changes — addressing the wave-4 review's
// "positional queryKey[1] coupling is silently fragile" Info finding.
const COMMENTS_QUERY_ROOT = 'object-comments'
const commentsQueryKey = (objectId, order) => [COMMENTS_QUERY_ROOT, objectId, order]
// Prefix key matching every cached order of ONE Object's list.
const commentsQueryKeyForObject = (objectId) => [COMMENTS_QUERY_ROOT, objectId]
// The Object id a cached comments key belongs to; undefined for any other key
// (including react-query's collapsed `null`/disabled key).
const objectIdFromCommentsKey = (key) =>
  Array.isArray(key) && key[0] === COMMENTS_QUERY_ROOT ? key[1] : undefined

function useObjectComments(objectId, order, enabled) {
  return useQuery({
    // The order dimension is part of the cache key (RESEARCH Pattern 3):
    // toggling ordering hits a distinct cached fetch, and returning to a
    // previously-fetched order renders from cache while isFetching drives
    // the keep-rows-visible refresh treatment.
    queryKey: commentsQueryKey(objectId, order),
    queryFn: () => api.get(`/objects/${objectId}/comments`, { params: { order } }).then((r) => r.data),
    enabled,
    // CR-01 (280-REVIEW.md): retain the previously fetched rows while a new
    // order's fetch is in flight, but ONLY when those rows belong to THIS
    // Object. The bare `keepPreviousData` helper is observer-scoped, not
    // key-scoped: it returned the last query's data even when the switch was
    // to a different Object's still-uncached list, rendering Object A's
    // comment rows under Object B until B's own fetch resolved. Scoping on the
    // Object id the retained key actually belongs to keeps the order-toggle
    // retention (same Object) while a switch to a different Object correctly
    // falls back to the ordinary loading state — no initialData, no staleTime
    // workaround, no separate render-site `isPlaceholderData` guard.
    placeholderData: (previousData, previousQuery) =>
      objectIdFromCommentsKey(previousQuery?.queryKey) === objectId ? previousData : undefined,
  })
}

// 280-02 tracer: collapsed-by-default lazy disclosure owning the inline
// composer (D-01...D-03), post-trim validation (D-07...D-10), server-
// confirmed save flow (D-11...D-13), and the chronological list with
// resolved author display names (D-04...D-06). Every tracer-visible string
// resolves through its catalogued browse.comments.* key (280-01 contract);
// list mutations invalidate the query — no optimistic row is ever rendered.
function ObjectCommentsSectionInner({ objectId, active }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const queryClient = useQueryClient()
  // The shared tab strip in ObjectDetailPanel now owns visibility as a
  // single controlled `active` prop — this section no longer tracks its own
  // expanded state.
  const sectionExpanded = active
  // D-04: created-asc (oldest first) is the default; the two arrow controls
  // beside the list heading toggle it (setOrder).
  const [order, setOrder] = useState('asc')
  const [draft, setDraft] = useState('')
  // 'empty' | 'length' | 'save' | null — inline form error (D-10), rendered
  // with role="alert" below the textarea.
  const [localError, setLocalError] = useState(null)
  const [saving, setSaving] = useState(false)
  // Per-row delete failure state (COMMENT-03): retains the row with the
  // documented local inline retry, mirroring deleteFile's rowErrors map.
  const [rowErrors, setRowErrors] = useState({})
  const headingRef = useRef(null)
  const textareaRef = useRef(null)
  const prevObjectIdRef = useRef(objectId)

  // CR-01 (280-REVIEW.md): the section is NOT remounted when the parent
  // switches to a different Object (ObjectDetailPanel is rendered without a
  // key and this component is module-scope), so every piece of per-Object
  // local state would otherwise survive the prop change. Reset it explicitly
  // whenever objectId changes — mirroring ObjectFilesSection's identical
  // useLayoutEffect — so a draft, stale error, expanded flag, or order chosen
  // for the previous Object can never leak into the newly selected one.
  // useLayoutEffect (not useEffect) so the reset is committed before the
  // browser paints the switched-to Object's now-stale disclosure/composer.
  useLayoutEffect(() => {
    if (prevObjectIdRef.current === objectId) return
    prevObjectIdRef.current = objectId
    setOrder('asc')
    setDraft('')
    setLocalError(null)
    setRowErrors({})
  }, [objectId])

  const { data: comments = [], isLoading, isFetching, isError, refetch } = useObjectComments(
    sectionExpanded ? objectId : null,
    order,
    sectionExpanded
  )

  // FEEXT-01/S2: one batch `itemBadges` resolution for the loaded comment list.
  // Not expanded (or still loading) means no ids and therefore no call.
  const commentIds = useMemo(() => comments.map((comment) => comment.id), [comments])
  // R4-03: a sanitized provenance projection (id -> managed_by ONLY) so the Jira
  // resolver can decide item origin from already-read rows without a second
  // request and without handing any plugin the author/body text. Memoized
  // because the host folds `JSON.stringify(context)` into the query key, so a
  // stable object for the same rows keeps the resolver from re-running.
  const commentBadgeContext = useMemo(() => {
    const itemsById = {}
    for (const comment of comments) itemsById[comment.id] = comment.managed_by
    return { objectId, itemsById }
  }, [objectId, comments])
  const commentBadges = usePluginItemBadges('comment', commentIds, commentBadgeContext)

  const handleLoadRetry = () => {
    refetch()
    headingRef.current?.focus()
  }

  const clearRowError = useCallback((commentId) => {
    setRowErrors((prev) => {
      if (!prev[commentId]) return prev
      const next = { ...prev }
      delete next[commentId]
      return next
    })
  }, [])

  // COMMENT-03/D-15: every delete awaits the shared danger confirmation;
  // success invalidates the list query (server-confirmed removal, never
  // optimistic), failure retains the row with a local inline retry —
  // mirroring ObjectFilesSection's deleteFile exactly.
  // The row's Object is captured before the awaited confirmation and DELETE,
  // because the user may switch Objects while either is in flight. Every
  // completion is attributed to `deletedFor`: the cache invalidation is a
  // key-scoped cache write and runs unconditionally, while the local row-error
  // state stays behind the current-Object guard so a stale failure can never
  // write into a different Object's view — the same split submitComment uses.
  const deleteComment = useCallback(async (comment) => {
    const deletedFor = objectId
    const confirmed = await confirmDialog({
      message: t('browse.comments.delete_confirm', 'Delete comment: permanently delete this comment? This action cannot be undone.'),
      danger: true,
    })
    if (!confirmed) return
    try {
      await api.delete(`/objects/${deletedFor}/comments/${comment.id}`)
      queryClient.invalidateQueries({ queryKey: commentsQueryKeyForObject(deletedFor) })
      if (prevObjectIdRef.current !== deletedFor) return
      clearRowError(comment.id)
    } catch {
      if (prevObjectIdRef.current === deletedFor) {
        setRowErrors((prev) => ({ ...prev, [comment.id]: 'delete' }))
      }
    }
  }, [objectId, queryClient, t, clearRowError])

  // D-07/D-09 client-side gate: trim boundary whitespace first, then reject
  // an empty-after-trim value and a >64,000-character value. The trimmed
  // value is what would be submitted (D-08); the live counter below shows
  // the entered value and does not alter what is submitted (UI-SPEC).
  const validateDraft = useCallback(() => {
    const trimmed = draft.trim()
    if (!trimmed) {
      setLocalError('empty')
      return null
    }
    if (trimmed.length > MAX_COMMENT_LENGTH) {
      setLocalError('length')
      return null
    }
    return trimmed
  }, [draft])

  // D-11...D-13: no optimistic row — the POST response is awaited, then the
  // list query is invalidated so the server's ordering/author/timestamp are
  // authoritative. On failure the complete trimmed draft stays in the
  // textarea with focus restored (D-12); only the submit action is disabled
  // while saving (D-13).
  // WR-01 (280-REVIEW.md): the completion runs after an `await`, by which point
  // the user may have switched Objects. `submittedFor` records which Object this
  // request was for, and the composer side effects are guarded against
  // `prevObjectIdRef.current` (updated synchronously by the reset effect above)
  // so a stale save can never write its draft or error into a different
  // Object's composer. `setSaving(false)` stays unguarded in `finally`: a
  // `return` inside `try` still runs it, so the newly selected Object's submit
  // button is never left disabled once the stale request settles.
  //
  // CR-01 (280-REVIEW.md, wave-4 review): the list invalidation is NOT local
  // composer state — it is a cache write keyed to the Object the comment was
  // saved for, and it must run unconditionally even when the user has since
  // switched Objects. Guarding it alongside `setDraft`/`setLocalError` left the
  // saved Object's list un-invalidated; with the app's global 30s staleTime and
  // no refetch-on-mount, the just-saved comment then stayed invisible when the
  // user returned to that Object. Only the composer mutations belong behind the
  // current-Object guard (mirroring `deleteComment`, which invalidates
  // unconditionally).
  const submitComment = useCallback(async () => {
    if (saving) return
    const submittedFor = objectId
    const trimmed = validateDraft()
    if (trimmed === null) {
      textareaRef.current?.focus()
      return
    }
    setSaving(true)
    try {
      await api.post(`/objects/${submittedFor}/comments`, { body: trimmed })
      queryClient.invalidateQueries({ queryKey: commentsQueryKeyForObject(submittedFor) })
      if (prevObjectIdRef.current !== submittedFor) return
      setDraft('')
      setLocalError(null)
      textareaRef.current?.focus()
    } catch {
      if (prevObjectIdRef.current === submittedFor) {
        setDraft(trimmed)
        setLocalError('save')
        textareaRef.current?.focus()
      }
    } finally {
      setSaving(false)
    }
  }, [saving, validateDraft, objectId, queryClient])

  // D-02: Ctrl+Enter (Windows/Linux) / Cmd+Enter (macOS) submits; plain
  // Enter inserts a newline.
  const handleTextareaKeyDown = (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault()
      submitComment()
    }
  }

  const describedBy = [
    'comment-keyboard-hint',
    'comment-char-count',
    localError ? 'comment-form-error' : null,
  ].filter(Boolean).join(' ')

  return (
    <div data-testid="object-comments-section">
      {sectionExpanded && (
        <div id="object-comments-body" className="p-6 pt-4 space-y-4" data-testid="comments-body">
          <h3 ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none" data-testid="comments-heading">
            {t('browse.comments.title', 'Comments')}
          </h3>
          {/* D-15: the composer is visible only to objects:update holders —
              visibility only; the backend record permission is the
              enforcement point. Read-only users keep the disclosure, list,
              and ordering controls. */}
          <Can resource="objects" action="update">
            <div className="space-y-2">
              <label
                htmlFor="comment-textarea"
                className="text-sm"
                data-testid="comments-composer-label"
              >
                {t('browse.comments.composer_label', 'New comment')}
              </label>
              <textarea
                id="comment-textarea"
                ref={textareaRef}
                value={draft}
                onChange={(event) => {
                  setDraft(event.target.value)
                  if (localError) setLocalError(null)
                }}
                onKeyDown={handleTextareaKeyDown}
                placeholder={t('browse.comments.placeholder', 'Write a comment...')}
                // Pitfall 6: one space-separated aria-describedby referencing
                // all three hint regions at once.
                aria-describedby={describedBy}
                aria-invalid={localError ? 'true' : 'false'}
                className="resize-y min-h-[96px] w-full rounded-md border border-input bg-background px-4 py-2 text-sm focus:ring-2 focus:ring-ring"
                data-testid="comments-textarea"
              />
              <div className="flex flex-wrap items-center justify-between gap-2">
                <div className="flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-muted-foreground">
                  <p id="comment-keyboard-hint" data-testid="comments-keyboard-hint">
                    {t('browse.comments.keyboard_hint', 'Ctrl/⌘+Enter to add')}
                  </p>
                  <p id="comment-char-count" data-testid="comments-char-count">
                    {t('browse.comments.char_count', '{{count}} / 64,000 characters', { count: draft.length })}
                  </p>
                </div>
                {/* D-13: only the submit action is disabled while saving;
                    the draft, textarea, and all other controls stay usable. */}
                <Button
                  type="button"
                  size="md"
                  loading={saving}
                  disabled={saving}
                  onClick={submitComment}
                  data-testid="comments-submit"
                >
                  {t('browse.comments.submit', 'Add comment')}
                </Button>
              </div>
              {localError && (
                <p id="comment-form-error" role="alert" className="text-xs text-destructive" data-testid="comments-form-error">
                  {localError === 'empty' && t('browse.comments.validation_empty', 'Enter a comment.')}
                  {localError === 'length' && t('browse.comments.validation_length', 'A comment can have at most 64,000 characters.')}
                  {localError === 'save' && t('browse.comments.save_failed', 'The comment could not be added. Try again.')}
                </p>
              )}
            </div>
          </Can>

          {/* D-04: exactly two compact ghost icon controls beside the list
              heading — visible even while the list is empty (UI-SPEC
              zero-one-many); the active choice is marked with aria-pressed
              plus bg-secondary text-primary, never both choices at once. */}
          <div className="flex items-center justify-end gap-1" data-testid="comments-order-controls">
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={order === 'asc' ? 'bg-secondary text-primary' : undefined}
              leftIcon="arrow-up"
              aria-label={t('browse.comments.order_asc', 'Oldest first')}
              title={t('browse.comments.order_asc', 'Oldest first')}
              aria-pressed={order === 'asc'}
              onClick={() => setOrder('asc')}
              data-testid="comments-order-asc"
            />
            <Button
              type="button"
              variant="ghost"
              size="icon"
              className={order === 'desc' ? 'bg-secondary text-primary' : undefined}
              leftIcon="arrow-down"
              aria-label={t('browse.comments.order_desc', 'Newest first')}
              title={t('browse.comments.order_desc', 'Newest first')}
              aria-pressed={order === 'desc'}
              onClick={() => setOrder('desc')}
              data-testid="comments-order-desc"
            />
          </div>

          {/* IN-06 (280-REVIEW.md): the live region is always rendered
              whenever the section body is, so it exists in the DOM before any
              of its four possible children change — otherwise the region and
              its first content are inserted in the same commit and many
              screen readers never announce it. */}
          <div aria-live="polite" data-testid="comments-live-region">
          {isLoading && comments.length === 0 && (
            <p className="text-sm text-muted-foreground" data-testid="comments-loading">{t('common.loading', 'Loading...')}</p>
          )}
          {isError && (
            <div className="flex items-center justify-between gap-3" data-testid="comments-load-error">
              <p className="text-sm text-destructive">{t('browse.comments.load_failed', 'Comments could not be loaded. Reload the comment list.')}</p>
              <Button type="button" variant="outline" size="sm" onClick={handleLoadRetry} data-testid="comments-load-retry">
                {t('browse.comments.load_retry', 'Reload comment list')}
              </Button>
            </div>
          )}
          {!isLoading && !isError && comments.length === 0 && (
            <div className="text-center py-6" data-testid="comments-empty">
              {/* UI-SPEC Typography: only weights 400 and 600 — the empty-state
                  emphasis role is 600, never font-medium (500). */}
              <p className="text-sm font-semibold">{t('browse.comments.empty.title', 'No comments yet')}</p>
              <p className="text-xs text-muted-foreground mt-1">{t('browse.comments.empty.description', "Add a comment to document this object's history.")}</p>
            </div>
          )}
          {!isLoading && comments.length > 0 && (
            <>
              {/* isFetching (not isLoading): an order-toggle refetch keeps the
                  currently rendered rows visible with a compact muted loading
                  treatment until the authoritative response arrives. Retained
                  rows are NOT hidden on a failed background refetch — the
                  isError banner above renders alongside them (WR-01). */}
              {isFetching && (
                <p className="text-xs text-muted-foreground" data-testid="comments-refreshing">
                  {t('common.loading', 'Loading...')}
                </p>
              )}
              <div className="rounded-lg border overflow-hidden" data-testid="comments-list">
                {comments.map((comment) => (
                  <div key={comment.id} className="border-b last:border-0 px-4 py-4 flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-3" data-testid="comments-row">
                    <div className="flex-1 min-w-0">
                      {/* D-13/D-16: native and connector-managed comments
                          stay in ONE list, in the same date order — no
                          separate external subsection. The badge sits
                          immediately BEFORE the author name so it explains
                          the unfamiliar external name before the reader
                          reaches it. 290 D-12/D-14: provenance comes from the
                          sanitized managed_by context and the plugin-drawn
                          badge only; the host never draws a Core origin pill
                          and never tests a plugin name. */}
                      <div className="text-xs text-muted-foreground" data-testid="comments-row-metadata">
                        {/* D-09/D-11: the backend guarantees a non-null
                            external author for every connector-managed row
                            (Plan 281-03), so the common.system fallback stays
                            reachable only for native rows — an external row
                            never renders as "System". */}
                        {comment.author_display_name || t('common.system', 'System')}
                        <PluginItemBadges badges={commentBadges.get(comment.id)} />
                        {' · '}
                        {formatDate(comment.created_at)}
                      </div>
                      {/* T-280-03: plain escaped text only — React's default
                          text-node rendering HTML-escapes the body; no
                          dangerouslySetInnerHTML, no markdown/HTML rendering. */}
                      <div className="mt-1 text-sm whitespace-pre-wrap break-words" data-testid="comments-row-body">
                        {comment.body}
                      </div>
                      {rowErrors[comment.id] === 'delete' && (
                        <div className="mt-1 flex flex-wrap items-center gap-2 text-xs" data-testid="comments-row-error">
                          <p className="text-destructive break-words">{t('browse.comments.delete_failed', 'The comment could not be deleted. Delete the comment again.')}</p>
                          <Button type="button" variant="ghost" size="sm" onClick={() => deleteComment(comment)} data-testid="comments-row-delete-retry">
                            {t('browse.comments.delete_retry', 'Delete comment again')}
                          </Button>
                        </div>
                      )}
                    </div>
                    <div className="shrink-0 flex items-center gap-1" data-testid="comments-row-actions">
                      {/* D-17: an externally-managed comment row never
                          renders the delete control at all — the whole
                          Can-gated element is omitted from the DOM
                          regardless of the viewer's objects:update
                          permission, mirroring the files section exactly.
                          290 D-12/D-14: only is_external (derived from
                          managed_by) governs the omission; a legacy Jira id
                          on a native row never hides a permitted delete.
                          Backend enforcement is the service-layer guard;
                          this omission is defense-in-depth. */}
                      {!comment.is_external && (
                        <Can resource="objects" action="update">
                          {/* D-15: visibility only — the backend record
                              permission on the DELETE route is the
                              enforcement. */}
                          <Button
                            type="button"
                            variant="ghost"
                            size="icon"
                            className="h-8 w-8 text-destructive hover:text-destructive"
                            leftIcon="trash-2"
                            aria-label={t('browse.comments.delete', 'Delete comment')}
                            title={t('browse.comments.delete', 'Delete comment')}
                            onClick={() => deleteComment(comment)}
                            data-testid="comments-row-delete"
                          />
                        </Can>
                      )}
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
          </div>
        </div>
      )}
    </div>
  )
}

// D-16: single-prop, fully self-contained section contract, exactly like
// ObjectFilesSection/ObjectHistorySection.
export default function ObjectCommentsSection({ objectId, active }) {
  return <ObjectCommentsSectionInner objectId={objectId} active={active} />
}