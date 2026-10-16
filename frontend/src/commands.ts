// The command controls (R5-fe-1): the pure half. Which verbs a board is offered, and how the
// newest verb's answer reads. `CommandControls.tsx` renders it and does the POSTs.
//
// The rules this file keeps:
//
// 1. **Offers are gated by the board's own announced `capabilities`**, never by a guess
//    about its agent version. A board that announces none of `apply` / `cancel` / `reboot`
//    (a legacy `['ota']` agent) is offered nothing. The offer reads the server's read model
//    (`deploy.state`, `deploy.is_terminal`) and never re-derives server state; when the
//    board or server disagrees with an offer (a race), the board's answer or the server's
//    409 sentence is what the operator sees.
// 2. **Nothing expires a command.** `result: null` is "sent, no answer yet" — and possibly
//    never (an old agent, an answer lost to a reboot). No timeout, no spinner, no "stuck".
//    A `deferred` answer waits for the board's safe moment, which may be indefinite.
// 3. **Device text is text.** `result` and `detail` come from the board: matched by
//    `Object.hasOwn` lookups (a board may say `constructor`), rendered verbatim otherwise.
// 4. **Open vocabulary.** `type` and `result` are looked up with a fallback to the raw
//    value, so a REST/CI `rollback` or a newer agent's answer still renders.
// 5. Never `null`, `undefined` or `NaN` in a string.
//
// Rollback is deliberately NOT offered here (DECISIONS 2026-10-16, R5-fe-1): it is the
// destructive verb and the task asked for Apply and Reboot. Its REST route stays for
// scripts, and its answer still renders in the last-command line.

import { type CommandAccepted, type CommandSummary, type DeviceSummary } from './api'
import { safeWindowWait } from './deploy'
import { formatAgo, formatWhen } from './format'

/** Verb labels, a lookup with an `?? raw` fallback. */
export const COMMAND_LABELS: Record<string, string> = {
  apply: 'Apply',
  cancel: 'Cancel',
  rollback: 'Rollback',
  reboot: 'Reboot',
}

/**
 * States a Cancel is offered from: the board has the command and has not switched images.
 * Not `requested` (the board has not taken it yet), nothing from `applying` on, and an
 * `awaiting_safe_window` only when it waits to APPLY (never on a rollback wait).
 */
export const CANCELLABLE_STATES = new Set(['staging', 'downloading', 'verifying', 'staged'])

export type OfferedCommands = {
  /** The cmd_id Apply names, or null when not offered. */
  apply: string | null
  /** The cmd_id Cancel names, or null when not offered. */
  cancel: string | null
  reboot: boolean
}

export function offeredCommands(
  device: Pick<DeviceSummary, 'capabilities' | 'deploy'>,
): OfferedCommands {
  const caps = new Set(device.capabilities ?? [])
  const deploy = device.deploy
  const open =
    deploy !== null && !deploy.is_terminal && typeof deploy.cmd_id === 'string' && deploy.cmd_id !== ''
      ? deploy
      : null

  const apply = caps.has('apply') && open !== null && open.state === 'staged' ? open.cmd_id : null
  const cancel =
    caps.has('cancel') &&
    open !== null &&
    (CANCELLABLE_STATES.has(open.state) || safeWindowWait(open) === 'apply')
      ? open.cmd_id
      : null

  return { apply, cancel, reboot: caps.has('reboot') }
}

type Answer = { text: string; tone: 'ok' | 'warn' | 'bad' | null }

const ANSWERS: Record<string, Answer> = {
  accepted: { text: 'accepted', tone: 'ok' },
  deferred: { text: 'deferred: the board waits for a safe moment', tone: 'warn' },
  refused: { text: 'refused', tone: 'bad' },
}

export type LastCommandLine = {
  verb: string
  answer: string
  tone: 'ok' | 'warn' | 'bad' | null
  /** Device text, verbatim; null when empty. */
  detail: string | null
  ago: string
  agoTitle: string
  /** `data-result`: the raw device word, or `none` for no answer yet. */
  result: string
}

/** The last-command line: `<verb> · <answer>[: <detail>] (<ago>)`. */
export function lastCommandLine(
  cmd: CommandSummary,
  device: Pick<DeviceSummary, 'online'>,
  now: number,
): LastCommandLine {
  const type = String(cmd.type)
  const verb = Object.hasOwn(COMMAND_LABELS, type) ? COMMAND_LABELS[type] : type

  let answer: string
  let tone: Answer['tone']
  if (cmd.result === null || cmd.result === undefined) {
    answer = 'sent, no answer yet'
    if (!device.online) answer += ' — the board is offline; it answers when it next connects'
    tone = null
  } else if (Object.hasOwn(ANSWERS, cmd.result)) {
    answer = ANSWERS[cmd.result].text
    tone = ANSWERS[cmd.result].tone
  } else {
    answer = cmd.result
    tone = null
  }

  const detail = typeof cmd.detail === 'string' && cmd.detail.trim() !== '' ? cmd.detail : null
  const when = cmd.answered_at ?? cmd.sent_at
  return {
    verb,
    answer,
    tone,
    detail,
    ago: formatAgo(when, now),
    agoTitle: formatWhen(when),
    result: cmd.result ?? 'none',
  }
}

/** The local line after a 202: only the surprising case, a board that is offline. */
export function commandAcceptedMessage(accepted: CommandAccepted): string | null {
  return accepted.device_online
    ? null
    : 'queued — the board is offline; the broker holds the command until it next connects'
}
