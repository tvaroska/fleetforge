/*
 * ff_selftest — your firmware's own check that an update works, before the board keeps it.
 *
 * This is the MAKER API (R5-fw-1, DECISIONS 2026-10-15): a header a firmware's own code
 * calls. The stock agent has no application and therefore never includes it; the two worked
 * examples (examples/basic_idf/main/main.c, and examples/Basic through Fleetforge.onSelfTest()
 * and Fleetforge.selfTestDone()) call every function it declares. Like the rest of include/
 * it is ADDITIVE-ONLY from the release that first ships it: a firmware built on it goes to
 * boards nobody can reach again.
 *
 *     static ff_selftest_result_t my_test(void *ctx, char *reason, size_t reason_len)
 *     {
 *         if (!sensor_responds()) {
 *             snprintf(reason, reason_len, "no sensor on i2c");
 *             return FF_SELFTEST_FAIL;
 *         }
 *         return FF_SELFTEST_PASS;
 *     }
 *     ...
 *     fleetforge_start();               // FIRST: arms the rollback timer
 *     ff_set_selftest(my_test, NULL);   // then register, before any slow initialisation
 *
 * What it is for: the agent already rolls an update back when the new image cannot reach the
 * fleet (a boot loop, a broken network stack). A self-test also catches "boots but the
 * application is broken": the sensor driver hangs, the control loop never starts. An image
 * may register ONE self-test.
 *
 * When it runs: at most once per boot, and only on a boot that needs confirming (an image an
 * OTA just wrote). It runs after the board has reached the fleet (its retained announce is
 * acknowledged), and the image is confirmed only if the self-test passes before the confirm
 * deadline; otherwise the board returns to the previous image. On a serially flashed board it
 * is never called.
 *
 * Where it runs: in the agent's own task (at least 4 KB of stack, low priority). Never in an
 * ISR, never in the MQTT event handler, never in the esp_timer task. Keep it short, or return
 * FF_SELFTEST_PENDING and finish the work in your own task.
 *
 * The contract:
 *
 *  - Results: return FF_SELFTEST_PASS or FF_SELFTEST_FAIL to answer at once. On FAIL, write a
 *    short reason into `reason`: a zeroed buffer of `reason_len` bytes, at least 28. Return
 *    FF_SELFTEST_PENDING to answer later with ff_selftest_report(). Any other return value
 *    counts as FAIL (fail-safe). The FIRST answer wins: a report made while the function is
 *    still running (from inside it or from another task) beats its return value, and later
 *    answers are ignored.
 *  - Reason: printable ASCII. The agent drops other bytes and ';'. The fleet sees at most 27
 *    characters, and the device shortens a longer reason. The reason is ignored on a pass.
 *    NULL or empty is allowed.
 *  - ff_set_selftest(fn, ctx): ESP_OK when registered. ESP_ERR_INVALID_ARG for a NULL `fn`.
 *    ESP_ERR_INVALID_STATE when a self-test is already registered or the session has already
 *    started. Register before fleetforge_start() / Fleetforge.begin(), or immediately after
 *    it (the worked examples do the latter, because fleetforge_start() / begin() must stay
 *    the first statement). Do not register after slow initialisation: the session fixes the
 *    registration when it starts, and a later one is refused. It never blocks. It logs only a
 *    refusal.
 *  - ff_selftest_report(passed, reason): completes a self-test the agent is waiting for.
 *    Returns ESP_OK when this call decided the result. Returns ESP_ERR_INVALID_STATE, and
 *    changes nothing, when no self-test is waiting: none registered, not yet asked, or
 *    already decided. Calling it when nothing waits is harmless, so a firmware may call it on
 *    every loop (the worked examples do). Callable from any task, NOT from an ISR. It never
 *    blocks, never allocates and never logs.
 */

#pragma once

#include <stdbool.h>
#include <stddef.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* What a self-test answers. The numbers are ABI from the release that ships them. */
typedef enum {
    FF_SELFTEST_PASS = 0,
    FF_SELFTEST_FAIL = 1,
    FF_SELFTEST_PENDING = 2,
} ff_selftest_result_t;

/* Your self-test. `ctx` is what you registered; `reason` is a zeroed buffer of `reason_len`
 * (>= 28) bytes for a FAIL's reason. See the file header for when and where it is called. */
typedef ff_selftest_result_t (*ff_selftest_fn)(void *ctx, char *reason, size_t reason_len);

/* Register this image's self-test. See the file header for the contract. */
esp_err_t ff_set_selftest(ff_selftest_fn fn, void *ctx);

/* Answer a self-test that returned FF_SELFTEST_PENDING. See the file header. */
esp_err_t ff_selftest_report(bool passed, const char *reason);

#ifdef __cplusplus
}
#endif
