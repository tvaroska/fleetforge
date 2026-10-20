import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ProjectAccess } from './ProjectAccess'
import { type Me } from './api'

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const me: Me = {
  token_id: '44444444-4444-4444-8444-444444444444',
  subject: 'admin',
  scopes: ['admin'],
  expires_at: null,
  is_superadmin: true,
  projects: [
    { id: '1', slug: 'default', name: 'Default', description: null, role: 'owner' },
    { id: '2', slug: 'robotics', name: 'Robotics', description: null, role: 'owner' },
  ],
}

afterEach(() => vi.restoreAllMocks())

describe('ProjectAccess', () => {
  it('switches the active project and opens the access & API tokens drawer', async () => {
    const onSelect = vi.fn()
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown, init?: RequestInit) => {
      const url = String(input)
      if (url === '/v1/projects' && !init?.method) {
        return json({
          projects: [
            { id: '1', slug: 'default', name: 'Default', description: null, role: 'owner', created_at: '2026-10-19T00:00:00Z' },
            { id: '2', slug: 'robotics', name: 'Robotics', description: null, role: 'owner', created_at: '2026-10-19T00:00:00Z' },
          ],
        })
      }
      if (url === '/v1/projects/default/members') {
        return json({
          members: [
            { user_id: 'u1', username: 'alice', display_name: 'Alice', role: 'operator', created_at: '2026-10-19T00:00:00Z' },
          ],
        })
      }
      if (url === '/v1/auth/tokens' && init?.method === 'POST') {
        return json(
          {
            id: 't1',
            token: 'ffa_test_token_secret',
            name: 'github-actions',
            subject: 'admin',
            scopes: ['read', 'write:artifacts', 'deploy'],
            project_id: '1',
            project_slug: 'default',
            created_at: '2026-10-19T00:00:00Z',
            expires_at: null,
          },
          201,
        )
      }
      if (url === '/v1/auth/tokens') {
        return json({ tokens: [] })
      }
      if (url === '/v1/users' && init?.method === 'POST') {
        return json(
          {
            id: 'u2',
            username: 'carol',
            email: 'carol@example.com',
            firebase_uid: null,
            has_local_password: false,
            display_name: 'Carol',
            is_superadmin: false,
            disabled_at: null,
            created_at: '2026-10-19T00:00:00Z',
          },
          201,
        )
      }
      if (url === '/v1/users') {
        return json({
          users: [
            {
              id: 'u1',
              username: 'alice',
              email: 'alice@example.com',
              firebase_uid: 'fb-uid-alice',
              has_local_password: false,
              display_name: 'Alice',
              is_superadmin: false,
              disabled_at: null,
              created_at: '2026-10-19T00:00:00Z',
            },
          ],
        })
      }
      return json({})
    })

    render(
      <ProjectAccess
        me={me}
        activeProject="default"
        onSelectProject={onSelect}
        onSessionExpired={vi.fn()}
      />,
    )

    const select = screen.getByLabelText('Project')
    await userEvent.selectOptions(select, 'robotics')
    expect(onSelect).toHaveBeenCalledWith('robotics')

    await userEvent.click(screen.getByRole('button', { name: /Project access & API tokens/i }))
    expect((await screen.findAllByText('alice')).length).toBeGreaterThanOrEqual(1)
    expect(await screen.findByText('alice@example.com')).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText('Invite username'), 'carol')
    await userEvent.type(screen.getByLabelText('Invite email'), 'carol@example.com')
    await userEvent.click(screen.getByRole('button', { name: /Invite user/i }))

    await userEvent.type(screen.getByLabelText('Token label'), 'github-actions')
    await userEvent.click(screen.getByRole('button', { name: /Issue API token/i }))

    await waitFor(() =>
      expect(screen.getByTestId('issued-admin-token')).toHaveTextContent('ffa_test_token_secret'),
    )
  })

  it.each([
    ['failed', /setup email could not be sent/i],
    ['not_configured', /Firebase is not configured on the server; no email was sent/i],
  ])('reports an invite whose email_delivery is %s', async (delivery, message) => {
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown, init?: RequestInit) => {
      const url = String(input)
      if (url === '/v1/projects') {
        return json({ projects: [] })
      }
      if (url === '/v1/projects/default/members') {
        return json({ members: [] })
      }
      if (url === '/v1/auth/tokens') {
        return json({ tokens: [] })
      }
      if (url === '/v1/users' && init?.method === 'POST') {
        return json(
          {
            id: 'u3',
            username: 'erin',
            email: 'erin@example.com',
            firebase_uid: null,
            has_local_password: false,
            display_name: null,
            is_superadmin: false,
            disabled_at: null,
            created_at: '2026-10-19T00:00:00Z',
            email_delivery: delivery,
          },
          201,
        )
      }
      if (url === '/v1/users') {
        return json({ users: [] })
      }
      return json({})
    })

    render(
      <ProjectAccess
        me={me}
        activeProject="default"
        onSelectProject={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    )

    await userEvent.click(screen.getByRole('button', { name: /Project access & API tokens/i }))
    await userEvent.type(await screen.findByLabelText('Invite username'), 'erin')
    await userEvent.type(screen.getByLabelText('Invite email'), 'erin@example.com')
    await userEvent.click(screen.getByRole('button', { name: /Invite user/i }))

    expect(await screen.findByText(message)).toBeInTheDocument()
  })

  const alice = {
    user_id: 'u1',
    username: 'alice',
    display_name: 'Alice',
    role: 'operator',
    created_at: '2026-10-19T00:00:00Z',
  }
  const userRow = (disabledAt: string | null) => ({
    id: 'u1',
    username: 'alice',
    email: null,
    firebase_uid: null,
    has_local_password: true,
    display_name: 'Alice',
    is_superadmin: false,
    disabled_at: disabledAt,
    created_at: '2026-10-19T00:00:00Z',
  })
  const tokenRow = {
    id: 'tok-1',
    name: 'github-actions',
    subject: 'admin',
    scopes: ['deploy'],
    project_id: '1',
    project_slug: 'default',
    created_at: '2026-10-19T00:00:00Z',
    expires_at: null,
    revoked_at: null,
  }

  // Stateful mock: PUT/DELETE on /members mutate the list the next GET returns.
  function mockApi(opts: { members?: unknown[]; tokens?: unknown[]; users?: unknown[] } = {}) {
    const members = [...(opts.members ?? [alice])] as Array<Record<string, unknown>>
    const fetchMock = vi
      .spyOn(globalThis, 'fetch')
      .mockImplementation(async (input?: unknown, init?: RequestInit) => {
        const url = String(input)
        const method = init?.method ?? 'GET'
        const memberMatch = url.match(/^\/v1\/projects\/default\/members\/([^/]+)$/)
        if (memberMatch && method === 'PUT') {
          const username = decodeURIComponent(memberMatch[1])
          const role = JSON.parse(String(init?.body)).role
          const row = { ...alice, user_id: `id-${username}`, username, display_name: null, role }
          const at = members.findIndex((m) => m.username === username)
          if (at >= 0) members[at] = row
          else members.push(row)
          return json(row)
        }
        if (memberMatch && method === 'DELETE') {
          const at = members.findIndex((m) => m.username === decodeURIComponent(memberMatch[1]))
          if (at >= 0) members.splice(at, 1)
          return new Response(null, { status: 204 })
        }
        if (url === '/v1/projects/default/members' && method === 'GET') {
          return json({ members })
        }
        if (url === '/v1/projects' && method === 'GET') return json({ projects: [] })
        if (url === '/v1/auth/tokens' && method === 'GET') return json({ tokens: opts.tokens ?? [] })
        if (url === '/v1/users' && method === 'GET') return json({ users: opts.users ?? [] })
        return json({})
      })
    return fetchMock
  }

  const sent = (fetchMock: ReturnType<typeof mockApi>, method: string) =>
    fetchMock.mock.calls.filter(([, init]) => (init as RequestInit | undefined)?.method === method)

  async function openDrawer() {
    render(
      <ProjectAccess
        me={me}
        activeProject="default"
        onSelectProject={vi.fn()}
        onSessionExpired={vi.fn()}
      />,
    )
    await userEvent.click(screen.getByRole('button', { name: /Project access & API tokens/i }))
  }

  it('adds a member with PUT /members/{username} and shows them in the list', async () => {
    const fetchMock = mockApi()
    await openDrawer()
    await screen.findByText('alice')

    await userEvent.type(screen.getByLabelText('Member username'), 'carol')
    await userEvent.selectOptions(screen.getByLabelText('Member role'), 'viewer')
    await userEvent.click(screen.getByRole('button', { name: 'Save member' }))

    const table = (await screen.findByText('carol')).closest('table') as HTMLElement
    expect(within(table).getByText('viewer')).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Members of default' })).toBeInTheDocument()

    const puts = sent(fetchMock, 'PUT')
    expect(puts).toHaveLength(1)
    expect(String(puts[0][0])).toBe('/v1/projects/default/members/carol')
    const init = puts[0][1] as RequestInit
    expect(JSON.parse(String(init.body))).toEqual({ role: 'viewer' })
    expect(
      sent(fetchMock, 'POST').filter(([url]) => String(url) === '/v1/projects/default/members'),
    ).toHaveLength(0)
  })

  it('asks before removing a member', async () => {
    const fetchMock = mockApi()
    await openDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'Remove alice' }))
    expect(screen.getByTestId('confirm-remove-member')).toHaveTextContent('Remove alice from default')
    expect(sent(fetchMock, 'DELETE')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Keep' }))
    expect(screen.queryByTestId('confirm-remove-member')).not.toBeInTheDocument()
    expect(sent(fetchMock, 'DELETE')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Remove alice' }))
    await userEvent.click(screen.getByRole('button', { name: 'Yes, remove alice' }))
    await waitFor(() => expect(sent(fetchMock, 'DELETE')).toHaveLength(1))
    expect(String(sent(fetchMock, 'DELETE')[0][0])).toBe('/v1/projects/default/members/alice')
    await waitFor(() =>
      expect(screen.queryByTestId('confirm-remove-member')).not.toBeInTheDocument(),
    )
  })

  it('asks before revoking a token', async () => {
    const fetchMock = mockApi({ tokens: [tokenRow] })
    await openDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'Revoke github-actions' }))
    expect(screen.getByTestId('confirm-revoke-token')).toHaveTextContent('Revoke github-actions?')
    expect(sent(fetchMock, 'POST')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Keep' }))
    expect(screen.queryByTestId('confirm-revoke-token')).not.toBeInTheDocument()
    expect(sent(fetchMock, 'POST')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Revoke github-actions' }))
    await userEvent.click(screen.getByRole('button', { name: 'Revoke token' }))
    await waitFor(() => expect(sent(fetchMock, 'POST')).toHaveLength(1))
    expect(String(sent(fetchMock, 'POST')[0][0])).toBe('/v1/auth/tokens/tok-1/revoke')
  })

  it('asks before disabling a user, but enables in one click', async () => {
    const fetchMock = mockApi({ users: [userRow(null)] })
    await openDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'Disable alice' }))
    expect(screen.getByTestId('confirm-disable-user')).toHaveTextContent('Disable alice?')
    expect(sent(fetchMock, 'PATCH')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Keep' }))
    expect(screen.queryByTestId('confirm-disable-user')).not.toBeInTheDocument()
    expect(sent(fetchMock, 'PATCH')).toHaveLength(0)

    await userEvent.click(screen.getByRole('button', { name: 'Disable alice' }))
    await userEvent.click(screen.getByRole('button', { name: 'Yes, disable alice' }))
    await waitFor(() => expect(sent(fetchMock, 'PATCH')).toHaveLength(1))
    const [url, init] = sent(fetchMock, 'PATCH')[0]
    expect(String(url)).toBe('/v1/users/alice')
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({ disabled: true })
  })

  it('enables a disabled user without asking', async () => {
    const fetchMock = mockApi({ users: [userRow('2026-10-19T01:00:00Z')] })
    await openDrawer()
    await userEvent.click(await screen.findByRole('button', { name: 'Enable alice' }))
    await waitFor(() => expect(sent(fetchMock, 'PATCH')).toHaveLength(1))
    expect(screen.queryByTestId('confirm-disable-user')).not.toBeInTheDocument()
    const init = sent(fetchMock, 'PATCH')[0][1] as RequestInit
    expect(JSON.parse(String(init.body))).toEqual({ disabled: false })
  })
})
