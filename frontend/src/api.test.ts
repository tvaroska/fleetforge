import { describe, expect, it, vi } from 'vitest'
import { api, ApiError } from './api'

function respond(status: number, body: string) {
  return vi.spyOn(globalThis, 'fetch').mockResolvedValue(new Response(body, { status }))
}

async function rejection(promise: Promise<unknown>): Promise<ApiError> {
  try {
    await promise
  } catch (err) {
    expect(err).toBeInstanceOf(ApiError)
    return err as ApiError
  }
  throw new Error('expected the call to reject')
}

// The 422 and 409 bodies below were captured from the dev API (R2b-be-1) with
// `curl -X PATCH /v1/devices/0000000fe601`, not written by hand.
const REAL_422_NAME_TOO_LONG =
  '{"detail":[{"type":"value_error","loc":["body","name"],"msg":"Value error, a name is at most 64 characters","input":"aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa","ctx":{"error":{}}}]}'
const REAL_409_NAME_TAKEN = '{"detail":"name already used by device 0000000fe602: Hen House"}'

describe('api.updateDevice', () => {
  it('sends an upper-case PATCH with exactly the keys given', async () => {
    const row = { device_id: 'A4CF12B3DE90', name: 'coop door' }
    const spy = respond(200, JSON.stringify(row))

    await expect(api.updateDevice('A4CF12B3DE90', { name: 'coop door' })).resolves.toEqual(row)

    const [url, init] = spy.mock.calls[0]
    expect(url).toBe('/v1/devices/A4CF12B3DE90')
    expect(init?.method).toBe('PATCH')
    expect(init?.body).toBe('{"name":"coop door"}')
    expect(init?.body).not.toContain('group_id')
    expect(init?.credentials).toBe('same-origin')
    expect(init?.headers).toMatchObject({ 'content-type': 'application/json' })
  })

  it('reads the FastAPI 422 array as one readable sentence', async () => {
    respond(422, REAL_422_NAME_TOO_LONG)
    const err = await rejection(api.updateDevice('0000000fe601', { name: 'a'.repeat(65) }))
    expect(err.status).toBe(422)
    expect(err.message).toBe('a name is at most 64 characters')
  })

  it('joins several validation messages with a semicolon', async () => {
    respond(
      422,
      JSON.stringify({
        detail: [
          { type: 'value_error', loc: ['body', 'name'], msg: 'Value error, first' },
          { type: 'extra_forbidden', loc: ['body', 'tag'], msg: 'Extra inputs are not permitted' },
        ],
      }),
    )
    const err = await rejection(api.updateDevice('0000000fe601', {}))
    expect(err.message).toBe('first; Extra inputs are not permitted')
  })

  it('passes a string detail (409) through verbatim', async () => {
    respond(409, REAL_409_NAME_TAKEN)
    const err = await rejection(api.updateDevice('0000000fe601', { name: 'Hen House' }))
    expect(err.status).toBe(409)
    expect(err.message).toBe('name already used by device 0000000fe602: Hen House')
  })

  it('falls back to the raw body when an array detail has no string msg', async () => {
    const body = '{"detail":[{"type":"x","loc":["body"]}]}'
    respond(422, body)
    const err = await rejection(api.updateDevice('0000000fe601', {}))
    expect(err.message).toBe(body)
  })

  it('reports a transport failure as unreachable', async () => {
    vi.spyOn(globalThis, 'fetch').mockRejectedValue(new TypeError('offline'))
    const err = await rejection(api.updateDevice('0000000fe601', { name: 'x' }))
    expect(err.status).toBe(0)
    expect(err.message).toBe('the API is unreachable')
  })
})
