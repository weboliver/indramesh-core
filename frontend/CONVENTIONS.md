# Frontend Conventions

This file records standing UI and code conventions for the IndraMesh Core frontend
(`frontend/src/`). New pages and components follow these rules by default —
do not reintroduce ad-hoc variants of a shared pattern that already exists
here.

---

## UI Standards (Phase 252: table-header-search-breadcrumb-ui-standards)

Two app-wide rules are codified as shared, reusable components and applied
consistently:

1. **Every table renders with a consistent header** (translated field-name
   column headers) **and is always searchable**.
2. **Every page reached via Edit / Details / Manage offers breadcrumb
   navigation** back to the page it was reached from or its overview.

The shared primitives live in `frontend/src/shared/components/`:

| Component | Purpose |
|-----------|---------|
| [`Table`](src/shared/components/Table.jsx) | Thin semantic wrapper around a native `<table>`: owns the `rounded-lg border overflow-x-auto` scroll shell. It is **not** a data-grid — pages compose the `<thead>`/`<tbody>` themselves so specialist rows (expandable rows, drag headers, inline editors, sticky matrix cells, nested history layouts) keep working. |
| [`TableToolbar`](src/shared/components/TableToolbar.jsx) | Sits below `PageHeader`, above `Table`. Always renders the standard [`BrowseSearchInput`](src/features/browse/BrowseSearchInput.jsx) (the D-02 search baseline) inside `role="search"`. Optional `filters` / `columns` / `actions` slots compose beside search; a `searchControl` slot replaces the input with a mode adapter while keeping the toolbar mounted. |
| [`Breadcrumb`](src/shared/components/Breadcrumb.jsx) | Renders an explicit ordered `items` array of `{ label, href? }` as semantic `<nav aria-label="Breadcrumb"><ol>…</ol></nav>` with React Router `Link`s, decorative `aria-hidden` separators, and `aria-current="page"` on the final (non-linked) item. |

### Required composition order (list pages)

The vertical scan order is fixed at all viewport widths:

```
PageHeader                       ← title / description / icon + action buttons (unchanged)
TableToolbar                     ← role="search"; BrowseSearchInput always present
  ├─ filters (optional)          ← page-owned filter controls (never hide or replace search)
  ├─ BrowseSearchInput           ← the standard search slot (unconditional)
  ├─ columns (optional)          ← column-visibility control
  └─ actions (optional)          ← toolbar-level page actions
Table                            ← native <table> in the bordered scroll shell
  ├─ thead.sticky scope="col"    ← translated field-name headers incl. Actions
  └─ tbody                       ← page-composed rows
PaginationBar                    ← follows the table
```

`PageHeader` is **never** modified to absorb search — search lives in the
`TableToolbar` below it (D-04).

### Header contract

- Every in-scope data table has a visible `<thead>` with **translated,
  field-name `<th scope="col">`** headers.
- Action-only columns are headed `Actions`; **no blank visual column headers
  are permitted** (translated labels even for reorder/checkbox columns).
- Keep the sticky header rule: `<thead className="sticky top-0 z-10">` inside
  the `Table` scroll shell, header cells on the dominant background with a
  bottom border, 12px semibold labels, 16px × 8px cell padding.
- Numeric/action columns stay right-aligned (`text-right`) when their cells
  are right-aligned.
- Body cells use 14px regular text with the same horizontal padding; rows
  keep `hover:bg-muted/30` where interactive.
- Partial values keep the established `—` placeholder; a missing optional
  value never removes its field-name header or shifts columns.

### Search contract (D-02 — `BrowseSearchInput` baseline)

Every migrated surface uses the shared search control:

- **300ms debounce**, **two-character minimum** before a non-empty query is
  applied, **URL-synced `q`** via `useSearchParams`, a **visible clear
  button** whenever text exists, and a Lucide search icon.
- **Query change or clear resets `page` to 1** while **preserving all
  page-owned filter parameters** (`setSearchParams(prev => …)` shape — the
  canonical adapter from `GroupList`).
- Complex filter pages (`AuditLogPage`, `ObjectTypeObjects`, `SearchPage`)
  add their filter bar **above or alongside** the standard search input,
  never replace or hide it; search and feature filters **combine** rather
  than silently clearing one another.
- A one-character entry must not issue a filtered request; clearing restores
  the unfiltered result.
- The toolbar has `role="search"`; search inputs carry a translated
  accessible label (`Search {resource}`) and the visible placeholder
  `Search {resource}...` (translated).
- Loading keeps the toolbar mounted (no layout shift); only controls whose
  existing page behavior requires it are disabled.

### Breadcrumb contract (D-03 — explicit items)

- Pages pass an explicit `items` array of `{ label, href? }`. Every item
  except the final/current one requires `href`; the final item has **no
  href** and renders as text with `aria-current="page"`.
- Place the breadcrumb **immediately before `PageHeader`**, 8px beneath it,
  inside the page's existing `space-y-6` rhythm.
- Labels use the **translated overview label** and the **loaded entity
  name** — never raw identifiers as fallback labels, never browser-history
  back paths, never URL-segment route inference.
- Long labels wrap at item boundaries (`flex-wrap` + `min-w-0 break-words`);
  the current label keeps a native `title` with its full accessible name —
  never ellipsize a current entity name without that alternative.
- Loading/error of the entity is owned by the enclosing page: the
  breadcrumb is presentational and receives labels from its page (render
  the page's established loading state before passing items; parent links
  remain deterministic when the current entity cannot be loaded).

### Composition examples

**Group-style (flat admin list → detail).** `GroupList` + `GroupDetail` are
the canonical simple composition (see `frontend/src/features/admin/`):

```jsx
<PageHeader title={t('admin.groups.title', 'Groups')}>…actions…</PageHeader>

<TableToolbar
  urlQ={q}
  setQ={setQ}
  placeholder={t('admin.groups.search_placeholder', 'Search Groups...')}
  ariaLabel={t('admin.groups.search_label', 'Search Groups')}
/>

{isLoading ? (
  <TableSkeleton rows={5} cols={3} />
) : total === 0 ? (
  q ? (
    <EmptyState variant="no-results" title={t('admin.groups.no_results_title', 'No matching groups')} … />
  ) : (
    <EmptyState variant="empty" title={t('admin.groups.empty_title', 'No groups yet')} … />
  )
) : (
  <div className="space-y-3">
    <Table>
      <thead className="sticky top-0 z-10">
        <tr className="border-b bg-background">
          <th scope="col" className="text-left px-4 py-2 text-xs font-semibold">{t('common.name', 'Name')}</th>
          <th scope="col" className="text-right px-4 py-2 text-xs font-semibold">{t('common.actions', 'Actions')}</th>
        </tr>
      </thead>
      <tbody>{/* page-composed rows, Can gates preserved */}</tbody>
    </Table>
    <PaginationBar … />
  </div>
)}
```

```jsx
// GroupDetail
<Breadcrumb items={[{ label: t('admin.groups.title', 'Groups'), href: '/groups' }, { label: group.name }]} />
<PageHeader …>…</PageHeader>
```

**Nested Browse-style (Source → Schema → ObjectType chain).** Browse detail
flows keep their hierarchy data loading as an adapter and delegate
presentation to the generic `Breadcrumb` — see
`frontend/src/features/browse/ObjectTypeBreadcrumb.jsx` and its pure items
builder `object-type-breadcrumb-items.js`:

```jsx
// ObjectTypeBreadcrumb (data-loading adapter)
const items = buildObjectTypeBreadcrumbItems({ source, schema, typeChain, trailing })
return <Breadcrumb items={items} />
```

For Browse specialist tables, compose the shared shell around the
page-owned specialist body (`ObjectTypeObjects.jsx` is the reference: the
objects grid composes `Table` + `TableToolbar` with the `FilterBuilder` in
the `filters` slot and the standard `BrowseSearchInput` always mounted —
server `q` and `filter_tree` combine).

### Error and Retry

The UI-SPEC error copy (`Could not load {resources}. Try again.`) and its
`Retry` action apply to pages that **already have a refetch path**
(`refetch` from a query hook). Pages without an existing refetch path keep
their established error presentation — this frontend-only phase does not
create data-fetching contracts where none existed.

### Do not

- Do not introduce a new table/search/breadcrumb variant when the shared
  components fit — extend `Table`/`TableToolbar`/`Breadcrumb` additively
  (e.g. the `tableClassName` and `searchTestId`/`searchControl` passthroughs)
  instead of forking.
- Do not flatten specialist rows into a generic row-object API — compose.
- Do not add backend search endpoints for local-filter surfaces; the
  per-surface search mode is fixed by the Phase 252 inventory (server `q` /
  local loaded-list filter / existing-query adapter).