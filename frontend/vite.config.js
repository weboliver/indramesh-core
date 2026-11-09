import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { visualizer } from 'rollup-plugin-visualizer'

// Import-map host runtime (D-11): the browser resolves `/plugin-runtime/host-*.js`
// to the Vite-transformed source shim in dev, so dev and build hand a plugin
// bundle the SAME module identity the host app itself imports. The build half is
// the `entryFileNames` function below (hash-free names for those eight entries).
// The import map's position (before the first module script) is owned by Vite's
// built-in postImportMapHook — do not add a transformIndexHtml reorder hook.
const pluginHostRuntime = {
  name: 'plugin-host-runtime',
  configureServer(server) {
    server.middlewares.use((req, _res, next) => {
      const match = (req.url || '').match(/^\/plugin-runtime\/([\w-]+\.js)(\?.*)?$/)
      if (match) req.url = `/src/shared/plugin-runtime/${match[1]}`
      next()
    })
  },
}

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    pluginHostRuntime,
    // Bundle-size report, opt-in only: `ANALYZE=1 npm run build`. It never belongs to a shipped
    // artifact — the public stage already forbids a `stats.html` (scripts/publish.sh) and the
    // release image copies nothing but the built assets, so emitting it by default only put a
    // module inventory of the source tree into the image.
    ...(process.env.ANALYZE === '1'
      ? [visualizer({
          filename: 'dist/stats.html',
          open: false,
          gzipSize: true,
          brotliSize: true,
        })]
      : []),
  ],
  optimizeDeps: {
    // All seven package specifiers the host shims re-export must be optimized in
    // ONE run: `react-router` is otherwise not pre-bundled (no host file imports
    // it directly), and a lazy re-optimization would full-reload the page while
    // host and shim briefly point at different dep hashes.
    include: [
      'react',
      'react/jsx-runtime',
      'react-dom',
      'react-dom/client',
      'react-router',
      'react-router-dom',
      '@tanstack/react-query',
    ],
  },
  server: {
    host: '0.0.0.0',
    port: 5173,
    allowedHosts: ['localhost', 'nginx'],
    watch: {
      // Ignore the playwright-report/ directory — the e2e-tests container writes
      // there during test runs, and Vite's file watcher treats each new test
      // artifact as a source change that triggers a full page reload. Those
      // reloads race the in-flight chunk requests and surface as
      // net::ERR_FAILED in the browser. Without this, every E2E run that
      // produces a non-empty report directory will fail auth.setup.js.
      ignored: ['**/playwright-report/**'],
    },
    proxy: {
      '/api/v1/': {
        target: 'http://backend:8000',
        changeOrigin: true,
      },
    },
  },
  build: {
    // Warn when any chunk exceeds 900KB (Vite's default is 500KB). The host
    // GraphiQL/Monaco editor was removed in Phase 304 — GraphQL Studio now lives
    // in the graphql-studio plugin with its own build — so the former
    // vendor-monaco exception is gone. The raised limit is retained as a
    // conservative global threshold: Vite/Rolldown 8 has no per-chunk exemption
    // (viteReporterPlugin binds chunkSizeWarningLimit globally), and 900KB still
    // catches real regressions on the remaining routes' chunks.
    chunkSizeWarningLimit: 900,
    rollupOptions: {
      // Vite's app-build default is `preserveEntrySignatures: false`, which
      // tree-shakes the eight host-runtime entries down to bare side-effect
      // imports (no exports) — the import map's targets then fail module
      // linking for every plugin bundle in production. `allow-extension` keeps
      // the entry signatures; Rolldown rejects `strict` alongside
      // codeSplitting.includeDependenciesRecursively: false below.
      preserveEntrySignatures: 'allow-extension',
      // One entry per host-runtime shim (+ the SPA index): the import map's keys
      // must stay byte-identical to these `host-*` names, and the entryFileNames
      // function below emits exactly those under /plugin-runtime/.
      input: {
        index: 'index.html',
        'host-react': 'src/shared/plugin-runtime/host-react.js',
        'host-jsx-runtime': 'src/shared/plugin-runtime/host-jsx-runtime.js',
        'host-react-dom': 'src/shared/plugin-runtime/host-react-dom.js',
        'host-react-dom-client': 'src/shared/plugin-runtime/host-react-dom-client.js',
        'host-router': 'src/shared/plugin-runtime/host-router.js',
        'host-router-dom': 'src/shared/plugin-runtime/host-router-dom.js',
        'host-query': 'src/shared/plugin-runtime/host-query.js',
        'host-api': 'src/shared/plugin-runtime/host-api.js',
      },
      output: {
        // Hash-free stable URLs for the eight shim entries only; the app entry
        // and every lazy chunk keep the hashed asset name.
        entryFileNames: (chunk) =>
          chunk.name.startsWith('host-') ? 'plugin-runtime/[name].js' : 'assets/[name]-[hash].js',
        // Vite 8 (Rolldown): the manualChunks function form is accepted but its
        // groups are re-merged by Rolldown's dependency-aware chunking
        // (includeDependenciesRecursively defaults to true). codeSplitting groups
        // reproduce the committed Phase 107 vendor map with identical names AND
        // identical module grouping (D-06: the map is the contract, not the
        // syntax). includeDependenciesRecursively: false restores Rollup
        // manualChunks semantics — a group captures only the modules matching its
        // own test, never their dependencies.
        // (Renamed from the deprecated `advancedChunks` key — same shape,
        // see https://rolldown.rs/reference/OutputOptions.codeSplitting.)
        codeSplitting: {
          includeDependenciesRecursively: false,
          groups: [
            // Existing vendor chunks
            // react + react-dom (+ react-compiler-runtime, should it appear as a
            // transitive dependency) — priority highest (matches original if/else
            // order). react-compiler-runtime is grouped here so it can never be
            // split into a lazy route chunk and create a circular ESM chunk import
            // (GAP-272-1, confirmed via built-bundle inspection).
            { name: 'vendor-react', test: /node_modules[\\/](react|react-dom|react-compiler-runtime)[\\/]/, priority: 30 },
            { name: 'vendor-query', test: /node_modules[\\/]@tanstack[\\/]react-query[\\/]/, priority: 25 },
            { name: 'vendor-axios', test: /node_modules[\\/]axios[\\/]/, priority: 25 },

            // New vendor chunks based on bundle analysis (107-03)
            // react-router-dom + react-router + @remix-run/router — 308KB main chunk
            { name: 'vendor-router', test: /node_modules[\\/](react-router-dom|react-router|@remix-run)[\\/]/, priority: 25 },

            // react-day-picker + date-fns + @date-fns/tz — date picker bundle
            { name: 'vendor-datepicker', test: /node_modules[\\/](react-day-picker|date-fns|@date-fns)[\\/]/, priority: 25 },

            // fuse.js — fuzzy search library
            { name: 'vendor-fuse', test: /node_modules[\\/]fuse\.js[\\/]/, priority: 25 },

            // Phase 304: the host GraphiQL/Monaco/graphiql-* stack was removed (it was never
            // manually chunked — GAP-272-1 showed that regex-matching part of a shared
            // dependency graph cannot reliably predict where Rolldown places every shared
            // low-level module, so it always fell through to automatic chunking). There is no
            // longer a lazy 4MB graphql route chunk to reason about; the remaining groups
            // below cover the whole app.

            // react-colorful — color picker
            { name: 'vendor-colorpicker', test: /node_modules[\\/]react-colorful[\\/]/, priority: 10 },

            // Small utility libraries grouped together (tailwind-merge, clsx, web-vitals)
            { name: 'vendor-utils', test: /node_modules[\\/](tailwind-merge|clsx|web-vitals)[\\/]/, priority: 10 },
          ],
        },
      },
    },
  },
})
