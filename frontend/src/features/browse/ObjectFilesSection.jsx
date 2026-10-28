import { useCallback, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import axios from 'axios'
import api from '../../shared/hooks/use-api'
import { useSystemSettings } from '../../shared/hooks/use-other-resources'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Can } from '../../shared/components/Can'
import { Button } from '../../shared/components/Button'
import { LucideIcon } from '../../shared/lib/lucide-icons'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { PluginItemBadges, usePluginItemBadges } from '../../shared/plugin-runtime/PluginItemBadges'

// D-06: the active per-file maximum defaults to 50 MB until an admin setting
// exists; the limit is per file, never an aggregate (D-06/D-07).
const DEFAULT_LIMIT_MB = 50
const MB_BYTES = 1024 * 1024

// WR-03: mirrors the backend's OBJECT_FILE_MAX_SIZE_MB_CAP
// (backend/app/api/settings.py) — clamp a persisted setting value that
// predates the cap (or was written directly) so the effective client-side
// limit can never exceed the boundary the backend itself enforces.
const MAX_LIMIT_MB = 2048

let localUploadSeq = 0

function nextLocalId() {
  localUploadSeq += 1
  return `upload-${localUploadSeq}`
}

function formatBytes(bytes) {
  if (!Number.isFinite(bytes) || bytes < 0) return null
  if (bytes < 1024) return `${bytes} B`
  if (bytes < MB_BYTES) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / MB_BYTES).toFixed(1)} MB`
}

// D-13: the download always uses the original filename, never a normalized or
// rewritten variant. The backend percent-encodes the original into the RFC
// 5987 `filename*=UTF-8''...` parameter with an ASCII quoted fallback — prefer
// the encoded original, then the quoted fallback, then the metadata name.
function attachmentFilename(disposition, fallback) {
  if (!disposition) return fallback
  const star = disposition.match(/filename\*=UTF-8''([^;]+)/i)
  if (star) {
    try {
      const decoded = decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
      if (decoded) return decoded
    } catch {
      // malformed encoding — fall through to the plain fallback below
    }
  }
  const plain = disposition.match(/filename="?([^";]+)"?/i)
  return plain ? plain[1].trim() : fallback
}

function useObjectFiles(objectId, enabled) {
  return useQuery({
    queryKey: ['object-files', objectId],
    queryFn: () => api.get(`/objects/${objectId}/files`).then((r) => r.data),
    enabled,
  })
}

// 279 tracer: collapsed-by-default lazy disclosure owning the native upload
// queue (D-01...D-05, D-08): committed rows belong to the React Query list;
// running/failed/cancelled queue entries are component state that survives
// any list refetch (Pitfall 5). Every tracer-visible string resolves through
// its catalogued `browse.files.*` key (279-06 contract).
function ObjectFilesSectionInner({ objectId, active }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const queryClient = useQueryClient()
  const { data: settings = {} } = useSystemSettings()
  // The shared tab strip in ObjectDetailPanel now owns visibility as a
  // single controlled `active` prop — this section no longer tracks its own
  // expanded state.
  const sectionExpanded = active
  const [queue, setQueue] = useState([])
  // 279-03: per-row operation state — which row's download is pending and
  // which committed rows carry a local download/delete failure (D-16/D-18/
  // D-19). Keyed by committed file id; unrelated rows stay untouched.
  const [downloadingId, setDownloadingId] = useState(null)
  const [rowErrors, setRowErrors] = useState({})
  const headingRef = useRef(null)
  const inputRef = useRef(null)
  const prevObjectIdRef = useRef(objectId)

  // WR-02: the queue/rowErrors/downloadingId state is intentionally local
  // (Pitfall 5, see below) so it survives a list refetch, but that means it
  // also survives the parent switching to a different Object without
  // unmounting this section. Reset it explicitly whenever objectId changes
  // so a stale queue entry from the previous object can never be retried
  // against the newly selected one — abort any in-flight uploads first so
  // they don't land against the wrong object mid-flight.
  useLayoutEffect(() => {
    if (prevObjectIdRef.current === objectId) return
    prevObjectIdRef.current = objectId
    setQueue((prev) => {
      prev.forEach((entry) => entry.controller?.abort())
      return []
    })
    setRowErrors({})
    setDownloadingId(null)
  }, [objectId])

  const parsedLimit = parseInt(settings.object_file_max_size_mb, 10)
  const limitMb = Number.isInteger(parsedLimit) && parsedLimit >= 1 && parsedLimit <= MAX_LIMIT_MB
    ? parsedLimit
    : DEFAULT_LIMIT_MB
  const limitBytes = limitMb * MB_BYTES

  const { data: files = [], isLoading, isError, refetch } = useObjectFiles(
    sectionExpanded ? objectId : null,
    sectionExpanded
  )

  // FEEXT-01/S2: one batch `itemBadges` resolution for the loaded file list.
  const fileIds = useMemo(() => files.map((file) => file.id), [files])
  // R4-03: sanitized provenance only (id -> managed_by) so the Jira resolver
  // decides item origin from already-read rows without a second request and
  // without handing any plugin the filename/uploader. Memoized because the host
  // folds `JSON.stringify(context)` into the query key.
  const fileBadgeContext = useMemo(() => {
    const itemsById = {}
    for (const file of files) itemsById[file.id] = file.managed_by
    return { objectId, itemsById }
  }, [objectId, files])
  const fileBadges = usePluginItemBadges('file', fileIds, fileBadgeContext)

  const startUpload = useCallback((entry) => {
    // D-08 re-check on retry: a browser-known oversize file stays a local
    // no-request failure even when its retry action is activated — the size
    // cannot change between attempts, so a POST would only re-fail upstream.
    if (entry.file && entry.file.size > limitBytes) {
      setQueue((prev) =>
        prev.map((e) =>
          e.localId === entry.localId
            ? { ...e, status: 'failed', error: 'oversize', progress: null, controller: null }
            : e
        )
      )
      return
    }
    const controller = new AbortController()
    const formData = new FormData()
    formData.append('file', entry.file, entry.file.name)
    setQueue((prev) =>
      prev.map((e) =>
        e.localId === entry.localId
          ? { ...e, status: 'running', progress: 0, error: null, controller }
          : e
      )
    )
    api.post(`/objects/${objectId}/files/upload`, formData, {
      signal: controller.signal,
      // The api instance defaults to application/json; FormData needs the
      // browser to attach its own multipart boundary (same convention as
      // useTriggerImportTask's imports/csv/upload call).
      headers: { 'Content-Type': 'multipart/form-data' },
      onUploadProgress: (evt) => {
        if (!evt.total) return
        setQueue((prev) =>
          prev.map((e) =>
            e.localId === entry.localId ? { ...e, progress: evt.loaded / evt.total } : e
          )
        )
      },
    })
      .then(() => {
        setQueue((prev) => prev.filter((e) => e.localId !== entry.localId))
        queryClient.invalidateQueries({ queryKey: ['object-files', objectId] })
      })
      .catch((error) => {
        if (axios.isCancel(error)) {
          setQueue((prev) =>
            prev.map((e) =>
              e.localId === entry.localId ? { ...e, status: 'cancelled', progress: null } : e
            )
          )
          return
        }
        setQueue((prev) =>
          prev.map((e) =>
            e.localId === entry.localId ? { ...e, status: 'failed', error: 'upload' } : e
          )
        )
      })
  }, [objectId, queryClient, limitBytes])

  // D-02/D-08: every selection appends to the existing queue; locally valid
  // files start immediately and in parallel; a browser-known oversized file
  // becomes a failed local entry without ever hitting the upload route.
  const handleFiles = useCallback((fileList) => {
    const selected = Array.from(fileList || [])
    if (selected.length === 0) return
    const newEntries = selected.map((file) => {
      const localId = nextLocalId()
      if (file.size > limitBytes) {
        return {
          localId, file, name: file.name, size: file.size,
          status: 'failed', error: 'oversize', progress: null, controller: null,
        }
      }
      return {
        localId, file, name: file.name, size: file.size,
        status: 'running', progress: 0, controller: null,
      }
    })
    setQueue((prev) => [...prev, ...newEntries])
    newEntries
      .filter((e) => e.status === 'running')
      .forEach((e) => startUpload(e))
  }, [limitBytes, startUpload])

  const removeEntry = useCallback((localId) => {
    setQueue((prev) => prev.filter((e) => e.localId !== localId))
  }, [])

  const clearRowError = useCallback((fileId) => {
    setRowErrors((prev) => {
      if (!prev[fileId]) return prev
      const next = { ...prev }
      delete next[fileId]
      return next
    })
  }, [])

  // D-13: attachment download through the authorized object-file route with
  // the original filename; the failure stays local to the row with its own
  // retry (D-18) and never affects unrelated rows.
  const downloadFile = useCallback(async (file) => {
    clearRowError(file.id)
    setDownloadingId(file.id)
    try {
      const response = await api.get(`/objects/${objectId}/files/${file.id}/download`, {
        responseType: 'blob',
      })
      const disposition = response.headers?.['content-disposition'] || ''
      const filename = attachmentFilename(disposition, file.filename)
      const url = URL.createObjectURL(new Blob([response.data], { type: file.content_type || 'application/octet-stream' }))
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = filename
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch {
      setRowErrors((prev) => ({ ...prev, [file.id]: 'download' }))
    } finally {
      setDownloadingId((prev) => (prev === file.id ? null : prev))
    }
  }, [objectId, clearRowError])

  // D-14/D-19: every delete awaits the shared danger confirmation; success
  // removes/refreshes the row promptly, failure retains it with a local retry.
  const deleteFile = useCallback(async (file) => {
    const confirmed = await confirmDialog({
      message: t('browse.files.delete_confirm', 'Delete file: permanently delete "{{name}}"? This action cannot be undone.', { name: file.filename }),
      danger: true,
    })
    if (!confirmed) return
    try {
      await api.delete(`/objects/${objectId}/files/${file.id}`)
      clearRowError(file.id)
      queryClient.invalidateQueries({ queryKey: ['object-files', objectId] })
    } catch {
      setRowErrors((prev) => ({ ...prev, [file.id]: 'delete' }))
    }
  }, [objectId, queryClient, t, clearRowError])

  const handleLoadRetry = () => {
    refetch()
    headingRef.current?.focus()
  }

  return (
    <div data-testid="object-files-section">
      {sectionExpanded && (
        <div id="object-files-body" className="p-6 pt-3 space-y-2" data-testid="files-body">
          <h3 ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none" data-testid="files-heading">
            {t('browse.files.title', 'Files')}
          </h3>
          {/* D-20/T-279-09: the update gate wraps only the mutation
              affordances — disclosure, list, metadata, and download stay
              visible to record readers. Visibility only; the record
              permission dependency on the routes is the enforcement. */}
          <Can resource="objects" action="update">
            <>
              <input
                ref={inputRef}
                type="file"
                multiple
                className="sr-only"
                data-testid="files-upload-input"
                onChange={(e) => {
                  handleFiles(e.target.files)
                  e.target.value = ''
                }}
              />
              {/* UI-SPEC: the drop zone is the primary drag surface and is
                  also keyboard-selectable; the explicit CTA below opens the
                  same visually-hidden native input. */}
              <div
                role="button"
                tabIndex={0}
                data-testid="files-drop-zone"
                onClick={() => inputRef.current?.click()}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' || e.key === ' ') {
                    e.preventDefault()
                    inputRef.current?.click()
                  }
                }}
                onDragOver={(e) => e.preventDefault()}
                onDrop={(e) => {
                  e.preventDefault()
                  handleFiles(e.dataTransfer.files)
                }}
                className="rounded-lg border border-dashed p-6 text-center text-sm text-muted-foreground cursor-pointer"
              >
                <LucideIcon name="upload" size={16} className="mx-auto mb-1" />
                {t('browse.files.drop_hint', 'Drag and drop files here or select them')}
              </div>
              <div className="flex items-center justify-between gap-3">
                <Button type="button" size="md" onClick={() => inputRef.current?.click()} data-testid="files-upload-action">
                  {t('browse.files.upload_action', 'Upload files')}
                </Button>
                {/* D-07: the active limit is visible before any selection. */}
                <p className="text-xs text-muted-foreground" data-testid="files-limit-hint">
                  {t('browse.files.max_size_hint', 'Maximum {{limit}} MB per file', { limit: limitMb })}
                </p>
              </div>
            </>
          </Can>

          {queue.map((entry) => {
            const pct = entry.progress === null || entry.progress === undefined ? 0 : Math.round(entry.progress * 100)
            return (
              <div key={entry.localId} data-testid="files-queue-entry" aria-live="polite" className="rounded-lg border px-3 py-2 space-y-1">
                <div className="flex items-center justify-between gap-3">
                  <span className="text-sm font-medium min-w-0 truncate" title={entry.name} data-testid="files-queue-name">
                    {entry.name}
                  </span>
                  <span className="text-xs text-muted-foreground shrink-0">{formatBytes(entry.size)}</span>
                </div>
                {entry.status === 'running' && (
                  <div className="flex items-center gap-2">
                    <div
                      className="flex-1 h-1.5 rounded bg-muted overflow-hidden"
                      role="progressbar"
                      aria-valuemin={0}
                      aria-valuemax={100}
                      aria-valuenow={pct}
                      data-testid="files-queue-progress"
                    >
                      <div className="h-full bg-primary transition-all" style={{ width: `${pct}%` }} />
                    </div>
                    <span className="text-xs text-muted-foreground w-10 text-right shrink-0">{pct}%</span>
                    <Button variant="ghost" size="sm" onClick={() => entry.controller?.abort()} data-testid="files-queue-cancel">
                      {t('browse.files.cancel', 'Cancel upload')}
                    </Button>
                  </div>
                )}
                {entry.status !== 'running' && (
                  <div className="flex flex-wrap items-center gap-2 text-xs">
                    {entry.status === 'failed' && entry.error === 'oversize' && (
                      <p className="text-destructive break-words" data-testid="files-local-error">
                        {t('browse.files.oversize_error', '"{{name}}" exceeds the limit of {{limit}}. The file was not uploaded.', { name: entry.name, limit: limitMb })}
                      </p>
                    )}
                    {entry.status === 'failed' && entry.error === 'upload' && (
                      <p className="text-destructive break-words" data-testid="files-local-error">
                        {t('browse.files.upload_failed', '"{{name}}" could not be uploaded. Retry the upload.', { name: entry.name })}
                      </p>
                    )}
                    {entry.status === 'cancelled' && (
                      <p className="text-muted-foreground break-words" data-testid="files-local-error">
                        {t('browse.files.upload_cancelled', 'Upload of "{{name}}" cancelled.', { name: entry.name })}
                      </p>
                    )}
                    <Button variant="ghost" size="sm" onClick={() => startUpload(entry)} data-testid="files-queue-retry">
                      {t('browse.files.upload_retry', 'Retry upload')}
                    </Button>
                    <Button variant="ghost" size="sm" onClick={() => removeEntry(entry.localId)} data-testid="files-queue-remove">
                      {t('browse.files.queue_remove', 'Remove file from upload list')}
                    </Button>
                  </div>
                )}
              </div>
            )
          })}

          {isLoading && queue.length === 0 && (
            <p className="text-sm text-muted-foreground">{t('common.loading', 'Loading...')}</p>
          )}
          {isError && (
            <div className="flex items-center justify-between gap-3" data-testid="files-load-error">
              <p className="text-sm text-destructive">{t('browse.files.load_failed', 'Files could not be loaded. Reload the file list.')}</p>
              <Button type="button" variant="outline" size="sm" onClick={handleLoadRetry} data-testid="files-load-retry">
                {t('browse.files.load_retry', 'Reload file list')}
              </Button>
            </div>
          )}
          {!isLoading && !isError && files.length === 0 && (
            <div className="text-center py-6" data-testid="files-empty">
              <p className="text-sm font-medium">{t('browse.files.empty.title', 'No files yet')}</p>
              <p className="text-xs text-muted-foreground mt-1">{t('browse.files.empty.description', 'Upload files to add them to this object.')}</p>
            </div>
          )}
          {!isLoading && !isError && files.length > 0 && (
            <div className="rounded-lg border overflow-hidden" data-testid="files-list">
              {files.map((file) => (
                <div key={file.id} className="border-b last:border-0 px-3 py-3 flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-3" data-testid="files-row">
                  <div className="flex-1 min-w-0">
                    {/* D-13/D-16: native and connector-managed rows stay
                        in ONE date-ordered list — no separate external
                        subsection. The plugin badge sits outside the truncate
                        span so it is never itself clipped, and an externally
                        managed row omits the delete affordance (D-17).
                        290 D-12/D-14: provenance comes from the sanitized
                        managed_by context and the plugin-drawn badge only;
                        the host never draws a Core origin pill and never
                        tests a plugin name. */}
                    <div className="flex items-center min-w-0" data-testid="files-row-name">
                      <span className="text-sm font-semibold min-w-0 truncate" title={file.filename}>
                        {file.filename}
                      </span>
                      <PluginItemBadges badges={fileBadges.get(file.id)} />
                    </div>
                    <div className="text-xs text-muted-foreground" data-testid="files-row-metadata">
                      {formatBytes(file.size_bytes) || t('common.empty_value', '—')}
                      {' · '}
                      {file.content_type || t('common.empty_value', '—')}
                      {' · '}
                      {file.uploader_display_name || t('common.empty_value', '—')}
                      {' · '}
                      {formatDate(file.created_at)}
                    </div>
                    {downloadingId === file.id && (
                      <p className="mt-1 text-xs text-muted-foreground" data-testid="files-row-download-pending">
                        {t('browse.files.download_pending', 'Downloading "{{name}}"...', { name: file.filename })}
                      </p>
                    )}
                    {rowErrors[file.id] === 'download' && (
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs" data-testid="files-row-error">
                        <p className="text-destructive break-words">
                          {t('browse.files.download_failed', 'Download of "{{name}}" failed. Retry the download.', { name: file.filename })}
                        </p>
                        <Button variant="ghost" size="sm" onClick={() => downloadFile(file)} data-testid="files-row-download-retry">
                          {t('browse.files.download_retry', 'Retry download')}
                        </Button>
                      </div>
                    )}
                    {rowErrors[file.id] === 'delete' && (
                      <div className="mt-1 flex flex-wrap items-center gap-2 text-xs" data-testid="files-row-error">
                        <p className="text-destructive break-words">{t('browse.files.delete_failed', 'The file could not be deleted. Delete the file again.')}</p>
                        <Button variant="ghost" size="sm" onClick={() => deleteFile(file)} data-testid="files-row-delete-retry">
                          {t('browse.files.delete_retry', 'Delete file again')}
                        </Button>
                      </div>
                    )}
                  </div>
                  <div className="shrink-0 flex items-center gap-1" data-testid="files-row-actions">
                    {/* D-14: Download renders unconditionally for every row,
                        externally-managed included — read-only means no
                        mutation affordance, not no access to the imported
                        content. */}
                    <Button
                      type="button"
                      variant="outline"
                      size="icon"
                      className="h-8 w-8"
                      leftIcon="download"
                      aria-label={downloadingId === file.id
                        ? t('browse.files.download_pending', 'Downloading "{{name}}"...', { name: file.filename })
                        : t('browse.files.download', 'Download file')}
                      title={t('browse.files.download', 'Download file')}
                      disabled={downloadingId === file.id}
                      onClick={() => downloadFile(file)}
                      data-testid="files-row-download"
                    />
                    {/* D-17: an externally-managed row never renders the
                        delete control at all — the whole Can-gated element is
                        omitted from the DOM regardless of the viewer's
                        objects:update permission (a stronger rule than the
                        Can gate itself, which merely shows/hides by
                        permission). 290 D-12/D-14: only is_external (derived
                        from managed_by) governs the omission; a legacy Jira
                        id on a native row never hides a permitted delete.
                        Backend enforcement is the service-layer guard; this
                        omission is defense-in-depth. */}
                    {!file.is_external && (
                      <Can resource="objects" action="update">
                        <Button
                          type="button"
                          variant="ghost"
                          size="icon"
                          className="h-8 w-8 text-destructive hover:text-destructive"
                          leftIcon="trash-2"
                          aria-label={t('browse.files.delete', 'Delete file')}
                          title={t('browse.files.delete', 'Delete file')}
                          onClick={() => deleteFile(file)}
                          data-testid="files-row-delete"
                        />
                      </Can>
                    )}
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

// D-20/R-04: read access preserves the disclosure, list, and download; only
// the mutation controls are gated on objects:update — the backend record
// permission dependencies are the enforcement point, this gate is
// visibility only.
export default function ObjectFilesSection({ objectId, active }) {
  return <ObjectFilesSectionInner objectId={objectId} active={active} />
}