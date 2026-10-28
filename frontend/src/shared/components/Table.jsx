/**
 * Shared Table primitive (Phase 252, UX-06).
 *
 * Thin semantic wrapper, NOT a data grid: owns the native bordered scroll
 * shell and the <table> element; pages retain specialist thead/tbody
 * ownership (composed rows, expandable audit rows, drag headers, inline
 * editors, sticky matrix cells, nested history layouts). Accepts either
 * composed `children` (thead + tbody) or explicit `header`/`body` slots.
 */
export default function Table({
  header,
  body,
  children,
  className = '',
  tableClassName = '',
  'data-testid': testId = 'table',
}) {
  return (
    <div className={`rounded-lg border overflow-x-auto ${className}`} data-testid={testId}>
      <table className={`w-full ${tableClassName}`}>
        {children ?? (
          <>
            {header}
            {body}
          </>
        )}
      </table>
    </div>
  )
}