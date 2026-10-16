/*
 * ff_sleep — a battery board says it sleeps, and asks when a deep sleep loses nothing.
 *
 * This is the MAKER API (R5-fw-7, DECISIONS 2026-10-16): a header a firmware's own code calls.
 * The stock agent has no application and therefore never includes it, and it never sleeps; the
 * two worked examples (examples/basic_idf/main/main.c, and examples/Basic through
 * Fleetforge.setPowerClass(), isConnected() and isReadyForSleep()) call every function it
 * declares. Like the rest of include/ it is ADDITIVE-ONLY from the release that first ships it:
 * a firmware built on it goes to boards nobody can reach again.
 *
 *     fleetforge_start();                          // FIRST: arms the rollback timer
 *     ff_set_selftest(my_test, NULL);              // optional
 *     ff_set_safe_window_cb(between_jobs, NULL);   // optional
 *     ff_set_power_class(FF_POWER_SLEEPY, WAKE_S); // then this, before any slow initialisation
 *     ...
 *     do_the_work();
 *     while (!ff_is_ready_for_sleep() && esp_timer_get_time() < WAKE_LIMIT_US) vTaskDelay(pdMS_TO_TICKS(100));
 *     esp_deep_sleep(WAKE_S * 1000000ULL);
 *
 * The power class. The fleet reads `power_class` from the announce to decide when a board is
 * offline: an `always_on` board is offline the moment its broker session drops, a `sleepy` one
 * only when it misses its expected wake. The board's ff_cfg (written at flash time, never by an
 * update) says which by default. A firmware that sleeps declares it here instead, and the
 * announce carries the declared class and interval in place of ff_cfg's: a board that becomes a
 * sleeper through an update would otherwise announce `always_on` for ever, and the fleet would
 * show it offline after its first sleep. The session logs once, at its start, which class it
 * announces and which ff_cfg value it replaces.
 *
 *  - ff_set_power_class(power_class, wake_s): ESP_OK when declared. FF_POWER_SLEEPY needs
 *    `wake_s` from 1 to 2147483647 (seconds between two wakes; the fleet expects one check-in
 *    per interval); FF_POWER_ALWAYS_ON needs `wake_s` 0. Anything else is
 *    ESP_ERR_INVALID_ARG. ESP_ERR_INVALID_STATE when a class is already declared (one per
 *    image) or the session has already started: call it right after fleetforge_start() /
 *    Fleetforge.begin() and the other registrations, as the worked examples do. The session
 *    fixes the declaration when it starts; the enrollment that may precede the first connect
 *    still carries ff_cfg's class, and the first announce corrects it. It never blocks. It
 *    logs only a refusal.
 *
 * ff_is_connected(): true while a broker session is up AND this board's announce was
 * acknowledged on that session, i.e. the fleet has heard from it on this connection. False
 * before the first connect, from a disconnect until the next announce is acknowledged, and
 * before fleetforge_start().
 *
 * ff_is_ready_for_sleep(): true only when a deep sleep right now loses nothing and breaks
 * nothing. A deep-sleep wake is a reset, and at a reset the bootloader boots whatever the boot
 * pointer names and rolls back an image that is still unconfirmed. So it is false while any of
 * these holds:
 *
 *  - this wake has not checked in yet: no announce acknowledged since the boot (a wake IS a
 *    boot). Commands the fleet queued while the board slept arrive before that acknowledgement,
 *    so an update waiting for the board has started by the time this clears;
 *  - a rollback is decided or waits for the safe window, or a reboot command waits for it;
 *  - an update is in progress: from `staging` until its reboot, including an image staged with
 *    `apply: on_command` that waits for its `apply` (or `cancel`);
 *  - the running image is not confirmed yet (it was just written by an update and its confirm,
 *    or its self-test, is still pending), or the fleet has not yet acknowledged the outcome
 *    (`confirmed`, `rolled_back`) this boot owes it;
 *  - a QoS 1 message (a status, a command answer, the announce, a heartbeat) waits to be sent
 *    or acknowledged. While the broker is unreachable the agent keeps queueing heartbeats, so a
 *    wake that lost its network is normally not ready;
 *  - a sample recorded with ff_telemetry() has not been published yet. The library publishes
 *    samples at most every 5 s, so readiness can lag the last ff_telemetry() call by up to 5 s.
 *
 * Both answer false before fleetforge_start(). Neither ever logs, so a loop may call them on
 * every pass. Call them from a task, never from an ISR and never from the esp_timer task:
 * ff_is_ready_for_sleep() reads the MQTT client's outbox, which takes the client's lock, so it
 * may wait for the session (while the broker is unreachable, up to one reconnect attempt).
 *
 * Your own wake limit. A wake that never reaches the broker never becomes ready, so give your
 * loop a limit (WAKE_LIMIT_US above). Keep it longer than the confirm deadline (300 s by
 * default, the `confirm_timeout_s` of a deploy) on a slow network: a sleep is a reset, and a
 * reset before an updated image is confirmed rolls the update back.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* How the fleet decides that this board is offline. */
typedef enum {
    FF_POWER_ALWAYS_ON = 0, /* announced `always_on`, expected_wake_interval_s null */
    FF_POWER_SLEEPY = 1,    /* announced `sleepy` with expected_wake_interval_s = wake_s */
} ff_power_class_t;

/* Declare this image's power class, overriding ff_cfg's. See the file header for the contract. */
esp_err_t ff_set_power_class(ff_power_class_t power_class, uint32_t wake_s);

/* A broker session is up and it acknowledged this board's announce. See the file header. */
bool ff_is_connected(void);

/* A deep sleep right now loses nothing and breaks nothing. See the file header for the list. */
bool ff_is_ready_for_sleep(void);

#ifdef __cplusplus
}
#endif
