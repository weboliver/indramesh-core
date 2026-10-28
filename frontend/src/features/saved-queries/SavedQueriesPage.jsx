import { useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useTranslation } from 'react-i18next'
import PageHeader from '../../shared/components/PageHeader'
import { Button } from '../../shared/components/Button'
import { useCan } from '../../shared/components/Can'
import { showToast } from '../../shared/components/Toast'
import { confirmDialog } from '../../shared/components/ConfirmDialog'
import { extractErrorMessage } from '../../shared/lib/utils'
import { usePluginRuntimeList } from '../../shared/plugin-runtime/PluginProvider'
import {
  useSaveQuery,
  useExecuteSavedQuery,
  fetchSavedQueriesExport,
  importSavedQueries,
} from '../../shared/hooks/use-saved-queries'
import { analyzeSavedQuery, parseSavedQueryVariables, validateSavedQueriesDocument } from './validate-saved-query'
import SavedQueriesList from './SavedQueriesList'
import SavedQueryEditor from './SavedQueryEditor'
import SavedQueryResultPanel from './SavedQueryResultPanel'
import ImportResultDialog from './ImportResultDialog'
import { buildStudioHandoff } from './studio-handoff'

const STUDIO_PLUGIN_NAME = 'graphql-studio'
const STUDIO_PAGE_PATH = `/plugins/${STUDIO_PLUGIN_NAME}/studio`

const EMPTY_EDITOR = { id: null, name: '', queryText: '', variablesText: '', operationName: '' }

function editorEquals(a, b) {
  return (
    a.id === b.id &&
    a.name === b.name &&
    a.queryText === b.queryText &&
    a.variablesText === b.variablesText
  )
}

// Map a server 422 reason to the field it describes (the create/update/execute
// routes return a bare reason string, not a structured path).
function mapServerField(reason) {
  const r = (reason || '').toLowerCase()
  if (r.includes('variables')) return 'variables'
  if (r.includes('operation name') || r.includes('multiple operations')) return 'operationName'
  return 'queryText'
}

// Core Saved Queries workspace (304 GQL-01/GQL-02/GQL-03, D-12..D-14).
// Works with no plugin active; a plugin provides only the optional Studio handoff.
export default function SavedQueriesPage() {
  const { t } = useTranslation()
  const canCreate = useCan('saved-queries', 'create')
  const canUpdate = useCan('saved-queries', 'update')
  const canDelete = useCan('saved-queries', 'delete')
  const canExecute = useCan('saved-queries', 'execute')
  const canRead = useCan('saved-queries', 'read')

  const [editor, setEditor] = useState(EMPTY_EDITOR)
  const [selectedId, setSelectedId] = useState(null)
  const [saveError, setSaveError] = useState(null) // { field, reason }
  const [result, setResult] = useState(null)
  const [executeError, setExecuteError] = useState(null) // { status, message }
  const baselineRef = useRef(EMPTY_EDITOR)
  const headingRef = useRef(null)

  const saveMutation = useSaveQuery()
  const executeMutation = useExecuteSavedQuery()
  const queryClient = useQueryClient()
  const fileInputRef = useRef(null)
  const [exporting, setExporting] = useState(false)
  const [importing, setImporting] = useState(false)
  const [importError, setImportError] = useState(null) // { path, reason } | { invalidFile, reason }
  const [importResult, setImportResult] = useState(null)
  const [importDialogOpen, setImportDialogOpen] = useState(false)
  const { data: runtimePlugins, isPending: runtimePluginsPending } = usePluginRuntimeList()
  const studioActive =
    !runtimePluginsPending &&
    Array.isArray(runtimePlugins) &&
    runtimePlugins.some((plugin) => plugin.name === STUDIO_PLUGIN_NAME)

  const analysis = useMemo(
    () => analyzeSavedQuery(editor.queryText, editor.operationName),
    [editor.queryText, editor.operationName]
  )
  const variablesParse = useMemo(
    () => parseSavedQueryVariables(editor.variablesText),
    [editor.variablesText]
  )

  const focusHeading = () => {
    requestAnimationFrame(() => {
      headingRef.current?.focus()
      headingRef.current?.scrollIntoView({ block: 'nearest' })
    })
  }

  const isDirty = () => !editorEquals(editor, baselineRef.current)

  const confirmDiscardIfDirty = async () => {
    if (!isDirty()) return true
    return Boolean(
      await confirmDialog({
        message: t('graphql.saved_queries.discard_unsaved', 'Discard unsaved changes to this query?'),
        danger: true,
        confirmLabel: t('graphql.saved_queries.discard_action', 'Discard changes'),
        cancelLabel: t('graphql.saved_queries.keep_action', 'Keep editing'),
      })
    )
  }

  const applyItem = (item) => {
    const next = {
      id: item.id,
      name: item.name ?? '',
      queryText: item.query_text ?? '',
      variablesText: item.variables ? JSON.stringify(item.variables, null, 2) : '',
      operationName: '',
    }
    setEditor(next)
    baselineRef.current = next
    setSelectedId(item.id)
    setSaveError(null)
    setExecuteError(null)
    setResult(null)
    focusHeading()
  }

  const handleLoad = async (item) => {
    if (item.id === editor.id && !isDirty()) return
    if (!(await confirmDiscardIfDirty())) return
    applyItem(item)
  }

  const handleNew = async () => {
    if (!(await confirmDiscardIfDirty())) return
    setEditor(EMPTY_EDITOR)
    baselineRef.current = EMPTY_EDITOR
    setSelectedId(null)
    setSaveError(null)
    setExecuteError(null)
    setResult(null)
    focusHeading()
  }

  const handleDeleted = (item) => {
    showToast(t('graphql.saved_queries.delete_success', 'Query deleted.'))
    if (item.id === editor.id) {
      setEditor(EMPTY_EDITOR)
      baselineRef.current = EMPTY_EDITOR
      setSelectedId(null)
      setResult(null)
      setExecuteError(null)
    }
  }

  const fieldErrors = {
    name: editor.name.trim() ? null : t('graphql.saved_queries.name_required', 'Enter a name.'),
    queryText: analysis.empty
      ? t('graphql.saved_queries.query_required', 'Enter a query.')
      : analysis.syntaxError
        ? analysis.syntaxError
        : analysis.noOperation
          ? t('graphql.saved_queries.query_required', 'Enter a query.')
          : analysis.hasNonQuery
            ? t('graphql.saved_queries.query_only', 'Only query operations are allowed. This document contains a mutation or subscription.')
            : null,
    variables: variablesParse.ok ? null : t('graphql.saved_queries.variables_invalid', 'Variables must be a valid JSON object.'),
    operationName: analysis.unknownOperationName
      ? t('graphql.saved_queries.unknown_operation', 'Unknown operation name.')
      : null,
  }
  if (saveError?.field) {
    fieldErrors[saveError.field] = saveError.reason
  }

  const hasPermissionToSave = editor.id ? canUpdate : canCreate
  const canSave =
    hasPermissionToSave &&
    Boolean(editor.name.trim()) &&
    !analysis.empty &&
    analysis.valid &&
    variablesParse.ok &&
    !saveMutation.isPending

  const queryTextDirty = Boolean(editor.id) && editor.queryText !== baselineRef.current.queryText
  let executeBlockReason = null
  if (editor.id && analysis.multiple && !editor.operationName) {
    executeBlockReason = t('graphql.saved_queries.multiple_operations', 'This document contains several operations. Provide the operationName to run it.')
  } else if (editor.id && queryTextDirty) {
    executeBlockReason = t('graphql.saved_queries.save_before_run', 'Save the query before running it.')
  }
  const canExecuteNow = Boolean(editor.id) && variablesParse.ok && !executeBlockReason && !executeMutation.isPending

  const handleSave = () => {
    if (!canSave) return
    saveMutation.mutate(
      {
        id: editor.id,
        name: editor.name.trim(),
        queryText: editor.queryText,
        variables: variablesParse.value,
      },
      {
        onSuccess: (saved) => {
          setSaveError(null)
          const next = {
            id: saved.id,
            name: saved.name ?? '',
            queryText: saved.query_text ?? '',
            variablesText: saved.variables ? JSON.stringify(saved.variables, null, 2) : '',
            operationName: editor.operationName,
          }
          setEditor(next)
          baselineRef.current = next
          setSelectedId(saved.id)
          showToast(t('graphql.saved_queries.save_success', 'Query saved.'))
        },
        onError: (error) => {
          const reason = extractErrorMessage(error)
          setSaveError({ field: mapServerField(reason), reason })
        },
      }
    )
  }

  const handleExecute = () => {
    if (!canExecuteNow) return
    setExecuteError(null)
    executeMutation.mutate(
      { id: editor.id, variables: variablesParse.value, operationName: editor.operationName || null },
      {
        onSuccess: (data) => setResult(data),
        onError: (error) => {
          setResult(null)
          const status = error?.response?.status
          if (status === 404) {
            setExecuteError({ status, message: t('graphql.saved_queries.query_gone', 'This saved query no longer exists. Reload the list.') })
          } else {
            setExecuteError({
              status,
              message: t('graphql.saved_queries.request_rejected', 'The request was rejected: {{reason}}', { reason: extractErrorMessage(error) }),
            })
          }
        },
      }
    )
  }

  const handleExport = async () => {
    setExporting(true)
    try {
      const exportDocument = await fetchSavedQueriesExport()
      const blob = new Blob([JSON.stringify(exportDocument, null, 2)], { type: 'application/json' })
      const url = URL.createObjectURL(blob)
      const anchor = document.createElement('a')
      anchor.href = url
      anchor.download = 'saved-queries.json'
      document.body.appendChild(anchor)
      anchor.click()
      anchor.remove()
      URL.revokeObjectURL(url)
    } catch (error) {
      showToast(t('graphql.saved_queries.export_failed', 'Export failed: {{reason}}', { reason: extractErrorMessage(error) }), { type: 'error' })
    } finally {
      setExporting(false)
    }
  }

  const handleImportClick = () => {
    setImportError(null)
    fileInputRef.current?.click()
  }

  const handleFileChange = async (event) => {
    const file = event.target.files?.[0]
    event.target.value = ''
    if (!file) return

    let importDocument
    try {
      importDocument = JSON.parse(await file.text())
    } catch (error) {
      setImportError({ invalidFile: true, reason: error.message })
      return
    }

    const validation = validateSavedQueriesDocument(importDocument)
    if (!validation.ok) {
      setImportError({ path: validation.path, reason: validation.reason })
      return
    }

    setImporting(true)
    try {
      const created = await importSavedQueries(importDocument)
      setImportResult(created)
      setImportDialogOpen(true)
      queryClient.invalidateQueries({ queryKey: ['saved-queries'] })
    } catch (error) {
      const detail = error?.response?.data?.detail
      if (detail && typeof detail === 'object' && detail.path) {
        setImportError({ path: detail.path, reason: detail.message })
      } else {
        setImportError({ path: '', reason: extractErrorMessage(error) })
      }
    } finally {
      setImporting(false)
    }
  }

  return (
    <div className="space-y-6" data-testid="saved-queries-page">
      <PageHeader
        title={t('graphql.saved_queries.title', 'GraphQL Library')}
        description={t('graphql.saved_queries.description', 'Reusable GraphQL queries. Run one with variables and inspect the JSON result.')}
        icon="bookmark"
      >
        <div className="flex flex-wrap items-center gap-2">
          <Button
            type="button"
            variant={editor.id ? 'outline' : 'primary'}
            size="md"
            onClick={handleNew}
            className="h-auto min-h-9 whitespace-normal"
            data-testid="saved-query-new"
          >
            {t('graphql.saved_queries.new_query', 'New query')}
          </Button>
          {canRead && (
            <Button
              type="button"
              variant="outline"
              size="md"
              onClick={handleExport}
              loading={exporting}
              className="h-auto min-h-9 whitespace-normal"
              data-testid="saved-query-export"
            >
              {t('graphql.saved_queries.export', 'Export')}
            </Button>
          )}
          {canCreate && (
            <Button
              type="button"
              variant="outline"
              size="md"
              onClick={handleImportClick}
              loading={importing}
              className="h-auto min-h-9 whitespace-normal"
              data-testid="saved-query-import"
            >
              {t('graphql.saved_queries.import', 'Import')}
            </Button>
          )}
          {studioActive && (
            <Button
              asChild
              variant="outline"
              size="md"
              className="h-auto min-h-9 whitespace-normal"
              data-testid="saved-query-open-studio"
            >
              <Link to={STUDIO_PAGE_PATH} state={buildStudioHandoff(editor)}>
                {t('graphql.saved_queries.open_in_studio', 'Open in Studio')}
              </Link>
            </Button>
          )}
          <input
            ref={fileInputRef}
            type="file"
            accept="application/json,.json"
            className="hidden"
            onChange={handleFileChange}
            data-testid="saved-query-import-input"
          />
        </div>
      </PageHeader>

      {importError && (
        <p role="alert" data-testid="saved-query-import-error" className="text-sm text-destructive">
          {importError.invalidFile
            ? t('graphql.saved_queries.import_invalid_file', 'The file is not a valid saved-queries export ({{reason}}).', { reason: importError.reason })
            : t('graphql.saved_queries.import_invalid_document', 'Nothing was imported. {{path}}: {{reason}}', { path: importError.path || '', reason: importError.reason })}
        </p>
      )}

      <div className="flex flex-col gap-6 xl:flex-row">
        <div className="h-[50vh] min-w-0 overflow-hidden rounded-lg border xl:h-[70vh] xl:basis-[38rem] xl:shrink-0">
          <SavedQueriesList
            onLoad={handleLoad}
            onDeleted={handleDeleted}
            selectedId={selectedId}
            canDelete={canDelete}
          />
        </div>
        <div className="min-w-0 flex-1 space-y-8">
          <SavedQueryEditor
            state={editor}
            onChange={(patch) => setEditor((prev) => ({ ...prev, ...patch }))}
            analysis={analysis}
            fieldErrors={fieldErrors}
            variablesHelp={t('graphql.saved_queries.variables_help', "Optional JSON object merged over the query's saved defaults; a request value wins.")}
            canSave={canSave}
            canExecute={canExecute}
            canExecuteNow={canExecuteNow}
            onSave={handleSave}
            onExecute={handleExecute}
            saving={saveMutation.isPending}
            executing={executeMutation.isPending}
            saveError={saveError?.reason ?? null}
            executeBlockReason={executeBlockReason}
            headingRef={headingRef}
          />
          <SavedQueryResultPanel
            result={result}
            error={executeError}
            isLoading={executeMutation.isPending}
          />
        </div>
      </div>

      <ImportResultDialog
        open={importDialogOpen}
        result={importResult}
        onClose={() => setImportDialogOpen(false)}
      />
    </div>
  )
}
