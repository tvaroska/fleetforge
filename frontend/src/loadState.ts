import { ApiError } from './api'

export type LoadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'loaded' }
  | { status: 'error'; message: string }

export const LOAD_IDLE: LoadState = { status: 'idle' }
export const LOAD_PENDING: LoadState = { status: 'loading' }
export const LOAD_READY: LoadState = { status: 'loaded' }

export function loadFailed(message: string): LoadState {
  return { status: 'error', message }
}

export function isLoaded(state: LoadState): boolean {
  return state.status === 'loaded'
}

export function loadErrorMessage(state: LoadState): string | null {
  return state.status === 'error' ? state.message : null
}

/**
 * Classifies a failed request: 401 triggers `onSessionExpired` and returns `null` so
 * callers do not flash a stale alert while dropping back to sign-in; every other error
 * resolves to a displayable message.
 */
export function resolveLoadError(
  err: unknown,
  fallback: string,
  onSessionExpired: () => void,
): string | null {
  if (err instanceof ApiError && err.isUnauthorized) {
    onSessionExpired()
    return null
  }
  return err instanceof Error && err.message.trim() !== '' ? err.message : fallback
}
