// The gate's job is to distinguish three states that all look like "no dashboard":
// a live session, a dead cookie, and a server that is down. Conflating the last two
// invites an operator to re-type the admin password at an API that cannot answer.

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { SessionGate } from './session'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const ME = {
  token_id: '44444444-4444-4444-8444-444444444444',
  subject: 'admin',
  scopes: ['admin'],
  expires_at: '2099-01-01T00:00:00Z',
}

describe('SessionGate', () => {
  it('renders the dashboard when the cookie is live', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse(ME))

    render(<SessionGate>{({ me }) => <p>signed in as {me.subject}</p>}</SessionGate>)

    expect(await screen.findByText(/signed in as admin/)).toBeInTheDocument()
  })

  it('shows the login form on 401 and never renders children', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ detail: 'nope' }, 401))
    const children = vi.fn()

    render(<SessionGate>{children}</SessionGate>)

    expect(await screen.findByLabelText(/admin password/i)).toBeInTheDocument()
    expect(children).not.toHaveBeenCalled()
  })

  it('logs in, then re-probes the session rather than trusting the login response', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ detail: 'nope' }, 401)) // initial /me
      .mockResolvedValueOnce(jsonResponse({ expires_at: '2099-01-01T00:00:00Z' })) // login
      .mockResolvedValue(jsonResponse(ME)) // /me again

    render(<SessionGate>{({ me }) => <p>signed in as {me.subject}</p>}</SessionGate>)

    await userEvent.type(await screen.findByLabelText(/admin password/i), 'hunter2')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByText(/signed in as admin/)).toBeInTheDocument()

    // The password went in a POST body, never a query string (nginx logs $request).
    const loginCall = fetchMock.mock.calls.find(([url]) => String(url) === '/v1/auth/login')
    expect(loginCall).toBeDefined()
    expect(String(loginCall?.[0])).not.toContain('hunter2')
    expect((loginCall?.[1] as RequestInit).method).toBe('POST')
  })

  it('reports a wrong password without wiping the form into a generic error', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(jsonResponse({ detail: 'nope' }, 401))
      .mockResolvedValueOnce(jsonResponse({ detail: 'invalid credentials' }, 401))

    render(<SessionGate>{() => <p>dashboard</p>}</SessionGate>)

    await userEvent.type(await screen.findByLabelText(/admin password/i), 'wrong')
    await userEvent.click(screen.getByRole('button', { name: /sign in/i }))

    expect(await screen.findByRole('alert')).toHaveTextContent(/incorrect password/i)
    expect(screen.queryByText('dashboard')).not.toBeInTheDocument()
  })

  it('distinguishes an unreachable API from a logged-out session', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('network down'))

    render(<SessionGate>{() => <p>dashboard</p>}</SessionGate>)

    // Still a login form (there is nothing else to show), but the reason is stated.
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent(/unreachable/i))
  })
})
