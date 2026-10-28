/**
 * Calculate relative luminance of a hex color.
 * Uses the sRGB relative luminance formula from WCAG 2.0.
 * @param {string} hexColor - Hex color with or without '#' prefix (e.g., '#6B7280' or '6B7280')
 * @returns {number} Luminance value between 0 (black) and 1 (white)
 */
function getLuminance(hexColor) {
  const hex = hexColor.replace('#', '')
  const r = parseInt(hex.substring(0, 2), 16) / 255
  const g = parseInt(hex.substring(2, 4), 16) / 255
  const b = parseInt(hex.substring(4, 6), 16) / 255
  // WCAG 2.0 relative luminance formula
  return 0.2126 * r + 0.7152 * g + 0.0722 * b
}

/**
 * Determine appropriate text color (black or white) for a given background color.
 * Uses luminance threshold to decide — matches the ReferenceTypeBadge pattern from Phase 76.
 * @param {string} bgHex - Background hex color
 * @param {number} [threshold=0.4] - Luminance threshold (below → white text, above → dark text)
 * @returns {string} '#FFFFFF' or '#1F2937' (Tailwind gray-800 — slightly softer than pure black)
 */
function getContrastTextColor(bgHex, threshold = 0.4) {
  const luminance = getLuminance(bgHex)
  return luminance < threshold ? '#FFFFFF' : '#1F2937'
}

/**
 * Get Tailwind-compatible color classes for a given background hex.
 * For use when inline styles aren't appropriate (e.g., Tailwind JIT compatibility).
 * Returns text color that contrasts with the background.
 * @param {string} bgHex - Background hex color
 * @returns {{ backgroundColor: string, color: string }} Inline style object
 */
export function getContrastStyle(bgHex, threshold = 0.4) {
  return {
    backgroundColor: bgHex,
    color: getContrastTextColor(bgHex, threshold),
  }
}
