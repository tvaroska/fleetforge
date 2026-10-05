// What the Fleet row says about the network (R2b-fe-13, Flow 3 step 5). Pure, no React.
//
// `ssid` and `known_networks` are the last values the board announced (R2b-be-5); they
// persist after it goes offline. An offline board therefore reads "last on: shed", never
// a cause: the server cannot tell out of range from powered off (R2b-spec-1). Only the
// result card, which reads the console, can say "none of its 2 known networks is in range".
//
// The SSID is device-controlled text. Render it inside `<bdi>` so a bidi control in it
// cannot reorder the rest of the row.

import { type DeviceSummary } from './api'

export type NetworkPlace = { label: 'on:' | 'last on:'; ssid: string; current: boolean }

export const LAST_ON_TITLE =
  'The network it was on when it was last seen. The server cannot tell out of range from powered off.'

export function networkPlace(d: Pick<DeviceSummary, 'online' | 'ssid'>): NetworkPlace | null {
  if (d.ssid === null) return null
  return { label: d.online ? 'on:' : 'last on:', ssid: d.ssid, current: d.online }
}

export function knowsNetworks(n: number | null): string | null {
  if (n === null) return null
  return `knows ${n} ${n === 1 ? 'network' : 'networks'}`
}
