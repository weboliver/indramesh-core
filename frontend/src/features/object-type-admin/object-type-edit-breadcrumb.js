/**
 * EditObjectType breadcrumb items builder (Phase 252, UX-07).
 *
 * The Object Type editor's navigation chain is built as D-03 explicit
 * Breadcrumb items: translated overview label linking to the Object Types
 * overview, the loaded ObjectType name linking back to its browse page, and
 * the current Edit label as the final aria-current item. Loaded display
 * names only — no raw identifier fallback (T-252-13).
 */
export function buildEditObjectTypeBreadcrumbItems({ overviewLabel, name, id, editLabel }) {
  const items = []
  if (overviewLabel) {
    items.push({ label: overviewLabel, href: '/browse/object-types' })
  }
  if (name && id) {
    items.push({ label: name, href: `/browse/object-types/${id}` })
  }
  if (editLabel) {
    items.push({ label: editLabel })
  }
  return items
}