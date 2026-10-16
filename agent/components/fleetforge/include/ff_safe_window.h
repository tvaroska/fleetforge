/*
 * ff_safe_window — your firmware says when a reboot is safe; every reboot the agent makes waits
 * for it.
 *
 * This is the MAKER API (R5-fw-5, DECISIONS 2026-10-16): a header a firmware's own code calls.
 * The stock agent has no application and therefore never includes it; the two worked examples
 * (examples/basic_idf/main/main.c, and examples/Basic through Fleetforge.onSafeWindow()) call
 * every function it declares. Like the rest of include/ it is ADDITIVE-ONLY from the release
 * that first ships it: a firmware built on it goes to boards nobody can reach again.
 *
 *     static volatile bool s_idle;      // your firmware sets it between two jobs
 *     static bool between_jobs(void *ctx) { (void)ctx; return s_idle; }
 *     ...
 *     fleetforge_start();                        // FIRST: arms the rollback timer
 *     ff_set_selftest(my_test, NULL);            // optional
 *     ff_set_safe_window_cb(between_jobs, NULL); // then this, before any slow initialisation
 *
 * What it is for: an update ends in a reboot, and so does a rollback. A board in the middle of
 * something that must not be cut short (a motor moving, a print, a valve open) registers a rule
 * that says whether a reboot right now is safe, and the agent waits for it. An image may
 * register ONE rule, and never removes it. With no rule registered every moment is safe.
 *
 * What waits for it: the reboot of an update (apply: auto, and the `apply` command), the
 * rollback after a failed confirm or a failed self-test, and the `rollback` and `reboot`
 * commands. While it says unsafe the fleet sees `awaiting_safe_window`, with the detail
 * `apply waits for a safe window` or `rollback waits for a safe window`, and the command is
 * answered `deferred`. There is no deadline on the wait: a rule that never says safe holds the
 * board until a reset.
 *
 * What does not wait: crashes, watchdogs, brownouts and power loss. And the confirm deadline
 * keeps counting while the rule says unsafe: the rollback is decided on time, only its reboot
 * waits, and the image is not confirmed meanwhile. A reset during that wait returns the board
 * to the previous image as well.
 *
 * The contract:
 *
 *  - The rule returns true when a reboot right now is safe. Keep it cheap, non-blocking and
 *    reentrant: read a flag. The agent calls it from its own tasks (its MQTT task, the OTA
 *    task and its reboot tasks), possibly at the same time, every 250 ms or so while a reboot
 *    waits, and once more immediately before every reboot it makes. Never from an ISR, never
 *    from the esp_timer task.
 *  - ff_set_safe_window_cb(fn, ctx): ESP_OK when registered. ESP_ERR_INVALID_ARG for a NULL
 *    `fn`. ESP_ERR_INVALID_STATE when a rule is already registered or the session has already
 *    started. Register right after fleetforge_start() / Fleetforge.begin() (and the self-test),
 *    as the worked examples do: the session fixes the registration when it starts, and a later
 *    one is refused. It never blocks. It logs only a refusal.
 */

#pragma once

#include <stdbool.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Your rule. `ctx` is what you registered. Return true when a reboot right now is safe. See
 * the file header for when and where it is called. */
typedef bool (*ff_safe_window_fn)(void *ctx);

/* Register this image's safe-window rule. See the file header for the contract. */
esp_err_t ff_set_safe_window_cb(ff_safe_window_fn fn, void *ctx);

#ifdef __cplusplus
}
#endif
