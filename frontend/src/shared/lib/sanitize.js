import DOMPurify from 'dompurify'
import MarkdownIt from 'markdown-it'

const markdown = new MarkdownIt({ html: false, linkify: false })

// D-08 (Reversibility: costly): this render-time layer complements backend bleach
// sanitization; neither layer is safe to remove as duplicate-looking protection.
const ALLOWED_TAGS = ['p', 'br', 'b', 'strong', 'u', 'h1', 'h2', 'h3']
const ALLOWED_ATTR = []

// If URI-bearing attributes such as href or src are allowed later, configure
// ALLOWED_URI_REGEXP too so javascript: and data: URL schemes remain blocked.
export function sanitizeHtml(rawHtml) {
  return DOMPurify.sanitize(rawHtml, { ALLOWED_TAGS, ALLOWED_ATTR })
}

export function renderMarkdownToSafeHtml(rawMarkdown) {
  const htmlOutput = markdown.render(rawMarkdown)
  return sanitizeHtml(htmlOutput)
}
