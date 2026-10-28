import { useRef, useState } from 'react'
import { useObjectHistory } from '../../shared/hooks/use-audit-logs'
import { useSystemSettings } from '../../shared/hooks/use-other-resources'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { Can } from '../../shared/components/Can'
import { PaginationBar } from '../../shared/components/PaginationBar'
import { Button } from '../../shared/components/Button'
import { useTranslation } from 'react-i18next'

// Raw ObjectValue storage-column keys — never shown verbatim as a label
// (D-07: no storage names such as `value_simple` as the primary label).
const RAW_VALUE_KEYS = new Set([
  'value_float', 'value_integer', 'value_boolean', 'value_simple',
  'value_datetime', 'value_struct',
])

// Status (and similarly-shaped structured) values are stored as {id, name}
// objects — show just the human-readable name instead of dumping raw JSON.
function simplifyValue(value) {
  if (value && typeof value === 'object' && typeof value.name === 'string') {
    return value.name
  }
  return value
}

function renderValue(rawValue, t) {
  const value = simplifyValue(rawValue)
  if (value === null || value === undefined) {
    return <span className="text-muted-foreground italic">{t('common.null_value', 'null')}</span>
  }
  if (typeof value === 'object') {
    return (
      <pre className="font-mono text-xs bg-background/50 rounded p-2 overflow-x-auto max-w-full">
        {JSON.stringify(value, null, 2)}
      </pre>
    )
  }
  return <span className="break-words">{String(value)}</span>
}

// D-07: for creates the old side is honestly absent (—, not the literal
// word "null"); for deletes the new side is absent the same way. Only a
// genuine UPDATE with a null value renders the literal "null".
function renderSide(rawValue, isOmittedSide, t) {
  if (isOmittedSide) {
    return <span className="text-muted-foreground">{t('common.empty_value', '—')}</span>
  }
  return renderValue(rawValue, t)
}

function objectFieldLabel(field, t) {
  switch (field) {
    case 'name': return t('browse.history.field.name', 'Name')
    case 'key': return t('browse.history.field.key', 'Key')
    case 'object_type_id': return t('browse.history.field.object_type_id', 'Object Type')
    case 'schema_id': return t('browse.history.field.schema_id', 'Schema')
    case 'source_id': return t('browse.history.field.source_id', 'Source')
    case 'created_at': return t('browse.history.field.created_at', 'Created at')
    case 'updated_at': return t('browse.history.field.updated_at', 'Updated at')
    default: return t('browse.history.field.changed_field', 'Changed field')
  }
}

function objectValueFieldLabel(field, t) {
  switch (field) {
    case 'object_id': return t('browse.history.field.value_object', 'Object')
    case 'object_type_attribute_id': return t('browse.history.field.value_attribute', 'Attribute')
    case 'attribute_type_id': return t('browse.history.field.attribute_type', 'Attribute type')
    case 'id': return t('browse.history.field.record_id', 'Record ID')
    default: return t('browse.history.field.changed_field', 'Changed field')
  }
}

// D-07: attribute-value entries always prefer the enriched attribute name;
// only fall back to a defensive raw/unknown-key label when it is missing.
function fieldLabel(entry, field, t) {
  if (entry.entity_type === 'object_refs') {
    return entry.reference_field_name || t('browse.history.field.reference', 'Reference')
  }
  if (entry.entity_type === 'object_values') {
    if (entry.attribute_name) return entry.attribute_name
    if (RAW_VALUE_KEYS.has(field)) return t('browse.history.field.attribute_value', 'Attribute value')
    return objectValueFieldLabel(field, t)
  }
  return objectFieldLabel(field, t)
}

// Flatten every eligible audit entry into one directly readable row per
// changed field (D-07) — an object CREATE/UPDATE can carry several changed
// fields in one entry; an object_values UPDATE normally carries exactly one.
function buildRows(items) {
  const rows = []
  for (const entry of items) {
    if (entry.entity_type === 'object_refs') {
      const targetChange = entry.changes?.to_object_id
      if (targetChange) {
        rows.push({
          rowKey: `${entry.id}-reference`,
          entry,
          field: 'reference',
          old: entry.reference_old_target_name ?? targetChange.old,
          new: entry.reference_new_target_name ?? targetChange.new,
        })
      }
      continue
    }
    const changeFields = Object.entries(entry.changes || {})
    for (const [field, change] of changeFields) {
      rows.push({
        rowKey: `${entry.id}-${field}`,
        entry,
        field,
        old: change?.old,
        new: change?.new,
      })
    }
  }
  return rows
}

function HistoryRow({ row, formatDate, t }) {
  const { entry, field } = row
  const label = fieldLabel(entry, field, t)
  const oldOmitted = entry.action === 'CREATE'
  const newOmitted = entry.action === 'DELETE'

  return (
    <div
      className="border-b last:border-0 px-3 py-3 flex flex-col gap-1.5 sm:flex-row sm:items-start sm:gap-3"
      data-testid="history-row"
    >
      <div className="sm:w-40 shrink-0 text-sm font-medium" data-testid="history-row-field">
        {label}
      </div>
      <div className="flex-1 min-w-0 flex flex-wrap items-center gap-2 text-sm" data-testid="history-row-values">
        <span className="sr-only">{t('browse.history.old_label', 'Old value:')}</span>
        <span className="text-red-600 dark:text-red-400">
          {renderSide(row.old, oldOmitted, t)}
        </span>
        <span className="text-muted-foreground px-1" aria-hidden="true">→</span>
        <span className="sr-only">{t('browse.history.new_label', 'New value:')}</span>
        <span className="text-green-600 dark:text-green-400">
          {renderSide(row.new, newOmitted, t)}
        </span>
      </div>
      <div className="sm:w-44 shrink-0 text-xs text-muted-foreground sm:text-right space-y-0.5">
        <div data-testid="history-row-date">{formatDate(entry.created_at)}</div>
        <div data-testid="history-row-actor">{entry.actor_display_name || t('common.system', 'System')}</div>
      </div>
    </div>
  )
}

function ObjectHistorySectionInner({ objectId, active }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  // The shared tab strip in ObjectDetailPanel now owns visibility as a
  // single controlled `active` prop — this section no longer tracks its own
  // expanded state.
  const sectionExpanded = active
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(25)
  const headingRef = useRef(null)
  const { data, isLoading, isError, refetch } = useObjectHistory(
    sectionExpanded ? objectId : null,
    { page, page_size: pageSize }
  )

  // ObjectValue CREATE/DELETE entries are plumbing (e.g. a type-change
  // delete+recreate) and are already excluded server-side (object_id-scoped
  // query) so `data.total` matches what's rendered here.
  const rows = buildRows(data?.items || [])

  // Changing page keeps the disclosure open and moves focus to the History
  // heading instead of resetting scroll/reading position (D-UI-SPEC §2).
  const handlePageChange = (nextPage) => {
    setPage(nextPage)
    headingRef.current?.focus()
  }

  return (
    <div data-testid="object-history-section">
      {sectionExpanded && (
        <div className="p-6 pt-3 space-y-3" data-testid="history-body">
          <h3 ref={headingRef} tabIndex={-1} className="text-lg font-semibold outline-none" data-testid="history-heading">
            {t('browse.history.title', 'History')}
          </h3>
          {isLoading && <p className="text-sm text-muted-foreground">{t('common.loading', 'Loading...')}</p>}
          {isError && (
            <div className="flex items-center justify-between gap-3">
              <p className="text-sm text-destructive">{t('browse.history.load_failed', 'Could not load history.')}</p>
              <Button type="button" variant="outline" size="sm" onClick={() => refetch()}>
                {t('common.retry', 'Retry')}
              </Button>
            </div>
          )}
          {!isLoading && !isError && rows.length === 0 && (
            <div className="text-center py-6">
              <p className="text-sm font-medium">{t('browse.history.empty_title', 'No history recorded')}</p>
              <p className="text-xs text-muted-foreground mt-1">
                {t('browse.history.empty_description', 'Changes to this object and its attribute values will appear here.')}
              </p>
            </div>
          )}
          {!isLoading && !isError && rows.length > 0 && (
            <div className="rounded-lg border overflow-hidden" aria-live="polite">
              {rows.map((row) => (
                <HistoryRow key={row.rowKey} row={row} formatDate={formatDate} t={t} />
              ))}
              {data?.total > 0 && (
                <PaginationBar
                  page={page}
                  pageSize={pageSize}
                  total={data.total}
                  onPageChange={handlePageChange}
                  onPageSizeChange={(size) => {
                    setPageSize(size)
                    setPage(1)
                  }}
                />
              )}
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function ObjectHistorySection({ objectId, active }) {
  const { data: settings = {} } = useSystemSettings()
  if (settings.history_enabled !== 'true') return null

  return (
    <Can resource="audit-log" action="read">
      <ObjectHistorySectionInner objectId={objectId} active={active} />
    </Can>
  )
}
