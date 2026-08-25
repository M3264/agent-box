import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// `base: './'` is required, not cosmetic: nginx serves this app at both `/` and
// `/hub/` (nginx-ag-full.conf), and the `/hub/` location strips the prefix before
// proxying. Absolute asset URLs would only ever be correct for one of the two
// mounts. Relative URLs resolve against whichever prefix the document was served
// from — which is also why the router is hash-based, so the document path stays
// `/` or `/hub/` no matter how deep the in-app route is.
export default defineConfig({
  base: './',
  plugins: [react()],
  build: {
    outDir: '../static',
    emptyOutDir: true,
    sourcemap: true,
  },
  server: {
    port: 5173,
    proxy: {
      '/api': 'http://127.0.0.1:8090',
      '/ws': { target: 'ws://127.0.0.1:8090', ws: true },
    },
  },
})
