import { LucideIcon } from '../../shared/lib/lucide-icons'
import { StatusIndicator } from './StatusIndicator'
import { EntityValueCell } from './EntityValueCell'
import { decodeHtml, stripHtml } from '../../shared/lib/value-utils'
import { getOptionLabel, findOptionByValue } from '../../shared/lib/string-utils'
import { isValidUrl, isValidEmail } from '../../shared/lib/link-utils'
import { useFormatDate } from '../../shared/hooks/use-format-date'
import { useTranslation } from 'react-i18next'
import { renderMarkdownToSafeHtml, sanitizeHtml } from '../../shared/lib/sanitize'

export default function TypedValue({ rawValue, attrType, attribute }) {
  const { t } = useTranslation()
  const formatDate = useFormatDate()
  const raw = rawValue
  if (raw && typeof raw === 'object' && raw.id && raw.name) {
    return <StatusIndicator value={raw} />
  }
  if ((attrType === 'user' || attrType === 'group') && raw) {
    return <EntityValueCell kind={attrType} raw={raw} />
  }
  if (attrType === 'textarea' && raw) {
    const format = attribute?.structure?.format || 'simpletext'
    const wrapperClassName = 'max-h-[7.5em] overflow-y-auto text-sm'

    if (format === 'html') {
      return <div className={wrapperClassName} dangerouslySetInnerHTML={{ __html: sanitizeHtml(String(raw)) }} />
    }

    if (format === 'markdown') {
      return <div className={wrapperClassName} dangerouslySetInnerHTML={{ __html: renderMarkdownToSafeHtml(String(raw)) }} />
    }

    return <div className={wrapperClassName}><span className="whitespace-pre-wrap">{String(raw)}</span></div>
  }
  if (raw && typeof raw === 'string') {
    const decoded = raw.includes('&quot;') || raw.includes('&lt;')
      ? decodeHtml(raw)
      : raw

    if (attrType === 'url' && decoded) {
      if (isValidUrl(decoded)) {
        return (
          <a href={decoded} target="_blank" rel="noopener noreferrer"
            className="text-primary hover:underline inline-flex items-center gap-1">
            {decoded} <LucideIcon name="external-link" size={14} className="text-muted-foreground" />
          </a>
        )
      }
      return <span className="text-sm">{decoded}</span>
    }

    if (attrType === 'email' && decoded) {
      if (isValidEmail(decoded)) {
        return (
          <a href={`mailto:${decoded}`}
            className="text-primary hover:underline inline-flex items-center gap-1">
            {decoded} <LucideIcon name="mail" size={14} className="text-muted-foreground" />
          </a>
        )
      }
      return <span className="text-sm">{decoded}</span>
    }

    if (attrType === 'select' && attribute?.structure?.options && decoded) {
      const options = attribute.structure.options
      const match = findOptionByValue(options, decoded)
      if (match) {
        return <span className="bg-muted rounded px-2 py-0.5 text-xs font-medium">{getOptionLabel(match)}</span>
      }
      return <span className="text-sm">{decoded}</span>
    }

    if (decoded.startsWith('{')) {
      try {
        const parsed = JSON.parse(decoded)
        if (parsed.id && parsed.name) {
          return <StatusIndicator value={parsed} />
        }
      } catch {}
    }

    if (/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(decoded)) {
      const d = new Date(decoded)
      if (!isNaN(d.getTime())) {
        return <span className="text-sm">{formatDate(decoded)}</span>
      }
    }

    const stripped = stripHtml(decoded)
    if (stripped !== decoded && stripped) {
      return <span className="text-sm">{stripped}</span>
    }
  }

  return raw ?? t('common.empty_value', '-')
}
