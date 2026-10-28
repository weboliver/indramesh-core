// Core client-side saved-query analysis (D-03/D-13).
//
// Built on the already-present host `graphql` package so the simple textarea
// editor gives immediate syntax / query-only / operationName feedback without
// any new editor dependency. This is advisory only: the server 422 remains
// authoritative (304 D-03). `parse` + `getOperationAST` mirror the exact rules
// the backend service applies (validate_saved_query_document).
import { parse, getOperationAST } from 'graphql'

/**
 * Analyze a saved-query document.
 *
 * @param {string} text raw query text
 * @param {string} [operationName] the transient selected operation (Execute only)
 * @returns {{
 *   empty: boolean,
 *   syntaxError: string|null,
 *   noOperation: boolean,
 *   hasNonQuery: boolean,
 *   multiple: boolean,
 *   operationNames: string[],
 *   unknownOperationName: boolean,
 *   valid: boolean,
 * }}
 */
export function analyzeSavedQuery(text, operationName) {
  const trimmed = typeof text === 'string' ? text.trim() : ''

  if (!trimmed) {
    return {
      empty: true,
      syntaxError: null,
      noOperation: false,
      hasNonQuery: false,
      multiple: false,
      operationNames: [],
      unknownOperationName: false,
      valid: false,
    }
  }

  let document
  try {
    document = parse(trimmed)
  } catch (error) {
    return {
      empty: false,
      syntaxError: error.message,
      noOperation: false,
      hasNonQuery: false,
      multiple: false,
      operationNames: [],
      unknownOperationName: false,
      valid: false,
    }
  }

  const operations = document.definitions.filter(
    (definition) => definition.kind === 'OperationDefinition'
  )
  const operationNames = operations
    .map((operation) => operation.name?.value)
    .filter((name) => Boolean(name))
  // The server rejects ANY mutation/subscription in the document, even one that
  // would not be selected, so a single non-query operation fails the document.
  const hasNonQuery = operations.some((operation) => operation.operation !== 'query')
  const noOperation = operations.length === 0
  const multiple = operations.length > 1
  const resolved = operationName ? getOperationAST(document, operationName) : null
  const unknownOperationName = Boolean(operationName) && resolved === null

  return {
    empty: false,
    syntaxError: null,
    noOperation,
    hasNonQuery,
    multiple,
    operationNames,
    unknownOperationName,
    valid: !noOperation && !hasNonQuery,
  }
}

/**
 * Parse the Variables textarea value. An empty field is valid and means "no
 * request variables" (the saved defaults still apply). Any non-empty value must
 * be a JSON object (array / scalar / null are rejected) — matching the D-02
 * flat key-merge contract.
 *
 * @param {string} text
 * @returns {{ ok: boolean, value: Record<string, unknown>|null, error: string|null }}
 */
export function parseSavedQueryVariables(text) {
  const trimmed = typeof text === 'string' ? text.trim() : ''
  if (!trimmed) return { ok: true, value: null, error: null }

  let parsed
  try {
    parsed = JSON.parse(trimmed)
  } catch (error) {
    return { ok: false, value: null, error: error.message }
  }

  if (parsed === null || typeof parsed !== 'object' || Array.isArray(parsed)) {
    return { ok: false, value: null, error: 'not an object' }
  }

  return { ok: true, value: parsed, error: null }
}

const SAVED_QUERIES_EXPORT_FORMAT = 'indramesh.saved-queries'
const SAVED_QUERIES_EXPORT_VERSION = 1

/**
 * Client-side shape + GraphQL validation of an import document, mirroring the
 * server's all-or-nothing rule (304 D-10), so an invalid entry is reported with
 * a path before any request is sent. The server remains authoritative.
 *
 * @param {unknown} document
 * @returns {{ ok: true } | { ok: false, path: string, reason: string }}
 */
export function validateSavedQueriesDocument(document) {
  if (!document || typeof document !== 'object' || Array.isArray(document)) {
    return { ok: false, path: '', reason: 'not a JSON object' }
  }
  if (document.format !== SAVED_QUERIES_EXPORT_FORMAT) {
    return { ok: false, path: 'format', reason: 'unexpected format' }
  }
  if (document.version !== SAVED_QUERIES_EXPORT_VERSION) {
    return { ok: false, path: 'version', reason: 'unsupported version' }
  }
  if (!Array.isArray(document.queries)) {
    return { ok: false, path: 'queries', reason: 'not a list' }
  }

  for (let index = 0; index < document.queries.length; index += 1) {
    const entry = document.queries[index]
    const base = `queries[${index}]`
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) {
      return { ok: false, path: base, reason: 'not an object' }
    }
    if (typeof entry.name !== 'string' || !entry.name.trim()) {
      return { ok: false, path: `${base}.name`, reason: 'missing name' }
    }
    if (typeof entry.query_text !== 'string' || !entry.query_text.trim()) {
      return { ok: false, path: `${base}.query_text`, reason: 'missing query' }
    }
    const analysis = analyzeSavedQuery(entry.query_text)
    if (analysis.syntaxError) {
      return { ok: false, path: `${base}.query_text`, reason: analysis.syntaxError }
    }
    if (analysis.noOperation) {
      return { ok: false, path: `${base}.query_text`, reason: 'no executable operation' }
    }
    if (analysis.hasNonQuery) {
      return { ok: false, path: `${base}.query_text`, reason: 'only query operations are allowed' }
    }
    if (entry.variables !== undefined && entry.variables !== null) {
      if (typeof entry.variables !== 'object' || Array.isArray(entry.variables)) {
        return { ok: false, path: `${base}.variables`, reason: 'not an object' }
      }
    }
  }

  return { ok: true }
}