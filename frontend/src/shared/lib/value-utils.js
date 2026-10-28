export function decodeHtml(str) {
  const txt = document.createElement('textarea')
  txt.innerHTML = str
  return txt.value
}

export function stripHtml(str) {
  if (/<[a-z][\s\S]*>/i.test(str)) {
    const t = document.createElement('textarea')
    t.innerHTML = str
    return t.value.replace(/<[^>]*>/g, '').trim()
  }
  return str
}

export function extractTypedValue(v, attrType) {
  if (attrType === 'boolean') return v.value_boolean ?? v.value_simple ?? null
  if (attrType === 'integer') return v.value_integer ?? v.value_simple ?? null
  if (attrType === 'double') return v.value_float ?? v.value_simple ?? null
  if (attrType === 'datetime') return v.value_datetime ?? v.value_simple ?? null
  if (attrType === 'status') return v.value_struct ?? null
  if (attrType === 'textarea') return v.value_simple ?? null
  if (v.value_simple !== null && v.value_simple !== undefined) {
    const raw = v.value_simple.includes('&quot;') || v.value_simple.includes('&lt;')
      ? decodeHtml(v.value_simple) : v.value_simple
    const cleaned = stripHtml(raw)
    try { return JSON.parse(cleaned) } catch { return cleaned }
  }
  return v.value_float ?? v.value_integer ?? v.value_boolean ?? v.value_datetime ?? v.value_struct ?? null
}

export function buildValuePayload(effectiveType, newValue) {
  const base = { value_simple: null, value_float: null, value_integer: null, value_boolean: null, value_datetime: null, value_struct: null }
  if (newValue === null || newValue === undefined || newValue === '') return base
  switch (effectiveType) {
    case 'integer': return { ...base, value_integer: parseInt(newValue, 10) }
    case 'double': return { ...base, value_float: parseFloat(newValue) }
    case 'boolean':
      if (typeof newValue === 'boolean') return { ...base, value_boolean: newValue }
      if (typeof newValue === 'string') return { ...base, value_boolean: newValue.toLowerCase() === 'true' }
      return { ...base, value_boolean: Boolean(newValue) }
    case 'datetime': return { ...base, value_datetime: newValue }
    case 'status': return { ...base, value_struct: typeof newValue === 'object' ? newValue : { id: '', name: String(newValue) } }
    default: return { ...base, value_simple: typeof newValue === 'object' ? JSON.stringify(newValue) : String(newValue) }
  }
}
