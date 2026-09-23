import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

import pkg from './package.json' with { type: 'json' }

// Build identity, frozen into the bundle at build time and rendered in the footer
// beside the API's own. A SPA is cached aggressively, so "which UI am I looking at?"
// is not answerable from the page otherwise — and a stale bundle talking to a fresh
// API is the failure this is here to make visible.
//
// The version comes from package.json (kept equal to pyproject.toml by
// tests/test_version.py); commit and build time come from build args, because the
// builder container has no git. Unset means a dev build, and says so.
const buildDefines = {
  __FF_VERSION__: JSON.stringify(pkg.version),
  __FF_COMMIT__: JSON.stringify(process.env.SOURCE_COMMIT || 'unknown'),
  __FF_BUILT_AT__: JSON.stringify(process.env.BUILT_AT || 'unknown'),
}

// The dev server and frontend/nginx.conf must agree: BOTH serve the SPA and
// proxy `/v1/*` to the API on the SAME origin. That is the one-origin invariant
// (design/production.md -> "Same origin, two backends"); if the two ever diverge,
// the browser starts reporting CORS errors and somebody "fixes" it by adding
// CORS middleware to the API, which is the wrong repair.
export default defineConfig({
  define: buildDefines,
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    // 8080 in dev as well as prod, so the Traefik label needs no override.
    port: 8080,
    strictPort: true,
    // Vite >= 6 rejects unknown Host headers; FF_DOMAIN is the single knob.
    allowedHosts: (process.env.FF_DOMAIN ?? 'localhost').split(','),
    proxy: {
      '/v1': {
        target: process.env.VITE_API_PROXY ?? 'http://api:8000',
        changeOrigin: false,
      },
    },
  },
  build: {
    outDir: 'dist',
  },
})
