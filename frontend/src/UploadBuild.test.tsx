// The "Upload a build" form (R2b-fe-7): header pre-fill, the raw-body POST, the server's
// sentences rendered verbatim, and the browser-side chip-mismatch refusal.

import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type DeviceSummary } from './api'
import { UploadBuild, uploadErrorMessage } from './UploadBuild'

function appBin(chip: number, version: string): File {
  const bytes = new Uint8Array(1024)
  const view = new DataView(bytes.buffer)
  bytes[0] = 0xe9
  view.setUint16(12, chip, true)
  view.setUint32(0x20, 0xabcd5432, true)
  bytes.set(new TextEncoder().encode(version), 0x30)
  bytes.set(new TextEncoder().encode('fleetforge-agent'), 0x50)
  return new File([bytes], 'app.bin')
}

const C6 = 13
const S3 = 9

function board(over: Partial<DeviceSummary> = {}): DeviceSummary {
  return { device_id: 'a4cf12b3de90', platform_type: 'esp32c6', partition_layout: 'ab-4m-v1', ...over } as DeviceSummary
}

function json(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  })
}

const uploaded = (over: object = {}) => ({
  sha256: 'a'.repeat(64),
  size_bytes: 1024,
  target: 'esp32c6',
  version: '1.6.0',
  partition_layout: 'ab-4m-v1',
  created: true,
  ...over,
})

function setup(devices: DeviceSummary[] | null = [board()]) {
  const onUploaded = vi.fn()
  const onSessionExpired = vi.fn()
  render(
    <UploadBuild devices={devices} onUploaded={onUploaded} onSessionExpired={onSessionExpired} />,
  )
  return { onUploaded, onSessionExpired }
}

async function pickFile(file: File) {
  await userEvent.upload(screen.getByLabelText(/Build \(\.bin\)/), file)
}

const button = () => screen.getByRole('button', { name: /^Upload/ })

afterEach(() => vi.restoreAllMocks())

describe('UploadBuild', () => {
  it('pre-fills target and version from the image header', async () => {
    setup([board(), board({ platform_type: 'esp32s3', device_id: 'b' })])
    await pickFile(appBin(C6, '1.6.0'))

    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.6.0'))
    expect(screen.getByLabelText('Chip target')).toHaveValue('esp32c6')
    expect(screen.getByTestId('upload-file-info')).toHaveTextContent(
      'app.bin — 1,024 bytes · built as fleetforge-agent 1.6.0 for esp32c6',
    )
  })

  it('posts the raw file with query metadata only, then reports and calls back', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(uploaded(), 201))
    const { onUploaded } = setup()
    const file = appBin(C6, '1.6.0')
    await pickFile(file)
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.6.0'))
    await userEvent.click(button())

    expect(await screen.findByTestId('upload-result')).toHaveTextContent(
      'Uploaded esp32c6 1.6.0 (1024 bytes). It is in the Deploy list of every esp32c6 board.',
    )
    expect(fetchMock).toHaveBeenCalledTimes(1)
    const [url, init] = fetchMock.mock.calls[0]
    expect(url).toBe('/v1/artifact?target=esp32c6&version=1.6.0&partition_layout=ab-4m-v1')
    expect(init?.method).toBe('POST')
    expect(init?.body).toBe(file)
    expect(init?.headers).toEqual({ 'content-type': 'application/octet-stream' })
    expect(onUploaded).toHaveBeenCalledTimes(1)
    expect(screen.getByLabelText('Version')).toHaveValue('')
  })

  it('says when no board of that chip is on the fleet yet', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(uploaded({ target: 'esp32c3' }), 201))
    setup([board()])
    await pickFile(appBin(5, '1.0.0'))
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.0.0'))
    await userEvent.click(button())

    expect(await screen.findByTestId('upload-result')).toHaveTextContent(
      'No esp32c3 board is in the fleet yet',
    )
  })

  it('reports an idempotent repeat as already uploaded', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json(uploaded({ created: false })))
    setup()
    await pickFile(appBin(C6, '1.6.0'))
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.6.0'))
    await userEvent.click(button())

    expect(await screen.findByTestId('upload-result')).toHaveTextContent(
      'Already uploaded: these exact bytes are esp32c6 1.6.0. Nothing changed.',
    )
  })

  it("renders the server's 409 sentence verbatim and does not call back", async () => {
    const detail =
      'esp32c6 version 1.5.0 already names artifact aaaa. A version label is not re-pointed; upload the new image under a new version.'
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail }, 409))
    const { onUploaded } = setup()
    await pickFile(appBin(C6, '1.5.0'))
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.5.0'))
    await userEvent.click(button())

    expect(await screen.findByTestId('upload-error')).toHaveTextContent(detail)
    expect(onUploaded).not.toHaveBeenCalled()
  })

  it('replaces a proxy HTML 413 with a plain sentence naming the size', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(
      new Response('<html><body>413 Request Entity Too Large</body></html>', { status: 413 }),
    )
    const file = appBin(C6, '1.7.0')
    setup()
    await pickFile(file)
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.7.0'))
    await userEvent.click(button())

    const error = await screen.findByTestId('upload-error')
    expect(error).toHaveTextContent('This file (1,024 bytes) is larger than the server accepts.')
    expect(error.textContent).not.toContain('<')
  })

  it('bounces to the login gate on a 401, with no error paragraph', async () => {
    vi.spyOn(globalThis, 'fetch').mockResolvedValue(json({ detail: 'no session' }, 401))
    const { onSessionExpired } = setup()
    await pickFile(appBin(C6, '1.6.0'))
    await waitFor(() => expect(screen.getByLabelText('Version')).toHaveValue('1.6.0'))
    await userEvent.click(button())

    await waitFor(() => expect(onSessionExpired).toHaveBeenCalledTimes(1))
    expect(screen.queryByTestId('upload-error')).toBeNull()
  })

  it('refuses in the browser a file built for another chip, and sends nothing', async () => {
    const fetchMock = vi.spyOn(globalThis, 'fetch')
    setup([board(), board({ platform_type: 'esp32s3', device_id: 'b' })])
    await pickFile(appBin(S3, '1.6.0'))
    await waitFor(() => expect(screen.getByLabelText('Chip target')).toHaveValue('esp32s3'))
    await userEvent.selectOptions(screen.getByLabelText('Chip target'), 'esp32c6')

    expect(screen.getByTestId('upload-target-mismatch')).toHaveTextContent(
      'This file was built for esp32s3',
    )
    expect(button()).toBeDisabled()
    expect(fetchMock).not.toHaveBeenCalled()
  })

  it('says a file with no ESP header was not read, and pre-fills nothing', async () => {
    setup([board(), board({ platform_type: 'esp32s3', device_id: 'b' })])
    await pickFile(new File([new Uint8Array(4096).fill(7)], 'x.bin'))

    expect(await screen.findByText(/Not recognised as an ESP-IDF app image/)).toBeInTheDocument()
    expect(screen.getByLabelText('Version')).toHaveValue('')
    expect(screen.getByLabelText('Chip target')).toHaveValue('')
    expect(button()).toBeDisabled()
  })

  it("defaults the layout to the one the chip's boards report", async () => {
    setup([board({ platform_type: 'esp32s3', partition_layout: 'ab-4m-arduino-v1' })])
    await pickFile(appBin(S3, '1.0.0'))

    await waitFor(() => expect(screen.getByLabelText('Chip target')).toHaveValue('esp32s3'))
    expect(screen.getByLabelText('Partition layout')).toHaveValue('ab-4m-arduino-v1')
  })

  it('offers fleet targets and the agent targets, as a select not free text', () => {
    setup([board({ platform_type: 'esp32h2' })])
    const select = screen.getByLabelText('Chip target')
    expect(select.tagName).toBe('SELECT')
    expect(Array.from(select.querySelectorAll('option')).map((o) => o.value)).toEqual([
      '',
      'esp32',
      'esp32c3',
      'esp32c6',
      'esp32h2',
      'esp32s3',
    ])
    // One fleet chip: it is the default.
    expect(select).toHaveValue('esp32h2')
  })
})

describe('uploadErrorMessage', () => {
  it('keeps a server sentence and an unreachable API as they are', () => {
    expect(uploadErrorMessage(new ApiError(409, 'already names it'), 5)).toBe('already names it')
    expect(uploadErrorMessage(new ApiError(0, 'the API is unreachable'), 5)).toBe(
      'the API is unreachable',
    )
  })

  it('names the status for an empty or HTML body it cannot explain', () => {
    expect(uploadErrorMessage(new ApiError(502, '<html>bad gateway</html>'), 5)).toBe(
      'The server refused the upload (HTTP 502); try again, and if it repeats check the server log.',
    )
    expect(uploadErrorMessage(new ApiError(500, 'HTTP 500'), 5)).toContain('HTTP 500')
  })
})
