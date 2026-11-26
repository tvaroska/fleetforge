import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, type ProjectSummary } from './api'
import { ContinuousDeploymentCard } from './ContinuousDeploymentCard'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function makeProject(overrides: Partial<ProjectSummary> = {}): ProjectSummary {
  return {
    id: '00000000-0000-4000-8000-000000000001',
    slug: 'default',
    name: 'Default',
    description: null,
    role: 'owner',
    repo_url: 'https://github.com/org/firmware',
    track_mode: 'branch',
    track_ref: 'main',
    auto_deploy_target: 'dev',
    ci_webhook_url: 'https://api.github.com/repos/org/firmware/dispatches',
    has_ci_webhook_header: true,
    created_at: '2026-11-19T17:00:00Z',
    ...overrides,
  }
}

describe('ContinuousDeploymentCard', () => {
  it('does not save defaults over settings it failed to load', async () => {
    let rejectPending!: (err: unknown) => void
    const listSpy = vi.spyOn(api, 'listProjects').mockImplementationOnce(
      () =>
        new Promise((_, reject) => {
          rejectPending = reject
        }),
    )
    const updateSpy = vi.spyOn(api, 'updateProject').mockResolvedValue(makeProject())

    render(
      <ContinuousDeploymentCard
        projectSlug="default"
        onDeployed={() => {}}
        onSessionExpired={() => {}}
      />,
    )

    // While initial load is pending, save must stay disabled and form submit must be a no-op.
    const saveBtn = screen.getByRole('button', { name: /save & sync tracking/i })
    expect(saveBtn).toBeDisabled()
    expect(screen.getByTestId('cd-loading')).toBeInTheDocument()
    fireEvent.submit(saveBtn.closest('form')!)
    expect(updateSpy).not.toHaveBeenCalled()

    // Reject the initial load: error surfaces visibly and defaults still cannot be saved.
    rejectPending(new ApiError(500, 'database temporarily unavailable'))

    const alert = await screen.findByTestId('cd-error')
    expect(alert).toHaveAttribute('role', 'alert')
    expect(alert).toHaveTextContent('database temporarily unavailable')
    expect(saveBtn).toBeDisabled()
    fireEvent.click(saveBtn)
    fireEvent.submit(saveBtn.closest('form')!)
    expect(updateSpy).not.toHaveBeenCalled()

    // A response that omits the active project also refuses to save defaults.
    listSpy.mockResolvedValueOnce({ projects: [makeProject({ slug: 'other-project' })] })
    fireEvent.click(screen.getByTestId('cd-retry-btn'))

    await waitFor(() =>
      expect(screen.getByTestId('cd-error')).toHaveTextContent(/default/i),
    )
    expect(saveBtn).toBeDisabled()
    fireEvent.submit(saveBtn.closest('form')!)
    expect(updateSpy).not.toHaveBeenCalled()

    // Retry once the API recovers: settings hydrate and saving becomes enabled.
    listSpy.mockResolvedValueOnce({ projects: [makeProject()] })
    fireEvent.click(screen.getByTestId('cd-retry-btn'))

    await waitFor(() => expect(saveBtn).toBeEnabled())
    expect(screen.queryByTestId('cd-error')).not.toBeInTheDocument()
    expect(screen.getByLabelText('Git repository URL')).toHaveValue(
      'https://github.com/org/firmware',
    )
    expect(screen.getByLabelText('Track mode')).toHaveValue('branch')
    expect(screen.getByLabelText('Tracked ref')).toHaveValue('main')
  })

  it('hydrates project settings and saves updated tracking configuration', async () => {
    vi.spyOn(api, 'listProjects').mockResolvedValue({ projects: [makeProject()] })
    const updateSpy = vi.spyOn(api, 'updateProject').mockResolvedValue(
      makeProject({
        track_mode: 'tag',
        track_ref: 'v*',
        auto_deploy_target: 'all',
        auto_deployed_devices: ['a4cf12b3de01'],
      }),
    )
    const onDeployed = vi.fn()

    render(
      <ContinuousDeploymentCard
        projectSlug="default"
        onDeployed={onDeployed}
        onSessionExpired={() => {}}
      />,
    )

    const saveBtn = screen.getByRole('button', { name: /save & sync tracking/i })
    await waitFor(() => expect(saveBtn).toBeEnabled())

    fireEvent.change(screen.getByLabelText('Track mode'), { target: { value: 'tag' } })
    fireEvent.change(screen.getByLabelText('Tracked ref'), { target: { value: 'v*' } })
    fireEvent.change(screen.getByLabelText('Auto-deploy target'), { target: { value: 'all' } })
    fireEvent.click(saveBtn)

    await waitFor(() =>
      expect(updateSpy).toHaveBeenCalledWith('default', {
        repo_url: 'https://github.com/org/firmware',
        track_mode: 'tag',
        track_ref: 'v*',
        auto_deploy_target: 'all',
        ci_webhook_url: 'https://api.github.com/repos/org/firmware/dispatches',
      }),
    )
    expect(onDeployed).toHaveBeenCalledTimes(1)
    expect(await screen.findByTestId('cd-status')).toHaveTextContent(/a4cf12b3de01/)
  })

  it('triggers onSessionExpired on 401 load without flashing an error alert or enabling save', async () => {
    vi.spyOn(api, 'listProjects').mockRejectedValue(new ApiError(401, 'unauthorized'))
    const onSessionExpired = vi.fn()

    render(
      <ContinuousDeploymentCard
        projectSlug="default"
        onDeployed={() => {}}
        onSessionExpired={onSessionExpired}
      />,
    )

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('cd-error')).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /save & sync tracking/i })).toBeDisabled()
  })
})
