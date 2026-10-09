/*
 * ff_progress — tell the server where in the boot sequence this board got to.
 *
 * The gap it closes: between "flashed" and "online" a board is invisible to the
 * dashboard. Everything agent_main.c walks through — link, clock, enroll, broker — is
 * announced on the serial console and nowhere else, and the serial console is exactly
 * what an operator does not have when a board is on a pole.
 *
 * **The honest limit, stated here so nobody builds on top of a promise this cannot
 * keep: a board with no route to the server reports nothing.** This is a debugging
 * aid for boards that get *partway* — link up but the token refused, enrolled but the
 * broker refusing the credential — and never a substitute for a serial console.
 *
 * Reports go to `POST /v1/device-progress` over the SAME HTTPS channel and under the
 * SAME `ffe_` enrollment token as ff_enroll: before it has enrolled a board holds no
 * MQTT credential, so there is no other channel and no other credential. The server
 * VERIFIES that token and never burns it, so reporting can never cost an enrolment.
 *
 * Five properties, each of which the boot sequence depends on:
 *
 *  1. **Never fatal, ever.** Every call returns void and every failure is at most one
 *     log line. A board must not fail to enroll because it could not say it was
 *     enrolling — that would turn a debugging aid into an outage.
 *  2. **Never retried and never blocking beyond its timeout** (5 s, far below the
 *     enroll path's 30 s). A stage report is stale the moment the next stage happens.
 *  3. **Self-disabling.** After a 401 the token is not going to start working, so the
 *     reporter switches itself off for this boot rather than talking to the server
 *     forever with a credential it has been told is dead.
 *  4. **The token is never logged**, in any branch. Same rule as ff_enroll.c.
 *  5. **Stages produced before the clock is set are HELD, not lost** (S0-fw-2). With
 *     CONFIG_MBEDTLS_HAVE_TIME_DATE=y a TLS handshake at epoch 0 fails certificate
 *     validity, so against an `https://` api_base every report made before
 *     ff_time_sync() — `link_up`, the first sign of life this feature exists for —
 *     used to vanish. Such a report is now buffered (3 deep, static, oldest dropped
 *     when full) and sent, oldest first, at the top of the first report made once the
 *     clock is sane, BEFORE that report's own POST, so the server's receipt order
 *     matches the board's. The gate is the URL scheme AND the clock: over `http://`
 *     nothing is ever held, which is what keeps the plaintext lab (`--no-ntp`)
 *     reporting `link_up` at link time. A held stage gets exactly one attempt, like
 *     every other report, and its `at` is the (slightly later) receipt time.
 *
 * **The honest limit in its new form: a board that never sets its clock against an
 * `https://` server still reports nothing — and that is the same board that cannot
 * enrol either**, because /v1/enroll needs the same handshake.
 */

#pragma once

#include "ff_cfg.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The stages this firmware reports. Free strings, not an enum on the wire: the server
 * stores the column as TEXT with no whitelist precisely so a newer agent can say
 * something an older server has never heard of (src/fleetforge/db/models.py ->
 * ProgressStage is advisory on that side too). */
#define FF_PROGRESS_LINK_UP "link_up"
#define FF_PROGRESS_TIME_SYNCED "time_synced"
#define FF_PROGRESS_ENROLLING "enrolling"
#define FF_PROGRESS_ENROLLED "enrolled"
#define FF_PROGRESS_MQTT_CONNECTED "mqtt_connected"
#define FF_PROGRESS_MQTT_REFUSED "mqtt_refused"
#define FF_PROGRESS_HALTED "halted"

/* Not a stage in the walk — a report ABOUT THE PREVIOUS BOOT, sent once, just after
 * `link_up`, when `esp_reset_reason()` says the last reset was a brownout. It is the
 * only way that fact ever reaches the server: a board still stuck in the loop has no
 * link and reports nothing, so this arrives from the boot that finally got through, and
 * says the rail is marginal on a board that now looks healthy. Reported after `link_up`
 * rather than before it so the "no stage precedes link_up" rule above stays true. */
#define FF_PROGRESS_BROWNOUT "brownout"

/*
 * Arm the reporter from the flash-time config. Until this is called — and after it is
 * called with a config carrying no token or no api_base — every ff_progress_report()
 * is a silent no-op, which is what makes it safe to call from park().
 *
 * Copies what it needs; `cfg` need not outlive the call.
 */
void ff_progress_init(const ff_cfg_t *cfg);

/*
 * Report one stage, best effort. `detail` may be NULL.
 *
 * `detail` is truncated to what the server accepts (200 chars) and stripped of control
 * characters, because the server refuses those outright — a park() reason with a
 * newline in it must still be reportable rather than silently 422.
 */
void ff_progress_report(const char *stage, const char *detail);

#ifdef __cplusplus
}
#endif
