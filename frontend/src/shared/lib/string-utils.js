export function truncate(str, maxLen) {
  if (str.length <= maxLen) return str
  return str.slice(0, maxLen) + '…'
}
export function getOptionValue(opt, idx) {
  return typeof opt === 'string' ? opt : (opt.value ?? opt.id ?? idx)
}

export function getOptionLabel(opt) {
  return typeof opt === 'string' ? opt : (opt.label ?? opt.name ?? opt.value)
}

export function findOptionByValue(options, value) {
  return options.find((opt, idx) => String(getOptionValue(opt, idx)) === String(value))
}
