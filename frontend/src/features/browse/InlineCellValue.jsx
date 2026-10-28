import { LucideIcon } from '../../shared/lib/lucide-icons'
import { StatusIndicator } from './StatusIndicator'
import { EntityValueCell } from './EntityValueCell'
import { truncate, getOptionLabel, findOptionByValue } from '../../shared/lib/string-utils'
import { isValidUrl, isValidEmail } from '../../shared/lib/link-utils'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import ReferenceChip from '../../shared/components/ReferenceChip'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'
import { renderMarkdownToSafeHtml, sanitizeHtml } from '../../shared/lib/sanitize'

const stopPropagation = (e) => e.stopPropagation()

function ReferenceTypeBadge({ referenceType }) {
  if (!referenceType) return null
  const hex = (referenceType.color || '#6B7280').replace('#', '')
  const r = parseInt(hex.substring(0, 2), 16) / 255
  const g = parseInt(hex.substring(2, 4), 16) / 255
  const b = parseInt(hex.substring(4, 6), 16) / 255
  const luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
  const textColor = luminance < 0.4 ? '#FFFFFF' : '#1F2937'
  return (
    <span
      className="inline-flex items-center rounded px-1.5 py-0.5 text-xs font-medium"
      style={{ backgroundColor: referenceType.color || '#6B7280', color: textColor }}
    >
      {referenceType.reference}
    </span>
  )
}

export default function InlineCellValue({ value, attrType, attribute, allValues, allRefs }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  // stopPropagation only for links — prevents editor from opening when user clicks a URL/email.
  // All other elements let the click bubble to the <td> which opens the editor.
  const stopForLink = (e) => e.stopPropagation()

  if (attrType === 'reference') {
    if (!allRefs || allRefs.length === 0) {
      return <span className="text-muted-foreground/50">{t('common.empty_value', '—')}</span>
    }

    const MAX_VISIBLE = 5
    const visible = allRefs.slice(0, MAX_VISIBLE)
    const overflow = allRefs.length - MAX_VISIBLE

    return (
      <div className="flex flex-wrap gap-2 items-center">
        <ReferenceTypeBadge referenceType={attribute?.reference_type} />
        {visible.map(ref => (
          <ReferenceChip
            key={ref.id}
            to={`/browse/objects/${ref.to_object_id}`}
            label={ref.to_object_name || ref.to_object_id}
            onClick={stopPropagation}
          />
        ))}
        {overflow > 0 && (
          <span className="inline-flex items-center bg-muted rounded px-2 py-0 text-xs text-muted-foreground font-medium">
            {selectPluralKey('browse.values.more_count', overflow) === 'browse.values.more_count_one'
              ? interpolateDefault(t('browse.values.more_count_one', '+{{count}} more'), { count: overflow })
              : interpolateDefault(t('browse.values.more_count_other', '+{{count}} more'), { count: overflow })}
          </span>
        )}
      </div>
    )
  }

  if (value === null || value === undefined || value === '') {
    return <span className="text-muted-foreground/50">{t('common.empty_value', '—')}</span>
  }

  if (attrType === 'user' || attrType === 'group') {
    return <EntityValueCell kind={attrType} raw={value} />
  }

  if (attrType === 'url') {
    const str = String(value)
    if (isValidUrl(str)) {
      return (
        <a
          href={str}
          target="_blank"
          rel="noopener noreferrer"
          onClick={stopForLink}
          title={str}
          className="text-primary hover:underline inline-flex items-center gap-1"
        >
          {truncate(str, 30)} <LucideIcon name="external-link" size={12} />
        </a>
      )
    }
    return <span title={str}>{truncate(str, 40)}</span>
  }

  if (attrType === 'email') {
    const str = String(value)
    if (isValidEmail(str)) {
      return (
        <a
          href={`mailto:${str}`}
          onClick={stopForLink}
          title={str}
          className="text-primary hover:underline inline-flex items-center gap-1"
        >
          {truncate(str, 40)} <LucideIcon name="mail" size={12} />
        </a>
      )
    }
    return <span title={str}>{truncate(str, 40)}</span>
  }

  if (attrType === 'select' && allValues && allValues.length > 0 && ((mc) => mc < 0 || mc > 1)(attribute?.maximum_cardinality ?? 1)) {
    const selectOptions = attribute?.structure?.options || []

    const MAX_VISIBLE = 5
    const visible = allValues.slice(0, MAX_VISIBLE)
    const overflow = allValues.length - MAX_VISIBLE

    return (
      <div className="flex flex-wrap gap-1 items-center">
        {visible.map((v) => {
          const rawVal = v.value_float ?? v.value_integer ?? v.value_boolean ?? v.value_datetime ?? v.value_simple ?? null
          const opt = rawVal !== null && rawVal !== undefined ? findOptionByValue(selectOptions, rawVal) : null
          return (
            <span key={v.id} className={`inline-flex items-center gap-1 rounded px-2 py-0 text-xs font-semibold ${opt ? 'bg-muted' : 'bg-muted/50 text-muted-foreground'}`}>
              {opt ? getOptionLabel(opt) : (rawVal !== null && rawVal !== undefined ? String(rawVal) : t('common.empty_value', '—'))}
            </span>
          )
        })}
        {overflow > 0 && (
          <span className="inline-flex items-center bg-muted rounded px-2 py-0 text-xs text-muted-foreground font-medium">
            {selectPluralKey('browse.values.more_count', overflow) === 'browse.values.more_count_one'
              ? interpolateDefault(t('browse.values.more_count_one', '+{{count}} more'), { count: overflow })
              : interpolateDefault(t('browse.values.more_count_other', '+{{count}} more'), { count: overflow })}
          </span>
        )}
      </div>
    )
  }

  if (attrType === 'select') {
    const options = attribute?.structure?.options || []
    const match = findOptionByValue(options, value)
    if (match) {
      return (
        <span className="bg-muted rounded px-1.5 py-0.5 text-xs font-medium">
          {getOptionLabel(match)}
        </span>
      )
    }
    return <span title={String(value)}>{truncate(String(value), 30)}</span>
  }

  if (attrType === 'boolean') {
    if (value === true) {
      return <span className="text-green-600 dark:text-green-400 text-sm font-bold">✓</span>
    }
    return <span className="text-muted-foreground text-sm font-bold">✗</span>
  }

  if (attrType === 'datetime') {
    const d = new Date(value)
    if (!isNaN(d.getTime())) {
      return <span title={String(value)}>{formatDate(value)}</span>
    }
    return <span title={String(value)}>{truncate(String(value), 40)}</span>
  }

  if (attrType === 'status') {
    try {
      const parsed = typeof value === 'object' ? value : JSON.parse(value)
      if (parsed && typeof parsed === 'object' && parsed.name) {
        return <StatusIndicator value={parsed} />
      }
    } catch {}
    return <span title={String(value)}>{truncate(String(value), 40)}</span>
  }

  if (attrType === 'integer' || attrType === 'double') {
    return <span title={String(value)}>{String(value)}</span>
  }

  if (attrType === 'textarea') {
    const format = attribute?.structure?.format || 'simpletext'
    const wrapperClassName = 'max-h-[7.5em] overflow-y-auto text-sm'

    if (format === 'html') {
      return <div className={wrapperClassName} dangerouslySetInnerHTML={{ __html: sanitizeHtml(String(value)) }} />
    }

    if (format === 'markdown') {
      return <div className={wrapperClassName} dangerouslySetInnerHTML={{ __html: renderMarkdownToSafeHtml(String(value)) }} />
    }

    return <div className={wrapperClassName}><span className="whitespace-pre-wrap">{String(value)}</span></div>
  }

  const str = String(value)
  return <span title={str.length > 40 ? str : undefined}>{truncate(str, 40)}</span>
}
