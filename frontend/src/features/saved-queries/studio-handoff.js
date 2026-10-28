// Pass an editor snapshot, not just its saved id: unsaved changes and drafts
// belong in Studio too. Router state keeps query text out of the URL.
export function buildStudioHandoff(editor) {
  if (!editor.id && !editor.name && !editor.queryText && !editor.variablesText) return undefined
  return {
    studioDocument: {
      id: editor.id,
      name: editor.name,
      query: editor.queryText,
      variables: editor.variablesText,
    },
  }
}
