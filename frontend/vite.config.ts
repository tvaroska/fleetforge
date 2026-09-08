import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// The dev server and frontend/nginx.conf must agree: BOTH serve the SPA and
// proxy `/v1/*` to the API on the SAME origin. That is the one-origin invariant
// (design/production.md -> "Same origin, two backends"); if the two ever diverge,
// the browser starts reporting CORS errors and somebody "fixes" it by adding
// CORS middleware to the API, which is the wrong repair.
export default defineConfig({
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
