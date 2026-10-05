import { describe, expect, it } from 'vitest'
import { knowsNetworks, networkPlace } from './network'

describe('networkPlace', () => {
  it('is "on:" for an online board and "last on:" for an offline one', () => {
    expect(networkPlace({ online: true, ssid: 'shed' })).toEqual({ label: 'on:', ssid: 'shed', current: true })
    expect(networkPlace({ online: false, ssid: 'shed' })).toEqual({ label: 'last on:', ssid: 'shed', current: false })
  })
  it('is null when the board reported no ssid', () => {
    expect(networkPlace({ online: true, ssid: null })).toBeNull()
  })
})

describe('knowsNetworks', () => {
  it.each([
    [null, null],
    [0, 'knows 0 networks'],
    [1, 'knows 1 network'],
    [2, 'knows 2 networks'],
    [4, 'knows 4 networks'],
  ])('%s -> %s', (n, text) => {
    expect(knowsNetworks(n)).toBe(text)
  })
})
