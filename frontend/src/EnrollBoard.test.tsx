// What these tests defend, in order of how much a regression would cost:
//
// 1. The plaintext token is rendered once and never persisted anywhere a later
//    session could read it. The server cannot re-derive it; a leak is also a
//    credential leak, since the token enrolls a board into the fleet.
// 2. A 401 bounces to the login screen instead of rendering as a page error — an
//    operator who is quietly logged out must not think enrollment is broken.
// 3. The displayed status is the server's derived status, not one recomputed here.
//    The API's status IS the burn predicate; a second implementation would
//    eventually say "active" about a token that cannot enroll.

import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { EnrollBoard } from './EnrollBoard'

const PLAINTEXT = 'ffe_11111111-1111-4111-8111-111111111111.s3cr3tsecretvalue'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

function emptyList() {
  return jsonResponse({ tokens: [] })
}

beforeEach(() => {
  // jsdom has no clipboard; the component must tolerate its absence.
  Object.assign(navigator, { clipboard: { writeText: vi.fn().mockResolvedValue(undefined) } })
})

describe('EnrollBoard', () => {
  it('shows the plaintext token once and keeps it out of any persistent store', async () => {
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(emptyList()) // initial history load
      .mockResolvedValueOnce(
        jsonResponse({
          id: '11111111-1111-4111-8111-111111111111',
          token: PLAINTEXT,
          group_id: null,
          expires_at: '2026-09-10T12:00:00Z',
          created_at: '2026-09-09T12:00:00Z',
        }),
      )
      .mockResolvedValueOnce(emptyList()) // refresh after issue

    render(<EnrollBoard onSessionExpired={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: /generate enrollment token/i }))

    expect(await screen.findByTestId('issued-token')).toHaveTextContent(PLAINTEXT)
    expect(screen.getByRole('alert')).toHaveTextContent(/shown once/i)

    // The credential must not survive anywhere the next page load could read it.
    expect(window.localStorage.getItem('token')).toBeNull()
    expect(JSON.stringify(window.localStorage)).not.toContain(PLAINTEXT)
    expect(JSON.stringify(window.sessionStorage)).not.toContain(PLAINTEXT)
    expect(window.location.search).not.toContain('ffe_')

    // Issued ungrouped, via POST, on a same-origin relative path.
    const [url, init] = fetchMock.mock.calls[1] as [string, RequestInit]
    expect(url).toBe('/v1/enrollment-tokens')
    expect(init.method).toBe('POST')
    expect(JSON.parse(init.body as string)).toEqual({ group_id: null })
    expect(init.credentials).toBe('same-origin')
  })

  it('dismissing the issued token removes it from the DOM for good', async () => {
    vi.spyOn(globalThis, 'fetch')
      .mockResolvedValueOnce(emptyList())
      .mockResolvedValueOnce(
        jsonResponse({
          id: '11111111-1111-4111-8111-111111111111',
          token: PLAINTEXT,
          group_id: null,
          expires_at: '2026-09-10T12:00:00Z',
          created_at: '2026-09-09T12:00:00Z',
        }),
      )
      .mockResolvedValue(emptyList())

    render(<EnrollBoard onSessionExpired={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: /generate enrollment token/i }))
    await screen.findByTestId('issued-token')

    await userEvent.click(screen.getByRole('button', { name: /done/i }))

    expect(screen.queryByTestId('issued-token')).not.toBeInTheDocument()
    expect(document.body.textContent).not.toContain(PLAINTEXT)
  })

  it('renders the status the server derived, not one recomputed from timestamps', async () => {
    // `expires_at` is in the past AND `used_at` is set, but the server says `used`.
    // A client that recomputed would likely say `expired` and mislead the operator
    // about why enrollment failed.
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      jsonResponse({
        tokens: [
          {
            id: '22222222-2222-4222-8222-222222222222',
            group_id: null,
            status: 'used',
            created_at: '2026-09-01T00:00:00Z',
            expires_at: '2026-09-02T00:00:00Z',
            used_at: '2026-09-01T01:00:00Z',
            used_by_device_id: 'a4cf12b3de90',
            revoked_at: null,
          },
        ],
      }),
    )

    render(<EnrollBoard onSessionExpired={vi.fn()} />)

    const row = within(await screen.findByRole('table')).getByRole('row', { name: /a4cf12b3de90/ })
    expect(within(row).getByText('used')).toBeInTheDocument()
    // A spent token cannot be revoked into a different state, so no button.
    expect(within(row).queryByRole('button', { name: /revoke/i })).not.toBeInTheDocument()
  })

  it('offers revoke only for active tokens and calls the revoke endpoint', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockImplementation((input, init) => {
      const url = String(input)
      if (url.endsWith('/revoke')) return Promise.resolve(new Response(null, { status: 204 }))
      void init
      return Promise.resolve(
        jsonResponse({
          tokens: [
            {
              id: '33333333-3333-4333-8333-333333333333',
              group_id: null,
              status: 'active',
              created_at: '2026-09-09T00:00:00Z',
              expires_at: '2099-01-01T00:00:00Z',
              used_at: null,
              used_by_device_id: null,
              revoked_at: null,
            },
          ],
        }),
      )
    })

    render(<EnrollBoard onSessionExpired={vi.fn()} />)
    await userEvent.click(await screen.findByRole('button', { name: /revoke/i }))

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([url]) =>
          String(url).includes('/v1/enrollment-tokens/33333333-3333-4333-8333-333333333333/revoke'),
        ),
      ).toBe(true),
    )
  })

  it('treats a 401 as a dead session rather than a page error', async () => {
    const onSessionExpired = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ detail: 'not authenticated' }, 401))

    render(<EnrollBoard onSessionExpired={onSessionExpired} />)

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalled())
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('surfaces a server error without claiming the session ended', async () => {
    const onSessionExpired = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(jsonResponse({ detail: 'database is down' }, 500))

    render(<EnrollBoard onSessionExpired={onSessionExpired} />)

    expect(await screen.findByRole('alert')).toHaveTextContent(/database is down/i)
    expect(onSessionExpired).not.toHaveBeenCalled()
  })
})
