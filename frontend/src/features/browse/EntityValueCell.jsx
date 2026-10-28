import { useTranslation } from 'react-i18next'
import { useUserPicker } from '../../shared/hooks/use-users'
import { useGroups } from '../../shared/hooks/use-groups'

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

// USER/GROUP read-back cell (245-05 D-03/D-07): resolves the stored UUID to a
// display name via the same ids-filter read path the pickers use
// (useUserPicker for users, useGroups for groups) — never fabricated. An
// unresolvable UUID or a legacy non-UUID value (D-07) renders the raw stored
// string verbatim, no badge/icon. Shared by TypedValue.jsx (ObjectDetail page)
// and InlineCellValue.jsx (the live ObjectDetailPanel browse-grid side panel)
// so both read-back surfaces resolve names identically.
export function EntityValueCell({ kind, raw }) {
  const { t } = useTranslation()
  const isUser = kind === 'user'
  const isUuid = typeof raw === 'string' && UUID_RE.test(raw)
  const ids = isUuid ? [raw] : undefined

  const userQuery = useUserPicker(undefined, isUser ? ids : undefined, 0, 1, { enabled: isUser && isUuid })
  const groupQuery = useGroups(undefined, !isUser ? ids : undefined, 0, 1, { enabled: !isUser && isUuid })
  const query = isUser ? userQuery : groupQuery

  const resolved = query.data?.items?.[0]
  const label = resolved ? (isUser ? resolved.username : resolved.name) : raw

  return (
    <span className="text-sm" data-testid={isUser ? 'user-value-cell' : 'group-value-cell'}>
      {label ?? t('common.empty_value', '-')}
    </span>
  )
}

