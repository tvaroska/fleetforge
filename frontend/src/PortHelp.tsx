// "My board isn't listed" — S0-fe-8.
//
// Observed at the bench on Windows 11 + Chrome: a CP2102 DevKit enumerated as a USB
// device, no VCP driver was bound, so no COM port existed and the chooser listed only the
// motherboard's COM1. Nothing on the page said why. The code was never at fault —
// `requestPort()` is called with no filters, so Chrome already offers everything the OS
// has — the gap is what the page says when what the OS has is nothing.
//
// The acceptance bar is an operator who has never installed a VCP driver reaching a COM
// port from this text alone. So: no Device Manager, and no "identify your chip" step that
// needs one — when unsure, install both drivers; they do not conflict.

const CP210X_DRIVER = 'https://www.silabs.com/developers/usb-to-uart-bridge-vcp-drivers'
const CH340_DRIVER = 'https://www.wch-ic.com/downloads/CH341SER_EXE.html'

export function PortHelp({
  open,
  onToggle,
}: {
  open: boolean
  onToggle: (open: boolean) => void
}) {
  return (
    <details
      data-testid="port-help"
      open={open}
      onToggle={(event) => onToggle(event.currentTarget.open)}
    >
      <summary>My board isn&apos;t listed</summary>
      <p>
        <strong>COM1 is never your board.</strong> It is a port built into the computer. If it is
        the only entry, the board is plugged in but has no port yet — almost always a missing
        driver.
      </p>
      <p>
        <strong>Boards on the chip&apos;s own USB</strong> (ESP32-S3, -C3, -C6, plugged into the
        port marked <em>USB</em>) need no driver anywhere. Most other boards talk through a small
        USB-to-serial chip next to the connector, and that chip needs a driver on Windows:
      </p>
      <ul>
        <li>
          <strong>CP2102 / CP2104</strong> (Silicon Labs — most ESP32 DevKit v1 boards):{' '}
          <a href={CP210X_DRIVER} target="_blank" rel="noreferrer">
            CP210x driver
          </a>
        </li>
        <li>
          <strong>CH340 / CH9102</strong> (WCH — most low-cost clones):{' '}
          <a href={CH340_DRIVER} target="_blank" rel="noreferrer">
            CH340 driver
          </a>
        </li>
      </ul>
      <p>
        Not sure which chip it is? Install both — they do not conflict. macOS and Linux have both
        drivers built in; Windows usually does not, and Windows Update often does not offer them.
      </p>
      <p>
        After installing: unplug the board, plug it back in, and press <em>Select port</em> again.
        It should now be listed as <em>CP210x USB to UART Bridge (COM…)</em> or{' '}
        <em>USB-SERIAL CH340 (COM…)</em>.
      </p>
      <p className="muted">
        Still nothing? Many USB cables only carry power — try one you know moves data, and a port
        directly on the computer rather than a hub. On Linux, if the port is listed but will not
        open, add yourself to the <code>dialout</code> group and log in again.
      </p>
    </details>
  )
}
