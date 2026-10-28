import { describe, it } from 'node:test'
import assert from 'node:assert/strict'
import { buildStudioHandoff } from './studio-handoff.js'
import { createStudioDocumentBridge } from '../../../../plugins/graphql-studio/frontend/studio-document.js'

describe('Library to Studio handoff', () => {
  it('carries the current saved query, including unsaved changes and variables', () => {
    const editor = {
      id: 'query-id', name: 'Edited name',
      queryText: 'query Assets($limit: Int!) { objects(limit: $limit) { id } }',
      variablesText: '{\n  "limit": 7\n}',
    }
    const state = buildStudioHandoff(editor)
    assert.deepEqual(state.studioDocument, {
      id: editor.id, name: editor.name,
      query: editor.queryText, variables: editor.variablesText,
    })
    editor.queryText = 'query Changed { __typename }'
    assert.notEqual(state.studioDocument.query, editor.queryText)

    // The exact router payload survives delayed editor initialization.
    const bridge = createStudioDocumentBridge()
    bridge.setDocument(state.studioDocument)
    let query = 'previous studio document'
    let variables = '{"old":true}'
    bridge.attach({
      queryEditor: { getValue: () => query, setValue: value => { query = value } },
      variableEditor: { getValue: () => variables, setValue: value => { variables = value } },
    })
    assert.deepEqual(bridge.getDocument(), {
      query: state.studioDocument.query, variables: state.studioDocument.variables,
    })
  })

  it('carries a new draft and preserves invalid variables for editing rather than discarding them', () => {
    assert.deepEqual(buildStudioHandoff({
      id: null, name: 'Draft', queryText: 'query Draft { __typename }', variablesText: '{unfinished',
    }), {
      studioDocument: {
        id: null, name: 'Draft', query: 'query Draft { __typename }', variables: '{unfinished',
      },
    })
  })

  it('keeps ordinary Studio navigation unchanged when the Library editor is empty', () => {
    assert.equal(buildStudioHandoff({ id: null, name: '', queryText: '', variablesText: '' }), undefined)
  })
})
