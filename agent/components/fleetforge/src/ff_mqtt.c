/*
 * ff_mqtt — the session. See ff_mqtt.h.
 *
 * The six wire properties from `src/fleetforge/simulator/device.py`, each repeated here
 * because getting one wrong is a silent wrong answer rather than an error:
 *
 * 1. announce + presence are RETAINED, hb is NOT. A retained heartbeat is replayed to the
 *    ingestor on every reconnect, and `ingestor/handlers.py` treats a retained message as
 *    a replay — `last_seen` would quietly stop advancing.
 * 2. the LWT is retained `{"online":false}` at QoS 1 on up/presence, so a server that
 *    restarts after a board died still learns it is offline.
 * 3. client_id = username = device_id, clean_session = false. Command durability comes
 *    from the persistent session, never from a retained dn/cmd.
 * 4. NO goodbye publish. A clean DISCONNECT does not fire the LWT, so the simulator
 *    publishes its own offline message on Ctrl-C — an ESP32 does not exit, so the agent
 *    deliberately has no such path. Do not "fix" this by adding one.
 * 5. subscribe to dn/# BEFORE publishing anything, so a command queued in the persistent
 *    session is drained before the board announces itself.
 * 6. a denied publish is invisible in MQTT 3.1.1: the broker drops it silently. "It
 *    published and nothing happened" is an ACL or credential symptom — which is why every
 *    publish is logged with its message id and the operator checks GET /v1/devices.
 */

#include "ff_mqtt_internal.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "ff_identity_internal.h"
#include "ff_ota.h"
#include "ff_progress.h"
#include "ff_selftest_internal.h"
#include "ff_telemetry_internal.h"
#include "ff_txn.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mqtt_client.h"

static const char *TAG = "ff-mqtt";

/* spec/device-protocol.md -> Topic namespace. Spelled once, here. */
#define TOPIC_ROOT "ff/v1/d"
#define TOPIC_MAX 96

#define QOS 1

/* 30 s, matching simulator/device.py::KEEPALIVE_S: comfortably under any NAT idle
 * timeout, and short enough that an always_on board's LWT fires within ~45 s of the
 * socket dying rather than minutes later. */
#define KEEPALIVE_S 30

/* spec/prd.md -> Requirements & targets -> Timing: "confirm timeout 300 s". The DEFAULT
 * deadline, and the one the boot arm (ff_mqtt_arm_confirm_timer, first statement of app_main,
 * before NVS) always starts with. R5-fw-3: once classify_txn() has read the transaction
 * record, the stage's own `confirm_timeout_s` (clamped to CONFIRM_TIMEOUT_MIN_S..MAX_S) takes
 * over, counted from the same image start (rearm_confirm_deadline). */
#if FF_ROLLBACK_TEST
/* 60 s in a rollback-test build. Shortening this does NOT weaken the test: the timer
 * under test is the one compiled into the NEW image, which is this one, so the
 * mechanism exercised is identical and only the wait is shorter. R5-fw-3: the same flag caps
 * a command's deadline at 60 s, so the server's 300 still gives 60 here (no re-arm, the
 * quickstart's R phase is unchanged) while a shorter command gives its own value. */
#define CONFIRM_TIMEOUT_S 60
#define CONFIRM_TIMEOUT_MAX_S 60
#else
#define CONFIRM_TIMEOUT_S 300
#define CONFIRM_TIMEOUT_MAX_S 3600
#endif
/* R5-spec-1 D10: a command's `confirm_timeout_s` is clamped to 30-3600 s. */
#define CONFIRM_TIMEOUT_MIN_S 30
_Static_assert(CONFIRM_TIMEOUT_MIN_S <= CONFIRM_TIMEOUT_S &&
                   CONFIRM_TIMEOUT_S <= CONFIRM_TIMEOUT_MAX_S,
               "the default confirm deadline must lie inside the clamp");

/* How long a timed-out image keeps running after the confirm timer fires, so a queued
 * `rolling_back` can leave the wire before the reboot. Bounded by an esp_timer that is
 * created at boot (see ff_mqtt_arm_confirm_timer), never by the MQTT client: the rollback must
 * happen whatever the session is doing. 2 s covers one pass of the esp-mqtt task loop. */
#define ROLLBACK_REPORT_GRACE_MS 2000
/* cJSON + one log line; the heartbeat task does the same work in the same size. */
#define ROLLBACK_REPORT_STACK 4096

static const char PRESENCE_ONLINE[] = "{\"online\":true}";
static const char PRESENCE_OFFLINE[] = "{\"online\":false}";

typedef struct {
    const ff_cfg_t *cfg;
    esp_mqtt_client_handle_t client;
    char topic_announce[TOPIC_MAX];
    char topic_presence[TOPIC_MAX];
    char topic_hb[TOPIC_MAX];
    char topic_status[TOPIC_MAX];
    char topic_telemetry[TOPIC_MAX];
    char topic_dn[TOPIC_MAX];
    int announce_msg_id;   /* the id whose PUBACK means "this board did its job" */
    bool session_confirmed; /* the announce has been acknowledged at least once */
    volatile bool connected; /* a broker session is up right now (read by the report task) */
    TaskHandle_t heartbeat; /* NULL until the first successful connect */
    char last_command_id[64];
} ff_mqtt_ctx_t;

static ff_mqtt_ctx_t s_ctx;

/* What this boot owes the server about the transaction ff_ota recorded before the last
 * reboot (ff_txn.h). Decided ONCE, at boot, by classify_txn() — from otadata, never from
 * the record alone — and only ever moved towards TXN_NONE afterwards. */
typedef enum {
    TXN_NONE,              /* nothing to report: the R1 behaviour */
    TXN_CONFIRMING,        /* running the recorded slot, PENDING_VERIFY: confirm or roll back */
    TXN_ALREADY_CONFIRMED, /* running the recorded slot, VALID: `confirmed` is owed */
    TXN_ROLLED_BACK,       /* the recorded slot was rejected; we are what it fell back to */
    TXN_BOOTED_NEW,        /* running the recorded slot, still NEW: the bootloader never armed
                            * verification. Nothing can roll it back. `confirmed` (with a
                            * detail) is owed once the session works. (R2b-fw-2) */
} txn_kind_t;

/* R2b-fw-2. The `detail` of a NEW-at-target `confirmed`. A constant (50 chars) so it can
 * never truncate against s_txn.detail[64]. */
#define DETAIL_BOOTED_NEW "the bootloader never armed rollback for this image"

/* R5-fw-2, R5-spec-1 D11: the self-test `detail` grammar. `selftest passed` on `confirmed`;
 * `selftest failed: <reason>; returned to <slot>` and `selftest timed out; returned to
 * <slot>` on `rolled_back`. The image under test stores the HEAD (everything before `; `)
 * in ff_txn while it still runs; the image the board returns to appends the tail. The whole
 * detail is at most DETAIL_MAX bytes (s_txn.detail[64] minus the terminator), and only the
 * reason is ever shortened. */
#define DETAIL_SELFTEST_PASSED "selftest passed"
#define DETAIL_SELFTEST_TIMED_OUT "selftest timed out"
#define DETAIL_SELFTEST_FAILED "selftest failed: "
#define DETAIL_SELFTEST_PREFIX "selftest "
#define DETAIL_RETURNED_TO "; returned to "
#define DETAIL_NO_REASON "no reason given"
#define DETAIL_MAX 63

static struct {
    txn_kind_t kind;
    char cmd_id[FF_TXN_MAX_CMD_ID];
    char detail[64];        /* slot labels only — never a URL */
    bool confirming_sent;   /* `confirming` is reported once per boot */
    int terminal_msg_id;    /* the queued `confirmed`/`rolled_back`; its PUBACK clears */
} s_txn = {.kind = TXN_NONE, .terminal_msg_id = -1};

static int enqueue_status(const char *cmd_id, const char *state, int pct, const char *detail);

/* ── the OTA confirm / rollback pair ──────────────────────────────────────────────────
 *
 * CRITICAL.md: "Device-side confirm timer / rollback path". Both functions are guarded by
 * ESP_OTA_IMG_PENDING_VERIFY and are therefore INERT on every serially flashed board: with
 * CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y and otadata written from ota-data-initial.bin,
 * the bootloader boots ota_0 in state UNDEFINED and arms no rollback timer. Only an image
 * written BY OTA enters PENDING_VERIFY.
 *
 * They are written now, at R0, for two reasons. An OTA'd image with no confirm call
 * bricks-by-rollback-loop the instant R2 ships. And the opposite mistake — calling
 * esp_ota_mark_app_valid_cancel_rollback() unconditionally at boot, which is what most
 * examples do — would silently disable auto-rollback for the whole fleet, since a bad
 * image would confirm itself before it had done anything. Hence: confirm only after the
 * board has demonstrably worked (broker connected AND the retained announce acknowledged).
 * R5-fw-2: an image that registered a custom self-test is held one step further — the ack
 * starts the self-test instead of confirming, and only its pass confirms, inside the same
 * deadline; a fail rolls back at once, with the reason stored for the image it returns to.
 *
 * Do not try to prove these by forcing the partition state; R2 owns the live test.
 *
 * R2-be-1 adds REPORTING to both branches and changes neither decision: `confirmed` is
 * queued only after esp_ota_mark_app_valid_cancel_rollback() returned ESP_OK, and the
 * timeout path may queue a best-effort `rolling_back` but never waits for it. Nothing in
 * confirm_timeout_cb() calls an esp_mqtt_client_* function — those take the client's lock,
 * which the mqtt task holds during network I/O, and a broken image is exactly the image
 * whose mqtt task may be wedged. */

static bool pending_verify(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (running == NULL || esp_ota_get_state_partition(running, &state) != ESP_OK) {
        return false;
    }
    return state == ESP_OTA_IMG_PENDING_VERIFY;
}

/* true only when this call marked an OTA image valid — the one fact `confirmed` may be
 * reported on. */
static bool confirm_this_image(void)
{
    if (!pending_verify()) {
        return false; /* the normal R0 path: nothing to confirm, nothing to roll back */
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "this image was written by OTA and is now CONFIRMED: the broker "
                      "accepted us and the retained announce was acknowledged");
        return true;
    }
    ESP_LOGE(TAG, "cannot confirm this OTA image (%s) — the bootloader will roll back "
                  "on the next reset",
             esp_err_to_name(err));
    return false;
}

/* R2b-fw-2. The TXN_BOOTED_NEW twin of confirm_this_image(): true only when THIS call moved
 * the running image from NEW to VALID, so `confirmed` still follows a successful
 * mark-valid and nothing else. Re-reads the state rather than trusting classify_txn(). In
 * IDF 5.5 mark-valid moves any non-VALID running entry to VALID; on a bootloader without
 * rollback the state is never consulted, so this changes nothing about how it boots. */
static bool accept_unverified_image(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (running == NULL || esp_ota_get_state_partition(running, &state) != ESP_OK ||
        state != ESP_OTA_IMG_NEW) {
        return false;
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "this image was written by OTA and booted without rollback armed; the "
                      "broker accepted us, so it is now marked valid and CONFIRMED");
        return true;
    }
    ESP_LOGE(TAG, "cannot mark this unverified OTA image valid (%s) — not reporting confirmed",
             esp_err_to_name(err));
    return false;
}

/* Pre-created at boot by ff_mqtt_arm_confirm_timer(), so the timeout path allocates nothing it
 * depends on. When it fires the grace period is over: roll back, reported or not. */
static esp_timer_handle_t s_rollback_timer;

/* R5-fw-3. The confirm timer itself, kept so rearm_confirm_deadline() can move it once the
 * transaction record has been read. NULL until ff_mqtt_arm_confirm_timer() created it (and on
 * every boot that is not an OTA boot). */
static esp_timer_handle_t s_confirm_timer;
/* R5-fw-3. esp_timer_get_time() at the moment the boot arm started the confirm timer: the
 * "image start" every deadline is counted from. Written once, before the timer runs. */
static int64_t s_confirm_origin_us;
/* R5-fw-3. The deadline in force, in seconds from s_confirm_origin_us: what the timeout
 * callback's log line reports. Read from the esp_timer task; an aligned 32-bit store is
 * atomic, and it is written only while the timer is stopped (or before it ever starts). */
static volatile uint32_t s_confirm_deadline_s = CONFIRM_TIMEOUT_S;

/* Set by confirm_timeout_cb() the moment it decides to roll back. Before the grace timer
 * the reboot followed that decision within microseconds; now it follows within
 * ROLLBACK_REPORT_GRACE_MS, and an announce ack arriving inside that window must not
 * confirm an image that is already on its way out — that would end in both `confirmed`
 * and `rolled_back` for one transaction. The decision made at the timeout stands. */
static volatile bool s_rollback_decided;

/* R5-fw-2. Whether this image registered a custom self-test: ff_selftest_freeze()'s answer,
 * written ONCE in ff_mqtt_run() before the first connect, so it is already true by the time
 * the announce ack can set s_ctx.session_confirmed. Set at the ack instead, there would be a
 * window where session_confirmed is true, this is still false, confirm_timeout_cb() returns,
 * and the image runs unconfirmed with no deadline. Outside s_ctx, which ff_mqtt_run() memsets.
 * (Not named ff_selftest_*: that prefix is the self-test API, and none of it belongs in the
 * timer context that reads this.) */
static volatile bool s_selftest_registered;

/* R5-fw-2. Two parties can decide a held image's fate and they race: the self-test result
 * (selftest_done(), in the ff_selftest task, possibly on the other core) and the confirm
 * deadline (confirm_timeout_cb(), in the esp_timer task). Each CLAIMS its decision under this
 * lock — s_confirm_claimed for a pass, s_rollback_decided for a fail or the deadline — and
 * the first claim wins. Only the flags are touched under it: mark-valid, mark-invalid and
 * every NVS write happen after the claim, outside the lock, because a flash write must never
 * run inside a portMUX critical section. */
static portMUX_TYPE s_decide_lock = portMUX_INITIALIZER_UNLOCKED;
static bool s_confirm_claimed;

/* R5-fw-2. `selftest failed: <reason>`, with the reason (already printable ASCII without `;`,
 * ff_selftest.c) shortened so that the returned-to image's `<head>; returned to <slot>` fits
 * DETAIL_MAX. Both A/B layouts have labels of one length (ota_0/ota_1), so the running label
 * stands in for the one the board will return to: 27 bytes of reason. */
static void selftest_failed_head(char *out, size_t len, const char *reason)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    const size_t label_len =
        running != NULL ? strnlen(running->label, sizeof(running->label)) : strlen("ota_0");
    const size_t fixed = strlen(DETAIL_SELFTEST_FAILED) + strlen(DETAIL_RETURNED_TO) + label_len;
    const int budget = fixed < DETAIL_MAX ? (int)(DETAIL_MAX - fixed) : 0;
    if (reason == NULL || reason[0] == '\0') {
        reason = DETAIL_NO_REASON;
    }
    snprintf(out, len, DETAIL_SELFTEST_FAILED "%.*s", budget, reason);
}

/* R5-fw-2. The returned-to image's `<head>; returned to <label>`. A head stored by this agent
 * already fits; as a defence against any other, the head is shortened so the whole stays
 * within DETAIL_MAX, never below the `selftest failed: ` prefix. */
static void compose_returned_detail(char *out, size_t len, const char *head, const char *label)
{
    const size_t tail_len = strlen(DETAIL_RETURNED_TO) + strlen(label);
    size_t head_len = strlen(head);
    if (head_len + tail_len > DETAIL_MAX) {
        const size_t prefix_len =
            strncmp(head, DETAIL_SELFTEST_FAILED, strlen(DETAIL_SELFTEST_FAILED)) == 0
                ? strlen(DETAIL_SELFTEST_FAILED)
                : head_len;
        const size_t room = tail_len < DETAIL_MAX ? DETAIL_MAX - tail_len : 0;
        head_len = room > prefix_len ? room : prefix_len;
    }
    snprintf(out, len, "%.*s" DETAIL_RETURNED_TO "%s", (int)head_len, head, label);
}

static void rollback_now_cb(void *arg)
{
    (void)arg;
    esp_ota_mark_app_invalid_rollback_and_reboot(); /* does not return on success */
    ESP_LOGE(TAG, "esp_ota_mark_app_invalid_rollback_and_reboot returned — there is no "
                  "other bootable slot to go back to");
}

/* One shot, off the esp_timer task: the enqueue takes the mqtt client's lock and may wait
 * on a wedged mqtt task. Waiting HERE is harmless — the rollback timer is already running
 * and does not care whether this ever finishes. */
static void rollback_report_task(void *arg)
{
    (void)arg;
    if (s_ctx.connected) {
        (void)enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLING_BACK, FF_STATUS_PCT_NONE, NULL);
    } else {
        /* It would only sit in the outbox and die with the reboot. The board we return
         * to reports `rolled_back`, which is the state that matters. */
        ESP_LOGW(TAG, "no broker session: %s for %s is not reported", FF_STATUS_ROLLING_BACK,
                 s_txn.cmd_id);
    }
    vTaskDelete(NULL);
}

/* The negative branch. Runs once, s_confirm_deadline_s after image start (CONFIRM_TIMEOUT_S
 * until the transaction record re-arms it, R5-fw-3), and only for an image the
 * bootloader is still waiting on. A board that cannot reach its fleet with a new image is
 * a board that must go back to the one that worked — unattended, because by definition
 * nobody can reach it to help. */
static void confirm_timeout_cb(void *arg)
{
    (void)arg;
    if (!pending_verify()) {
        return;
    }
    /* R5-fw-2: a working session confirms on its own only when no self-test is registered.
     * With one, the ack started the self-test and the image is still unconfirmed: the
     * deadline still applies to it. */
    if (s_ctx.session_confirmed && !s_selftest_registered) {
        return;
    }

    /* R5-fw-2: claim the rollback (see s_decide_lock). Without a self-test nothing else ever
     * claims, and this is the plain `s_rollback_decided = true` it always was. */
    bool confirm_claimed;
    bool already_decided;
    portENTER_CRITICAL(&s_decide_lock);
    confirm_claimed = s_confirm_claimed;
    already_decided = s_rollback_decided;
    if (!confirm_claimed && !already_decided) {
        s_rollback_decided = true;
    }
    portEXIT_CRITICAL(&s_decide_lock);
    if (confirm_claimed) {
        /* The self-test passed first; its confirm is being written right now. */
        ESP_LOGW(TAG, "the confirm deadline passed as a passing self-test confirms this image "
                      "— not rolling back");
        return;
    }
    if (already_decided) {
        /* The backstop: the self-test failed and claimed the rollback, but its task (priority
         * 1) has not got as far as the reboot by the deadline. Nothing to report here. */
        ESP_LOGE(TAG, "the failed self-test's rollback has not happened by the confirm "
                      "deadline — rolling back now");
        esp_ota_mark_app_invalid_rollback_and_reboot(); /* does not return */
        return;
    }
    if (s_ctx.session_confirmed) {
        ESP_LOGE(TAG, "no self-test result %d s after an OTA boot — marking this image invalid "
                      "and rolling back to the previous slot",
                 (int)s_confirm_deadline_s);
    } else {
        ESP_LOGE(TAG, "no working session %d s after an OTA boot — marking this image invalid "
                      "and rolling back to the previous slot",
                 (int)s_confirm_deadline_s);
    }

    /* The invariant: past the guard above, every path leaves either the rollback timer
     * running or the rollback already called. Nothing here blocks, and nothing here calls
     * the mqtt client. No ESP_ERROR_CHECK: an abort in PENDING_VERIFY would roll back too,
     * but by accident. With no transaction to report on (an image written by an R1 agent)
     * there is nothing to wait for, and the rollback is immediate, exactly as before. */
    if (s_txn.kind == TXN_CONFIRMING && s_rollback_timer != NULL &&
        esp_timer_start_once(s_rollback_timer, (uint64_t)ROLLBACK_REPORT_GRACE_MS * 1000ULL) ==
            ESP_OK) {
        if (xTaskCreate(rollback_report_task, "ff_rbrep", ROLLBACK_REPORT_STACK, NULL, 4, NULL) !=
            pdPASS) {
            ESP_LOGE(TAG, "cannot start the %s report — rolling back unreported in %d ms",
                     FF_STATUS_ROLLING_BACK, ROLLBACK_REPORT_GRACE_MS);
        }
        return; /* rollback_now_cb() fires in ROLLBACK_REPORT_GRACE_MS */
    }
    esp_ota_mark_app_invalid_rollback_and_reboot(); /* does not return */
}

/* R5-fw-2. The self-test's answer, from the ff_selftest task (priority 1, never the esp-mqtt
 * task or esp_timer), once per boot, for a boot the announce ack held (mqtt_event_handler).
 * Task context: it may block on the client lock, allocate and write NVS, as
 * rollback_report_task() does.
 *
 * Accepted race, documented: on a pass s_txn.kind / terminal_msg_id are written here while the
 * mqtt task may read them. They are int-sized aligned stores; the worst case is a missed
 * ff_txn_clear_if() (the PUBACK lands before terminal_msg_id is assigned) or a `confirmed`
 * repeated by a reconnect — both say the same outcome again, which the server deduplicates
 * (R2-be-1). The result is deliberately NOT handed to the mqtt task with
 * esp_mqtt_dispatch_custom_event(): with the default one-slot event queue it is drained only
 * when another mqtt event is dispatched, so it would stall while disconnected. */
static void selftest_done(bool passed, const char *reason)
{
    /* Claim first (s_decide_lock). A deadline that already claimed the rollback stands. */
    bool too_late;
    portENTER_CRITICAL(&s_decide_lock);
    too_late = s_rollback_decided;
    if (!too_late) {
        if (passed) {
            s_confirm_claimed = true;
        } else {
            s_rollback_decided = true;
        }
    }
    portEXIT_CRITICAL(&s_decide_lock);
    if (too_late) {
        ESP_LOGW(TAG, "the self-test answered after the confirm deadline — the rollback already "
                      "decided stands; NOT confirming");
        return;
    }

    const txn_kind_t kind = s_txn.kind;
    if (passed) {
        ESP_LOGW(TAG, "the custom self-test passed inside the confirm deadline — confirming "
                      "this OTA image");
        if (kind == TXN_BOOTED_NEW) {
            /* DECISIONS R5-fw-2 (5): the never-armed image keeps DETAIL_BOOTED_NEW,
             * already in s_txn.detail — the one sign this board cannot roll back by time. */
            if (accept_unverified_image()) {
                s_txn.kind = TXN_ALREADY_CONFIRMED;
                s_txn.terminal_msg_id =
                    enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, s_txn.detail);
            }
            return;
        }
        /* TXN_CONFIRMING reports; TXN_NONE (an image OTA'd by an agent before 0.4.0, no
         * record) is confirmed and reports nothing, as on the ack path. */
        const bool marked = confirm_this_image();
        if (marked && kind == TXN_CONFIRMING) {
            /* Detail before kind: a reconnect in between repeats the same words. */
            strlcpy(s_txn.detail, DETAIL_SELFTEST_PASSED, sizeof(s_txn.detail));
            s_txn.kind = TXN_ALREADY_CONFIRMED;
            s_txn.terminal_msg_id =
                enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, s_txn.detail);
        }
        return;
    }

    /* A fail. The reason goes to ff_txn NOW, at decision time and before any wait (D11), over
     * the `selftest timed out` the ack pre-stored, so the image the board returns to can say
     * why. TXN_BOOTED_NEW rolls itself back too (decision 9): IDF 5.5 never boots an INVALID
     * entry, armed bootloader or not, and a firmware that says it is broken must not keep
     * running. */
    const bool owed = kind == TXN_CONFIRMING || kind == TXN_BOOTED_NEW;
    if (owed) {
        char head[FF_TXN_MAX_DETAIL];
        selftest_failed_head(head, sizeof(head), reason);
        (void)ff_txn_set_detail(s_txn.cmd_id, head);
    }
    ESP_LOGE(TAG, "the custom self-test failed — marking this image invalid and rolling back to "
                  "the previous slot");

    if (owed && s_rollback_timer == NULL) {
        /* TXN_BOOTED_NEW: no confirm timer was armed, so nothing created this. Task context,
         * so allocating here is fine. */
        const esp_timer_create_args_t rollback_args = {
            .callback = rollback_now_cb,
            .name = "ff_rollback",
        };
        if (esp_timer_create(&rollback_args, &s_rollback_timer) != ESP_OK) {
            s_rollback_timer = NULL;
        }
    }
    /* The same invariant as confirm_timeout_cb(): from here the grace timer is running or the
     * rollback has been called. With nothing to report on, it is immediate. */
    if (owed && s_rollback_timer != NULL &&
        esp_timer_start_once(s_rollback_timer, (uint64_t)ROLLBACK_REPORT_GRACE_MS * 1000ULL) ==
            ESP_OK) {
        if (s_ctx.connected) {
            (void)enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLING_BACK, FF_STATUS_PCT_NONE, NULL);
        } else {
            ESP_LOGW(TAG, "no broker session: %s for %s is not reported", FF_STATUS_ROLLING_BACK,
                     s_txn.cmd_id);
        }
        return; /* rollback_now_cb() fires in ROLLBACK_REPORT_GRACE_MS */
    }
    esp_ota_mark_app_invalid_rollback_and_reboot(); /* does not return on success */
    ESP_LOGE(TAG, "esp_ota_mark_app_invalid_rollback_and_reboot returned — there is no other "
                  "bootable slot to go back to");
}

/* A second call would create a second one-shot confirm timer and leak its handle. */
static bool s_confirm_armed;

/* R2-fw-4: called as the first statement of app_main, so the budget counts from the moment
 * this image starts executing and no wait anywhere in app_main (network bring-up,
 * enrollment, park()) can outlast it. Everything used here exists before app_main:
 * esp_timer is started by IDF startup and pending_verify() reads otadata only. No NVS, no
 * netif, no mqtt client, no transaction record — classify_txn() may run after this. */
void ff_mqtt_arm_confirm_timer(void)
{
    if (s_confirm_armed) {
        ESP_LOGW(TAG, "the confirm timer is already armed — ignoring the second call");
        return;
    }
    s_confirm_armed = true;

    if (!pending_verify()) {
        /* Inert at R0 by design; the line is here so the log tells the truth about which
         * branch a future OTA board took. */
        ESP_LOGD(TAG, "not an OTA boot: no confirm timer armed");
        return;
    }
    /* Created now, used only if the confirm timer fires. If it cannot be created the
     * timeout path rolls back immediately, exactly as before R2-be-1. */
    const esp_timer_create_args_t rollback_args = {
        .callback = rollback_now_cb,
        .name = "ff_rollback",
    };
    if (esp_timer_create(&rollback_args, &s_rollback_timer) != ESP_OK) {
        s_rollback_timer = NULL;
        ESP_LOGW(TAG, "no rollback grace timer: a timed-out image will roll back without "
                      "reporting %s",
                 FF_STATUS_ROLLING_BACK);
    }

    const esp_timer_create_args_t args = {
        .callback = confirm_timeout_cb,
        .name = "ff_confirm",
    };
    esp_timer_handle_t timer = NULL;
    if (esp_timer_create(&args, &timer) == ESP_OK) {
        /* R5-fw-3: kept, with its origin and deadline, so the transaction record can re-arm it
         * for the stage's own deadline counted from this same moment. */
        s_confirm_timer = timer;
        s_confirm_deadline_s = CONFIRM_TIMEOUT_S;
        s_confirm_origin_us = esp_timer_get_time();
        ESP_ERROR_CHECK(esp_timer_start_once(timer, (uint64_t)CONFIRM_TIMEOUT_S * 1000000ULL));
        ESP_LOGW(TAG, "OTA boot: %d s from now to reach the fleet or roll back",
                 CONFIRM_TIMEOUT_S);
    } else {
        ESP_LOGE(TAG, "cannot create the confirm timer: this OTA image cannot roll itself "
                      "back — only a reset returns this board to its previous slot");
    }
}

/* R5-spec-1 D10: a requested confirm deadline, clamped to CONFIRM_TIMEOUT_MIN_S..MAX_S.
 * Compared as a double BEFORE any cast (casting an out-of-range double is undefined): NaN,
 * negatives and 0 give the minimum, anything past the maximum (inf too) gives the maximum,
 * and a fraction is truncated. */
static uint32_t confirm_timeout_clamp(double requested_s)
{
    if (!(requested_s >= CONFIRM_TIMEOUT_MIN_S)) {
        return CONFIRM_TIMEOUT_MIN_S;
    }
    if (requested_s > CONFIRM_TIMEOUT_MAX_S) {
        return CONFIRM_TIMEOUT_MAX_S;
    }
    return (uint32_t)requested_s;
}

/* R5-fw-3. Move the running confirm timer to `deadline_s` seconds after image start
 * (s_confirm_origin_us), once classify_txn() has read the stage's deadline from the
 * transaction record. Main task, before the first connect; never from esp_timer.
 *
 * The boot arm's 300 s (60 s under FF_ROLLBACK_TEST) stays the deadline for everything before
 * this point: an image that hangs before ff_mqtt_run() never gets here and keeps it.
 *
 * esp_timer_stop() + esp_timer_start_once(), NEVER esp_timer_restart(): IDF 5.5's restart
 * checks whether the timer is armed BEFORE it takes the timer-list lock, so it can re-insert a
 * timer that has just been dispatched and the callback runs twice — the second run takes the
 * backstop branch and reboots without the `rolling_back` report. esp_timer_stop() checks
 * under the lock: when it fails the timer has fired (or is firing), the decision is made, and
 * it stands.
 *
 * Not paused while the moment is unsafe (R5-spec-1 D10): the safe-window wait (R5-fw-5) must
 * never stop, extend or re-arm this timer. Only the reboot waits. */
static void rearm_confirm_deadline(uint32_t deadline_s)
{
    if (!s_confirm_armed || s_confirm_timer == NULL) {
        ESP_LOGE(TAG, "transaction %s: no confirm timer to re-arm — the boot arm's outcome "
                      "stands",
                 s_txn.cmd_id);
        return;
    }
    if (deadline_s == s_confirm_deadline_s) {
        ESP_LOGI(TAG, "transaction %s: confirm deadline stays %u s", s_txn.cmd_id,
                 (unsigned)deadline_s);
        return;
    }

    int64_t remaining_us =
        (int64_t)deadline_s * 1000000 - (esp_timer_get_time() - s_confirm_origin_us);
    if (remaining_us < 0) {
        remaining_us = 0; /* already overdue: it fires at once, in the esp_timer task */
    }
    if (esp_timer_stop(s_confirm_timer) != ESP_OK) {
        ESP_LOGW(TAG, "transaction %s: the confirm timer has already fired — its decision "
                      "stands",
                 s_txn.cmd_id);
        return;
    }
    /* Stopped: the callback cannot run until the start below, so this store is race-free. */
    s_confirm_deadline_s = deadline_s;
    /* Cannot fail after a successful stop. If it did, the abort resets an image still in
     * PENDING_VERIFY and the bootloader rolls it back: the boot arm's own posture. */
    ESP_ERROR_CHECK(esp_timer_start_once(s_confirm_timer, (uint64_t)remaining_us));
    ESP_LOGW(TAG, "transaction %s: confirm deadline %u s from image start (%d s left)",
             s_txn.cmd_id, (unsigned)deadline_s, (int)(remaining_us / 1000000));
}

/* ── the transaction that crossed the reboot (R2-be-1) ──────────────────────────────
 *
 * Read once, before connecting. The record says WHICH transaction; otadata says WHAT
 * happened to it. No outcome is ever authored from the record alone: a stale record that
 * turned into a false `rolled_back` would be a lie kept forever in deploy_events. */

static void classify_txn(void)
{
    s_txn.kind = TXN_NONE;
    s_txn.cmd_id[0] = '\0';
    s_txn.detail[0] = '\0';
    s_txn.confirming_sent = false;
    s_txn.terminal_msg_id = -1;

    /* R2b-fw-2: the measurement behind `rollback_capable: true`. Only the bootloader ever
     * writes PENDING_VERIFY (NEW -> PENDING_VERIFY; the app side never does), so booting in
     * it proves this board's bootloader rolls back — with or without a transaction record.
     * Deliberately before the record: an image OTA'd by an agent older than 0.4.0 left no
     * record, and its first boot of this agent must still learn it. TXN_CONFIRMING below
     * implies PENDING_VERIFY, so that branch is covered too. Main task, before
     * esp_mqtt_client_start(): every announce is built after this. */
    if (pending_verify()) {
        ff_identity_note_rollback_capable();
    }

    ff_txn_t rec;
    esp_err_t err = ff_txn_load(&rec);
    if (err == ESP_ERR_NVS_NOT_FOUND) {
        return;
    }
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read the transaction record (%s) — no outcome will be reported "
                      "for it",
                 esp_err_to_name(err));
        return;
    }
    strlcpy(s_txn.cmd_id, rec.cmd_id, sizeof(s_txn.cmd_id));

    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running != NULL && running->address == rec.target_addr) {
        esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
        if (esp_ota_get_state_partition(running, &state) == ESP_OK) {
            if (state == ESP_OTA_IMG_PENDING_VERIFY) {
                s_txn.kind = TXN_CONFIRMING;
                ESP_LOGW(TAG, "transaction %s: confirming on %s", s_txn.cmd_id, running->label);
                /* R5-fw-3: the stage's deadline, clamped again with THIS image's limits (an
                 * FF_ROLLBACK_TEST image caps it at 60 s). 0 = a record from before R5-fw-3. */
                rearm_confirm_deadline(rec.confirm_timeout_s == 0
                                           ? CONFIRM_TIMEOUT_S
                                           : confirm_timeout_clamp((double)rec.confirm_timeout_s));
                return;
            }
            if (state == ESP_OTA_IMG_VALID) {
                /* We confirmed, then reset before the `confirmed` PUBACK. Say it again; the
                 * server deduplicates. R5-fw-2: a stored self-test head means this image was
                 * held, and under the hold VALID at the target is reachable only through a
                 * self-test pass — so the repeat says the same words as the first time. */
                s_txn.kind = TXN_ALREADY_CONFIRMED;
                if (strncmp(rec.detail, DETAIL_SELFTEST_PREFIX, strlen(DETAIL_SELFTEST_PREFIX)) ==
                    0) {
                    strlcpy(s_txn.detail, DETAIL_SELFTEST_PASSED, sizeof(s_txn.detail));
                }
                ESP_LOGW(TAG, "transaction %s: already confirmed on %s", s_txn.cmd_id,
                         running->label);
                return;
            }
            if (state == ESP_OTA_IMG_NEW) {
                /* R2b-fw-2. The image applied and is running, but the bootloader never put
                 * it in PENDING_VERIFY: no rollback is armed and none can happen. Not
                 * `failed` — fw_version already shows the target — and not `confirming`,
                 * which would promise a deadline that does not exist. `confirmed` with a
                 * detail at the announce ack. Nothing is stored or announced about the
                 * board here: `false` waits for R2b-test-5 (DECISIONS 2026-10-04 A3). */
                s_txn.kind = TXN_BOOTED_NEW;
                strlcpy(s_txn.detail, DETAIL_BOOTED_NEW, sizeof(s_txn.detail));
                ESP_LOGW(TAG, "transaction %s: running %s in state new — the bootloader never "
                              "put it in pending_verify; rollback_capable would be false, "
                              "which this agent does not report until it is benched "
                              "(R2b-test-5)",
                         s_txn.cmd_id, running->label);
                return;
            }
        }
    } else if (running != NULL) {
        /* INVALID (our confirm timer) or ABORTED (the bootloader found it still
         * PENDING_VERIFY at a reset): either way the board left the recorded slot. */
        const esp_partition_t *invalid = esp_ota_get_last_invalid_partition();
        if (invalid != NULL && invalid->address == rec.target_addr) {
            s_txn.kind = TXN_ROLLED_BACK;
            if (strncmp(rec.detail, DETAIL_SELFTEST_PREFIX, strlen(DETAIL_SELFTEST_PREFIX)) ==
                0) {
                /* R5-fw-2: the image under test stored why it is gone (its self-test failed,
                 * or no result came before the deadline or a reset). */
                compose_returned_detail(s_txn.detail, sizeof(s_txn.detail), rec.detail,
                                        running->label);
            } else {
                snprintf(s_txn.detail, sizeof(s_txn.detail), "returned to %s; %s did not confirm",
                         running->label, invalid->label);
            }
            ESP_LOGW(TAG, "transaction %s: rolled_back (%s)", s_txn.cmd_id, s_txn.detail);
            return;
        }
    }

    /* A USB re-flash over a live record, an UNDEFINED state, anything without evidence. */
    ESP_LOGW(TAG, "stale transaction record for %s — discarded", s_txn.cmd_id);
    (void)ff_txn_clear_if(s_txn.cmd_id);
    s_txn.cmd_id[0] = '\0';
}

/* On every connect, after the announce: whatever this boot still owes. Mqtt-task context,
 * so everything here is enqueued, not published. */
static void report_txn_on_connect(void)
{
    switch (s_txn.kind) {
    case TXN_CONFIRMING:
        if (!s_txn.confirming_sent) {
            s_txn.confirming_sent =
                enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMING, FF_STATUS_PCT_NONE, NULL) > 0;
        }
        break;
    case TXN_ALREADY_CONFIRMED:
        /* `detail` is empty on every path but a NEW-at-target confirm (R2b-fw-2) and a
         * self-test pass (R5-fw-2), which a reconnect before the PUBACK must repeat word for
         * word. */
        if (s_txn.terminal_msg_id <= 0) {
            s_txn.terminal_msg_id =
                enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100,
                               s_txn.detail[0] != '\0' ? s_txn.detail : NULL);
        }
        break;
    case TXN_BOOTED_NEW:
        /* Nothing on connect: `confirming` would promise a rollback deadline this image
         * does not have. `confirmed` follows the announce ack (mqtt_event_handler). */
        break;
    case TXN_ROLLED_BACK:
        if (s_txn.terminal_msg_id <= 0) {
            s_txn.terminal_msg_id = enqueue_status(s_txn.cmd_id, FF_STATUS_ROLLED_BACK,
                                                   FF_STATUS_PCT_NONE, s_txn.detail);
        }
        break;
    case TXN_NONE:
    default:
        break;
    }
}

/* ── the session ──────────────────────────────────────────────────────────────────── */

static void heartbeat_task(void *arg)
{
    ff_mqtt_ctx_t *ctx = (ff_mqtt_ctx_t *)arg;
    const int64_t boot_us = esp_timer_get_time();
    while (true) {
        vTaskDelay(pdMS_TO_TICKS((uint32_t)ctx->cfg->hb_s * 1000));
        uint32_t uptime_s = (uint32_t)((esp_timer_get_time() - boot_us) / 1000000);
        char *payload = ff_identity_heartbeat_json(uptime_s);
        if (payload == NULL) {
            continue;
        }
        /* retain = 0. See property 1 at the top of this file — this is the one publish
         * whose retain flag must be zero, and the failure it causes is silent. */
        int id = esp_mqtt_client_publish(ctx->client, ctx->topic_hb, payload, 0, QOS, 0);
        ESP_LOGI(TAG, "publish %s (qos 1, no retain, msg_id %d, uptime %" PRIu32 " s)",
                 ctx->topic_hb, id, uptime_s);
        free(payload);
    }
}

static void on_connected(ff_mqtt_ctx_t *ctx)
{
    ESP_LOGI(TAG, "mqtt connected as %s (%s)", ff_device_id(), ctx->cfg->mqtt_uri);

    /* The last stage of an arrival, and the one that ends it: the retained announce
     * published a few lines below is what puts this board in the fleet proper, after
     * which the dashboard stops listing it as arriving at all. Reported anyway, because
     * the gap between this and the announce landing is where a broker ACL problem
     * lives. A no-op on a board that had a stored credential and never enrolled. */
    ff_progress_report(FF_PROGRESS_MQTT_CONNECTED, NULL);

    /* FIRST, before any publish: a command queued in the persistent session must be
     * drained before this board tells the fleet it is here. */
    int id = esp_mqtt_client_subscribe(ctx->client, ctx->topic_dn, QOS);
    ESP_LOGI(TAG, "subscribe %s (msg_id %d)", ctx->topic_dn, id);

    char *announce = ff_identity_announce_json(ctx->cfg);
    if (announce != NULL) {
        ctx->announce_msg_id =
            esp_mqtt_client_publish(ctx->client, ctx->topic_announce, announce, 0, QOS, 1);
        ESP_LOGI(TAG, "publish %s (qos 1, retain, msg_id %d)", ctx->topic_announce,
                 ctx->announce_msg_id);
#if FF_ROLLBACK_TEST
        /* The single injected fault. The announce IS published and IS retained, so the
         * board really does appear in the fleet on this version — that is what makes the
         * rollback observable from the dashboard rather than only from a serial console.
         * What we throw away is the msg_id, so the PUBACK can never match and
         * `session_confirmed` stays false forever. That is precisely the state
         * `confirm_timeout_cb()` exists to rescue a board from, and the only branch of
         * the confirm/rollback pair that has never run on hardware. */
        ctx->announce_msg_id = -1;
        ESP_LOGE(TAG, "FF_ROLLBACK_TEST: ignoring the announce ack on purpose — this "
                      "image must roll back in %d s",
                 (int)s_confirm_deadline_s);
#endif
        free(announce);
    } else {
        ESP_LOGE(TAG, "out of memory building the announce payload");
    }

    id = esp_mqtt_client_publish(ctx->client, ctx->topic_presence, PRESENCE_ONLINE,
                                 (int)strlen(PRESENCE_ONLINE), QOS, 1);
    ESP_LOGI(TAG, "publish %s (qos 1, retain, msg_id %d) %s", ctx->topic_presence, id,
             PRESENCE_ONLINE);

    ctx->connected = true;
    /* After the announce, so the server already knows which version is running when the
     * outcome lands. */
    report_txn_on_connect();

    /* One task for the life of the process: esp-mqtt reconnects underneath us, and
     * restarting the heartbeat on every reconnect would drift the interval. */
    if (ctx->heartbeat == NULL) {
        if (xTaskCreate(heartbeat_task, "ff_hb", 4096, ctx, 4, &ctx->heartbeat) != pdPASS) {
            ESP_LOGE(TAG, "cannot start the heartbeat task");
            ctx->heartbeat = NULL;
        } else {
            ESP_LOGI(TAG, "heartbeat every %d s -> %s (not retained)", ctx->cfg->hb_s,
                     ctx->topic_hb);
        }
    }

    /* R4-fw-2. Last: identity, presence and the owed outcome are queued first, and the
     * telemetry drain publishes through the same client lock, so it cannot overtake them. */
    ff_telemetry_session_up(ctx->client, ctx->topic_telemetry);
}

/* The `up/status` body, key for key as `simulator/device.py::_status()` builds it and
 * `ingestor/protocol.py::StatusPayload` parses it — including the explicit nulls. Heap;
 * the caller frees it. NULL when out of memory (already logged). */
static char *build_status_payload(const char *cmd_id, const char *state, int pct,
                                  const char *detail)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        ESP_LOGE(TAG, "out of memory building a status payload (%s)", state);
        return NULL;
    }
    cJSON_AddStringToObject(root, "cmd_id", cmd_id);
    cJSON_AddStringToObject(root, "state", state);
    if (pct < 0) {
        cJSON_AddNullToObject(root, "pct");
    } else {
        cJSON_AddNumberToObject(root, "pct", pct);
    }
    if (detail != NULL) {
        cJSON_AddStringToObject(root, "detail", detail);
    } else {
        cJSON_AddNullToObject(root, "detail");
    }

    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        ESP_LOGE(TAG, "out of memory serialising a status payload (%s)", state);
    }
    return payload;
}

void ff_mqtt_publish_status(const char *cmd_id, const char *state, int pct, const char *detail)
{
    if (s_ctx.client == NULL) {
        /* Before ff_mqtt_run() there is no session to publish on. Cannot happen today —
         * the only caller is ff_ota, and a command can only arrive over a live session —
         * but a dropped status is an outcome lost forever, so it says so. */
        ESP_LOGE(TAG, "cannot report %s for %s: there is no mqtt session yet", state, cmd_id);
        return;
    }
    char *payload = build_status_payload(cmd_id, state, pct, detail);
    if (payload == NULL) {
        return;
    }

    /* retain = 1, and this is the third publish with a retain flag that is load-bearing:
     * the ingestor re-subscribes on every connect, so the retained status is how an
     * outcome published while it was down is delivered at all. */
    int msg_id = esp_mqtt_client_publish(s_ctx.client, s_ctx.topic_status, payload, 0, QOS, 1);
    /* `state` and `pct` only. `detail` is not logged: it is the one field a future caller
     * could put a signed URL in, and the serial console is read by people. */
    ESP_LOGI(TAG, "publish %s (qos 1, retain, msg_id %d) cmd_id=%s state=%s pct=%d",
             s_ctx.topic_status, msg_id, cmd_id, state, pct);
    free(payload);
}

/* The same message, QUEUED rather than sent: esp_mqtt_client_enqueue() stores it in the
 * outbox and the mqtt task sends it. The variant for the esp-mqtt event handler and for the
 * rollback report task. QoS 1, retained (same reason as above), store = true so a reconnect
 * resends it. Returns the msg_id — the PUBACK to wait for — or a value <= 0 on failure. */
static int enqueue_status(const char *cmd_id, const char *state, int pct, const char *detail)
{
    if (s_ctx.client == NULL) {
        ESP_LOGE(TAG, "cannot report %s for %s: there is no mqtt session yet", state, cmd_id);
        return -1;
    }
    char *payload = build_status_payload(cmd_id, state, pct, detail);
    if (payload == NULL) {
        return -1;
    }
    int msg_id =
        esp_mqtt_client_enqueue(s_ctx.client, s_ctx.topic_status, payload, 0, QOS, 1, true);
    /* Same rule as above: never `detail`. */
    ESP_LOGI(TAG, "publish %s (qos 1, retain, queued, msg_id %d) cmd_id=%s state=%s pct=%d",
             s_ctx.topic_status, msg_id, cmd_id, state, pct);
    free(payload);
    return msg_id;
}

/* ── commands ─────────────────────────────────────────────────────────────────────── */

/* Copy one string field into a fixed buffer, distinguishing "absent" from "would be
 * truncated". The distinction is the point: a silently truncated URL is a fetch of a
 * mangled link whose failure looks like a network fault, and a silently truncated digest
 * matches nothing. Both are reported as what they are. */
typedef enum {
    FIELD_OK,
    FIELD_MISSING,
    FIELD_TOO_LONG,
} field_result_t;

static field_result_t take_string(const cJSON *obj, const char *key, char *out, size_t len)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
    if (!cJSON_IsString(item) || item->valuestring[0] == '\0') {
        return FIELD_MISSING;
    }
    if (strlcpy(out, item->valuestring, len) >= len) {
        out[0] = '\0';
        return FIELD_TOO_LONG;
    }
    return FIELD_OK;
}

/* Exactly 64 characters, each 0-9 or a-f, then NUL: the server's own rule
 * (`storage/blobs.py::SHA256_HEX`) and what ff_ota.h documents. Never normalised, always
 * rejected (the identity.py idiom): an uppercase digest is not "the same digest", it is a
 * command no server of ours wrote. Hand-rolled on purpose — isxdigit() accepts A-F. */
static bool is_lowercase_sha256(const char *s)
{
    for (size_t i = 0; i < 64; i++) {
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) {
            return false; /* also stops at an early NUL: a short digest */
        }
    }
    return s[64] == '\0';
}

/* Parse a `stage` and hand it to ff_ota. This function is the command seam: it is the only
 * place the `dn/cmd` JSON is interpreted, and what leaves it is a plain C struct — ff_ota.c
 * never sees a cJSON object and never builds a topic string. */
static void on_stage(const cJSON *root, const char *id)
{
    ff_ota_cmd_t cmd;
    memset(&cmd, 0, sizeof(cmd));

    if (strlcpy(cmd.cmd_id, id, sizeof(cmd.cmd_id)) >= sizeof(cmd.cmd_id)) {
        /* No `failed` publish: it could only carry the TRUNCATED id, which maps to no
         * transaction the server knows about, and the ingestor would file an outcome
         * against a command nobody issued. */
        ESP_LOGE(TAG, "stage command id is longer than this firmware's %u-byte buffer — "
                      "ignored; no status can be reported against an id we cannot echo",
                 (unsigned)sizeof(cmd.cmd_id));
        return;
    }

    /* A re-delivery of the transaction this board already has (R2-fw-6). on_command()'s
     * dedupe remembers one id, so after any other command the broker's redelivery or the
     * server's re-POST (`reused: true`, same id) gets here. Refusing it would publish
     * `failed` against the update that is running or staged, and the server would end the
     * row and drop the real outcome. Nothing in it is parsed or acted on. */
    if (ff_ota_is_handling(cmd.cmd_id)) {
        ESP_LOGW(TAG, "stage id=%s is the update this board is already carrying out — "
                      "ignored (re-delivery); its outcome is reported when it ends",
                 cmd.cmd_id);
        return;
    }

    const cJSON *artifact = cJSON_GetObjectItemCaseSensitive(root, "artifact");
    if (!cJSON_IsObject(artifact)) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "no artifact in command");
        return;
    }

    switch (take_string(artifact, "url", cmd.url, sizeof(cmd.url))) {
    case FIELD_OK:
        break;
    case FIELD_MISSING:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "artifact url missing");
        return;
    case FIELD_TOO_LONG:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "artifact url too long");
        return;
    }
    /* Unverified bytes are never applied, so a command with no usable digest is not a
     * command this agent can carry out at all. A digest that is present but not spelled
     * the one way the server spells it (64 lowercase hex) is refused HERE, before any I/O:
     * the alternative is erasing the spare slot — the previous known-good image — and
     * downloading for minutes to learn what the command already said (R2-fw-1). */
    switch (take_string(artifact, "sha256", cmd.sha256, sizeof(cmd.sha256))) {
    case FIELD_OK:
        break;
    case FIELD_MISSING:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "artifact sha256 missing");
        return;
    case FIELD_TOO_LONG:
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "artifact sha256 malformed");
        return;
    }
    if (!is_lowercase_sha256(cmd.sha256)) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "artifact sha256 malformed");
        return;
    }
    /* Optional, and only ever logged. */
    (void)take_string(artifact, "version", cmd.version, sizeof(cmd.version));

    const cJSON *size = cJSON_GetObjectItemCaseSensitive(artifact, "size");
    if (cJSON_IsNumber(size) && size->valuedouble > 0) {
        cmd.size = (size_t)size->valuedouble;
    }

    /* `artifact.sig` is parsed by nobody and required by nobody: spec/device-protocol.md
     * shows the field, `broker/commands.py::stage_payload()` deliberately never emits it
     * (R1 has no artifact signer, and an empty `sig` would teach a device to accept one),
     * and the signed URL IS the authorization at R1. Ignored, not rejected. */

    /* `apply`: anything other than the explicit `on_command` means apply now. The default
     * is "auto" (api/schemas.py), and an unknown value erring towards applying matches the
     * simulator — a board that stages and then sits there is the failure nobody notices. */
    cmd.apply_now = true;
    const cJSON *apply = cJSON_GetObjectItemCaseSensitive(root, "apply");
    if (cJSON_IsString(apply) && strcmp(apply->valuestring, "on_command") == 0) {
        cmd.apply_now = false;
    }

    /* `confirm_timeout_s` (R5-fw-3, R5-spec-1 D10): the new image's confirm deadline,
     * counted from its start. Resolved HERE, the one place the wire is interpreted: clamped
     * to 30-3600 s, CONFIRM_TIMEOUT_S when absent or not a number. ff_ota records it in the
     * transaction; the new image re-arms its confirm timer with it once it reads the record.
     * Logged without %f: newlib-nano's printf may have no float support. */
    cmd.confirm_timeout_s = CONFIRM_TIMEOUT_S;
    const cJSON *confirm = cJSON_GetObjectItemCaseSensitive(root, "confirm_timeout_s");
    if (cJSON_IsNumber(confirm)) {
        cmd.confirm_timeout_s = confirm_timeout_clamp(confirm->valuedouble);
        if (!(confirm->valuedouble >= CONFIRM_TIMEOUT_MIN_S &&
              confirm->valuedouble <= CONFIRM_TIMEOUT_MAX_S)) {
            ESP_LOGW(TAG, "stage id=%s: confirm_timeout_s is outside %d-%d s — using %u s",
                     cmd.cmd_id, CONFIRM_TIMEOUT_MIN_S, CONFIRM_TIMEOUT_MAX_S,
                     (unsigned)cmd.confirm_timeout_s);
        }
    } else if (confirm != NULL) {
        ESP_LOGW(TAG, "stage id=%s: confirm_timeout_s is not a number — using %u s", cmd.cmd_id,
                 (unsigned)cmd.confirm_timeout_s);
    }

    esp_err_t err = ff_ota_start(&cmd);
    if (err == ESP_ERR_INVALID_STATE) {
        /* Reported against the NEW cmd_id — a different one; the same id never gets here
         * (ff_ota_is_handling() above). The honest answer, and it keeps deploy_events
         * truthful: this command was received and will not be carried out. */
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "another update is already in progress");
    } else if (err != ESP_OK) {
        ff_mqtt_publish_status(cmd.cmd_id, FF_STATUS_FAILED, FF_STATUS_PCT_NONE,
                               "cannot start the update");
        ESP_LOGE(TAG, "ff_ota_start: %s", esp_err_to_name(err));
    }
}

static void on_command(ff_mqtt_ctx_t *ctx, const char *topic, int topic_len, const char *data,
                       int data_len)
{
    cJSON *root = cJSON_ParseWithLength(data, (size_t)data_len);
    const char *type = "?";
    const char *id = NULL;
    if (root != NULL) {
        const cJSON *type_item = cJSON_GetObjectItemCaseSensitive(root, "type");
        const cJSON *id_item = cJSON_GetObjectItemCaseSensitive(root, "id");
        if (cJSON_IsString(type_item)) {
            type = type_item->valuestring;
        }
        if (cJSON_IsString(id_item)) {
            id = id_item->valuestring;
        }
    }

    /* Dedupe on `id`: with clean_session=false and QoS 1 the broker WILL redeliver, and
     * spec/device-protocol.md makes commands idempotent by id for exactly this reason. */
    if (id != NULL && strcmp(id, ctx->last_command_id) == 0) {
        ESP_LOGW(TAG, "duplicate command id=%s — ignored (QoS 1 redelivery)", id);
        cJSON_Delete(root);
        return;
    }
    if (id != NULL) {
        strlcpy(ctx->last_command_id, id, sizeof(ctx->last_command_id));
    }

    ESP_LOGI(TAG, "%.*s id=%s type=%s", topic_len, topic, id != NULL ? id : "?", type);

    if (strcmp(type, "stage") == 0) {
        if (id == NULL) {
            /* A status with no cmd_id is dropped by the ingestor by design, so there is
             * nothing to report against — say so on the console instead. */
            ESP_LOGE(TAG, "a stage command with no id cannot be deduplicated or reported "
                          "against — ignored");
        } else {
            on_stage(root, id);
        }
    } else {
        /* Every other type in spec/device-protocol.md — apply, cancel, rollback, identify,
         * reboot, set_cfg — is unimplemented at R1. NOT reported as `failed`: an unknown
         * type is not a failed deploy, it has no cmd_id the server is tracking as one, and
         * MQTT 3.1.1 gives the broker no way to deny it anyway. */
        ESP_LOGW(TAG, "command type=%s is not implemented by this agent — ignored", type);
    }
    cJSON_Delete(root);
}

static void mqtt_event_handler(void *arg, esp_event_base_t base, int32_t event_id, void *data)
{
    (void)base;
    ff_mqtt_ctx_t *ctx = (ff_mqtt_ctx_t *)arg;
    const esp_mqtt_event_handle_t event = (esp_mqtt_event_handle_t)data;

    switch ((esp_mqtt_event_id_t)event_id) {
    case MQTT_EVENT_CONNECTED:
        on_connected(ctx);
        break;

    case MQTT_EVENT_DISCONNECTED:
        /* Not an error and not handled: esp-mqtt reconnects with its own backoff. Logged
         * because the serial console is the only diagnostic a board has. */
        ctx->connected = false;
        ff_telemetry_session_down(); /* samples buffer until the next connect */
        ESP_LOGW(TAG, "mqtt disconnected; the client will reconnect");
        break;

    case MQTT_EVENT_SUBSCRIBED:
        ESP_LOGI(TAG, "subscribed (msg_id %d)", event->msg_id);
        break;

    case MQTT_EVENT_PUBLISHED:
        /* The PUBACK for the retained announce is the moment this board has demonstrably
         * done its job: authenticated, authorised (property 6 — a denied publish never
         * gets here) and accepted. It is the only thing allowed to confirm an OTA image. */
        if (event->msg_id == ctx->announce_msg_id && !ctx->session_confirmed) {
            ctx->session_confirmed = true;
            ESP_LOGI(TAG, "announce acknowledged by the broker");
            if (s_rollback_decided) {
                ESP_LOGW(TAG, "the announce ack arrived after the confirm timeout — the "
                              "rollback already decided stands; NOT confirming");
            } else if (s_selftest_registered &&
                       (pending_verify() || s_txn.kind == TXN_BOOTED_NEW)) {
                /* R5-fw-2: the hold. This image registered a custom self-test, so reaching
                 * the fleet is no longer enough: the ack starts the self-test in its own task
                 * and only its pass confirms (selftest_done), inside the same deadline. The
                 * outcome head is pre-stored STRICTLY BEFORE the run — selftest_done may run at
                 * once on the other core, and its `failed` head must not be overwritten — so a
                 * deadline (or a reset) with the self-test still pending reports `selftest
                 * timed out` and the esp_timer path never writes NVS. TXN_BOOTED_NEW has no
                 * deadline and no pre-store. */
                if (s_txn.kind == TXN_CONFIRMING) {
                    (void)ff_txn_set_detail(s_txn.cmd_id, DETAIL_SELFTEST_TIMED_OUT);
                }
                esp_err_t err = ff_selftest_run(selftest_done);
                if (err != ESP_OK) {
                    /* Fail-safe: no confirm without a pass. In pending_verify the deadline
                     * rolls this image back with the pre-stored `selftest timed out`. */
                    ESP_LOGE(TAG, "cannot start the custom self-test (%s) — NOT confirming",
                             esp_err_to_name(err));
                }
            } else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING) {
                /* Reported only on a successful mark-valid. If marking failed nothing is
                 * said: the bootloader rolls back at the next reset and the image we
                 * return to reports `rolled_back`. From here this boot owes `confirmed`,
                 * and a reconnect before it is queued retries it. */
                s_txn.kind = TXN_ALREADY_CONFIRMED;
                s_txn.terminal_msg_id =
                    enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, NULL);
            } else if (s_txn.kind == TXN_BOOTED_NEW && accept_unverified_image()) {
                /* R2b-fw-2. The same gate, for an image the bootloader never armed: the
                 * deploy ends `confirmed`, with the detail naming what was missing. */
                s_txn.kind = TXN_ALREADY_CONFIRMED;
                s_txn.terminal_msg_id =
                    enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, s_txn.detail);
            }
        }
        /* The terminal state is delivered: the transaction is closed, and only now is the
         * record cleared. A reset before this re-derives the same outcome from otadata at
         * the next boot and says it again, which the server deduplicates. Clearing at
         * enqueue time would trade that harmless repeat for a lost outcome. */
        if (s_txn.kind != TXN_NONE && s_txn.terminal_msg_id > 0 &&
            event->msg_id == s_txn.terminal_msg_id) {
            (void)ff_txn_clear_if(s_txn.cmd_id);
            s_txn.kind = TXN_NONE;
            s_txn.terminal_msg_id = -1;
        }
        break;

    case MQTT_EVENT_DATA:
        on_command(ctx, event->topic, event->topic_len, event->data, event->data_len);
        break;

    case MQTT_EVENT_ERROR:
        if (event->error_handle != NULL &&
            event->error_handle->error_type == MQTT_ERROR_TYPE_CONNECTION_REFUSED) {
            /* 4/5 = bad credentials / not authorised. The stored password is either wrong
             * or the dynsec client was removed; either way re-enrolling is the fix, and
             * that needs a fresh token. */
            ESP_LOGE(TAG, "broker refused the connection (return code %d). The stored "
                          "credential is not accepted; this board needs a new enrollment.",
                     event->error_handle->connect_return_code);
            /* The one stage a board can reach where everything except the fleet works:
             * it has a link, a clock, a credential and an API it can reach, and the
             * dashboard would otherwise show it as simply never having arrived. */
            char reason[64];
            snprintf(reason, sizeof(reason), "broker connack %d",
                     event->error_handle->connect_return_code);
            ff_progress_report(FF_PROGRESS_MQTT_REFUSED, reason);
        } else {
            ESP_LOGE(TAG, "mqtt transport error");
        }
        break;

    default:
        break;
    }
}

esp_err_t ff_mqtt_run(const ff_cfg_t *cfg, const ff_cred_t *cred)
{
    /* confirm_timeout_cb may already be armed and reads s_ctx.session_confirmed: this runs
     * once, over the static zero-init, so the callback sees the same values either way. */
    memset(&s_ctx, 0, sizeof(s_ctx));
    s_ctx.cfg = cfg;
    s_ctx.announce_msg_id = -1;

    const char *id = ff_device_id();
    snprintf(s_ctx.topic_announce, sizeof(s_ctx.topic_announce), TOPIC_ROOT "/%s/up/announce", id);
    snprintf(s_ctx.topic_presence, sizeof(s_ctx.topic_presence), TOPIC_ROOT "/%s/up/presence", id);
    snprintf(s_ctx.topic_hb, sizeof(s_ctx.topic_hb), TOPIC_ROOT "/%s/up/hb", id);
    snprintf(s_ctx.topic_status, sizeof(s_ctx.topic_status), TOPIC_ROOT "/%s/up/status", id);
    snprintf(s_ctx.topic_telemetry, sizeof(s_ctx.topic_telemetry), TOPIC_ROOT "/%s/up/telemetry",
             id);
    snprintf(s_ctx.topic_dn, sizeof(s_ctx.topic_dn), TOPIC_ROOT "/%s/dn/#", id);

    const esp_mqtt_client_config_t config = {
        .broker = {
            .address.uri = cfg->mqtt_uri,
            .verification.crt_bundle_attach = esp_crt_bundle_attach,
        },
        .credentials = {
            /* client_id == username == device_id. The dynsec ACL binds to `%u`, so the
             * username is a security control, not a label (broker/provisioner.py). */
            .client_id = id,
            .username = cred->mqtt_user,
            .authentication.password = cred->mqtt_pass,
        },
        .session = {
            .last_will = {
                .topic = s_ctx.topic_presence,
                .msg = PRESENCE_OFFLINE,
                .msg_len = (int)strlen(PRESENCE_OFFLINE),
                .qos = QOS,
                .retain = 1, /* property 2 — without this a restarted server never learns */
            },
            .disable_clean_session = true, /* property 3 */
            .keepalive = KEEPALIVE_S,
        },
    };

    s_ctx.client = esp_mqtt_client_init(&config);
    if (s_ctx.client == NULL) {
        ESP_LOGE(TAG, "cannot create the mqtt client for %s", cfg->mqtt_uri);
        return ESP_FAIL;
    }
    ESP_ERROR_CHECK(esp_mqtt_client_register_event(s_ctx.client, ESP_EVENT_ANY_ID,
                                                   mqtt_event_handler, &s_ctx));

    /* R5-fw-1: the firmware's self-test registration is fixed from here, before the first
     * connect builds an announce. R5-fw-2: the answer gates the confirm (the hold in
     * mqtt_event_handler, the deadline guard in confirm_timeout_cb) and becomes the `selftest`
     * capability — set here, before the first connect and before the ack can set
     * session_confirmed, never at the ack. */
    s_selftest_registered = ff_selftest_freeze();
    ff_identity_note_selftest(s_selftest_registered);

    /* What the last reboot left to report. The confirm timer is NOT armed here: it was
     * armed first thing in app_main (R2-fw-4, ff_mqtt_arm_confirm_timer), so it may fire
     * before, during or after this classification. That is safe: s_txn.kind starts
     * TXN_NONE, so a timeout before classification rolls back immediately and unreported;
     * kind becomes TXN_CONFIRMING only after cmd_id is written; and the report task copes
     * with no client (s_ctx.connected, enqueue_status). */
    ff_txn_init();
    classify_txn();

    ESP_LOGI(TAG, "connecting to %s as %s", cfg->mqtt_uri, id);
    esp_err_t err = esp_mqtt_client_start(s_ctx.client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot start the mqtt client: %s", esp_err_to_name(err));
        return err;
    }

    /* esp-mqtt runs its own task; this one just stays out of the way. There is no
     * shutdown path and no goodbye publish (property 4). */
    while (true) {
        vTaskDelay(portMAX_DELAY);
    }
}
