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
import { BoardConsolePanel } from './BoardConsole'
import { type ConsoleFactory } from './boardConsole'
import { uriSecrets, type DiagnosticContext } from './diagnostics'
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
  createConsole,
}: {
  onSessionExpired: () => void
  // Injected by the tests only: jsdom has no `navigator.serial` (see `flasher.ts`).
  createFlasher?: FlasherFactory
  createConsole?: ConsoleFactory
}) {
  const [form, setForm] = useState<FormState>(INITIAL_FORM)
  const [board, setBoard] = useState<string>(OTHER_BOARD_ID)
  const [manifest, setManifest] = useState<AgentManifest | null>(null)
  const [manifestError, setManifestError] = useState<string | null>(null)
  /** S0-fe-7, and a footnote in a bundle rather than anything on this page. */
  const [serverVersion, setServerVersion] = useState<string | null>(null)
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
    // Independent of the manifest, and SILENT on failure by design: `/v1/healthz` is
    // unauthenticated and does no I/O server-side, so a failure here says nothing about
    // this page's session. It must never set `manifestError` and must never call
    // `onSessionExpired` — the bundle simply reads "unavailable".
    api
      .health()
      .then((value) => {
        if (live) setServerVersion(value.version)
      })
      .catch(() => {})
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

  // S0-fe-6. Every fault a re-flash fixes is either a spent token or a stale broker
  // credential, and both are cleared by the same thing: the flash mints a FRESH single-use
  // token (`flash.ts` rule 3), and the agent drops the credential it was holding the first
  // time it boots with a token it has not seen before (`ff_store_sync_token`, S0-fw-4).
  // Nothing here erases NVS — that would cost the board its cached radio calibration.
  //
  // Not memoised on purpose: it is only ever used from an `onClick`, and `config` is a
  // fresh literal on every render, so a `useCallback` would churn and buy nothing — and
  // one with an honest dependency list would be rebuilt every render anyway.
  const recoverByReflash = () =>
    state.reflash({ config, baudRate: form.baudRate })

  // S0-fe-7. A plain literal for the same reason `recoverByReflash` is: `config` is a
  // fresh object on every render, so a memo with an honest dependency list would be
  // rebuilt every render anyway. The form has no NTP field, so `ntp` is null —
  // "(firmware default)" — rather than a value this page invented.
  const diagnostics: DiagnosticContext = {
    chip,
    build: state.build,
    agentVersion: manifest?.agent_version ?? null,
    serverVersion,
    config: {
      apiBase,
      mqttUri: form.mqttUri,
      link: form.link,
      ssid: form.ssid,
      ntp: null,
      hbS: form.hbS || null,
      power: form.power,
      wakeS: form.wakeS || null,
      pskLength: form.psk.length,
    },
    // Scrub input only. The passphrase, plus any password typed into either URI — a broker
    // password must be scrubbed out of every section, not only out of the URI that carried it.
    knownSecrets: [form.psk, ...uriSecrets(form.mqttUri), ...uriSecrets(apiBase)],
  }

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

      <h3>3 · Flash</h3>
      <p className="muted">
        Every flash mints a fresh single-use token. The board notices the new token on its
        first boot, clears the broker credential it was holding and enrols again — so a board
        that has already been enrolled re-registers without erasing its cached radio
        calibration.
      </p>
      {configError !== null && (
        <p className="bad" role="alert" data-testid="config-error">
          {configError}
        </p>
      )}
      <p>
        <button
          type="button"
          onClick={() =>
            void state.flash({ config, baudRate: form.baudRate })
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
            The board is rebooting
            {state.flashedDeviceId === null ? (
              '.'
            ) : (
              <>
                {' '}
                as <code>{state.flashedDeviceId}</code>.
              </>
            )}{' '}
            Watch it come up below — the console opens by itself, resets the board once so the log
            starts at the top, and names the cause if it stops short of the fleet.
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

      {/* Below the flash log, and outside it: the console is useful on a board that was
          flashed last week, not only on one flashed in this tab. `autoWatch` is what makes
          the just-flashed case need no click at all. */}
      <BoardConsolePanel
        autoWatch={state.phase === 'done'}
        createConsole={createConsole}
        diagnostics={diagnostics}
        // No button when the form could not produce a valid blob — re-flashing the same
        // invalid config is a button that cannot work.
        onReflash={configError === null ? recoverByReflash : undefined}
        reflashBlockedReason={
          configError === null
            ? null
            : 'Fill in the network details in step 2 to re-flash from here.'
        }
      />
    </section>
  )
}
