#!/usr/bin/env node

import { readFile, readdir } from 'node:fs/promises'
import { extname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { PHASE231_DYNAMIC_DEFAULTS } from '../src/shared/lib/phase231-dynamic-contract.js'

const root = resolve(join(resolve(fileURLToPath(import.meta.url), '..', '..')))
const sourceRoot = join(root, 'src')
const catalogPath = join(root, 'locales/en/systemui.json')
const keyPattern = /^[a-z0-9_.]+$/
const interpolationPattern = /{{\s*(-?\s*[a-zA-Z0-9_.]+)\s*}}/g
const callPattern = /\bt\(\s*(['"])([^'"\\]+)\1\s*,\s*(['"])((?:\\.|(?!\3)[\s\S])*?)\3/g

async function sourceFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true })
  const nested = await Promise.all(entries.map((entry) => {
    const filePath = join(directory, entry.name)
    return entry.isDirectory() ? sourceFiles(filePath) : [filePath]
  }))
  return nested.flat().filter((filePath) => ['.js', '.jsx'].includes(extname(filePath)))
}

function tokens(value) {
  return [...value.matchAll(interpolationPattern)].map((match) => match[1].replace(/\s/g, '')).sort()
}

function decodeDefault(value) {
  return value.replace(/\\(['"])/g, '$1').replace(/\\n/g, '\n')
}

const catalog = JSON.parse(await readFile(catalogPath, 'utf8'))
const errors = []
const catalogKeys = Object.keys(catalog)

for (const [key, defaultText] of Object.entries(PHASE231_DYNAMIC_DEFAULTS)) {
  if (!(key in catalog)) errors.push(`missing dynamic catalog key: ${key}`)
  else if (catalog[key] !== defaultText) errors.push(`dynamic default mismatch: ${key}`)
}

for (const key of catalogKeys) {
  if (!keyPattern.test(key)) errors.push(`invalid key: ${key}`)
  if (/\{\{?[^}]*,\s*(plural|select|selectordinal)\b/.test(catalog[key])) {
    errors.push(`ICU plural syntax: ${key}`)
  }
  if (typeof catalog[key] !== 'string' || catalog[key].length === 0) {
    errors.push(`empty default: ${key}`)
  }
}

const files = await sourceFiles(sourceRoot)
const calls = []
for (const filePath of files) {
  const source = await readFile(filePath, 'utf8')
  for (const match of source.matchAll(callPattern)) {
    const key = match[2]
    const defaultText = decodeDefault(match[4])
    calls.push({ key, defaultText, filePath })
    if (!(key in catalog)) errors.push(`missing catalog key: ${key} (${filePath})`)
    if (!keyPattern.test(key)) errors.push(`invalid source key: ${key} (${filePath})`)
    if (key in catalog && tokens(catalog[key]).join('|') !== tokens(defaultText).join('|')) {
      errors.push(`token mismatch: ${key} (${filePath})`)
    }
    if (key in catalog && defaultText.includes('{{') && catalog[key] !== defaultText) {
      errors.push(`default mismatch: ${key} (${filePath})`)
    }
  }
}

const uniqueSourceKeys = new Set(calls.map(({ key }) => key))
const unusedCatalogKeys = catalogKeys.filter((key) => !uniqueSourceKeys.has(key))

if (errors.length > 0) {
  console.error(`phase231 catalog audit failed with ${errors.length} issue(s)`)
  errors.forEach((error) => console.error(`- ${error}`))
  process.exit(1)
}

console.log(`phase231 catalog audit passed: 0 missing/invalid source strings; ${catalogKeys.length} catalog keys, ${uniqueSourceKeys.size} source keys, ${files.length} source files`)
if (unusedCatalogKeys.length > 0) {
  console.log(`- ${unusedCatalogKeys.length} catalog keys are source-dynamic or shared defaults not visible to the static call scan`)
}
