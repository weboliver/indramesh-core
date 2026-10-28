export default {
  root: import.meta.dirname,
  build: {
    lib: {
      entry: 'index.jsx',
      formats: ['es'],
      fileName: () => 'example.js',
    },
    outDir: '../static',
    emptyOutDir: true,
    cssCodeSplit: false,
    rollupOptions: {
      external: [
        'react',
        'react/jsx-runtime',
        'react-dom',
        'react-dom/client',
        'react-router',
        'react-router-dom',
        '@tanstack/react-query',
        '@indramesh/plugin-api',
      ],
    },
  },
}