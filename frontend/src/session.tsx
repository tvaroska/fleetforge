// The login gate.
//
// There is no client-side session state worth the name: the credential is an HttpOnly
// cookie the page cannot read, so "am I logged in?" is answered by asking the server
// (`GET /v1/auth/me`) and by watching for a 401 on any later call. Nothing is mirrored
// into localStorage — a copy would only ever disagree with the cookie.

import { useCallback, useEffect, useState, type ReactNode } from 'react'
import { ApiError, api, type Me } from './api'

type State =
  | { phase: 'checking' }
  | { phase: 'anonymous'; detail?: string }
  | { phase: 'authenticated'; me: Me }

export function useSession() {
  const [state, setState] = useState<State>({ phase: 'checking' })

  const probe = useCallback(async () => {
    try {
      setState({ phase: 'authenticated', me: await api.me() })
    } catch (error) {
      if (error instanceof ApiError && error.isUnauthorized) {
        setState({ phase: 'anonymous' })
        return
      }
      // A transport/5xx failure is NOT "logged out" — saying so would invite the
      // operator to re-type a password at a server that is simply down.
      setState({ phase: 'anonymous', detail: error instanceof Error ? error.message : 'unknown error' })
    }
  }, [])

  useEffect(() => {
    void probe()
  }, [probe])

  /** Any 401 from a page-level call means the cookie died mid-session. */
  const expire = useCallback(() => setState({ phase: 'anonymous' }), [])

  return { state, refresh: probe, expire }
}

export function LoginForm({ onAuthenticated }: { onAuthenticated: () => void }) {
  const [username, setUsername] = useState('admin')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(password, username.trim() || 'admin')
      // Drop the password from memory the moment it is spent.
      setPassword('')
      onAuthenticated()
    } catch (err) {
      setError(
        err instanceof ApiError && err.isUnauthorized
          ? 'Incorrect password.'
          : err instanceof Error
            ? err.message
            : 'login failed',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <main>
      <h1>Fleetforge</h1>
      <form onSubmit={submit}>
        <label htmlFor="username">Email or username</label>
        <input
          id="username"
          name="username"
          type="text"
          autoComplete="username"
          placeholder="alice@example.com or admin"
          value={username}
          onChange={(event) => setUsername(event.target.value)}
          disabled={busy}
          required
        />
        <label htmlFor="password">Admin password</label>
        <input
          id="password"
          name="password"
          type="password"
          autoComplete="current-password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={busy}
          required
        />
        <button type="submit" disabled={busy || password === ''}>
          {busy ? 'Signing in…' : 'Sign in'}
        </button>
      </form>
      {error !== null && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
    </main>
  )
}

function readResetOobCode(): string | null {
  if (typeof window === 'undefined') return null
  const params = new URLSearchParams(window.location.search)
  const mode = params.get('mode')
  const code = params.get('oobCode')?.trim() ?? ''
  if (code === '' || (mode !== null && mode !== 'resetPassword')) {
    return null
  }
  return code
}

function clearResetParamsInUrl(): void {
  if (typeof window === 'undefined') return
  const url = new URL(window.location.href)
  for (const key of ['mode', 'oobCode', 'apiKey', 'continueUrl', 'lang']) {
    url.searchParams.delete(key)
  }
  const next = url.pathname + (url.search ? url.search : '') + url.hash
  window.history.replaceState(null, '', next)
}

export function PasswordResetForm({
  oobCode,
  onAuthenticated,
  onCancel,
}: {
  oobCode: string
  onAuthenticated: () => void
  onCancel: () => void
}) {
  const [password, setPassword] = useState('')
  const [confirmPassword, setConfirmPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    if (password.length < 8) {
      setError('Password must be at least 8 characters.')
      return
    }
    if (password !== confirmPassword) {
      setError('Passwords do not match.')
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.resetPassword(oobCode, password)
      setPassword('')
      setConfirmPassword('')
      clearResetParamsInUrl()
      onAuthenticated()
    } catch (err) {
      setError(
        err instanceof ApiError && err.isUnauthorized
          ? 'This password setup link is invalid or has already been used.'
          : err instanceof Error
            ? err.message
            : 'password reset failed',
      )
    } finally {
      setBusy(false)
    }
  }

  return (
    <main data-testid="password-reset-form">
      <h1>Fleetforge</h1>
      <p>Set a password for your invited account (at least 8 characters).</p>
      <form onSubmit={submit}>
        <label htmlFor="new-password">New password</label>
        <input
          id="new-password"
          name="new-password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          disabled={busy}
          required
        />
        <label htmlFor="confirm-password">Confirm password</label>
        <input
          id="confirm-password"
          name="confirm-password"
          type="password"
          autoComplete="new-password"
          minLength={8}
          value={confirmPassword}
          onChange={(event) => setConfirmPassword(event.target.value)}
          disabled={busy}
          required
        />
        <button
          type="submit"
          disabled={busy || password.length < 8 || confirmPassword.length < 8}
        >
          {busy ? 'Saving…' : 'Set password & sign in'}
        </button>{' '}
        <button type="button" onClick={onCancel} disabled={busy}>
          Back to sign in
        </button>
      </form>
      {error !== null && (
        <p className="bad" role="alert">
          {error}
        </p>
      )}
    </main>
  )
}

export function SessionGate({
  children,
  banner,
}: {
  children: (session: { me: Me; expire: () => void; signOut: () => void }) => ReactNode
  // Shown while checking and signed out (R2b-fe-1: the strip's versions-only form), so
  // the login screen keeps its version line. The signed-in page renders its own.
  banner?: ReactNode
}) {
  const { state, refresh, expire } = useSession()
  const [oobCode, setOobCode] = useState<string | null>(() => readResetOobCode())

  async function signOut() {
    try {
      await api.logout()
    } catch {
      // A failed logout still ends the session locally; the cookie is capped server-side.
    }
    expire()
  }

  if (oobCode !== null) {
    return (
      <>
        {banner}
        <PasswordResetForm
          oobCode={oobCode}
          onAuthenticated={() => {
            setOobCode(null)
            void refresh()
          }}
          onCancel={() => {
            clearResetParamsInUrl()
            setOobCode(null)
          }}
        />
      </>
    )
  }

  if (state.phase === 'checking') {
    return (
      <>
        {banner}
        <main aria-busy="true">Checking session…</main>
      </>
    )
  }
  if (state.phase === 'anonymous') {
    return (
      <>
        {banner}
        {state.detail !== undefined && (
          <p className="bad" role="alert">
            {state.detail}
          </p>
        )}
        <LoginForm onAuthenticated={() => void refresh()} />
      </>
    )
  }
  return <>{children({ me: state.me, expire, signOut })}</>
}
