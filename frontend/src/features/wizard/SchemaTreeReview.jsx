import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import TreeNode from './TreeNode'

// Core-owned review of a schema draft (D-05). It is reused by the shared wizard
// for an accepted plugin draft: for a plugin draft there is no `schema_name`
// yet — the Core-owned name input lives outside this component — so the header
// falls back to the `Schema draft` identity. `provenance` and `warnings` are
// inert text/strings only; no plugin node, callback, model or stylesheet is ever
// retained in Core session state (D-08). Visible plugin attribution was removed
// per the approved 306-UI-SPEC (user decision 2026-10-10); the inert provenance
// is retained internally as a non-visible diagnostic attribute only.
export default function SchemaTreeReview({
    schemaData,
    toggledIds,
    onToggle,
    expandedIds,
    onExpand,
    warnings = [],
    provenance = null,
}) {
    const { t } = useTranslation()
    if (!schemaData) return null

    const objectTypes = schemaData.object_types || []

    return (
        <div className="rounded-lg border bg-card" data-provided-by={provenance?.owner ?? undefined}>
            <div className="flex items-center gap-2 px-4 py-3 border-b">
                <LucideIcon name="database" size={16} className="text-primary" />
                <span className="text-sm font-semibold text-foreground break-words min-w-0">
                    {schemaData.schema_name || t('plugins.seams.wizard.draft_identity', 'Schema draft')}
                </span>
                <span className="text-xs text-muted-foreground ml-auto shrink-0">
                    {(() => {
                        const count = objectTypes.length
                        const defaultText = count === 1
                            ? t('wizard.tree.object_types_one', '{{count}} ObjectTypes')
                            : t('wizard.tree.object_types_other', '{{count}} ObjectTypes')
                        return interpolateDefault(defaultText, { count })
                    })()}
                </span>
            </div>

            {Array.isArray(warnings) && warnings.length > 0 && (
                <div
                    className="mx-4 mt-3 rounded-md bg-amber-50 dark:bg-amber-950 p-3 border border-amber-200 dark:border-amber-800"
                    data-testid="plugin-draft-warnings"
                >
                    <p className="text-xs font-medium text-amber-800 dark:text-amber-200 mb-2">
                        {t('common.warnings', 'Warnings')}
                    </p>
                    <ul className="list-disc list-inside text-xs text-amber-700 dark:text-amber-300 space-y-0.5">
                        {warnings.map((warning, i) => <li key={i}>{warning}</li>)}
                    </ul>
                </div>
            )}

            <div className="divide-y max-h-[50vh] overflow-y-auto">
                {objectTypes.map((ot, i) => (
                    <TreeNode
                        key={i}
                        node={ot}
                        depth={0}
                        isExpanded={expandedIds.has(ot.name)}
                        isToggled={toggledIds.has(ot.name)}
                        onToggle={onToggle}
                        onExpand={onExpand}
                        isCheckable={true}
                    />
                ))}
            </div>
        </div>
    )
}
