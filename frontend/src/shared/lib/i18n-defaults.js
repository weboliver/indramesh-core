export function interpolateDefault(value, variables) {
  return Object.entries(variables).reduce(
    (result, [name, replacement]) => result.replaceAll(`{{${name}}}`, String(replacement)),
    value
  )
}
