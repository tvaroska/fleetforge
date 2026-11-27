// Configuration form for `FlashBoard` (broker URI, Wi-Fi/Ethernet, reporting).
// Keeps one `<form>` per Wi-Fi network so browser password managers pair each SSID/PSK,
// while never persisting credentials in Web Storage, cookies, or URLs.

import { MAX_NETWORKS, NETS_MIN_AGENT } from './ffcfg'
import type { PreflightInstall } from './preflight'

/** 921600 first for CP210x/native-USB boards; 115200 is the CH340 fallback. */
export const BAUD_RATES = [921600, 460800, 115200]

export type NetworkRow = { key: number; ssid: string; psk: string }

export type FormState = {
  mqttUri: string
  link: string
  networks: NetworkRow[]
  hbS: string
  power: string
  wakeS: string
  baudRate: number
}

export function FlashConfigForm({
  apiBase,
  form,
  readsNets,
  install,
  onSetField,
  onSetNetwork,
  onAddNetwork,
  onRemoveNetwork,
}: {
  apiBase: string
  form: FormState
  readsNets: boolean | null
  install: PreflightInstall | null
  onSetField: <K extends keyof FormState>(key: K, value: FormState[K]) => void
  onSetNetwork: (index: number, patch: Partial<Omit<NetworkRow, 'key'>>) => void
  onAddNetwork: () => void
  onRemoveNetwork: (index: number) => void
}) {
  const wifi = form.link === 'wifi'

  return (
    <>
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
            onChange={(event) => onSetField('mqttUri', event.target.value)}
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
            onChange={() => onSetField('link', 'wifi')}
          />{' '}
          Wi-Fi
        </label>
        <label className="choice">
          <input
            type="radio"
            name="link"
            value="ethernet"
            checked={form.link === 'ethernet'}
            onChange={() => onSetField('link', 'ethernet')}
          />{' '}
          Ethernet
        </label>
        {wifi && (
          <>
            <p className="muted">
              Network 1 is tried first. The board joins the first network on this list it can see
              and stays on it until the link drops. At most {MAX_NETWORKS}.
            </p>
            {form.networks.map((row, index) => (
              <form
                key={row.key}
                className="network-row"
                onSubmit={(event) => event.preventDefault()}
                noValidate
              >
                <fieldset>
                  <legend>Network {index + 1}</legend>
                  <label>
                    SSID{' '}
                    <input
                      type="text"
                      name="ssid"
                      value={row.ssid}
                      onChange={(event) => onSetNetwork(index, { ssid: event.target.value })}
                      autoComplete="username"
                      autoCapitalize="none"
                      autoCorrect="off"
                      spellCheck={false}
                    />
                  </label>{' '}
                  <label>
                    Passphrase{' '}
                    <input
                      type="password"
                      name="psk"
                      value={row.psk}
                      onChange={(event) => onSetNetwork(index, { psk: event.target.value })}
                      autoComplete="current-password"
                    />
                  </label>
                  {index > 0 && (
                    <>
                      {' '}
                      <button
                        type="button"
                        aria-label={`Remove network ${index + 1}`}
                        onClick={() => onRemoveNetwork(index)}
                      >
                        Remove
                      </button>
                    </>
                  )}
                </fieldset>
              </form>
            ))}
            <p>
              <button
                type="button"
                onClick={onAddNetwork}
                disabled={form.networks.length >= MAX_NETWORKS}
              >
                Add another network
              </button>{' '}
              {form.networks.length >= MAX_NETWORKS && (
                <span className="muted">A board knows at most {MAX_NETWORKS} networks.</span>
              )}
            </p>
            <p className="muted" data-testid="password-manager-hint">
              Your browser&apos;s password manager can fill these in. Check it filled the Wi-Fi
              passphrase, not your dashboard password.
            </p>
            {form.networks.length > 1 && readsNets === false && install !== null && (
              <p className="warn" data-testid="old-agent-warning">
                This board gets agent {install.agentVersion}, which joins only network 1. Networks
                2-{form.networks.length} need agent {NETS_MIN_AGENT} or later.
              </p>
            )}
          </>
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
              onChange={(event) => onSetField('hbS', event.target.value)}
            />
          </label>
        </p>
        <label className="choice">
          <input
            type="radio"
            name="power"
            value="always_on"
            checked={form.power === 'always_on'}
            onChange={() => onSetField('power', 'always_on')}
          />{' '}
          Always on
        </label>
        <label className="choice">
          <input
            type="radio"
            name="power"
            value="sleepy"
            checked={form.power === 'sleepy'}
            onChange={() => onSetField('power', 'sleepy')}
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
                onChange={(event) => onSetField('wakeS', event.target.value)}
              />
            </label>{' '}
            <span className="muted">presence is 2.5 x this</span>
          </p>
        )}
      </fieldset>
    </>
  )
}
