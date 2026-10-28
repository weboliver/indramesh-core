#!/usr/bin/env node

import { readFile } from 'node:fs/promises'
import { join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(join(resolve(fileURLToPath(import.meta.url), '..', '..')))
const catalog = JSON.parse(await readFile(join(root, 'locales/en/systemui.json'), 'utf8'))
const tokenPattern = /{{\s*count\s*}}/g
const errors = []
const families = new Set()

for (const key of Object.keys(catalog)) {
  if (key.endsWith('_one')) families.add(key.slice(0, -4))
  if (key.endsWith('_other')) families.add(key.slice(0, -6))
}

for (const base of [...families].sort()) {
  const one = catalog[`${base}_one`]
  const other = catalog[`${base}_other`]
  if (typeof one !== 'string' || typeof other !== 'string') {
    errors.push(`missing plural variant: ${base}`)
    continue
  }
  if (!/{{\s*count\s*}}/.test(one) || !/{{\s*count\s*}}/.test(other)) errors.push(`missing {{count}} token: ${base}`)
  tokenPattern.lastIndex = 0
}

if (errors.length > 0) {
  console.error(`phase231 plural audit failed with ${errors.length} issue(s)`)
  errors.forEach((error) => console.error(`- ${error}`))
  process.exit(1)
}

console.log(`phase231 plural audit passed: ${families.size} explicit _one/_other families`)
