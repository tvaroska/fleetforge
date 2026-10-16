/*
 * ff_safe_mode — rapid-crash safe mode (R5-fw-6, R5-spec-1 D13).
 *
 * Three `panic`, `int_wdt`, `task_wdt` or `wdt` resets within 60 s on the same confirmed image
 * hold the board in safe mode: only the fleetforge agent runs, so the board still connects,
 * announces (`"safe_mode": true`), sends heartbeats and carries out `stage`, `apply`,
 * `rollback` and `reboot`, and the firmware's own code does not run. That is the OTA-only
 * recovery loop: a crash-looping image can still be updated out of trouble.
 *
 * It leaves safe mode after a successful update (another image runs), on a `reboot` command
 * (a software restart) or at a power cycle (RTC memory is lost). A brownout neither counts nor
 * leaves. Crashes of an image still waiting for its confirm are the bootloader's business (it
 * rolls such an image back) and never count.
 *
 * This is the PUBLIC half (the `fleetforge` component's include/, ADDITIVE-ONLY from the
 * release that first ships it, like ff_mqtt.h). The cached answer the announce reads is
 * component-private, in src/ff_safe_mode_internal.h.
 */

#pragma once

#include <stdbool.h>

#ifdef __cplusplus
extern "C" {
#endif

/* Decide whether THIS boot is in safe mode. Call ONCE, right after
 * ff_mqtt_arm_confirm_timer(), before anything that can fail or wait. Returns true when this
 * boot is in safe mode.
 *
 * A boot sequence that runs firmware code of its own (Fleetforge.begin(), the ESP-IDF
 * example's fleetforge_start()) must then start the agent and never return to that code; the
 * stock agent has none and simply runs as always.
 *
 * Needs nothing but RTC memory, the reset reason and otadata: no NVS, no netif, no
 * esp_timer. It never writes flash. Idempotent: a second call returns the first answer and
 * counts nothing. It logs at most one line, and only when something is worth saying. */
bool ff_safe_mode_check(void);

#ifdef __cplusplus
}
#endif
