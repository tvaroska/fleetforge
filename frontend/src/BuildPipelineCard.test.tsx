import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { api, type BuildJobSummary } from './api'
import { BuildPipelineCard } from './BuildPipelineCard'
import { type EventSourceLike } from './fleet'

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
})

function makeJob(overrides: Partial<BuildJobSummary> = {}): BuildJobSummary {
  return {
    id: '11111111-2222-3333-4444-555555555555',
    project_id: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee',
    repo_url: 'https://github.com/example/fw',
    git_ref: 'v1.4.0',
    commit_sha: null,
    target: 'esp32s3',
    version: '1.4.0',
    partition_layout: 'ab-4m-v1',
    idf_image: 'espressif/idf:v5.3.2@sha256:6b21a5b7',
    cache_key: null,
    status: 'queued',
    cache_hit: false,
    artifact_sha256: null,
    error_detail: null,
    triggered_by: 'admin',
    created_at: '2026-11-19T17:00:00Z',
    started_at: null,
    finished_at: null,
    ...overrides,
  }
}

describe('BuildPipelineCard', () => {
  it('does not fetch when inactive, then queues a build and streams SSE logs to completion', async () => {
    const listSpy = vi.spyOn(api, 'listBuilds').mockResolvedValue({ builds: [] })
    const createSpy = vi.spyOn(api, 'createBuild').mockResolvedValue(makeJob())
    const onArtifactsChanged = vi.fn()

    let capturedEs: EventSourceLike | null = null
    const createEs = (url: string): EventSourceLike => {
      const es: EventSourceLike = {
        readyState: 1,
        onopen: null,
        onmessage: null,
        onerror: null,
        close: vi.fn(),
      }
      expect(url).toContain('/v1/projects/default/builds/11111111-2222-3333-4444-555555555555/logs/stream')
      capturedEs = es
      return es
    }

    const { rerender } = render(
      <BuildPipelineCard
        projectSlug="default"
        active={false}
        profiles={null}
        onArtifactsChanged={onArtifactsChanged}
        onSessionExpired={() => {}}
        createEventSource={createEs}
      />,
    )
    expect(listSpy).not.toHaveBeenCalled()

    rerender(
      <BuildPipelineCard
        projectSlug="default"
        active={true}
        profiles={null}
        onArtifactsChanged={onArtifactsChanged}
        onSessionExpired={() => {}}
        createEventSource={createEs}
      />,
    )
    await waitFor(() => expect(listSpy).toHaveBeenCalledWith('default'))

    fireEvent.change(screen.getByLabelText('Build git ref'), { target: { value: 'v1.4.0' } })
    fireEvent.change(screen.getByLabelText('Build version'), { target: { value: '1.4.0' } })
    fireEvent.click(screen.getByTestId('queue-build-btn'))

    await waitFor(() =>
      expect(createSpy).toHaveBeenCalledWith('default', {
        git_ref: 'v1.4.0',
        target: 'esp32s3',
        version: '1.4.0',
        partition_layout: 'ab-4m-v1',
      }),
    )

    expect(capturedEs).not.toBeNull()
    await act(async () => {
      capturedEs!.onmessage?.(
        new MessageEvent('message', {
          data: JSON.stringify({
            status: 'running',
            chunk: '[1/3] Compiling esp32s3\n',
            done: false,
          }),
        }),
      )
    })
    expect(screen.getByTestId('build-log-pre').textContent).toContain('[1/3] Compiling esp32s3')

    listSpy.mockResolvedValueOnce({
      builds: [
        makeJob({
          status: 'succeeded',
          cache_hit: true,
          artifact_sha256: 'f'.repeat(64),
        }),
      ],
    })
    await act(async () => {
      capturedEs!.onmessage?.(
        new MessageEvent('message', {
          data: JSON.stringify({
            status: 'succeeded',
            chunk: '[artifact] stored signed .bin\n',
            done: true,
            artifact_sha256: 'f'.repeat(64),
          }),
        }),
      )
    })

    await waitFor(() => expect(onArtifactsChanged).toHaveBeenCalledTimes(1))
    expect(screen.getByTestId('build-log-pre').textContent).toContain('[artifact] stored signed .bin')
  })

  it('supports cancelling a queued build and retrying a failed build', async () => {
    vi.spyOn(api, 'listBuilds').mockResolvedValue({
      builds: [
        makeJob({ id: 'job-queued', status: 'queued', version: '1.4.1' }),
        makeJob({
          id: 'job-failed',
          status: 'failed',
          version: '1.4.2',
          error_detail: 'compile error in main.c',
        }),
      ],
    })
    const cancelSpy = vi
      .spyOn(api, 'cancelBuild')
      .mockResolvedValue(makeJob({ id: 'job-queued', status: 'cancelled', version: '1.4.1' }))
    const retrySpy = vi
      .spyOn(api, 'retryBuild')
      .mockResolvedValue(makeJob({ id: 'job-retried', status: 'queued', version: '1.4.2' }))
    vi.spyOn(api, 'getBuild').mockResolvedValue({
      ...makeJob({ id: 'job-retried', status: 'queued', version: '1.4.2' }),
      log_text: '',
    })

    render(
      <BuildPipelineCard
        projectSlug="default"
        active={true}
        profiles={null}
        onArtifactsChanged={() => {}}
        onSessionExpired={() => {}}
      />,
    )

    await waitFor(() => expect(screen.getAllByTestId('build-job-row')).toHaveLength(2))
    fireEvent.click(screen.getByTestId('cancel-build-btn'))
    await waitFor(() => expect(cancelSpy).toHaveBeenCalledWith('default', 'job-queued'))

    const retryButtons = screen.getAllByTestId('retry-build-btn')
    fireEvent.click(retryButtons[retryButtons.length - 1]!)
    await waitFor(() => expect(retrySpy).toHaveBeenCalledWith('default', 'job-failed'))
  })
})
