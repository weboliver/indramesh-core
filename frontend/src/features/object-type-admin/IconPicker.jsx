import { useState, useMemo } from 'react'
import { LUCIDE_ICON_NAMES, LucideIcon } from '../../shared/lib/lucide-icons'
import { cn } from '../../shared/lib/utils'
import { useTranslation } from 'react-i18next'
import { interpolateDefault } from '../../shared/lib/i18n-defaults'
import { selectPluralKey } from '../../shared/lib/i18n'

// Icon grid cells intentionally NOT migrated to <Button> — they are custom
// interactive cells (icon + label + selected-state ring), not buttons per D-04.

const MAX_VISIBLE_ICONS = 120

export default function IconPicker({ value, onChange, className }) {
  const { t } = useTranslation()
  const [search, setSearch] = useState('')

  const filtered = useMemo(() => {
    if (!search) return LUCIDE_ICON_NAMES
    const q = search.toLowerCase()
    return LUCIDE_ICON_NAMES.filter(name => name.includes(q))
  }, [search])

  const visible = useMemo(() => filtered.slice(0, MAX_VISIBLE_ICONS), [filtered])

  return (
    <div className={cn('space-y-3', className)}>
      <input
        type="text"
        placeholder={t('admin.icons.search_placeholder', 'Search icons...')}
        value={search}
        onChange={e => setSearch(e.target.value)}
        className="w-full rounded-md border border-input bg-background px-3 py-2 text-sm focus:outline-none focus:ring-1 focus:ring-primary"
      />
      {filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-8">
          {interpolateDefault(t('admin.icons.no_icons_found', 'No icons found for "{{query}}"'), { query: search })}
        </p>
      ) : (
        <>
          <div className="max-h-80 overflow-y-auto grid grid-cols-[repeat(auto-fill,minmax(80px,1fr))] gap-1">
            {visible.map(name => (
              <button
                key={name}
                type="button"
                onClick={() => onChange(name)}
                className={cn(
                  'flex flex-col items-center gap-1 p-2 rounded-md cursor-pointer hover:bg-accent hover:text-accent-foreground transition-colors',
                  value === name && 'ring-2 ring-primary ring-offset-2'
                )}
              >
                <LucideIcon name={name} size={24} />
                <span className="text-xs text-muted-foreground truncate max-w-full">
                  {name}
                </span>
              </button>
            ))}
          </div>
          {filtered.length > MAX_VISIBLE_ICONS && (
            <p className="text-xs text-muted-foreground text-center">
              {selectPluralKey('admin.icons.showing_count', filtered.length) === 'admin.icons.showing_count_one'
                 ? interpolateDefault(t('admin.icons.showing_count_one', 'Showing {{count}} of {{total}} icon — refine your search to find more'), { count: MAX_VISIBLE_ICONS, total: filtered.length })
                 : interpolateDefault(t('admin.icons.showing_count_other', 'Showing {{count}} of {{total}} icons — refine your search to find more'), { count: MAX_VISIBLE_ICONS, total: filtered.length })}
            </p>
          )}
        </>
      )}
    </div>
  )
}
