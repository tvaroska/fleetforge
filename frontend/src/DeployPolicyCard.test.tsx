import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type DeployPolicySummary, type DeviceGroupSummary } from './api'
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
  it('creates a multi-ring policy and surfaces halted canary evaluation reasons', async () => {
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

    await waitFor(() => expect(createSpy).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('deploy-policy-row').textContent).toContain('canary → prod')

    fireEvent.change(screen.getByLabelText('Evaluate version for canary-to-prod'), {
      target: { value: '2.0.0' },
    })
    fireEvent.click(screen.getByTestId('evaluate-policy-btn'))

    await waitFor(() =>
      expect(evalSpy).toHaveBeenCalledWith('default', SAMPLE_POLICY.id, '2.0.0'),
    )
    expect(screen.getByTestId('policy-eval-summary').textContent).toContain(
      'ring 0 halted: device a4cf12b3de01 rolled_back on version 2.0.0',
    )
  })
})
