import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, api, type DeployPolicySummary, type DeviceGroupSummary } from './api'
import { DeployPolicyCard } from './DeployPolicyCard'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

const GROUPS: DeviceGroupSummary[] = [
  {
    id: '00000000-0000-4000-8000-000000000010',
    project_id: '00000000-0000-4000-8000-000000000001',
    name: 'canary',
    description: null,
    created_at: '2026-11-19T00:00:00Z',
    updated_at: '2026-11-19T00:00:00Z',
  },
  {
    id: '00000000-0000-4000-8000-000000000020',
    project_id: '00000000-0000-4000-8000-000000000001',
    name: 'prod',
    description: null,
    created_at: '2026-11-19T00:00:00Z',
    updated_at: '2026-11-19T00:00:00Z',
  },
]

const SAMPLE_POLICY: DeployPolicySummary = {
  id: '00000000-0000-4000-8000-000000000099',
  project_id: '00000000-0000-4000-8000-000000000001',
  name: 'canary-to-prod',
  match_mode: 'tag',
  match_ref: 'v*',
  ring_group_ids: [GROUPS[0]!.id, GROUPS[1]!.id],
  max_parallel: 5,
  require_sim_passed: true,
  min_soak_s: 60,
  min_free_heap_bytes: 65536,
  enabled: true,
  created_at: '2026-11-19T17:00:00Z',
  updated_at: '2026-11-19T17:00:00Z',
}

describe('DeployPolicyCard', () => {
  it('creates a multi-ring policy, confirms before evaluating, and surfaces halted canary reasons', async () => {
    const listSpy = vi.spyOn(api, 'listPolicies').mockResolvedValue({ policies: [] })
    const createSpy = vi.spyOn(api, 'createPolicy').mockResolvedValue(SAMPLE_POLICY)
    const evalSpy = vi.spyOn(api, 'evaluatePolicy').mockResolvedValue({
      policy_id: SAMPLE_POLICY.id,
      version: '2.0.0',
      status: 'halted',
      halt_reason: 'ring 0 halted: device a4cf12b3de01 rolled_back on version 2.0.0',
      rings: [
        {
          ring_index: 0,
          group_id: GROUPS[0]!.id,
          status: 'halted',
          total_devices: 1,
          confirmed_devices: 0,
          in_flight_devices: 0,
        },
        {
          ring_index: 1,
          group_id: GROUPS[1]!.id,
          status: 'pending',
          total_devices: 2,
          confirmed_devices: 0,
          in_flight_devices: 0,
        },
      ],
      deployed: [],
    })

    const { rerender } = render(
      <DeployPolicyCard
        projectSlug="default"
        active={false}
        groups={GROUPS}
        onDeployed={() => {}}
        onSessionExpired={() => {}}
      />,
    )
    expect(listSpy).not.toHaveBeenCalled()

    rerender(
      <DeployPolicyCard
        projectSlug="default"
        active={true}
        groups={GROUPS}
        onDeployed={() => {}}
        onSessionExpired={() => {}}
      />,
    )
    await waitFor(() => expect(listSpy).toHaveBeenCalledWith('default'))

    fireEvent.change(screen.getByLabelText('Policy name'), { target: { value: 'canary-to-prod' } })
    fireEvent.change(screen.getByLabelText('Add cohort ring'), { target: { value: GROUPS[0]!.id } })
    fireEvent.change(screen.getByLabelText('Add cohort ring'), { target: { value: GROUPS[1]!.id } })
    fireEvent.click(screen.getByTestId('create-policy-btn'))

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith('default', {
        name: 'canary-to-prod',
        match_mode: 'tag',
        match_ref: 'v*',
        ring_group_ids: [GROUPS[0]!.id, GROUPS[1]!.id],
        max_parallel: 5,
        min_soak_s: 0,
        require_sim_passed: false,
      }),
    )
    expect(screen.getByTestId('deploy-policy-row').textContent).toContain('canary → prod')

    fireEvent.change(screen.getByLabelText('Evaluate version for canary-to-prod'), {
      target: { value: '2.0.0' },
    })

    // First click requests inline confirmation without staging firmware.
    fireEvent.click(screen.getByTestId('evaluate-policy-btn'))
    expect(evalSpy).not.toHaveBeenCalled()
    expect(screen.getByTestId('confirm-evaluate-policy')).toBeInTheDocument()

    // Cancel aborts evaluation without calling the API.
    fireEvent.click(screen.getByTestId('cancel-evaluate-policy-btn'))
    expect(screen.queryByTestId('confirm-evaluate-policy')).not.toBeInTheDocument()
    expect(evalSpy).not.toHaveBeenCalled()

    // Confirming dispatches api.evaluatePolicy with the exact arguments.
    fireEvent.click(screen.getByTestId('evaluate-policy-btn'))
    fireEvent.click(screen.getByTestId('confirm-evaluate-policy-btn'))

    await waitFor(() =>
      expect(evalSpy).toHaveBeenCalledWith('default', SAMPLE_POLICY.id, '2.0.0'),
    )
    expect(screen.queryByTestId('confirm-evaluate-policy')).not.toBeInTheDocument()
    expect(screen.getByTestId('policy-eval-summary').textContent).toContain(
      'ring 0 halted: device a4cf12b3de01 rolled_back on version 2.0.0',
    )
  })

  it('sends the exact evaluate POST request body after confirmation', async () => {
    let evaluateUrl: string | null = null
    let evaluateBody: Record<string, unknown> | null = null
    vi.spyOn(globalThis, 'fetch').mockImplementation(async (input?: unknown, init?: RequestInit) => {
      const url = String(input)
      if (url === '/v1/projects/default/policies' && (!init?.method || init.method === 'GET')) {
        return new Response(JSON.stringify({ policies: [SAMPLE_POLICY] }), {
          status: 200,
          headers: { 'content-type': 'application/json' },
        })
      }
      if (url.endsWith('/evaluate') && init?.method === 'POST') {
        evaluateUrl = url
        evaluateBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return new Response(
          JSON.stringify({
            policy_id: SAMPLE_POLICY.id,
            version: '2.0.1',
            status: 'in_progress',
            halt_reason: null,
            rings: [],
            deployed: [],
          }),
          { status: 200, headers: { 'content-type': 'application/json' } },
        )
      }
      return new Response(JSON.stringify({}), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      })
    })

    render(
      <DeployPolicyCard
        projectSlug="default"
        active={true}
        groups={GROUPS}
        onDeployed={() => {}}
        onSessionExpired={() => {}}
      />,
    )

    await waitFor(() => expect(screen.getByTestId('deploy-policy-row')).toBeInTheDocument())
    fireEvent.change(screen.getByLabelText('Evaluate version for canary-to-prod'), {
      target: { value: ' 2.0.1 ' },
    })
    fireEvent.click(screen.getByTestId('evaluate-policy-btn'))
    expect(evaluateUrl).toBeNull()

    fireEvent.click(screen.getByTestId('confirm-evaluate-policy-btn'))
    await waitFor(() => expect(evaluateUrl).not.toBeNull())
    expect(evaluateUrl).toBe(`/v1/projects/default/policies/${SAMPLE_POLICY.id}/evaluate`)
    expect(evaluateBody).toEqual({ version: '2.0.1' })
  })

  it('surfaces load errors via role="alert" with Retry and expires session on 401', async () => {
    const listSpy = vi
      .spyOn(api, 'listPolicies')
      .mockRejectedValueOnce(new ApiError(500, 'policy query timed out'))
      .mockResolvedValueOnce({ policies: [SAMPLE_POLICY] })
    const onSessionExpired = vi.fn()

    const { rerender } = render(
      <DeployPolicyCard
        projectSlug="default"
        active={true}
        groups={GROUPS}
        onDeployed={() => {}}
        onSessionExpired={onSessionExpired}
      />,
    )

    const alert = await screen.findByTestId('policy-error')
    expect(alert).toHaveAttribute('role', 'alert')
    expect(alert).toHaveTextContent('policy query timed out')
    expect(screen.queryByTestId('policy-empty')).not.toBeInTheDocument()

    fireEvent.click(screen.getByTestId('policy-retry-btn'))
    await waitFor(() => expect(screen.getByTestId('deploy-policy-row')).toBeInTheDocument())
    expect(listSpy).toHaveBeenCalledTimes(2)
    expect(screen.queryByTestId('policy-error')).not.toBeInTheDocument()

    listSpy.mockRejectedValueOnce(new ApiError(401, 'unauthorized'))
    rerender(
      <DeployPolicyCard
        projectSlug="other-project"
        active={true}
        groups={GROUPS}
        onDeployed={() => {}}
        onSessionExpired={onSessionExpired}
      />,
    )
    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
  })
})
