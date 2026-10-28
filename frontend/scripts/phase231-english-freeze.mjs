#!/usr/bin/env node

import { mkdir, readFile, writeFile } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const root = resolve(dirname(fileURLToPath(import.meta.url)), '..')
const baselinePath = resolve(root, 'e2e/fixtures/phase231-english-baseline.json')

const baseline = {
  version: 1,
  language: 'en',
  routes: {
    search: {
      visible: [
        { role: 'button', name: 'Tools menu' },
        { role: 'button', name: 'Configuration menu' },
        { role: 'button', name: 'Integration menu' },
        { role: 'button', name: 'Administration menu' },
        { role: 'button', name: 'Toggle dark mode' },
        { role: 'button', name: 'User menu' },
      ],
      text: ['Tools', 'Configuration', 'Integration', 'Administration'],
      attributes: [
        { testId: 'tools-menu-trigger', attribute: 'aria-label', value: 'Tools menu' },
        { testId: 'config-menu-trigger', attribute: 'aria-label', value: 'Configuration menu' },
        { testId: 'tasks-menu-trigger', attribute: 'aria-label', value: 'Integration menu' },
        { testId: 'admin-menu-trigger', attribute: 'aria-label', value: 'Administration menu' },
      ],
    },
    browse: { url: '/browse/object-types?lang=en', headings: ['Object Types'] },
    search_page: { url: '/search?lang=en', headings: ['Search'] },
    object_type_admin: { url: '/object-types/new?lang=en', headings: ['Create Object Type'] },
    tasks: { url: '/tasks?lang=en', headings: ['Tasks'] },
    schedules: { url: '/schedules?lang=en', headings: ['Schedules'] },
    imports: { url: '/imports?lang=en', headings: ['Import Definitions'] },
    exports: { url: '/exports?lang=en', headings: ['Export Definitions'] },
    conflicts: { url: '/conflicts?lang=en', headings: ['Conflicts'] },
    targets: { url: '/configuration/targets?lang=en', headings: ['Targets'] },
    graphql: { url: '/tools/saved-queries?lang=en', testIds: ['saved-queries-page'] },
    settings: { url: '/configuration/settings?lang=en', headings: ['Settings'] },
    admin: { url: '/users?lang=en', headings: ['Users'] },
    permissions: { url: '/configuration/permissions?lang=en', headings: ['Permissions'] },
    profile: { url: '/profile?lang=en', headings: ['My Profile'] },
    login: { url: '/login?lang=en', selectors: ['form'] },
    wizard: { url: '/configuration/templates?lang=en', headings: ['Template Library'] },
  },
}

async function main() {
  const command = process.argv[2] === '--check' ? 'check' : process.argv[2]
  if (command === '--help' || command === '-h' || !command) {
    console.log('Usage: node scripts/phase231-english-freeze.mjs <baseline|check|--check>')
    console.log('  baseline  write the deterministic English shell fixture')
    console.log('  check     verify the fixture is present and valid JSON')
    return
  }

  if (command === 'baseline') {
    await mkdir(dirname(baselinePath), { recursive: true })
    await writeFile(baselinePath, `${JSON.stringify(baseline, null, 2)}\n`)
    console.log(`phase231 English baseline written: ${baselinePath}`)
    return
  }

  if (command === 'check') {
    const actual = JSON.parse(await readFile(baselinePath, 'utf8'))
    if (JSON.stringify(actual) !== JSON.stringify(baseline)) {
      throw new Error('phase231 English baseline drifted from the embedded canonical baseline')
    }
    console.log('phase231 English baseline is valid')
    return
  }

  throw new Error(`Unknown command: ${command}`)
}

main().catch((error) => {
  console.error(error.message)
  process.exitCode = 1
})
