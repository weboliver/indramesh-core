import { Button } from './Button'
import { useTranslation } from 'react-i18next'

const TECHNICAL_OPERATORS = {
  equals: '=',
  notEquals: '!=',
  greaterThan: '>',
  lessThan: '<',
  greaterThanOrEqual: '>=',
  lessThanOrEqual: '<=',
  contains: '~',
}

const TECHNICAL_EXAMPLES = {
  equals: '"Name" = "Server-01"',
  notEquals: '"Status" != "Retired"',
  greaterThan: '"Priority" > 3',
  lessThan: '"Priority" < 3',
  greaterThanOrEqual: '"Priority" >= 3',
  lessThanOrEqual: '"Priority" <= 3',
  contains: '"Description" ~ "server"',
  boolean: 'objectType = "Server" AND ("Status" = "Active" OR "Status" = "Pending")',
  objectType: 'objectType = "Server"\nobjectType IN ("Server", "Laptop")\nobjectTypeAndChildren("Hardware")',
  reference: 'object HAVING outR(objectType = "Server") AND object HAVING inR(objectType = "Rack", refType IN ("Located In"))',
  orderBy: 'objectType = "Server" ORDER BY updated DESC',
}

// Static AQL syntax reference modal (HELP-02, Plan 196-01). 100% static
// content — no data hooks, no state, no interactivity beyond Close/backdrop
// (D-05/D-06). All copy is verbatim from the UI-SPEC Copywriting Contract.
export default function AqlSyntaxHelpModal({ onClose }) {
  const { t } = useTranslation()
  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50" onClick={onClose}>
      <div
        className="rounded-lg border bg-card p-4 w-full max-w-2xl shadow-xl max-h-[80vh] overflow-y-auto"
        onClick={(e) => e.stopPropagation()}
        data-testid="aql-syntax-help-modal"
      >
        <div className="flex items-center justify-between mb-3">
           <h2 className="text-lg font-semibold">{t('aql.syntax_reference', 'AQL Syntax Reference')}</h2>
           <Button type="button" variant="outline" size="sm" onClick={onClose}>{t('common.close', 'Close')}</Button>
        </div>

        <div className="space-y-4">
          <div className="space-y-1">
             <h3 className="text-base">{t('aql.comparison_operators', 'Comparison Operators')}</h3>
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b">
                   <th className="text-left py-1 pr-2">{t('aql.symbol', 'Symbol')}</th>
                   <th className="text-left py-1 pr-2">{t('aql.meaning', 'Meaning')}</th>
                   <th className="text-left py-1 pr-2">{t('aql.example', 'Example')}</th>
                </tr>
              </thead>
              <tbody>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.equals}</td>
                  <td className="py-1 pr-2">{t('aql.equals', 'Equals')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.equals}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.notEquals}</td>
                  <td className="py-1 pr-2">{t('aql.not_equals', 'Not equals')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.notEquals}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.greaterThan}</td>
                  <td className="py-1 pr-2">{t('aql.greater_than', 'Greater than')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.greaterThan}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.lessThan}</td>
                  <td className="py-1 pr-2">{t('aql.less_than', 'Less than')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.lessThan}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.greaterThanOrEqual}</td>
                  <td className="py-1 pr-2">{t('aql.greater_than_or_equal', 'Greater than or equal to')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.greaterThanOrEqual}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.lessThanOrEqual}</td>
                  <td className="py-1 pr-2">{t('aql.less_than_or_equal', 'Less than or equal to')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.lessThanOrEqual}</td>
                </tr>
                <tr className="border-b last:border-0">
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_OPERATORS.contains}</td>
                  <td className="py-1 pr-2">{t('aql.contains_text_match', 'Contains (text match)')}</td>
                  <td className="py-1 pr-2 font-mono">{TECHNICAL_EXAMPLES.contains}</td>
                </tr>
              </tbody>
            </table>
          </div>

          <div className="space-y-1">
            <h3 className="text-base">{t('aql.boolean_logic', 'Boolean Logic')}</h3>
            <p className="text-sm text-muted-foreground">
              {t('aql.boolean_logic_description', 'Combine conditions with AND, OR, and NOT. Use parentheses to control evaluation order.')}
            </p>
            <pre className="font-mono text-sm whitespace-pre-wrap bg-muted rounded-md p-2">{TECHNICAL_EXAMPLES.boolean}</pre>
          </div>

          <div className="space-y-1">
            <h3 className="text-base">{t('aql.object_type_filter', 'objectType Filter')}</h3>
            <p className="text-sm text-muted-foreground">
              {t('aql.object_type_filter_description', "Filter by ObjectType with =, match several with IN (...), or include an ObjectType's descendants with objectTypeAndChildren(...).")}
            </p>
            <pre className="font-mono text-sm whitespace-pre-wrap bg-muted rounded-md p-2">{TECHNICAL_EXAMPLES.objectType}</pre>
          </div>

          <div className="space-y-1">
            <h3 className="text-base">{t('aql.reference_traversal', 'Reference Traversal (HAVING)')}</h3>
            <p className="text-sm text-muted-foreground">
              {t('aql.reference_traversal_description', 'Find objects that reference — or are referenced by — other objects matching a nested condition, using object HAVING inR(...) or object HAVING outR(...). Optionally restrict which reference types are followed with refType IN (...).')}
            </p>
            <pre className="font-mono text-sm whitespace-pre-wrap bg-muted rounded-md p-2">{TECHNICAL_EXAMPLES.reference}</pre>
          </div>

          <div className="space-y-1">
            <h3 className="text-base">{t('aql.order_by', 'Order By')}</h3>
            <p className="text-sm text-muted-foreground">
              {t('aql.order_by_description', 'Sort results with ORDER BY, followed by name, created, or updated, and an optional ASC or DESC direction (default ASC).')}
            </p>
            <pre className="font-mono text-sm whitespace-pre-wrap bg-muted rounded-md p-2">{TECHNICAL_EXAMPLES.orderBy}</pre>
          </div>
        </div>
      </div>
    </div>
  )
}
