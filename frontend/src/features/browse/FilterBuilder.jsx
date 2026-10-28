import { Button } from '../../shared/components/Button'
import FilterGroup from './FilterGroup'
import { useTranslation } from 'react-i18next'

export default function FilterBuilder({
  filterTree,
  setFilterTree,
  attributes,
  attributeTypes,
}) {
  const { t } = useTranslation()
  const hasFilters = filterTree && ((filterTree.conditions && filterTree.conditions.length > 0) || (filterTree.groups && filterTree.groups.length > 0))

  const handleTreeChange = (updatedGroup) => {
    const hasConditions = updatedGroup.conditions && updatedGroup.conditions.length > 0
    const hasGroups = updatedGroup.groups && updatedGroup.groups.length > 0
    if (!hasConditions && !hasGroups) {
      setFilterTree(null)
    } else {
      setFilterTree(updatedGroup)
    }
  }

  if (!hasFilters) {
    return (
      <Button
        type="button"
        variant="outline"
        size="sm"
        leftIcon="filter"
        onClick={() =>
          setFilterTree({
            op: 'AND',
            conditions: [{ attr_id: '', op: 'eq', val: '' }],
            groups: [],
          })
        }
      >
        {t('browse.filters.add_filter', '+ Add filter')}
      </Button>
    )
  }

  return (
    <div className="w-full border rounded-lg bg-card p-2 space-y-1">
      <FilterGroup
        group={filterTree}
        attributes={attributes}
        attributeTypes={attributeTypes}
        onChange={handleTreeChange}
        onRemove={null}
        onClearAll={() => setFilterTree(null)}
        nestingLevel={0}
      />
    </div>
  )
}
