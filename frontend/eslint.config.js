import reactHooks from 'eslint-plugin-react-hooks'
import reactPlugin from 'eslint-plugin-react'
import globals from 'globals'

export default [
  // Standard artifact directories — the dev tooling writes here constantly
  // (playwright-report/test-results during E2E runs, dist/ on build), so
  // they must never be scanned as source.
  {
    ignores: ['node_modules/', 'dist/', 'playwright-report/', 'test-results/'],
  },
  // Application source — browser globals, JSX syntax, react-hooks rules.
  {
    files: ['src/**/*.{js,jsx}'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      parserOptions: { ecmaFeatures: { jsx: true } },
      globals: globals.browser,
    },
    plugins: {
      'react-hooks': reactHooks,
      'react': reactPlugin,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
      // D-03 amendment: core no-unused-vars cannot see JSX usage; this single
      // react rule marks JSX-referenced components as used. No other react
      // rules are enabled (D-01 minimal set).
      'react/jsx-uses-vars': 'error',
      'no-unused-vars': 'error',
      'no-undef': 'error',
      'eqeqeq': 'error',
    },
  },
  // Root config files — node globals for vite.config.js and playwright.config.js's
  // process.env. Same ruleset as src (D-09).
  {
    files: ['vite.config.js', 'playwright.config.js'],
    languageOptions: {
      ecmaVersion: 'latest',
      sourceType: 'module',
      globals: globals.node,
    },
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      'react-hooks/rules-of-hooks': 'error',
      'react-hooks/exhaustive-deps': 'error',
      'no-unused-vars': 'error',
      'no-undef': 'error',
      'eqeqeq': 'error',
    },
  },
]
