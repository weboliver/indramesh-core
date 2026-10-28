import { useEffect, useRef, useState } from 'react'
import { useTranslation } from 'react-i18next'
import { Button } from './Button'
import { useUserPicker } from '../hooks/use-users'
import { useGroups } from '../hooks/use-groups'
import { interpolateDefault } from '../lib/i18n-defaults'

const PAGE_SIZE = 25
const DEBOUNCE_MS = 300

// Searchable USER/GROUP attribute-value picker (245-05 D-03/D-07): a single
// component shared by both kinds, mirroring ObjectReferenceSelector's
// search-input + bordered-dropdown UX. USER data comes from the content-gated
// GET /users/picker (useUserPicker) — never the admin-gated GET /users/ list
// — and GROUP data from GET /groups/ (useGroups); both hooks share the same
// ids-filter convention for resolved-name read-back.
export function EntityValuePicker({ kind, value, onChange, label, error, disabled = false }) {
  const { t } = useTranslation()
  const isUser = kind === 'user'
  const ns = isUser ? 'users' : 'groups'
  const testId = isUser ? 'user-picker' : 'group-picker'
  const rowTestId = isUser ? 'user-picker-row' : 'group-picker-row'

  const [query, setQuery] = useState('')
  const [debouncedQuery, setDebouncedQuery] = useState('')
  const [isOpen, setIsOpen] = useState(false)
  const [activeIndex, setActiveIndex] = useState(-1)
  const containerRef = useRef(null)

  useEffect(() => {
    const timer = setTimeout(() => setDebouncedQuery(query), DEBOUNCE_MS)
    return () => clearTimeout(timer)
  }, [query])

  // Search list — both hooks are always called (rules-of-hooks), only the one
  // matching `kind` is actually enabled/fetching.
  const userListQuery = useUserPicker(debouncedQuery, undefined, 0, PAGE_SIZE, { enabled: isUser && isOpen })
  const groupListQuery = useGroups(debouncedQuery, undefined, 0, PAGE_SIZE, { enabled: !isUser && isOpen })
  const listQuery = isUser ? userListQuery : groupListQuery

  // Resolved display name for the currently stored value (editing an existing
  // object) — same ids-filter read path the TypedValue read-back cell uses.
  const userResolveQuery = useUserPicker(undefined, value ? [value] : undefined, 0, 1, { enabled: isUser && !!value })
  const groupResolveQuery = useGroups(undefined, value ? [value] : undefined, 0, 1, { enabled: !isUser && !!value })
  const resolveQuery = isUser ? userResolveQuery : groupResolveQuery

  const items = listQuery.data?.items ?? []
  const isLoading = listQuery.isLoading
  const isError = listQuery.isError

  const resolvedItem = resolveQuery.data?.items?.[0]
  const selectedLabel = resolvedItem ? (isUser ? resolvedItem.username : resolvedItem.name) : value

  useEffect(() => {
    function handleClickOutside(e) {
      if (containerRef.current && !containerRef.current.contains(e.target)) {
        setIsOpen(false)
        setActiveIndex(-1)
      }
    }
    document.addEventListener('mousedown', handleClickOutside)
    return () => document.removeEventListener('mousedown', handleClickOutside)
  }, [])

  const getRowLabel = (item) => (isUser ? item.username : item.name)

  const selectItem = (item) => {
    onChange(item.id)
    setQuery('')
    setDebouncedQuery('')
    setIsOpen(false)
    setActiveIndex(-1)
  }

  const handleKeyDown = (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault()
      setIsOpen(true)
      setActiveIndex((i) => Math.min(i + 1, items.length - 1))
    } else if (e.key === 'ArrowUp') {
      e.preventDefault()
      setActiveIndex((i) => Math.max(i - 1, 0))
    } else if (e.key === 'Enter') {
      e.preventDefault()
      const item = items[activeIndex]
      if (item) selectItem(item)
    } else if (e.key === 'Escape') {
      setIsOpen(false)
      setActiveIndex(-1)
    }
  }

  const baseInputClass = `w-full rounded-md border px-3 py-2 text-sm bg-background ${
    error ? 'border-destructive' : 'border-input'
  } focus:outline-none focus:ring-1 focus:ring-primary`

  if (value) {
    return (
      <div
        ref={containerRef}
        data-testid={testId}
        className="flex items-center gap-2 rounded-md border bg-muted/30 px-3 py-2"
      >
        <span className="text-sm flex-1 truncate text-primary">
          {interpolateDefault(t(`browse.${ns}.selected`, 'Selected: {{value}}'), { value: selectedLabel ?? value })}
        </span>
        <Button
          type="button"
          variant="ghost"
          size="sm"
          disabled={disabled}
          onClick={() => onChange(null)}
          className="hover:text-destructive"
          aria-label={t(`browse.${ns}.clear`, 'Clear')}
        >
          {t(`browse.${ns}.clear`, 'Clear')}
        </Button>
      </div>
    )
  }

  return (
    <div ref={containerRef} className="relative">
      <input
        type="text"
        value={query}
        disabled={disabled}
        onChange={(e) => { setQuery(e.target.value); setIsOpen(true); setActiveIndex(-1) }}
        onFocus={() => setIsOpen(true)}
        onKeyDown={handleKeyDown}
        placeholder={t(`browse.${ns}.search`, isUser ? 'Search users...' : 'Search groups...')}
        className={baseInputClass}
        aria-label={label}
        data-testid={testId}
      />
      {isOpen && (
        <div className="rounded-md border bg-background shadow-lg max-h-48 overflow-y-auto absolute z-10 w-full">
          {isLoading ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">{t('browse.references.loading', 'Loading...')}</p>
          ) : isError ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">
              {t(`browse.${ns}.load_failed`, isUser
                ? 'Failed to load users. Check your connection and try again.'
                : 'Failed to load groups. Check your connection and try again.')}
            </p>
          ) : items.length === 0 ? (
            <p className="px-3 py-2 text-sm text-muted-foreground">
              {debouncedQuery
                ? t(`browse.${ns}.no_results`, 'No results')
                : t(`browse.${ns}.no_${ns}`, isUser ? 'No users available' : 'No groups available')}
            </p>
          ) : (
            items.map((item, idx) => (
              <div
                key={item.id}
                data-testid={rowTestId}
                onMouseDown={(e) => e.preventDefault()}
                onClick={() => selectItem(item)}
                className={`px-3 py-2 cursor-pointer hover:bg-accent ${idx === activeIndex ? 'bg-accent' : ''}`}
              >
                <div className="text-sm font-normal text-foreground">{getRowLabel(item)}</div>
                {isUser && item.email && (
                  <div className="text-xs text-muted-foreground">{item.email}</div>
                )}
              </div>
            ))
          )}
        </div>
      )}
    </div>
  )
}

