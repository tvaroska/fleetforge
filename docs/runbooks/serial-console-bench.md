# Bench-verifying the serial console on a bridge-chip board

Run top to bottom at the bench. This is the script for TODO.md `S0-test-1`. Checks A-D
change nothing on the board. Check E re-flashes it, so it runs last, behind a stop.

## Why it needs a bench

The software half (classifier, state machine, copy) is proven in jsdom against replays of
real `agent/main/*.c` output. These checks are properties of a USB bridge chip and an OS,
not of the classifier, so jsdom cannot see them: driver presence, baud, the DTR/RTS reset
wiring, whether `port.close()` really frees the COM port, and whether the port survives
`hard_reset`. Re-acquire is an OS-and-driver property: re-run this when the bench driver
or Windows build changes.

## The bench

- **Windows + Chrome on Windows** (not WSL). Leave `usbipd` *Not shared*.
- **Board: the ESP32-S3 DevKitC-1, `UART` socket, not `USB` (native, COM3).** The `UART`
  socket is an on-board USB-UART bridge (normally CP2102N `10c4:ea60`, some revisions use
  CH343 `1a86:55d3`). It is the same bridge-chip path as any CP2102/CH340 DevKit: VCP
  driver, DTR/RTS auto-reset, UART0. The primary console is UART0 at 115200
  (`CONFIG_ESP_CONSOLE_UART_NUM=0`, `CONFIG_ESP_CONSOLE_UART_BAUDRATE=115200` in
  `agent/dist/esp32s3/sdkconfig.resolved`). Any other CP2102/CH340 board also qualifies.
  Using the S3 `UART` socket is proposed, pending Boris's confirmation.
- Chrome's chooser shows the OS name (e.g. `Silicon Labs CP210x USB to UART Bridge
  (COM5)`), never "ESP32".

Constants this runbook relies on (`frontend/src/serialConsole.ts`): the console re-reads
`navigator.serial.getPorts()` every 250 ms (`ACQUIRE_RETRY_MS = 250`) for 8 s
(`ACQUIRE_TIMEOUT_MS = 8000`); `reboot()` pulses RTS with DTR low for `RESET_PULSE_MS =
150`; the console opens at 115200.

## Record first

| Item | Value |
|------|-------|
| Windows build | |
| Chrome version | |
| Driver name + version (after install) | |
| COM number | |
| Console log line `port: ... (USB vvvv:pppp)` | |

The `port:` line is logged when the port is picked (`checkChosenPort`, `flasher.ts`). It
is the evidence of which bridge was actually tested.

## Check A - driver-absent onboarding (folded in from S0-fe-8)

Setup (admin tools are allowed for setup, not for the operator run), in PowerShell:

```powershell
pnputil /enum-drivers | Select-String -Context 0,6 -Pattern 'silabser|ch343|ch341'
# if present:  pnputil /delete-driver oemNN.inf /uninstall /force
```

Unplug and replug the `UART` socket and confirm the chooser lists no new COM port. If
Windows Update rebinds a driver by itself, record that as the finding: the "no COM port"
dead end does not happen for that chip on that Windows build, and the check counts as not
reproducible rather than passed.

Operator run: open `https://bingo.tvaroska.sk`, flash page, *Select port*.

- Expect only COM1, or nothing. Picking COM1 must be refused with "That port is built into
  the computer (COM1 on most Windows PCs), not a USB board..." and point at "My board isn't
  listed".
- Following only that panel (driver link, unplug/replug, *Select port* again) must reach
  the bridge COM port. No Device Manager.
- Both driver links resolve in the bench browser (the dev box gets a 403 from Akamai for
  Silicon Labs; the CH340 link answers 200). If the board is CH343 (`1a86`), note whether
  the WCH CH341SER link covers it. That is a finding to record, not to fix here.

## Check B - 115200 decodes cleanly

"Watch a board", pick the `UART` COM port, press the physical EN/RST button.

Pass: a readable `ESP-ROM:esp32s3-...` banner, `rst:0x1 (POWERON),boot:0x8
(SPI_FAST_FLASH_BOOT)`, `I (...) boot: ESP-IDF v5.5.x`, and the agent's version line.
Fail signature: plausible-looking mojibake and the milestone classifier staying idle.

## Check C - the EN pulse boots the app

Click "Reboot the board".

Pass: the same banner with `SPI_FAST_FLASH_BOOT`, then the app log. Fail signature:
`boot:0x0 (DOWNLOAD...)` / `waiting for download`: wiring or polarity inverted against
`reboot()` in `serialConsole.ts`.

## Check D - release really releases

Negative control first: while the console is watching, open PuTTY (Serial, COMn, 115200).
It must fail with "Access denied" or port in use. If it opens, the check cannot
discriminate; stop and fix the setup.

Then click "Release the port" and open PuTTY again. It must open, and pressing EN must show
the boot log in PuTTY. Close PuTTY afterwards. Fail signature: "Access denied" after the
release, meaning `port.close()` is not reached.

## Check E - re-acquire after hard_reset (DESTRUCTIVE, run last)

> **STOP. This re-flashes the prod board `94a990dd09a4` and ends its 0.3.1 baseline.**
> The serial flash writes `ota-data-initial.bin` (0xF000), burns a fresh enrolment token and
> rewrites `ff_cfg`, so the board boots `ota_0` with whatever bundle prod serves. TODO.md
> relies on prod's board being on 0.3.1, so the deploy that first carries 0.4.x to it still
> parks at `rebooting`. Do this only once the R2 OTA run that needs that baseline is done,
> or once you have decided to give the baseline up. Checks A-D do not need it.

`hard_reset` only happens at the end of a flash (`esptoolFlasher.ts`), so "Reboot the
board" does not exercise re-acquire. Only a flash does.

1. Record the dashboard version of `94a990dd09a4` before.
2. Flash from the flash page using the `UART` COM port. Do not flash from COM3 (native):
   that is `S0-test-2`.
3. Pass: after the write, the console opens by itself (no click, no chooser), streams the
   boot log, and never shows "No board is available to watch". The board reaches *On the
   fleet*. The console polls for 250 ms steps up to 8 s; record the time from `hard_reset`
   to the first log line if visible.
4. Record the dashboard version after.

## Results

| Check | Pass/fail | Observed | Evidence |
|-------|-----------|----------|----------|
| A driver-absent onboarding | | | |
| B 115200 decodes | | | |
| C EN pulse boots app | | | |
| D release releases | | | |
| E re-acquire after hard_reset | | | |

Anything that fails comes back as a new S0 task with the observed behaviour (`/new-task`).
On all-pass, `S0-test-1` flips `[x]` and the evidence goes into `docs/features/enrollment.md`
(the S0-fe-1 *T2 evidence* paragraph and the S0-fe-8 *bench half still owed* paragraph)
when it is archived.

`S0-test-2` reuses Check E on COM3 (the native `USB` socket), where the port comes back as
a different `SerialPort`.
