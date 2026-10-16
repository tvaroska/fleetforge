// What these tests defend (R5-fe-1):
//
// 1. A verb is offered only to a board that announces it, and only on the transaction
//    states where it means something. A legacy `['ota']` board is offered nothing.
// 2. Cancel is never offered on a rollback wait, nor before the board has the command.
// 3. The last-command line never expires a missing answer, renders device words as text
//    (prototype keys included) and never prints `null` / `undefined` / `NaN`.

import { describe, expect, it } from 'vitest'
import { type CommandSummary, type DeploySummary, type DeviceSummary } from './api'
import { commandAcceptedMessage, lastCommandLine, offeredCommands } from './commands'

const NOW = Date.parse('2026-09-10T12:00:00Z')
const ALL = ['ota', 'apply', 'cancel', 'rollback', 'reboot', 'selftest']

function deploy(overrides: Partial<DeploySummary> = {}): DeploySummary {
  return {
    cmd_id: 'cmd-a',
    state: 'staged',
    at: '2026-09-10T11:59:40Z',
    is_terminal: false,
    artifact_version: '1.5.0',
    from_version: '1.4.2',
    pct: null,
    detail: null,
    ...overrides,
  }
}

const board = (capabilities: string[], d: DeploySummary | null): Pick<DeviceSummary, 'capabilities' | 'deploy'> => ({
  capabilities,
  deploy: d,
})

function command(overrides: Partial<CommandSummary> = {}): CommandSummary {
  return {
    command_id: 'c0ffee',
    type: 'apply',
    cmd_id: 'cmd-a',
    sent_at: '2026-09-10T11:59:50Z',
    sent_by: { subject: 'admin', credential: null },
    result: null,
    detail: null,
    answered_at: null,
    ...overrides,
  }
}

describe('offeredCommands', () => {
  it('offers Apply, Cancel and Reboot on a held stage of a capable board', () => {
    expect(offeredCommands(board(ALL, deploy()))).toEqual({ apply: 'cmd-a', cancel: 'cmd-a', reboot: true })
  })

  it('offers nothing to a legacy board, whatever the state', () => {
    for (const state of ['staged', 'downloading', 'awaiting_safe_window']) {
      expect(offeredCommands(board(['ota'], deploy({ state })))).toEqual({ apply: null, cancel: null, reboot: false })
    }
    expect(offeredCommands(board(['ota'], null))).toEqual({ apply: null, cancel: null, reboot: false })
  })

  it('gates each verb on its own capability', () => {
    expect(offeredCommands(board(['ota', 'apply'], deploy()))).toEqual({ apply: 'cmd-a', cancel: null, reboot: false })
    expect(offeredCommands(board(['ota', 'cancel'], deploy()))).toEqual({ apply: null, cancel: 'cmd-a', reboot: false })
    expect(offeredCommands(board(['ota', 'reboot'], deploy()))).toEqual({ apply: null, cancel: null, reboot: true })
  })

  it('offers Reboot with no deploy at all, and nothing else', () => {
    expect(offeredCommands(board(ALL, null))).toEqual({ apply: null, cancel: null, reboot: true })
  })

  it('offers neither Apply nor Cancel on a terminal deploy or one with no cmd_id', () => {
    for (const d of [
      deploy({ is_terminal: true }),
      deploy({ state: 'cancelled', is_terminal: true }),
      deploy({ cmd_id: null }),
      deploy({ cmd_id: '' }),
    ]) {
      const offer = offeredCommands(board(ALL, d))
      expect(offer.apply).toBeNull()
      expect(offer.cancel).toBeNull()
      expect(offer.reboot).toBe(true)
    }
  })

  it.each([
    ['requested', null, null],
    ['staging', null, 'cmd-a'],
    ['downloading', null, 'cmd-a'],
    ['verifying', null, 'cmd-a'],
    ['staged', 'cmd-a', 'cmd-a'],
    ['applying', null, null],
    ['rebooting', null, null],
    ['confirming', null, null],
    ['rolling_back', null, null],
    ['teleporting', null, null],
    ['constructor', null, null],
  ])('state %s: apply %s, cancel %s', (state, apply, cancel) => {
    const offer = offeredCommands(board(ALL, deploy({ state })))
    expect(offer.apply).toBe(apply)
    expect(offer.cancel).toBe(cancel)
  })

  it('offers Cancel on an apply wait but never on a rollback wait, and Apply on neither', () => {
    const applyWait = deploy({ state: 'awaiting_safe_window', detail: 'apply waits for a safe window' })
    const autoHold = deploy({ state: 'awaiting_safe_window', detail: null, steps: [{ state: 'staged', at: NOW.toString() }] })
    const rollbackWait = deploy({ state: 'awaiting_safe_window', detail: 'rollback waits for a safe window' })
    expect(offeredCommands(board(ALL, applyWait))).toMatchObject({ apply: null, cancel: 'cmd-a' })
    expect(offeredCommands(board(ALL, autoHold))).toMatchObject({ apply: null, cancel: 'cmd-a' })
    expect(offeredCommands(board(ALL, rollbackWait))).toMatchObject({ apply: null, cancel: null })
  })
})

const outputs: string[] = []
function line(cmd: CommandSummary, online = true) {
  const result = lastCommandLine(cmd, { online }, NOW)
  outputs.push(result.verb, result.answer, result.detail ?? '', result.ago, result.agoTitle, result.result)
  return result
}

describe('lastCommandLine', () => {
  it('reads a missing answer as sent, no answer yet — never stuck, never timed out', () => {
    const l = line(command())
    expect(l).toMatchObject({ verb: 'Apply', answer: 'sent, no answer yet', tone: null, result: 'none', ago: '10 s ago' })
  })

  it('says why there is no answer when the board is offline', () => {
    expect(line(command(), false).answer).toBe(
      'sent, no answer yet — the board is offline; it answers when it next connects',
    )
  })

  it('maps the three known answers, with tones', () => {
    expect(line(command({ result: 'accepted', answered_at: '2026-09-10T11:59:55Z' }))).toMatchObject({
      answer: 'accepted',
      tone: 'ok',
      result: 'accepted',
      ago: '5 s ago',
    })
    expect(line(command({ result: 'deferred', answered_at: '2026-09-10T11:59:55Z' }))).toMatchObject({
      answer: 'deferred: the board waits for a safe moment',
      tone: 'warn',
    })
    const refused = line(command({ type: 'reboot', result: 'refused', detail: 'an update is in progress' }))
    expect(refused).toMatchObject({ verb: 'Reboot', answer: 'refused', tone: 'bad', detail: 'an update is in progress' })
  })

  it('renders an unknown answer, an unknown verb and prototype keys as themselves', () => {
    expect(line(command({ result: 'pondering' }))).toMatchObject({ answer: 'pondering', tone: null })
    expect(line(command({ type: 'rollback' })).verb).toBe('Rollback')
    expect(line(command({ type: 'teleport' })).verb).toBe('teleport')
    expect(line(command({ type: 'constructor', result: 'constructor' }))).toMatchObject({
      verb: 'constructor',
      answer: 'constructor',
      result: 'constructor',
    })
  })

  it('drops an empty detail', () => {
    expect(line(command({ result: 'deferred', detail: '' })).detail).toBeNull()
    expect(line(command({ result: 'deferred', detail: '   ' })).detail).toBeNull()
  })

  it('never prints null, undefined or NaN', () => {
    line(command({ answered_at: 'garbage', result: 'accepted' }))
    expect(outputs.length).toBeGreaterThan(20)
    for (const s of outputs) expect(s).not.toMatch(/null|undefined|NaN/)
  })
})

describe('commandAcceptedMessage', () => {
  const accepted = { command_id: 'c', device_id: 'd', type: 'reboot', cmd_id: null }
  it('is silent for an online board and says queued for an offline one', () => {
    expect(commandAcceptedMessage({ ...accepted, device_online: true })).toBeNull()
    expect(commandAcceptedMessage({ ...accepted, device_online: false })).toBe(
      'queued — the board is offline; the broker holds the command until it next connects',
    )
  })
})
