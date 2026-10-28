export function shouldShowWorkerWarning(plugin) {
  if (!plugin) return false
  return (
    plugin.state === 'active' &&
    plugin.requires_worker_update === true &&
    (!plugin.worker_loaded_at || plugin.worker_loaded_version !== plugin.version)
  )
}

// Phase 287 (D-13/D-14): the page shows the failure surface only when the API
// returns a non-empty bounded `database_error` string. No client-side
// derivation of diagnostic content exists — the field is core-authored.
export function shouldShowDatabaseError(plugin) {
  if (!plugin) return false
  return typeof plugin.database_error === 'string' && plugin.database_error !== ''
}

// Phase 318: a plugin declaring `requires_llm` carries the Core LLM status the
// API projects onto it. The page warns only for the two non-working states; a
// plugin without the flag (status null) or with a configured LLM shows nothing.
export function llmWarningKind(plugin) {
  if (!plugin || plugin.requires_llm !== true) return null
  if (plugin.llm_status === 'not_configured' || plugin.llm_status === 'misconfigured') return plugin.llm_status
  return null
}

export function isKillSwitchInteractive(state) {
  return state === 'active' || state === 'disabled'
}

export function nextKillSwitchTarget(state) {
  if (state === 'active') return 'disabled'
  if (state === 'disabled') return 'active'
  return null
}

function isConstBranch(branch) {
  return Boolean(branch) && typeof branch === 'object' && !Array.isArray(branch) && 'const' in branch
}

function enumOptions(values) {
  return values.map((value) => ({ value, label: String(value) }))
}

function constBranchOptions(branches) {
  return branches.map((branch) => ({
    value: branch.const,
    label: branch.title !== undefined ? branch.title : String(branch.const),
  }))
}

// WR-04: a <select> always hands back a DOM string, so an `enum` of numbers or
// a `oneOf`/`anyOf` const branch of booleans would be submitted as "1"/"true"
// and rejected by the schema (jsonschema compares exact values). Resolve the
// raw input back to the option's declared value.
export function coerceConfigFieldValue(field, rawValue) {
  if (!Array.isArray(field?.options)) return rawValue
  const option = field.options.find((candidate) => String(candidate.value) === rawValue)
  return option ? option.value : rawValue
}

export function buildConfigFields(configSchema) {
  const properties = configSchema?.properties
  if (!properties || typeof properties !== 'object' || Array.isArray(properties)) return []
  const required = Array.isArray(configSchema.required) ? configSchema.required : []

  return Object.entries(properties).map(([key, spec]) => {
    const field = {
      key,
      kind: 'unsupported',
      required: required.includes(key),
      defaultValue: spec && typeof spec === 'object' && 'default' in spec ? spec.default : undefined,
    }

    if (!spec || typeof spec !== 'object' || Array.isArray(spec)) {
      field.unsupportedType = 'unknown'
      return field
    }

    if (spec.$ref) {
      field.unsupportedType = '$ref'
      return field
    }

    if (Array.isArray(spec.enum)) {
      field.kind = 'enum'
      field.options = enumOptions(spec.enum)
      return field
    }

    if (Array.isArray(spec.oneOf)) {
      if (spec.oneOf.length > 0 && spec.oneOf.every(isConstBranch)) {
        field.kind = 'enum'
        field.options = constBranchOptions(spec.oneOf)
      } else {
        field.unsupportedType = 'oneOf'
      }
      return field
    }

    if (Array.isArray(spec.anyOf)) {
      if (spec.anyOf.length === 1 && isConstBranch(spec.anyOf[0])) {
        field.kind = 'enum'
        field.options = constBranchOptions(spec.anyOf)
      } else {
        field.unsupportedType = 'anyOf'
      }
      return field
    }

    switch (spec.type) {
      case 'string':
        field.kind = spec.format === 'password' ? 'password' : 'string'
        return field
      case 'number':
        field.kind = 'number'
        return field
      case 'integer':
        field.kind = 'integer'
        return field
      case 'boolean':
        field.kind = 'boolean'
        return field
      case 'object':
        field.unsupportedType = 'object'
        return field
      case 'array':
        field.unsupportedType = 'array'
        return field
      default:
        field.unsupportedType = typeof spec.type === 'string' ? spec.type : 'unknown'
        return field
    }
  })
}

export function buildConfigPayload(fields, draft = {}, original = {}) {
  const config = {}

  fields.forEach((field) => {
    if (field.kind === 'password') {
      const draftValue = draft[field.key]
      const originalValue = original[field.key]
      const changed = draftValue !== undefined && draftValue !== '' && draftValue !== originalValue
      if (changed) config[field.key] = draftValue
      return
    }

    const value = draft[field.key] !== undefined ? draft[field.key] : field.defaultValue
    if (value !== undefined) config[field.key] = value
  })

  return { config }
}