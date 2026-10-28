import { LucideIcon } from '../../shared/lib/lucide-icons'
import { getOptionValue, getOptionLabel } from '../../shared/lib/string-utils'
import { useTranslation } from 'react-i18next'
import { PHASE231_FILTER_OPERATOR_LABELS } from '../../shared/lib/phase231-dynamic-contract'

const OPERATOR_MAP = {
  integer: ['eq', 'lt', 'lte', 'gt', 'gte', 'between'],
  double: ['eq', 'lt', 'lte', 'gt', 'gte', 'between'],
  datetime: ['eq', 'after', 'before', 'between'],
  boolean: ['eq'],
  text: ['contains', 'eq', 'starts_with'],
  select: ['eq'],
  status: ['eq'],
  reference: ['contains', 'eq', 'starts_with'],
}

const SYSTEM_ATTR_NAMES = new Set(['Created', 'Updated', 'Key'])

function getAttrDataType(attr, attributeTypes) {
  if (!attr || !attr.attribute_type_id) return 'text'
  const at = attributeTypes.find(t => t.id === attr.attribute_type_id)
  return at?.data_type || 'text'
}

export default function FilterConditionRow({
  condition,
  attributes,
  attributeTypes,
  onChange,
  onRemove,
}) {
  const { t } = useTranslation()
  const selectedAttr = attributes.find(a => a.id === condition.attr_id)
  const dataType = selectedAttr ? getAttrDataType(selectedAttr, attributeTypes) : 'text'
  const validOps = OPERATOR_MAP[dataType] || ['eq']
  const isBetween = condition.op === 'between'
  const isDate = dataType === 'datetime'
  const valueInputType = isDate ? 'date' : 'text'
  const isSelect = dataType === 'select'
  const isStatus = dataType === 'status'
  const selectOptions = selectedAttr?.structure?.options || []
  const statusOptions = selectedAttr?.structure?.status_options || []

  // Status values are persisted as a JSON blob ({id, name}) — see buildValuePayload's
  // 'status' case in ObjectTypeObjects.jsx — so the filter value must match that exact
  // shape rather than a plain id, and the dropdown's selection is derived by parsing it back.
  const currentStatusId = (() => {
    if (!condition.val) return ''
    try {
      const parsed = JSON.parse(condition.val)
      return parsed?.id !== null && parsed?.id !== undefined ? String(parsed.id) : ''
    } catch {
      return ''
    }
  })()

  const handleAttrChange = (e) => {
    const newAttrId = e.target.value
    const newAttr = attributes.find(a => a.id === newAttrId)
    const newDataType = newAttr ? getAttrDataType(newAttr, attributeTypes) : 'text'
    const validOpsForNewType = OPERATOR_MAP[newDataType] || ['eq']
    const newOp = validOpsForNewType[0]

    onChange({
      ...condition,
      attr_id: newAttrId,
      op: newOp,
      val: '',
      val2: '',
    })
  }

  const handleOpChange = (e) => {
    onChange({
      ...condition,
      op: e.target.value,
      val: '',
      val2: '',
    })
  }

  const handleValChange = (e) => {
    onChange({
      ...condition,
      val: e.target.value,
    })
  }

  const handleVal2Change = (e) => {
    onChange({
      ...condition,
      val2: e.target.value,
    })
  }

  const handleStatusChange = (e) => {
    const opt = statusOptions.find(o => String(o.id) === e.target.value)
    onChange({
      ...condition,
      val: opt ? JSON.stringify({ id: opt.id, name: opt.name }) : '',
    })
  }

  const handleToggleNegate = () => {
    onChange({
      ...condition,
      negate: !condition.negate,
    })
  }

  return (
    <div className="flex flex-wrap items-center gap-1 py-1">
      <button
        type="button"
        onClick={handleToggleNegate}
        aria-pressed={!!condition.negate}
        title={t('browse.filters.negate_condition', 'Negate this condition')}
        className={`text-xs px-2 py-1 rounded-full font-medium transition-colors ${
          condition.negate
            ? 'bg-destructive text-destructive-foreground'
            : 'bg-muted text-muted-foreground hover:bg-muted/80'
        }`}
      >
        {t('browse.filters.not', 'NOT')}
      </button>
      <select
        value={condition.attr_id || ''}
        onChange={handleAttrChange}
        className="rounded border border-input bg-background px-2 py-1 text-xs w-[150px]"
      >
        <option value="">{t('browse.filters.select_attribute', 'Select attribute...')}</option>
        {attributes
          .filter(a => !SYSTEM_ATTR_NAMES.has(a.name))
          .map(attr => (
            <option key={attr.id} value={attr.id}>
              {attr.name}
            </option>
          ))}
      </select>

      <select
        value={condition.op || 'eq'}
        onChange={handleOpChange}
        disabled={!condition.attr_id}
        className="rounded border border-input bg-background px-2 py-1 text-xs w-[100px] disabled:opacity-50 disabled:cursor-not-allowed"
      >
        {validOps.map(op => (
          <option key={op} value={op}>
            {(() => {
              const label = PHASE231_FILTER_OPERATOR_LABELS[op]
              return label ? t(label.key, label.defaultText) : op
            })()}
          </option>
        ))}
      </select>

      {isSelect ? (
        <select
          value={condition.val || ''}
          onChange={handleValChange}
          disabled={!condition.attr_id}
          className="rounded border border-input bg-background px-2 py-1 text-xs w-[130px] flex-grow disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <option value="">{t('browse.filters.select_value', 'Select value...')}</option>
          {selectOptions.map((opt, idx) => {
            const optValue = getOptionValue(opt, idx)
            return (
              <option key={opt.id ?? optValue ?? idx} value={optValue}>{getOptionLabel(opt)}</option>
            )
          })}
        </select>
      ) : isStatus ? (
        <select
          value={currentStatusId}
          onChange={handleStatusChange}
          disabled={!condition.attr_id}
          className="rounded border border-input bg-background px-2 py-1 text-xs w-[130px] flex-grow disabled:opacity-50 disabled:cursor-not-allowed"
        >
          <option value="">{t('browse.filters.select_status', 'Select status...')}</option>
          {statusOptions.map((opt) => (
            <option key={opt.id} value={opt.id}>{opt.name}</option>
          ))}
        </select>
      ) : isBetween ? (
        <>
          <input
            type={valueInputType}
            value={condition.val || ''}
            onChange={handleValChange}
            disabled={!condition.attr_id || !condition.op}
            placeholder={isDate ? undefined : t('browse.filters.minimum', 'Min')}
            className="rounded border border-input bg-background px-2 py-1 text-xs w-[100px] disabled:opacity-50 disabled:cursor-not-allowed"
          />
          <input
            type={valueInputType}
            value={condition.val2 || ''}
            onChange={handleVal2Change}
            disabled={!condition.attr_id || !condition.op}
            placeholder={isDate ? undefined : t('browse.filters.maximum', 'Max')}
            className="rounded border border-input bg-background px-2 py-1 text-xs w-[100px] disabled:opacity-50 disabled:cursor-not-allowed"
          />
        </>
      ) : (
        <input
          type={valueInputType}
          value={condition.val || ''}
          onChange={handleValChange}
          disabled={!condition.attr_id || !condition.op}
          placeholder={isDate ? undefined : t('browse.filters.value', 'Value')}
          className="rounded border border-input bg-background px-2 py-1 text-xs w-[130px] flex-grow disabled:opacity-50 disabled:cursor-not-allowed"
        />
      )}

      <button
        type="button"
        onClick={onRemove}
        className="text-destructive hover:text-destructive/80 transition-colors"
        aria-label={t('browse.filters.remove_condition', 'Remove condition')}
      >
        <LucideIcon name="x" size={14} />
      </button>
    </div>
  )
}
