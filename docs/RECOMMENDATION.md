# Fleetforge — recommendation

**Date:** 2026-09-23
**Status:** product review — not a spec, not a plan.
**Audience:** the operator of this instance, deciding what to build next.

A reading of specs, documentation and code against the people who would actually use
this. Live task state stays in `TODO.md`. Landing any of the recommendations below is a
`/new-feature` (or a release), not an edit of this file.

The architecture is the strong part of this repo. The gap is that the waist has a
working demo on one side and a swarm roadmap on the other, and almost nothing a person
with a sketch and a board in the other room can pick up.

---

## Who this is actually for

| User | When | What they need to not bounce |
|---|---|---|
| **Alex** — Arduino/PlatformIO, 3–15 ESP32s in boxes | **v1, stated primary** | Drop-in library, USB once, then OTA of *their* firmware, a bad `.bin` comes back by itself |
| **Boris** — operator of `bingo.tvaroska.sk` | **today, the only real user** | A hosted instance whose Deploy button does what it says, and whose health checks mean something |
| **Sarah** — HIL rack | shared with v1 | Fast reflash, custom confirm, diagnostics |
| **Marcus / Siddharth** | V2 | CLI, rings, provenance, signing |
| **Elena** — field swarm | V3 | Disconnected gateway, coordinated apply, deltas |

v1 is explicitly *not a public product*. That is fine. It does not change the fact that
the PRD’s user is Alex, CUJ-1 is Alex’s day, and every persona matrix cell that is P0
for Alex is still open.

What actually works on metal, as of 2026-09-23:

- Chrome → flash prebuilt agent → enroll → green in the fleet list (R0, unaided)
- Dashboard Deploy of an *agent* `.bin` → version changes (~25 s)
- One brick mode recovers itself: boots, joins, never gets announce acked → rolls back in
  71 s — on an `FF_ROLLBACK_TEST` build, where `CONFIRM_TIMEOUT_S` is 60 rather than the
  shipped 300 (`ff_mqtt.c:56-63`). The mechanism proved is the real one. The wait is not.

That is a complete demo of Fleetforge. It is not a product Alex can put in a chicken coop.

---

## Gap 1 — You cannot OTA firmware the user wrote

This is the product. Everything else is in service of it. It does not exist.

CUJ-1 step 2 is “add the library to the sketch.” There is no library. The agent in
`agent/main/` is a connect-and-update-itself binary. It does not water plants, drive a
frame, or blink Morse. A hobbyist who has to port the coop door to ESP-IDF to get OTA
will not.

The evidence is not “R3 is planned.” The evidence is the surface they would actually
touch:

- **No Arduino / PlatformIO / IDF component to embed**. `R3-fw-2`–`R3-fw-5` and
  `R3-test-1` are unchecked. The two landed R3 tasks are a partition-table spike
  (`R3-fw-1`) and a written journey (`R3-spec-1`) — necessary, and neither is the
  on-ramp. A CUJ nobody can walk is the gap stated precisely, not evidence against it.
- **The dashboard can Deploy but cannot Upload**. `frontend/src/api.ts` has
  `listArtifacts` and `deployDevice`. It has no upload method.
  `docs/runbooks/upload-artifact.sh` exists *because of this* (“until it grows a form
  this script is the only way to get something deployable into the fleet”). Alex’s unit
  of work is a `.bin` from the IDE. The path from that file to a board is curl + admin
  password on stdin.
- **No device naming, no tags, no “this is the coop door.”** Flow 1 step 7 is in the
  spec. `GET /v1/devices` returns `name: null` and there is no PATCH. The fleet table is
  a list of 12-hex MACs.
- **README still says the agent, the flasher, and OTA do not exist**. A second person
  (or future-you) bounces before they find `TODO.md`.

R3 is correctly sequenced *after* R2. A library is a multiplier on but safe deploy
currently is. That does not make the gap smaller. Until the four verbs live in
`setup()`/`loop()`, every later release is a feature of firmware Alex did not write.

Arduino is P0 for Alex, Elena, *and* Marcus. The prebuilt agent is the demo of the
library, not a substitute for it.

---

## Gap 2 — “Safe” is still a claim covering one of three brick modes

The pitch is the first sentence of the README. A bad build triggers a catch *before* the fleet.
Any device that gets one recovers itself. v1 has no “before the fleet” (sim is V2).
Recovery on metal covers **one** failure mode.

From `TODO.md` after `R1-test-1`:

| Failure | Recovery today |
|---|---|
| Image fails to boot | Bootloader A/B — standard IDF, **not exercised by us** |
| Boots, joins, never confirms | **Proven** 2026-09-23 (`confirm_timeout_cb` on metal) |
| Boots, announce is acked, app is broken anyway | **Confirms itself. No automatic recovery**. This is the residual gamble. |

That third case is the common one: a null deref in `loop()`, a show that starts
garbage, a sensor that never reads. Confirm is “broker accepted the announce,” not “the
thing you care about works.” R5’s custom self-test is what closes it. R2 as specified
does not.

Worse, the parts of R2 that *are* specified will fail a v1 board:

- **Sleepy confirm is a 300 s wall clock**. `CONFIRM_TIMEOUT_S` is compile-time 300.
  `confirm_timeout_s` on the command parses and **ignored** (`ff_mqtt.c`). The PRD’s
  e-paper “wakes, refreshes, sleeps” will roll back a perfect image. ESPHome already
  burned this (`boot_is_good_on_shutdown`). It is in the persona pain list. It is not
  in the agent.
- **No in-image safe-mode**. A/B does not cover “boots, then crashes before MQTT.” That
  board looks dead. ESPHome’s answer is an NVS boot counter into a reduced image —
  cheap, no flash-time immutable. Not scoped.
- **The dashboard lies about success**. R1’s walk ends at `rebooting`. `is_terminal`
  stays false forever, so every successful deploy renders as in-flight. An operator
  watching the coop door cannot tell “it worked” from “it is still going” from “it hung.”
  Measured 2026-09-23 running the CUJ suite: a clean deploy parked at
  `{"state": "rebooting", "is_terminal": false}` and stayed there. Nothing writes
  `CONFIRMED` — `TERMINAL_DEPLOY_STATES` lists it (`db/models.py:172`) and no code path
  reaches it, because confirm reporting is `R2-be-1`/`R2-fw-3`.
- **Integrity is complete. Provenance is not**. `ff_ota` reads the written slot back,
  sha256s it from flash, and refuses to boot into a mismatch (`ff_ota.c:100`, `:422`) —
  the strong version of the check, not the download-stream one. What is missing is any
  reason to trust the digest. Nothing proves the `stage` command came from you. That is
  R6 signing, and it is correctly later.

The 2026-09-23 session is the same shape as a product gap, not just an ops finding:
Deploy had **never worked on prod** (`ARTIFACT_URL_SECRET` / `PUBLIC_BASE_URL` unwired),
`mosquitto-init` has never reached the running broker. `/v1/healthz` was green the
whole time. For a hosted-for-one v1, the operator *is* the user. A control plane that
reports healthy while the only path that matters is broken is how you brick a board you
cannot walk to.

Until R2 is boring — checksum, device-armed confirm that survives sleepy,
`confirmed`/`rolled_back` on the wire, dashboard that says done, and the third brick
mode at least named — Deploy on a board in the attic is still a bet.

---

## Gap 3 — After the first flash, the product still needs the USB cable it exists to delete

The problem statement is devices they cannot easily reach. The v1 provisioner is USB
config flash. A Wi-Fi change is a reflash. That makes the *second* operation (the one
that happens after you install the thing) require the same physical access the product
exists to eliminate.

Alex’s actual life with 3–15 boards:

1. Desk AP → house AP → travel router. PSK rotates. The board is in a box, a garden, a
   frame.
2. Board #2…#N is the same as board #1: re-enter Wi-Fi, re-detect chip, re-flash.
   `spec/open-questions.md` already asks whether the repeat path deserves its own
   design. Yes.
3. The only flash path is Web Serial, Chromium, one click, HTTPS. Firefox/Safari cannot
   onboard. There is no CLI. Marcus and Sarah both list batch CLI as P0. Alex hits it
   the moment they are on a laptop that is not Chrome.

Wi-Fi creds live in `ff_cfg`, a 4 KB partition the browser flasher writes at `0x12000`.
The running app has no write path back. Improv (serial first, BLE later) is the
community answer and is not on the ladder — [`HOBBYIST.md`](HOBBYIST.md) recommended it,
`releases.md` did not slot it. SoftAP is post-v1 by decision.

This is not “missing a nice-to-have provisioner.” It is the product claim failing on
the second day. USB-once-then-OTA only holds if the *link* can recover without USB.
Today it cannot. Combined with gap 1 (you can only OTA the demo agent) and gap 2 (and
only somewhat safely), the field loop is: crawl under the porch with a laptop anyway.

---

## What I would not put in the top 3

- **V3 swarm / gateway / Thread / delta**. Right architecture, wrong decade for five
  DevKits. `parent_device_id` reserved in the schema is enough.
- **Server-side compile / simulation**. Correctly V2. At this scale a bad build costs
  one reboot, once rollback is real.
- **Home Assistant, self-host TLS, groups-as-hierarchy**. Distribution and scale, not
  the on-ramp.
- **Application config through `dn/cfg`**. Correct refusal. That path becomes a worse
  ESPHome.

The docs-as-engineering-system are unusually good (`spec/` protected, `DECISIONS.md`
actually explains, protocol near-frozen and treated that way). They are also aimed at
the builder. There is no operator manual for “flash a board, upload a `.bin`, recover
from a brownout.” The dashboard must *be* that manual. It cannot be, while
Deploy hangs, Upload is a shell script, and the README says none of this exists.

---

## If you only do three things

1. Close R2 until a broken sketch comes back by itself *and the dashboard says so*.
2. Ship the Arduino library so CUJ-1 is walkable.
3. Give the running agent a way to take a new PSK without a USB cable.

Until then Fleetforge is a very good demo of Fleetforge.

---

## Related

- [`docs/HOBBYIST.md`](HOBBYIST.md) — earlier hobbyist review (2026-09-21). This file
  is a fresh read after R0 and R1 closed on metal
- [`docs/personas/PERSONAS.md`](personas/PERSONAS.md) — canonical personas and priority
  matrix
- [`spec/prd.md`](../spec/prd.md) — users, scope, KPIs
- [`spec/cujs.md`](../spec/cujs.md) — CUJ-1, currently unwalkable
- [`docs/releases.md`](releases.md) — what each release adds
- [`TODO.md`](../TODO.md) — live status
