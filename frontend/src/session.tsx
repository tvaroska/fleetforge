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
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  async function submit(event: React.FormEvent) {
    event.preventDefault()
    setBusy(true)
    setError(null)
    try {
      await api.login(password)
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

export function SessionGate({
  children,
}: {
  children: (session: { me: Me; expire: () => void; signOut: () => void }) => ReactNode
}) {
  const { state, refresh, expire } = useSession()

  async function signOut() {
    try {
      await api.logout()
    } catch {
      // A failed logout still ends the session locally; the cookie is capped server-side.
    }
    expire()
  }

  if (state.phase === 'checking') return <main aria-busy="true">Checking session…</main>
  if (state.phase === 'anonymous') {
    return (
      <>
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
