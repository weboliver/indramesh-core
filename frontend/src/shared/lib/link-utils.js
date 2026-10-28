export function isValidUrl(str) {
  return /^https?:\/\//i.test(str)
}

export function isValidEmail(str) {
  return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(str)
}
