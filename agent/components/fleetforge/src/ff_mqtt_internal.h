/*
 * ff_mqtt, the component-private half — the `up/status` vocabulary and its publisher.
 * PRIVATE (src/, never reachable from a consumer's main): see include/ff_mqtt.h for the
 * session itself. Only ff_mqtt and ff_ota publish a transaction state; a firmware
 * built on the component never does.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "ff_mqtt.h"

#ifdef __cplusplus
extern "C" {
#endif

/* The `up/status` states this agent can publish, spelled once because the publisher
 * (ff_mqtt) and its only caller (ff_ota) have to agree byte for byte with
 * `src/fleetforge/ingestor/protocol.py` and with spec/device-protocol.md's machine.
 *
 * The first seven are ff_ota's walk up to the reboot. The last four are the outcome
 * (R2-be-1), and ONLY ff_mqtt publishes them, from the session that observed it:
 * `confirming` once the new image has a broker session, `confirmed` at the announce PUBACK
 * that confirms it, `rolling_back` best-effort when the confirm timer fires, and
 * `rolled_back` from the image the board RETURNED to.
 *
 * R5-fw-5: `awaiting_safe_window` is the wait for the firmware's safe-window rule
 * (ff_safe_window.h), and it is reported only when the rule says unsafe at the moment a reboot
 * is due: before an apply's reboot (pct 100, like `staged`/`applying`; detail
 * FF_DETAIL_APPLY_WAITS), published by ff_ota for apply:auto and enqueued by the session for
 * the `apply` command; and before a decided rollback's reboot (pct null, like `rolling_back`;
 * detail FF_DETAIL_ROLLBACK_WAITS), by the session only. A board with no rule never reports it.
 *
 * R5-fw-4: `cancelled` belongs to the pre-reboot walk too. It ends a transaction that the
 * `cancel` command stopped before its reboot: published by ff_ota's task when the cancel
 * caught the transfer (or the staged wait) still running, enqueued by the session when the
 * staged image was already waiting. It is ahead of the spec: R5-spec-1 Patch A3 adds it to
 * spec/device-protocol.md's machine, and is not applied yet. Never a status for the `cancel`
 * command itself, whose answer goes to `up/ack`. */
#define FF_STATUS_STAGING "staging"
#define FF_STATUS_DOWNLOADING "downloading"
#define FF_STATUS_VERIFYING "verifying"
#define FF_STATUS_STAGED "staged"
#define FF_STATUS_APPLYING "applying"
#define FF_STATUS_REBOOTING "rebooting"
#define FF_STATUS_FAILED "failed"
#define FF_STATUS_CONFIRMING "confirming"
#define FF_STATUS_CONFIRMED "confirmed"
#define FF_STATUS_ROLLING_BACK "rolling_back"
#define FF_STATUS_ROLLED_BACK "rolled_back"
#define FF_STATUS_CANCELLED "cancelled"
#define FF_STATUS_AWAITING_SAFE_WINDOW "awaiting_safe_window"

/* R5-fw-5, R5-spec-1 D12: the two `detail`s of `awaiting_safe_window`, which say which reboot
 * waits. Deliberately not named FF_STATUS_*: they are details, not states. Each fits the
 * 63-byte detail budget. */
#define FF_DETAIL_APPLY_WAITS "apply waits for a safe window"
#define FF_DETAIL_ROLLBACK_WAITS "rollback waits for a safe window"
#ifndef __cplusplus
_Static_assert(sizeof(FF_DETAIL_APPLY_WAITS) <= 64, "an up/status detail is at most 63 bytes");
_Static_assert(sizeof(FF_DETAIL_ROLLBACK_WAITS) <= 64, "an up/status detail is at most 63 bytes");
#endif

/* `pct` for a state that has no meaningful percentage: published as JSON `null` rather
 * than as a plausible-looking 0, for the same reason ff_identity reports a null `rssi`. */
#define FF_STATUS_PCT_NONE (-1)

/* Publish one `up/status` transition — QoS 1 and **RETAINED**. */
void ff_mqtt_publish_status(const char *cmd_id, const char *state, int pct, const char *detail);

/* R5-fw-7 (ff_sleep.h). A broker session is up and its announce was acknowledged on this
 * connection. Any task (never an ISR); never blocks, never logs. */
bool ff_mqtt_is_connected(void);

/* R5-fw-7 (ff_sleep.h). NULL when nothing the session holds forbids a deep sleep now, else a
 * short reason. Task context (never an ISR or the esp_timer task); never logs, never touches NVS. */
const char *ff_mqtt_sleep_blocker(void);

/* H5 / R7-cargo-1: Register a non-app cargo capability (`artifact.type` != "app") and
 * complete a verified cargo update without rebooting the host MCU. */
esp_err_t ff_mqtt_register_cargo_type(const char *type);
void ff_mqtt_complete_cargo(const char *cmd_id, const char *type, const char *version);

/* CUJ-3: Venue Offline Mode and offline PENDING_VERIFY self-test confirmation. */
void ff_mqtt_set_offline_mode(bool offline);
esp_err_t ff_mqtt_confirm_offline(void);

#ifdef FF_MQTT_INTERNAL_IMPL
#include "cJSON.h"
#include "ff_txn.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"
#include "mqtt_client.h"

#define TOPIC_ROOT "ff/v1/d"
#define TOPIC_MAX 96
#define QOS 1
#define KEEPALIVE_S 30

#define PRESENCE_ONLINE "{\"online\":true}"
#define PRESENCE_OFFLINE "{\"online\":false}"

#ifndef FF_MQTT_MAIN_TU
#if defined(FF_ROLLBACK_TEST) && FF_ROLLBACK_TEST
#define CONFIRM_TIMEOUT_S 60
#define CONFIRM_TIMEOUT_MAX_S 60
#else
#define CONFIRM_TIMEOUT_S 300
#define CONFIRM_TIMEOUT_MAX_S 3600
#endif
#define CONFIRM_TIMEOUT_MIN_S 30

#define DETAIL_BOOTED_NEW "the bootloader never armed rollback for this image"
#define DETAIL_SELFTEST_PASSED "selftest passed"
#define DETAIL_SELFTEST_TIMED_OUT "selftest timed out"
#define DETAIL_SELFTEST_FAILED "selftest failed: "
#define DETAIL_SELFTEST_PREFIX "selftest "
#define DETAIL_RETURNED_TO "; returned to "
#define DETAIL_NO_REASON "no reason given"
#define DETAIL_MAX 63
#define DETAIL_ROLLED_BACK_BY_COMMAND "rolled back by command"
#endif

typedef struct {
    const ff_cfg_t *cfg;
    esp_mqtt_client_handle_t client;
    char topic_announce[TOPIC_MAX];
    char topic_presence[TOPIC_MAX];
    char topic_hb[TOPIC_MAX];
    char topic_status[TOPIC_MAX];
    char topic_telemetry[TOPIC_MAX];
    char topic_log[TOPIC_MAX];
    char topic_ack[TOPIC_MAX];
    char topic_cfg[TOPIC_MAX];
    char topic_dn[TOPIC_MAX];
    int announce_msg_id;
    bool session_confirmed;
    volatile bool connected;
    TaskHandle_t heartbeat;
    char last_command_id[64];
} ff_mqtt_ctx_t;

typedef enum {
    TXN_NONE,
    TXN_CONFIRMING,
    TXN_ALREADY_CONFIRMED,
    TXN_ROLLED_BACK,
    TXN_BOOTED_NEW,
} txn_kind_t;

typedef struct {
    txn_kind_t kind;
    char cmd_id[FF_TXN_MAX_CMD_ID];
    char detail[64];
    bool confirming_sent;
    int terminal_msg_id;
} ff_mqtt_txn_state_t;

#define s_ctx ff_mqtt__ctx
#define s_txn ff_mqtt__txn
#define s_decide_lock ff_mqtt__decide_lock
#define s_confirm_claimed ff_mqtt__confirm_claimed
#define s_rollback_decided ff_mqtt__rollback_decided
#define s_rollback_commanded ff_mqtt__rollback_commanded
#define s_rollback_waiting ff_mqtt__rollback_waiting
#define s_selftest_registered ff_mqtt__selftest_registered
#define s_acked_this_connection ff_mqtt__acked_this_connection
#define s_stay_awake ff_mqtt__stay_awake
#define s_cmd_reboot_pending ff_mqtt__cmd_reboot_pending
#define s_reboot_cmd_id ff_mqtt__reboot_cmd_id
#define s_confirm_deadline_s ff_mqtt__confirm_deadline_s

#define pending_verify ff_mqtt__pending_verify
#define held_txn_id ff_mqtt__held_txn_id
#define confirm_this_image ff_mqtt__confirm_this_image
#define accept_unverified_image ff_mqtt__accept_unverified_image
#define selftest_done ff_mqtt__selftest_done
#define confirm_timeout_clamp ff_mqtt__confirm_timeout_clamp
#define classify_txn ff_mqtt__classify_txn
#define report_txn_on_connect ff_mqtt__report_txn_on_connect
#define on_connected ff_mqtt__on_connected
#define enqueue_status ff_mqtt__enqueue_status
#define enqueue_ack ff_mqtt__enqueue_ack
#define on_command ff_mqtt__on_command
#define on_cfg ff_mqtt__on_cfg

extern ff_mqtt_ctx_t s_ctx;
extern ff_mqtt_txn_state_t s_txn;
extern portMUX_TYPE s_decide_lock;
extern bool s_confirm_claimed;
extern volatile bool s_rollback_decided;
extern bool s_rollback_commanded;
extern volatile bool s_rollback_waiting;
extern volatile bool s_selftest_registered;
extern volatile bool s_acked_this_connection;
extern volatile bool s_stay_awake;
extern volatile bool s_cmd_reboot_pending;
extern char s_reboot_cmd_id[FF_TXN_MAX_CMD_ID];
extern volatile uint32_t s_confirm_deadline_s;

bool pending_verify(void);
const char *held_txn_id(void);
bool confirm_this_image(void);
bool accept_unverified_image(void);
void selftest_done(bool passed, const char *reason);
uint32_t confirm_timeout_clamp(double requested_s);
void classify_txn(void);
void report_txn_on_connect(void);
void on_connected(ff_mqtt_ctx_t *ctx);
int enqueue_status(const char *cmd_id, const char *state, int pct, const char *detail);
void enqueue_ack(const char *id, const char *type, const char *result, const char *cmd_id,
                 const char *detail);
void on_command(ff_mqtt_ctx_t *ctx, const char *topic, int topic_len, const char *data,
                int data_len);
void on_cfg(ff_mqtt_ctx_t *ctx, const char *data, int data_len);
#endif

#ifdef __cplusplus
}
#endif
