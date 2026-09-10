import '@testing-library/jest-dom/vitest'
import { webcrypto } from 'node:crypto'
import { afterEach, vi } from 'vitest'
import { cleanup } from '@testing-library/react'

// jsdom's `crypto` has `getRandomValues` and NO `subtle`. The flasher verifies the
// sha256 of every downloaded part against the manifest before writing a byte (R0-fe-3,
// `flash.ts`) — without this polyfill a test that exercised that path would silently
// take the "no digest available" branch and prove nothing.
if (globalThis.crypto?.subtle === undefined) {
  Object.defineProperty(globalThis, 'crypto', { value: webcrypto, configurable: true })
}

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})
