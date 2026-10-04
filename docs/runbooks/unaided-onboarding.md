# Unaided onboarding re-run (R2b-test-1)

Run top to bottom, with an observer and a tester. This is the script for TODO.md
`R2b-test-1`, which extends `S0-test-3` (passed 2026-09-22 on the old UI). Nothing in it can
be done by an agent: it needs a person who has not seen the code, the bench board, and the
R2b flow on prod.

## Purpose and the bar

The bar is `spec/standards.md` -> *Unaided onboarding* -> **Exercised unaided**, and the
`S0-test-3` bar in `docs/features/enrollment.md` ("Someone who did not see the code
onboards a board unaided"): two runs, no assistance, no repo access, only what is on screen.

- **Run 1** onboards a board end to end.
- **Run 2** diagnoses a deliberately induced fault.

Both pass only if the tester never reads a UART log and never asks a question. **The fact
that they got stuck is the finding, not their skill.** Anything they hesitate on comes back
as a new Sprint 0 task, even if the run "passed".

The target operator connects a USB cable, follows instructions, and does not know what a
brownout or a partition table is.

## Why it needs a person and a bench

The software half (the console, the result card, the redacted bundle) is proven in jsdom and
in a real Chromium against replayed logs (last section). What it cannot prove is whether a
real person can act on the words, and whether a real native-USB board survives the flash,
the re-enumeration and the re-acquire on Windows. The Linux dev box does not enumerate
boards over Web Serial; the bench is **Windows + Chrome on Windows**, the S3 on its native
USB socket as COM3 (`DECISIONS.md` 2026-10-02).

## Before the run (STOP until all hold)

Each line has the command that proves it. Do not start while any is false.

1. **Prod runs the R2b flow.** `curl -s https://bingo.tvaroska.sk/v1/healthz` shows a version
   above 0.4.2 and a commit that contains `4b06281`:
   `git merge-base --is-ancestor 4b06281 <prod-commit> && echo "R2b on prod" || echo "R2b NOT on prod"`.
   If not, the owner runs `/release fleetforge minor` first (a prod deploy; it needs the
   owner's explicit go-ahead).
   *Snapshot 2026-10-04:* version 0.4.2, commit `9200e0f8e4c6`, built 2026-10-04T13:58:23Z;
   **R2b NOT on prod** (fe-1 `132f6bf` to fe-6 `4b06281` are all after it).
2. **Prod's flasher serves the repo's agent.** `just agent-check-prod` prints
   `CHECK-VERSION OK` (`S0-infra-10`). The run should flash the agent that will ship.
   *Snapshot 2026-10-04:* **STALE**, esp32 / esp32c3 / esp32c6 at 0.2.0 and esp32s3 at 0.3.2,
   all BEHIND repo 0.4.5. Publishing is `docs/runbooks/artifact-storage.md` -> *Publish to
   production's GCS* and needs the owner's go-ahead.
3. **`S0-bug-1`'s power-cycle diagnosis is done and recorded.** The run re-flashes
   `94a990dd09a4`, which ends the evidence for "Cause B" (power removed vs firmware wedge).
   Check TODO.md `S0-bug-1`.
4. **A tester who has not seen the codebase.** Prefer someone other than the `S0-test-3`
   tester (that person has seen the old UI). If it is the same person, record it.
5. **Hand-over state.** The observer logs in to the dashboard and opens "Add a board" BEFORE
   handing over (the account, the login and reaching the page are out of scope per the
   standard). The tester gets the Wi-Fi name and passphrase on paper and nothing else. The
   observer does not speak after hand-over. The tester may give up; that is recorded as a
   finding.

## Record first

| Item | Value |
|------|-------|
| Windows build | |
| Chrome version | |
| Prod UI / API version + commit (healthz) | |
| Agent version the flasher serves | |
| Board | `94a990dd09a4`, ESP32-S3, COM3, `303a:1001` |
| Cable (length, hub or direct) | |
| Tester (initials only; seen the old UI?) | |
| Date / time UTC | |

## Run 1 - onboard end to end (the re-flash of the known board)

The board is already on the fleet, so this is a known-board re-flash. The observer ticks each
item as it happens, without helping. Each is tied to the task that built it.

- [ ] The status strip shows UI and API versions equal, and the board once it is detected
      (`R2b-fe-1`).
- [ ] The pre-flight card says this is a known board, with "Re-flashing issues a new token
      and re-enrols it; its current baseline ends.", and the button reads "Re-flash and
      re-enrol this board" (`R2b-fe-2`).
- [ ] After the write the tester touches nothing. The console re-acquires the native-USB
      port by itself and never shows "No board is available to watch" (`R2b-fe-5`). Record the
      `watching ...: opened on try N, T ms into the 8 s window` notice; it also serves
      `S0-test-2` Check F (`serial-console-bench.md`).
- [ ] The milestone timeline reaches **On the fleet**, and the success card shows device id,
      firmware, layout, link, clock source, enrolled, on the fleet, UI and API (`R2b-fe-3`).
- [ ] The tester names the board from the card and finds it by that name in the fleet table
      (`R2b-fe-6`).
- [ ] **Observer-only extra, after the tester is done.** Press the board's RST once during a
      second watch. The restart is counted with a reason ("Rebooted 1x: ...") and the
      milestones that were lost are shown as lost (`R2b-fe-4`).

## Run 2 - diagnose an induced fault

**Primary: a wrong Wi-Fi passphrase.** The observer writes a wrong passphrase on the paper
(deterministic, no hardware tricks). The tester must reach the failure card, say in their own
words what is wrong, and fix it (re-flash with the right passphrase) using only the screen.
Expected card: "Wi-Fi: the board could not join the network", one next action, one copy
button.

Alternatives, if time allows:

- **Brownout:** a thin cable through an unpowered hub. Expected: "Power: the board's supply is
  collapsing (brownout)" and a restart count; no software remedy.
- **Flash write failure:** the flash-failed card must say cable / port / baud first
  (`R2b-fe-3`).
- **Spent token** is not practical to induce in the new flow (every flash mints a fresh
  token). The software rehearsal covers it.

## Pass / fail and what to file

- **Pass:** both runs end without a question and without the tester opening the raw log.
- Every hesitation over about 30 s, every question and every wrong click is written down
  verbatim with the screen it happened on. Each becomes a new Sprint 0 task via `/new-task`
  (`S0-fe-N` or `S0-bug-N`), even if the run passed.
- **Record the result** in the `R2b-test-1` entry of `docs/features/enrollment.md` and tick
  the TODO line (via `/implement`'s archive step). If the board is now on the fleet, also tick
  `S0-bug-1` (its acceptance allows "or re-flashed").

## Software rehearsal (a proxy, not the acceptance)

`frontend/scripts/onboarding-rehearsal.mjs` replays real agent log lines through a fake
`navigator.serial` in a real Chromium on the dev stack and grades the result card against the
bar above (one headline, one next action, at most one remedy, one working "Copy diagnostic
bundle", no raw log tokens in the headline or next text). Six scenarios: `happy`,
`bench-2026-10-04`, `brownout-loop`, `reboot-during-watch`, `wrong-psk`, `spent-token`.

```
just up        # the dev stack; rebuild stale images first (see below)
cd frontend
FF_ADMIN_PASSWORD=<dev admin password> \
  PLAYWRIGHT_MODULE="$(npm root -g)/playwright/index.mjs" \
  node scripts/onboarding-rehearsal.mjs http://localhost:8088 /tmp/ff-r2b-test-1
```

It prints `PASS|FAIL <id>` per scenario, writes a `.png` and a `.txt` per scenario, and exits
non-zero on any FAIL. `REHEARSAL_BREAK=<id>` flips one scenario's expectation on purpose, to
show the harness can fail.

It covers steps 5-6 of Flow 1 only. It does **not** cover detecting the chip, the flash
itself, or a real native-USB re-enumeration, and it does not replace the run above.

Dev-stack gotchas: the dev admin password is whatever `.env` says (`FF_ADMIN_PASSWORD`), which
need not be the `fleetforge-dev-only` default. `just rebuild <svc>` builds, then fails at
`docker compose up -d` on a box with pruned images (it tries to pull `minio/mc`); after the
build, run `docker compose up -d --no-deps --no-build <svc>`. Do not `just nuke` or `just down`.
