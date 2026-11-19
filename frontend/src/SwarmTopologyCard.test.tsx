import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type ArtifactSummary } from './api'
import { type Artifacts } from './deploy'
import { SwarmTopologyCard } from './SwarmTopologyCard'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function fakeArtifacts(items: ArtifactSummary[]): Artifacts {
  const byTarget = new Map<string, ArtifactSummary[]>()
  for (const item of items) {
    const list = byTarget.get(item.target) ?? []
    list.push(item)
    byTarget.set(item.target, list)
  }
  return { byTarget, loaded: true, error: null, reload: () => {} }
}

describe('SwarmTopologyCard', () => {
  it('renders UGV -> child drone hierarchy and generates binary delta patches', async () => {
    const shaA = '1'.repeat(64)
    const shaB = '2'.repeat(64)
    const patchSha = '3'.repeat(64)

    const topoSpy = vi.spyOn(api, 'getSwarmTopology').mockResolvedValue({
      project: 'default',
      direct_device_count: 1,
      gateways: [
        {
          gateway_id: 'a4cf12b30001',
          name: 'ugv-carrier-01',
          platform_type: 'esp32s3',
          fw_version: '2.0.0',
          online: true,
          relay_base_url: 'http://192.168.4.1:8080',
          cached_artifacts: [shaB],
          children: [
            {
              device_id: 'a4cf12b30002',
              name: 'drone-alpha',
              platform_type: 'esp32c6',
              fw_version: '1.0.0',
              link_type: 'wifi',
              online: true,
              has_delta_capability: true,
            },
          ],
        },
      ],
      deltas: [],
    })
    const deltaSpy = vi.spyOn(api, 'createArtifactDelta').mockResolvedValue({
      from_sha256: shaA,
      to_sha256: shaB,
      patch_sha256: patchSha,
      patch_size_bytes: 1840,
      target_size_bytes: 131072,
      savings_pct: 98.6,
      format: 'ffdelta-v1',
      created_at: '2026-11-19T17:30:00Z',
    })

    const artifacts = fakeArtifacts([
      {
        target: 'esp32c6',
        version: '1.0.0',
        sha256: shaA,
        size_bytes: 131072,
        partition_layout: 'ab-4m-v1',
        kind: 'user_firmware',
        created_at: '2026-11-19T17:00:00Z',
      },
      {
        target: 'esp32c6',
        version: '1.1.0',
        sha256: shaB,
        size_bytes: 131072,
        partition_layout: 'ab-4m-v1',
        kind: 'user_firmware',
        created_at: '2026-11-19T17:10:00Z',
      },
    ])

    render(
      <SwarmTopologyCard
        projectSlug="default"
        active={true}
        artifacts={artifacts}
        onSessionExpired={() => {}}
      />,
    )

    await waitFor(() => expect(topoSpy).toHaveBeenCalledWith('default'))
    expect(screen.getByTestId('swarm-gateway-row').textContent).toContain('ugv-carrier-01')
    expect(screen.getByTestId('swarm-gateway-row').textContent).toContain('drone-alpha [Δ]')

    fireEvent.change(screen.getByLabelText('Delta base artifact'), { target: { value: shaA } })
    fireEvent.change(screen.getByLabelText('Delta target artifact'), { target: { value: shaB } })
    fireEvent.click(screen.getByTestId('generate-delta-btn'))

    await waitFor(() => expect(deltaSpy).toHaveBeenCalledWith(shaB, shaA, undefined))
    expect(screen.getByTestId('swarm-delta-status').textContent).toContain('98.6% airtime saved')
  })
})
