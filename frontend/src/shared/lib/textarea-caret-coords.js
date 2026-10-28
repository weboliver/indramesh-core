// Mirror-div caret-coordinate utility (D-04). No browser API returns pixel
// coordinates for a textarea selection index, so we clone the textarea's
// computed style onto a hidden absolutely-positioned <div>, fill it with the
// pre-caret text plus a marker <span>, and read the span's offsetTop/offsetLeft.
// Technique ported from the public-domain component/textarea-caret-position
// algorithm — no new npm dependency (repo convention: hand-roll small UI
// primitives, see DropdownMenu.jsx/Button.jsx).
//
// Recompute on EVERY call — never cache: `resize-y` textareas and window
// reflows invalidate cached coordinates (Pitfall 4).

const MIRROR_COPY_PROPS = [
  'boxSizing', 'width', 'height', 'overflowX', 'overflowY',
  'borderTopWidth', 'borderRightWidth', 'borderBottomWidth', 'borderLeftWidth', 'borderStyle',
  'paddingTop', 'paddingRight', 'paddingBottom', 'paddingLeft',
  'fontStyle', 'fontVariant', 'fontWeight', 'fontStretch', 'fontSize', 'fontSizeAdjust',
  'lineHeight', 'fontFamily', 'textAlign', 'textTransform', 'textIndent', 'textDecoration',
  'letterSpacing', 'wordSpacing', 'tabSize', 'whiteSpace', 'wordWrap', 'wordBreak',
]

export function getCaretCoordinates(textarea, position) {
  const div = document.createElement('div')
  document.body.appendChild(div)
  const style = div.style
  const computed = window.getComputedStyle(textarea)
  style.whiteSpace = 'pre-wrap'
  style.wordWrap = 'break-word'
  style.position = 'absolute'
  style.visibility = 'hidden'
  MIRROR_COPY_PROPS.forEach((prop) => {
    style[prop] = computed[prop]
  })
  div.textContent = textarea.value.substring(0, position)
  const span = document.createElement('span')
  span.textContent = textarea.value.substring(position) || '.'
  div.appendChild(span)
  const coordinates = {
    top: span.offsetTop + parseInt(computed['borderTopWidth']),
    left: span.offsetLeft + parseInt(computed['borderLeftWidth']),
    height: parseInt(computed['lineHeight']),
  }
  document.body.removeChild(div)
  return coordinates
}
