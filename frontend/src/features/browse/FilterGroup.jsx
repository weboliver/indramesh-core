import FilterConditionRow from './FilterConditionRow'
import { useTranslation } from 'react-i18next'

export default function FilterGroup({
  group,
  attributes,
  attributeTypes,
  onChange,
  onRemove,
  onClearAll,
  nestingLevel = 0,
}) {
  const { t } = useTranslation()
  const handleToggleOp = (newOp) => {
    onChange({
      ...group,
      op: newOp,
    })
  }

  const handleConditionChange = (index, updatedCondition) => {
    const newConditions = [...(group.conditions || [])]
    newConditions[index] = updatedCondition
    onChange({
      ...group,
      conditions: newConditions,
    })
  }

  const handleConditionRemove = (index) => {
    const newConditions = [...(group.conditions || [])].filter((_, i) => i !== index)
    onChange({
      ...group,
      conditions: newConditions,
    })
  }

  const handleGroupChange = (index, updatedGroup) => {
    const newGroups = [...(group.groups || [])]
    newGroups[index] = updatedGroup
    onChange({
      ...group,
      groups: newGroups,
    })
  }

  const handleGroupRemove = (index) => {
    const newGroups = [...(group.groups || [])].filter((_, i) => i !== index)
    onChange({
      ...group,
      groups: newGroups,
    })
  }

  const handleAddCondition = () => {
    const newConditions = [...(group.conditions || []), { attr_id: '', op: 'eq', val: '' }]
    onChange({
      ...group,
      conditions: newConditions,
    })
  }

  const handleAddGroup = () => {
    const newGroups = [...(group.groups || []), { op: 'AND', conditions: [], groups: [] }]
    onChange({
      ...group,
      groups: newGroups,
    })
  }

  return (
    <div className="space-y-1" style={{ marginLeft: nestingLevel > 0 ? nestingLevel * 16 : 0 }}>
      {/* AND/OR toggle pills + delete group button + clear all (root only) */}
      <div className="flex items-center justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className="flex items-center gap-1">
            <button
              type="button"
              onClick={() => handleToggleOp('AND')}
              className={`text-xs px-2 py-1 rounded-full font-medium transition-colors ${
                group.op === 'AND'
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-muted-foreground hover:bg-muted/80'
              }`}
            >
              {t('browse.filters.and', 'AND')}
            </button>
            <button
              type="button"
              onClick={() => handleToggleOp('OR')}
              className={`text-xs px-2 py-1 rounded-full font-medium transition-colors ${
                group.op === 'OR'
                  ? 'bg-primary text-primary-foreground'
                  : 'bg-muted text-muted-foreground hover:bg-muted/80'
              }`}
            >
              {t('browse.filters.or', 'OR')}
            </button>
          </div>
          {onRemove && (
            <button
              type="button"
              onClick={onRemove}
              className="text-xs text-destructive hover:text-destructive/80 transition-colors font-medium"
            >
              {t('browse.filters.remove_group', 'Remove group')}
            </button>
          )}
        </div>
        {onClearAll && (
          <button
            type="button"
            onClick={onClearAll}
            className="text-xs text-muted-foreground hover:text-foreground transition-colors font-medium"
          >
            {t('browse.filters.clear_all', 'Clear all')}
          </button>
        )}
      </div>

      {/* Conditions and nested groups with left border */}
      <div className="border-l-2 border-border pl-2 space-y-1">
        {(group.conditions || []).map((cond, i) => (
          <FilterConditionRow
            key={i}
            condition={cond}
            nestingLevel={0}
            onChange={(updated) => handleConditionChange(i, updated)}
            onRemove={() => handleConditionRemove(i)}
            attributes={attributes}
            attributeTypes={attributeTypes}
          />
        ))}

        {(group.groups || []).map((childGroup, i) => (
          <FilterGroup
            key={i}
            group={childGroup}
            nestingLevel={nestingLevel + 1}
            onChange={(updated) => handleGroupChange(i, updated)}
            onRemove={() => handleGroupRemove(i)}
            attributes={attributes}
            attributeTypes={attributeTypes}
          />
        ))}

        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={handleAddCondition}
            className="text-xs text-muted-foreground hover:text-foreground py-1 transition-colors"
          >
            {t('browse.filters.add_condition', '+ Add condition')}
          </button>
          {nestingLevel < 1 && (
            <button
              type="button"
              onClick={handleAddGroup}
              className="text-xs text-muted-foreground hover:text-foreground py-1 transition-colors"
            >
              {t('browse.filters.add_group', '+ Add filter group')}
            </button>
          )}
        </div>
      </div>
    </div>
  )
}
