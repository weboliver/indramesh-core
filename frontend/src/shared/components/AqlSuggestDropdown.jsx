import { useRef, useEffect } from 'react'
import { cn } from '../lib/utils'
import { getCaretCoordinates } from '../lib/textarea-caret-coords'
import Braces from 'lucide-react/dist/esm/icons/braces'
import GitBranch from 'lucide-react/dist/esm/icons/git-branch'
import Type from 'lucide-react/dist/esm/icons/type'
import Link from 'lucide-react/dist/esm/icons/link'
import Equal from 'lucide-react/dist/esm/icons/equal'
import Tag from 'lucide-react/dist/esm/icons/tag'
import HelpCircle from 'lucide-react/dist/esm/icons/help-circle'
import { useTranslation } from 'react-i18next'
import { PHASE231_AQL_KIND_LABELS } from '../lib/phase231-dynamic-contract'

// UI-SPEC Copywriting Contract — exact strings, sentence case, singular.
// One distinct lucide icon per kind (planner discretion, per-icon ESM imports).
const KIND_ICON = {
  'syntax-keyword': Braces,
  'boolean-keyword': GitBranch,
  field: Type,
  'ref-fn': Link,
  operator: Equal,
  'objectType-value': Tag,
}

const panelChrome =
  'rounded-md border bg-card text-card-foreground shadow-lg z-50 overflow-hidden max-h-96 overflow-y-auto'

const rowChrome = 'flex w-full items-center gap-2 px-4 py-2 h-9 text-sm cursor-pointer'

// Presentational, caret-anchored suggestion dropdown (Plan 03). Deliberately
// dumb: no fetch state, no keyboard state — items/activeIndex/cursorPos and all
// keyboard handling live in SearchPage (Plan 04) so Enter-gating (AC-08)
// resolves synchronously off committed state (RESEARCH Pitfall 3).
export default function AqlSuggestDropdown({
  textareaRef,
  cursorPos,
  items,
  activeIndex,
  onAccept,
  onHover,
  onOpenHelp,
}) {
  const { t } = useTranslation()
  // Per-row DOM refs for D-11: keep the keyboard-highlighted row visible
  // within the max-h-96 scroll area when activeIndex changes.
  // Hoisted above the empty-return so the hook call count is stable across
  // renders (Rules of Hooks) — the guard makes it a no-op when no panel shows.
  const rowRefs = useRef([])
  useEffect(() => {
    if (activeIndex >= 0 && rowRefs.current[activeIndex]) {
      rowRefs.current[activeIndex].scrollIntoView({ block: 'nearest' })
    }
  }, [activeIndex])

  if (!items || items.length === 0) {
    return null
  }

  // Recompute on every render — never memo/cache (Pitfall 4: resize-y textarea
  // + window reflow both invalidate cached coordinates).
  const coords = textareaRef.current
    ? getCaretCoordinates(textareaRef.current, cursorPos)
    : { top: 0, left: 0, height: 0 }

  return (
    <div
      data-testid="aql-suggest-dropdown"
      role="listbox"
      className={cn(panelChrome)}
      style={{
        position: 'absolute',
        top: coords.top + coords.height,
        left: coords.left,
      }}
    >
      {items.map((item, index) => {
        const active = index === activeIndex
        const Icon = KIND_ICON[item.kind] || Type
        return (
          <div
            key={`${item.kind}-${item.value}-${index}`}
            ref={(el) => {
              rowRefs.current[index] = el
            }}
            data-testid="aql-suggest-row"
            role="option"
            aria-selected={active}
            onMouseDown={(e) => {
              // preventDefault on mousedown (fires BEFORE the textarea blur)
              // keeps the textarea focused so the click is not lost to
              // blur-before-click (Pitfall 5 / D-14) — accept runs here, in
              // the mousedown handler, not in a click handler.
              e.preventDefault()
              onAccept(item, index)
            }}
            onMouseEnter={() => onHover(index)}
            className={cn(rowChrome, active && 'bg-accent text-accent-foreground')}
          >
            <Icon size={14} className={active ? 'text-primary' : 'text-muted-foreground'} />
            {item.value}
            <span className="ml-auto text-xs text-muted-foreground">
               {PHASE231_AQL_KIND_LABELS[item.kind]
                 ? t(PHASE231_AQL_KIND_LABELS[item.kind].key, PHASE231_AQL_KIND_LABELS[item.kind].defaultValue)
                : item.kind}
            </span>
          </div>
        )
      })}
      {/* Sibling element after {items.map(...)}, NOT an items[] entry — keeps
          it out of Up/Down wraparound and Enter-gating (AC-08), per D-07. */}
      <div
        data-testid="aql-suggest-help-row"
        onMouseDown={(e) => {
          // Same preventDefault-before-blur pattern as existing rows — keeps
          // focus on the textarea so the click isn't lost to blur-before-click.
          e.preventDefault()
          onOpenHelp()
        }}
        className={cn(rowChrome, 'border-t border-border text-muted-foreground')}
      >
        <HelpCircle size={14} className="text-primary" />
         {t('aql.syntax_help', 'AQL syntax help')}
      </div>
    </div>
  )
}
