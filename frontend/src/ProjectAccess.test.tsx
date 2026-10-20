import { render, screen, waitFor } from '@testing-library/react'
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
})
