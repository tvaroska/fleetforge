import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api } from './api'
import { SimulationBadge } from './SimulationBadge'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

describe('SimulationBadge', () => {
  it('renders passed badge and triggers re-simulation on click', async () => {
    const spy = vi.spyOn(api, 'simulateArtifact').mockResolvedValue({
      sha256: 'a'.repeat(64),
      target: 'esp32s3',
      sim_status: 'passed',
      sim_detail: 'booted cleanly and passed R5 self-test',
      sim_ran_at: '2026-11-19T16:00:00Z',
    })
    const onSimulated = vi.fn()

    render(
      <SimulationBadge
        sha256={'a'.repeat(64)}
        projectSlug="rover"
        simStatus="passed"
        simDetail="booted cleanly"
        onSimulated={onSimulated}
        onSessionExpired={() => {}}
      />,
    )

    expect(screen.getByTestId('sim-passed-badge').textContent).toContain('sim passed')
    fireEvent.click(screen.getByTestId('run-sim-btn'))
    await waitFor(() => expect(spy).toHaveBeenCalledWith('a'.repeat(64), 'rover'))
    expect(onSimulated).toHaveBeenCalledTimes(1)
  })

  it('renders failed badge with failure detail', () => {
    render(
      <SimulationBadge
        sha256={'b'.repeat(64)}
        projectSlug="default"
        simStatus="failed"
        simDetail="Guru Meditation panic"
        onSimulated={() => {}}
        onSessionExpired={() => {}}
      />,
    )

    expect(screen.getByTestId('sim-failed-badge').textContent).toContain(
      '[! sim failed: Guru Meditation panic]',
    )
  })
})
