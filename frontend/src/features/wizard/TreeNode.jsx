import { LucideIcon } from '../../shared/lib/lucide-icons'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import AttributeTypeBadge from './AttributeTypeBadge'

export default function TreeNode({ node, depth, isExpanded, isToggled, onToggle, onExpand, isCheckable }) {
    const { t } = useTranslation()
    const isObjectType = depth === 0

    return (
        <div>
            <div
                className={`flex items-center gap-2 py-1.5 min-h-[44px] px-3 ${isObjectType && !isToggled ? 'opacity-50' : ''}`}
                style={{ paddingLeft: `${depth * 20 + 12}px` }}
                role="treeitem"
                aria-expanded={isObjectType ? isExpanded : undefined}
                aria-checked={isCheckable ? isToggled : undefined}
                tabIndex={0}
            >
                {isObjectType && (
                    <button
                        type="button"
                        onClick={() => onExpand(node.name)}
                        className="p-1 text-muted-foreground hover:text-primary"
                         aria-label={isExpanded ? t('wizard.tree.collapse', 'Collapse') : t('wizard.tree.expand', 'Expand')}
                    >
                        <LucideIcon name={isExpanded ? 'chevron-down' : 'chevron-right'} size={16} />
                    </button>
                )}

                {isCheckable && (
                    <input
                        type="checkbox"
                        checked={isToggled}
                        onChange={() => onToggle(node.name)}
                        className="accent-primary"
                         aria-label={interpolateDefault(t('wizard.tree.include', 'Include {{name}}'), { name: node.name })}
                    />
                )}

                {isObjectType && (
                    <LucideIcon name={node.icon_key || 'box'} size={16} className="text-muted-foreground flex-shrink-0" />
                )}

                <span className={`text-sm ${isObjectType ? 'font-medium text-foreground' : 'text-foreground'} ${isObjectType && !isToggled ? 'line-through' : ''}`}>
                    {node.name}
                </span>

                {!isObjectType && node.type_string && (
                    <AttributeTypeBadge
                        typeString={node.type_string}
                        referencedName={node.referenced_object_type_name}
                    />
                )}

                {!isObjectType && node.is_required && (
                    <span className="text-destructive text-xs">*</span>
                )}

                {isObjectType && !isExpanded && (
                    <span className="text-xs text-muted-foreground ml-1">
                        {interpolateDefault(t('wizard.tree.attribute_count', '({{count}})'), {
                            count: node.attributes?.length || 0,
                        })}
                    </span>
                )}
            </div>

            {isObjectType && isExpanded && node.attributes?.map((attr, i) => (
                <TreeNode
                    key={i}
                    node={attr}
                    depth={1}
                    isExpanded={false}
                    isToggled={false}
                    onToggle={() => {}}
                    onExpand={() => {}}
                    isCheckable={false}
                />
            ))}
        </div>
    )
}
