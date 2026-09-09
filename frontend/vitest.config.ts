import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Deliberately SEPARATE from vite.config.ts.
//
// `vite.config.ts` is loaded by the `frontend` container, whose node_modules holds
// runtime and build dependencies only. Importing `vitest/config` there makes the dev
// server fail to boot with ERR_MODULE_NOT_FOUND — which surfaces as a bare 404 from
// Traefik's `web` entrypoint, because the router drops a backend that is not healthy.
// Test configuration therefore lives in a file the container never reads.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
  },
})
