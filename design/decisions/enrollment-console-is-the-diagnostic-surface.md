# The console panel is the diagnostic surface of record for onboarding

**Date:** 2026-09-11
**Area:** enrollment
**Status:** Accepted

## Context

The first real-hardware session put an ESP32-DevKit v1 on the bench against prod. Every
component of R0 worked — chip detect, config bake, flash, partition layout, the agent
booting and loading `ff_cfg` correctly. The board still never enrolled, because it
brownouts during Wi-Fi PHY calibration and resets, forever.

The board said so, on every cycle, in one line: `E BOD: Brownout detector was
triggered`. Diagnosing it took an engineer reading a serial log pasted by hand into a
chat window, after several wrong turns caused by the panel displaying a **Network up**
checkmark left over from an earlier boot.

Nothing server-side could have helped. A board that brownouts before associating never
reaches the network, so `device_progress`, the arrivals list and the fleet view are all
structurally blind to it — the exact limit `progress.py` already states in its module
docstring. The board's own UART is the only witness, and the browser holding the port
is the only thing that can hear it.

## Decision

Treat the console panel — not the server — as the diagnostic surface of record for
everything between "flashed" and "on the fleet", and hold it to a stated standard:
anything the board says that the panel cannot explain is a defect in the panel.

Concretely this means the panel owns three responsibilities it did not previously have:
classifying output that carries no ESP-IDF log tag, keeping its milestone claims true
over time rather than accumulating them, and producing an escalation artifact when it
cannot diagnose something itself.

The target operator is a technician with no ESP32 knowledge. That is what makes this a
standard rather than a preference: "an embedded engineer can work it out from the log"
was true on 2026-09-11 and is not a passing grade.

## Consequences

**The parser becomes load-bearing, and it is fragile.** `classifyConsoleLine` matches
log strings that exist in `agent/main/*.c`, and it failed here precisely because the
brownout line does not use the ESP-IDF log format. Every new failure mode is a parser
change. The alternative — having the agent report structured faults, starting with
`esp_reset_reason()` at boot — trades that fragility for a firmware round-trip, which is
the worst possible dependency for a board that will not come online. Recorded in
`spec/open-questions.md` rather than settled.

**Onboarding gets an acceptance criterion that cannot be automated.** "A person who has
not seen the codebase onboards a board unaided" needs a person. It is still the
criterion that decides the feature, so it is written down as one (`S0-test-3`) instead
of being replaced by the parts of it that a test runner can check.

**R0 is not done when the pieces work.** R0's stated risk is onboarding. Filing the
gap as a P0 that gates the release, rather than as polish, follows from that — the
release either retires its risk or it does not.
