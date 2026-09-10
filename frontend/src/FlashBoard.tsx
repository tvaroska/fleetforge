// "Flash a board" — R0-fe-3, and the last screen of R0's done-when: plug in a board,
// flash & register it from the browser, watch it come online.
//
// This file renders and collects input. Every rule lives in `flash.ts` (what may be
// written, and in what order) or `ffcfg.ts` (the blob format); the serial port lives
// behind the seam in `flasher.ts`. Two things here are load-bearing and easy to undo:
//
// * **No `localStorage`, no `sessionStorage`, anywhere in this feature.** The Wi-Fi
//   passphrase and the enrollment token are both credentials, and the token is a
//   fleet-join credential the server cannot re-derive. One rule, easy to review.
// * **The two URLs are derived from `window.location`, never compiled in.** See
//   `defaultMqttUri` below.

import { useCallback, useEffect, useState } from 'react'
import { ApiError, api, type AgentManifest } from './api'
import { OTHER_BOARD_ID, shortlist } from './boards'
import { buildFfCfgFields, validateFfCfg, type FlashConfigInput } from './ffcfg'
import { formatBytes, predictDeviceId, useFlashBoard } from './flash'
import { webSerialSupported, type FlasherFactory } from './flasher'

/** 921600 first: it is what a CP210x/native-USB board wants. 115200 is the CH340 escape. */
const BAUD_RATES = [921600, 460800, 115200]

/**
 * The device-facing broker URI, derived from the page.
 *
 * There is no API that serves it: `Settings.mqtt_host`/`mqtt_port` are the *internal*
 * address (`mosquitto:1883`), and the device-facing one exists nowhere in the API's
 * config. Deriving it from `location` is correct for both deployments we have —
 * `mqtt://localhost:8883` in dev (plaintext through Traefik's `mqtt` entrypoint) and
 * `mqtts://bingo.tvaroska.sk:8883` in production (TLS terminated at Traefik) — and the
 * field is editable for anything else. Do NOT "improve" this into a hardcoded host.
 */
function defaultMqttUri(): string {
  if (typeof window === 'undefined') return 'mqtt://localhost:8883'
  const scheme = window.location.protocol === 'https:' ? 'mqtts' : 'mqtt'
  return `${scheme}://${window.location.hostname}:8883`
}

/** The one-origin invariant means the API base cannot be anything but this page's origin. */
function defaultApiBase(): string {
  return typeof window === 'undefined' ? '' : window.location.origin
}

type FormState = {
  mqttUri: string
  link: string
  ssid: string
  psk: string
  hbS: string
  power: string
  wakeS: string
  baudRate: number
  eraseAll: boolean
}

const INITIAL_FORM: FormState = {
  mqttUri: defaultMqttUri(),
  link: 'wifi',
  ssid: '',
  psk: '',
  // Blank on purpose: an absent `hb_s` leaves the firmware's own default in place rather
  // than freezing today's number into every board flashed today.
  hbS: '',
  power: 'always_on',
  wakeS: '',
  baudRate: BAUD_RATES[0],
  eraseAll: true,
}

/**
 * The same two functions `flash.ts` runs first, run again on every keystroke.
 *
 * Not redundant: the engine's copy is the one that protects the board, this one is the
 * one that keeps a half-filled form from ever reaching it — a `sleepy` board with no wake
 * interval would otherwise spend a single-use token before `POST /v1/enroll` refused the
 * identity. The token is minted only in `flash.ts`, and only after this has passed too.
 *
 * The message comes from `ffcfg.ts`, which is careful never to quote a value, so this
 * cannot put a passphrase on screen.
 */
function validationMessage(input: FlashConfigInput): string | null {
  try {
    validateFfCfg(buildFfCfgFields(input))
    return null
  } catch (err) {
    return err instanceof Error ? err.message : 'the configuration is not valid'
  }
}

function Unavailable({ reason }: { reason: 'no-web-serial' | 'insecure' }) {
  return (
    <section aria-labelledby="flash-heading">
      <h2 id="flash-heading">Flash a board</h2>
      <p className="muted">
        {reason === 'no-web-serial' ? (
          <>
            Flashing needs the Web Serial API: use Chrome or Edge. Chromium-only is an
            accepted v1 limit (<code>spec/prd.md</code> — client constraint). Everything else on
            this page works in any browser; you can still generate a token below and flash the
            board with <code>esptool.py</code>.
          </>
        ) : (
          <>
            The browser will not grant serial access over an insecure connection. Open this
            dashboard over HTTPS, or on <code>http://localhost</code>, and reload.
          </>
        )}
      </p>
    </section>
  )
}

export function FlashBoard({
  onSessionExpired,
  createFlasher,
}: {
  onSessionExpired: () => void
  // Injected by the tests only: jsdom has no `navigator.serial` (see `flasher.ts`).
  createFlasher?: FlasherFactory
}) {
  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [board, setBoard] = useState<string>(OTHER_BOARD_ID)
  const [manifest, setManifest] = useState<AgentManifest | null>(null)
  const [manifestError, setManifestError] = useState<string | null>(null)
  const state = useFlashBoard({ onSessionExpired, createFlasher })

  const supported = webSerialSupported()
  const secure = typeof window === 'undefined' || window.isSecureContext

  const set = useCallback(<K extends keyof FormState>(key: K, value: FormState[K]) => {
    setForm((current) => ({ ...current, [key]: value }))
  }, [])

  useEffect(() => {
    if (!supported || !secure) return
    let live = true
    api
      .agentManifest()
      .then((value) => {
        if (live) setManifest(value)
      })
      .catch((err: unknown) => {
        if (!live) return
        if (err instanceof ApiError && err.isUnauthorized) {
          onSessionExpired()
          return
        }
        setManifestError(err instanceof Error ? err.message : 'could not read the agent manifest')
      })
    return () => {
      live = false
    }
  }, [supported, secure, onSessionExpired])

  if (!supported) return <Unavailable reason="no-web-serial" />
  if (!secure) return <Unavailable reason="insecure" />

  const chip = state.chip
  const candidates = chip === null ? [] : shortlist(chip)
  const predicted = chip === null ? null : predictDeviceId(chip)
  const apiBase = defaultApiBase()
  const config: FlashConfigInput = {
    apiBase,
    mqttUri: form.mqttUri,
    link: form.link,
    ssid: form.ssid,
    psk: form.psk,
    hbS: form.hbS,
    power: form.power,
    wakeS: form.wakeS,
  }
  const configError = validationMessage(config)

  return (
    <section aria-labelledby="flash-heading">
      <h2 id="flash-heading">Flash a board</h2>
      <p className="muted">
        Connect an ESP32 over USB, confirm what it is, and write the agent plus this board&apos;s
        own configuration. A single-use enrollment token is minted for it automatically — you do
        not need to generate one below.
      </p>

      {manifestError !== null && (
        <p className="bad" role="alert">
          {manifestError}
        </p>
      )}
      {manifest !== null && (
        <p className="muted" data-testid="agent-targets">
          Agent {manifest.agent_version} ·{' '}
          {manifest.builds.map((b) => `${b.target} (${b.chip_family})`).join(', ')}
        </p>
      )}

      <h3>1 · The board</h3>
      <p>
        <label>
          Baud rate{' '}
          <select
            value={form.baudRate}
            onChange={(event) => set('baudRate', Number(event.target.value))}
            disabled={state.busy}
          >
            {BAUD_RATES.map((rate) => (
              <option key={rate} value={rate}>
                {rate}
              </option>
            ))}
          </select>
        </label>{' '}
        <button
          type="button"
          onClick={() => void state.connect(form.baudRate)}
          disabled={state.busy}
        >
          {chip === null ? 'Select port and detect' : 'Select a different port'}
        </button>
      </p>

      {chip !== null && (
        <>
          <dl data-testid="chip-info">
            <dt>Chip</dt>
            <dd>{chip.description}</dd>
            <dt>MAC</dt>
            <dd>{chip.macAddress ?? 'unavailable'}</dd>
            <dt>Flash</dt>
            <dd>{formatBytes(chip.flashSizeBytes)}</dd>
            <dt>Features</dt>
            <dd>{chip.features.join(', ') || '—'}</dd>
            <dt>Device ID</dt>
            <dd>
              {predicted === null ? 'unknown' : <code>{predicted}</code>}{' '}
              <span className="muted">
                (predicted — the board reads its own eFuse MAC at boot)
              </span>
            </dd>
          </dl>

          <fieldset>
            <legend>Which board is this?</legend>
            <p className="muted">
              Detection is chip-level. This picks nothing on the server — it is a label for you.
            </p>
            {candidates.map((candidate) => (
              <label key={candidate.id} className="choice">
                <input
                  type="radio"
                  name="board"
                  value={candidate.id}
                  checked={board === candidate.id}
                  onChange={() => setBoard(candidate.id)}
                />{' '}
                {candidate.label}
              </label>
            ))}
            <label className="choice">
              <input
                type="radio"
                name="board"
                value={OTHER_BOARD_ID}
                checked={board === OTHER_BOARD_ID}
                onChange={() => setBoard(OTHER_BOARD_ID)}
              />{' '}
              Other — enter manually
            </label>
          </fieldset>
        </>
      )}

      <h3>2 · What to bake in</h3>
      <dl>
        <dt>Server</dt>
        <dd>
          <code>{apiBase}</code>{' '}
          <span className="muted">(this page&apos;s origin — the board must reach it)</span>
        </dd>
      </dl>
      <p>
        <label>
          Broker URI{' '}
          <input
            type="text"
            value={form.mqttUri}
            onChange={(event) => set('mqttUri', event.target.value)}
            size={32}
          />
        </label>{' '}
        <span className="muted">mqtts:// selects TLS on the device</span>
      </p>

      <fieldset>
        <legend>Network</legend>
        <label className="choice">
          <input
            type="radio"
            name="link"
            value="wifi"
            checked={form.link === 'wifi'}
            onChange={() => set('link', 'wifi')}
          />{' '}
          Wi-Fi
        </label>
        <label className="choice">
          <input
            type="radio"
            name="link"
            value="ethernet"
            checked={form.link === 'ethernet'}
            onChange={() => set('link', 'ethernet')}
          />{' '}
          Ethernet
        </label>
        {form.link === 'wifi' && (
          <p>
            <label>
              SSID{' '}
              <input
                type="text"
                value={form.ssid}
                onChange={(event) => set('ssid', event.target.value)}
                autoComplete="off"
              />
            </label>{' '}
            <label>
              Passphrase{' '}
              <input
                type="password"
                value={form.psk}
                onChange={(event) => set('psk', event.target.value)}
                autoComplete="off"
              />
            </label>
          </p>
        )}
      </fieldset>

      <fieldset>
        <legend>Reporting</legend>
        <p>
          <label>
            Heartbeat seconds{' '}
            <input
              type="number"
              min={1}
              value={form.hbS}
              placeholder="firmware default"
              onChange={(event) => set('hbS', event.target.value)}
            />
          </label>
        </p>
        <label className="choice">
          <input
            type="radio"
            name="power"
            value="always_on"
            checked={form.power === 'always_on'}
            onChange={() => set('power', 'always_on')}
          />{' '}
          Always on
        </label>
        <label className="choice">
          <input
            type="radio"
            name="power"
            value="sleepy"
            checked={form.power === 'sleepy'}
            onChange={() => set('power', 'sleepy')}
          />{' '}
          Sleepy
        </label>
        {form.power === 'sleepy' && (
          <p>
            <label>
              Wake interval seconds{' '}
              <input
                type="number"
                min={1}
                value={form.wakeS}
                onChange={(event) => set('wakeS', event.target.value)}
              />
            </label>{' '}
            <span className="muted">presence is 2.5 x this</span>
          </p>
        )}
      </fieldset>

      <p>
        <label className="choice">
          <input
            type="checkbox"
            checked={form.eraseAll}
            onChange={(event) => set('eraseAll', event.target.checked)}
          />{' '}
          Erase flash first (clears any stored credential)
        </label>
      </p>
      <p className="muted">
        Leave it on unless you know better: a board that already enrolled keeps its broker
        credential in NVS and will reuse it, so the fresh token baked in here would never be
        spent and the board would not re-register.
      </p>

      <h3>3 · Flash</h3>
      {configError !== null && (
        <p className="bad" role="alert" data-testid="config-error">
          {configError}
        </p>
      )}
      <p>
        <button
          type="button"
          onClick={() =>
            void state.flash({ config, eraseAll: form.eraseAll, baudRate: form.baudRate })
          }
          disabled={state.busy || chip === null || configError !== null}
        >
          Flash this board
        </button>
      </p>

      {state.error !== null && (
        <p className="bad" role="alert">
          {state.error}
        </p>
      )}

      {state.step !== '' && (
        <p aria-live="polite" data-testid="flash-step">
          {state.step}
        </p>
      )}

      {state.progress !== null && (
        <p data-testid="flash-progress">
          {state.progress.label} — part {state.progress.partIndex + 1} of{' '}
          {state.progress.partCount}
          <progress value={state.progress.written} max={state.progress.total || 1} />
        </p>
      )}

      {state.phase === 'done' && (
        <section className="issued" aria-labelledby="flashed-heading">
          <h3 id="flashed-heading">Flashed</h3>
          <p>
            The board is rebooting. It should appear in the fleet above within a few seconds
            {state.flashedDeviceId === null ? (
              '.'
            ) : (
              <>
                {' '}
                as <code>{state.flashedDeviceId}</code>.
              </>
            )}
          </p>
          <p>
            <button type="button" onClick={state.reset}>
              Flash another board
            </button>
          </p>
        </section>
      )}

      {state.log.length > 0 && (
        <details data-testid="flash-log">
          <summary>esptool output ({state.log.length} lines)</summary>
          <pre className="log">{state.log.join('\n')}</pre>
        </details>
      )}
    </section>
  )
}
