#!/usr/bin/env node
/**
 * Build-output inspection for the Example plugin registration bundle.
 *
 * Fails the build unless `plugins/example/static/example.js` is a lightweight
 * registration bundle that:
 *   * imports ONLY the host singletons, externalized by identity;
 *   * makes no blob-relative sibling chunk/CSS/worker/font request;
 *   * leaks no heavy marker (`monaco`, `graphiql`, `codicon`).
 *
 * The detector is self-tested at startup: it must flag a synthetic non-host
 * import and a synthetic relative dynamic import, and must NOT flag a host-only
 * module. A regression in the detector therefore fails the build too.
 */
import { existsSync, readFileSync, readdirSync } from 'node:fs'
import path from 'node:path'
import { pathToFileURL } from 'node:url'

const HERE = import.meta.dirname
const STATIC_DIR = path.resolve(HERE, '..', 'static')
const REGISTRATION_FILE = path.join(STATIC_DIR, 'example.js')

// The exact host singleton allow-list (frontend/index.html import map). Keep in
// sync with vite.config.mjs.
export const HOST_SINGLETONS = [
  'react',
  'react/jsx-runtime',
  'react-dom',
  'react-dom/client',
  'react-router',
  'react-router-dom',
  '@tanstack/react-query',
  '@indramesh/plugin-api',
]
const HOST_SET = new Set(HOST_SINGLETONS)

// Static `import ... from`, bare `import "..."`, `export ... from`, and literal
// dynamic `import("...")` specifiers. Line/quote anchoring keeps library strings
// that merely contain the word "from" from being treated as imports.
const IMPORT_SPECIFIER_PATTERNS = [
  /^[ \t]*import\s+[^;\n]*?\bfrom\s+["']([^"']+)["']/gm,
  /^[ \t]*import\s+["']([^"']+)["']/gm,
  /^[ \t]*export\s+(?:\*(?:\s+as\s+[\w$]+)?|\{[^}]*\})\s+from\s+["']([^"']+)["']/gm,
  /\bimport\s*\(\s*["']([^"']+)["']\s*\)/g,
]

// Markers that must never appear in the lightweight registration bundle.
const HEAVY_MARKERS = ['monaco', 'graphiql', 'codicon']

const VIOLATION_PATTERNS = [
  { label: 'external font/image URL', re: /url\(\s*["']?(?:\.\.?\/)[^)"']*["']?\s*\)/g },
  { label: 'external font/image URL', re: /url\(\s*["']?\/[^)"']*["']?\s*\)/g },
  { label: 'external font/image URL', re: /url\(\s*["']?https?:[^)"']*["']?\s*\)/g },
  { label: 'CSS @import', re: /@import\b/g },
  { label: 'blob-relative sibling resolution', re: /import\s*\(\s*["']\.\.?\//g },
  { label: 'blob-relative sibling resolution', re: /import\.meta\.url/g },
  { label: 'blob-relative sibling resolution', re: /new\s+URL\s*\(\s*["']\.\.?\//g },
  { label: 'external worker URL', re: /new\s+Worker\s*\(\s*["'](?!data:|blob:)[^"']*["']/g },
]

/**
 * Returns the list of contract violations found in a bundle source string.
 * Exported so the detector can be exercised directly.
 */
export function findViolations(code) {
  const violations = []
  for (const re of IMPORT_SPECIFIER_PATTERNS) {
    re.lastIndex = 0
    let match
    while ((match = re.exec(code)) !== null) {
      if (!HOST_SET.has(match[1])) {
        violations.push(`non-host runtime import: ${match[1]}`)
      }
    }
  }
  for (const { label, re } of VIOLATION_PATTERNS) {
    re.lastIndex = 0
    let match
    while ((match = re.exec(code)) !== null) {
      violations.push(`${label}: ${match[0].slice(0, 80)}`)
    }
  }
  return violations
}

/**
 * Returns the heavy markers found in a bundle source string (case-insensitive).
 * Exported so the detector can be exercised directly.
 */
export function findHeavyMarkers(code) {
  const lower = code.toLowerCase()
  return HEAVY_MARKERS.filter((marker) => lower.includes(marker))
}

function assertDetector() {
  const nonHost = 'import evil from "left-pad";\nexport default evil;\n'
  if (!findViolations(nonHost).some((v) => v.includes('left-pad'))) {
    throw new Error('detector self-test: a non-host import was NOT detected')
  }
  const relative = 'const lazy = () => import("./sibling-chunk.js");\n'
  if (!findViolations(relative).some((v) => v.includes('sibling-chunk'))) {
    throw new Error('detector self-test: a relative dynamic import was NOT detected')
  }
  const hostOnly = 'import { useState } from "react";\nexport default useState;\n'
  const falsePositives = findViolations(hostOnly)
  if (falsePositives.length > 0) {
    throw new Error(`detector self-test: false positive on a host import: ${falsePositives.join('; ')}`)
  }
  if (findHeavyMarkers(hostOnly).length > 0) {
    throw new Error('detector self-test: false positive on a host-only bundle')
  }
  if (findHeavyMarkers('const name = "MonacoEditor";').length === 0) {
    throw new Error('detector self-test: a heavy marker was NOT detected')
  }
}

function main() {
  const problems = []
  try {
    assertDetector()
  } catch (error) {
    console.error('verify-bundle: FAILED')
    console.error(` - ${error.message}`)
    process.exit(1)
  }

  if (!existsSync(REGISTRATION_FILE)) problems.push(`missing registration output: ${REGISTRATION_FILE}`)

  if (existsSync(STATIC_DIR)) {
    const cssFiles = readdirSync(STATIC_DIR).filter((name) => name.endsWith('.css'))
    if (cssFiles.length > 0) problems.push(`separate CSS asset(s) emitted: ${cssFiles.join(', ')}`)
  }

  if (existsSync(REGISTRATION_FILE)) {
    const code = readFileSync(REGISTRATION_FILE, 'utf8')
    problems.push(...findViolations(code).map((violation) => `registration bundle: ${violation}`))
    for (const marker of findHeavyMarkers(code)) {
      problems.push(`registration bundle leaked heavy marker: ${marker}`)
    }
  }

  if (problems.length > 0) {
    console.error('verify-bundle: FAILED')
    for (const problem of problems) console.error(` - ${problem}`)
    process.exit(1)
  }

  console.log(
    'verify-bundle: OK — lightweight registration bundle (host-only imports, no heavy markers, no sibling assets)',
  )
}

// Run only when invoked directly (`node verify-bundle.mjs`), so the detector
// can be imported and unit-exercised without side effects.
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main()
}
