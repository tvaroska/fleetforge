import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// Deliberately SEPARATE from vite.config.ts.
//
// `vite.config.ts` is loaded by the `frontend` container, whose node_modules holds
// runtime and build dependencies only. Importing `vitest/config` there makes the dev
// server fail to boot with ERR_MODULE_NOT_FOUND — which surfaces as a bare 404 from
// Traefik's `web` entrypoint, because the router drops a backend that is not healthy.
// Test configuration therefore lives in a file the container never reads.
//
// Pure-TypeScript `.test.ts` suites that do not render React components or access DOM
// globals opt into `// @vitest-environment node` at file top to skip jsdom boot overhead.
export default defineConfig({
  plugins: [react()],
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./src/setupTests.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    testTimeout: 15000,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/setupTests.ts',
        'src/main.tsx',
        'src/vite-env.d.ts',
        'src/apiGenerated.ts',
        'src/apiTypes.ts',
        'src/flashTestHelpers.ts',
      ],
      reporter: ['text', 'json-summary'],
      thresholds: {
        lines: 75,
        functions: 75,
        statements: 75,
      },
    },
  },
})
