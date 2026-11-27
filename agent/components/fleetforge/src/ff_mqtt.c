/*
 * ff_mqtt — the session lifecycle, event dispatch, sleep readiness, and offline confirm.
 *
 * Six wire invariants shared with `src/fleetforge/simulator/device.py`:
 * 1. announce + presence are RETAINED; hb is NOT retained.
 * 2. LWT is retained `{"online":false}` at QoS 1 on `up/presence`.
 * 3. `client_id = username = device_id`, `clean_session = false`.
 * 4. No clean goodbye publish on ESP32 (it runs forever).
 * 5. Subscribe to `dn/#` BEFORE publishing `up/announce`.
 * 6. Every publish logs its `msg_id` so ACL drops are diagnosable.
 *
 * Boot confirmation, deadline clamp, and rollback gate live in `ff_mqtt_boot.c`;
 * outbound heartbeat/announce/status/ack publishing lives in `ff_mqtt_pub.c`;
 * inbound `dn/cmd` and `dn/cfg` parsing and verb handlers live in `ff_mqtt_rx.c`.
 */

#define FF_MQTT_INTERNAL_IMPL
#define FF_MQTT_MAIN_TU
#include "ff_mqtt_internal.h"

#include <stdio.h>
#include <string.h>

#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "ff_identity_internal.h"
#include "ff_log_internal.h"
#include "ff_progress.h"
#include "ff_safe_window_internal.h"
#include "ff_sec_internal.h"
#include "ff_selftest_internal.h"
#include "ff_sleep_internal.h"
#include "ff_telemetry_internal.h"

static const char *TAG = "ff-mqtt";

/* spec/prd.md -> Requirements & targets -> Timing: "confirm timeout 300 s". */
#if FF_ROLLBACK_TEST
#define CONFIRM_TIMEOUT_S 60
#define CONFIRM_TIMEOUT_MAX_S 60
#else
#define CONFIRM_TIMEOUT_S 300
#define CONFIRM_TIMEOUT_MAX_S 3600
#endif
#define CONFIRM_TIMEOUT_MIN_S 30
_Static_assert(CONFIRM_TIMEOUT_MIN_S <= CONFIRM_TIMEOUT_S &&
                   CONFIRM_TIMEOUT_S <= CONFIRM_TIMEOUT_MAX_S,
               "the default confirm deadline must lie inside the clamp");

/* Detail grammar constants shared with `tests/test_selftest_outcome.py` and `ff_mqtt_*`. */
#define DETAIL_BOOTED_NEW "the bootloader never armed rollback for this image"
#define DETAIL_SELFTEST_PASSED "selftest passed"
#define DETAIL_SELFTEST_TIMED_OUT "selftest timed out"
#define DETAIL_SELFTEST_FAILED "selftest failed: "
#define DETAIL_SELFTEST_PREFIX "selftest "
#define DETAIL_RETURNED_TO "; returned to "
#define DETAIL_NO_REASON "no reason given"
#define DETAIL_MAX 63
#define DETAIL_ROLLED_BACK_BY_COMMAND "rolled back by command"

ff_mqtt_ctx_t s_ctx;
ff_mqtt_txn_state_t s_txn = {.kind = TXN_NONE, .terminal_msg_id = -1};

volatile bool s_rollback_decided;
volatile bool s_selftest_registered;
static volatile bool s_boot_classified;
static bool s_boot_classify_claimed;

portMUX_TYPE s_decide_lock = portMUX_INITIALIZER_UNLOCKED;

static bool claim_boot_classify(void)
{
    portENTER_CRITICAL(&s_decide_lock);
    bool mine = !s_boot_classified && !s_boot_classify_claimed;
    if (mine) {
        s_boot_classify_claimed = true;
    }
    portEXIT_CRITICAL(&s_decide_lock);
    if (!mine) {
        while (!s_boot_classified) {
            vTaskDelay(1);
        }
    }
    return mine;
}

bool s_confirm_claimed;
bool s_rollback_commanded;
volatile bool s_rollback_waiting;
volatile bool s_acked_this_connection;
static volatile bool s_checked_in;
volatile bool s_stay_awake;
volatile bool s_cmd_reboot_pending;
char s_reboot_cmd_id[FF_TXN_MAX_CMD_ID];

bool pending_verify(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (running == NULL || esp_ota_get_state_partition(running, &state) != ESP_OK) {
        return false;
    }
    return state == ESP_OTA_IMG_PENDING_VERIFY;
}

const char *held_txn_id(void)
{
    return s_txn.kind != TXN_NONE && s_txn.cmd_id[0] != '\0' ? s_txn.cmd_id : NULL;
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
        ctx->connected = false;
        s_acked_this_connection = false;
        ff_telemetry_session_down();
        ff_log_session_down();
        ESP_LOGW(TAG, "mqtt disconnected; the client will reconnect");
        break;

    case MQTT_EVENT_SUBSCRIBED:
        ESP_LOGI(TAG, "subscribed (msg_id %d)", event->msg_id);
        break;

    case MQTT_EVENT_PUBLISHED:
        if (event->msg_id == ctx->announce_msg_id) {
            s_acked_this_connection = true;
            s_checked_in = true;
        }
        if (event->msg_id == ctx->announce_msg_id) {
            bool first_confirm = false;
            bool rollback_decided = false;
            portENTER_CRITICAL(&s_decide_lock);
            if (!ctx->session_confirmed) {
                ctx->session_confirmed = true;
                first_confirm = true;
                rollback_decided = s_rollback_decided;
                if (!rollback_decided && !s_selftest_registered) {
                    s_confirm_claimed = true;
                }
            }
            portEXIT_CRITICAL(&s_decide_lock);
            if (first_confirm) {
                ESP_LOGI(TAG, "announce acknowledged by the broker");
                if (rollback_decided) {
                    ESP_LOGW(TAG, "announce ack arrived after confirm timeout — NOT confirming");
                } else if (s_selftest_registered &&
                           (pending_verify() || s_txn.kind == TXN_BOOTED_NEW)) {
                    if (s_txn.kind == TXN_CONFIRMING) {
                        (void)ff_txn_set_detail(s_txn.cmd_id, DETAIL_SELFTEST_TIMED_OUT);
                    }
                    esp_err_t err = ff_selftest_run(selftest_done);
                    if (err != ESP_OK) {
                        ESP_LOGE(TAG, "cannot start the custom self-test (%s) — NOT confirming",
                                 esp_err_to_name(err));
                    }
                } else if (confirm_this_image() && s_txn.kind == TXN_CONFIRMING) {
                    s_txn.kind = TXN_ALREADY_CONFIRMED;
                    s_txn.terminal_msg_id =
                        enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, NULL);
                } else if (s_txn.kind == TXN_BOOTED_NEW && accept_unverified_image()) {
                    s_txn.kind = TXN_ALREADY_CONFIRMED;
                    s_txn.terminal_msg_id =
                        enqueue_status(s_txn.cmd_id, FF_STATUS_CONFIRMED, 100, s_txn.detail);
                }
            }
        }
        if (s_txn.kind != TXN_NONE && s_txn.terminal_msg_id > 0 &&
            event->msg_id == s_txn.terminal_msg_id) {
            (void)ff_txn_clear_if(s_txn.cmd_id);
            s_txn.kind = TXN_NONE;
            s_txn.terminal_msg_id = -1;
        }
        break;

    case MQTT_EVENT_DATA:
        if (event->data_len < 0 || event->topic_len < 0 || event->current_data_offset != 0 ||
            event->total_data_len != event->data_len) {
            ESP_LOGE(TAG, "refusing truncated/fragmented MQTT message (len=%d total=%d offset=%d)",
                     event->data_len, event->total_data_len, event->current_data_offset);
            break;
        }
        if (event->topic != NULL && (size_t)event->topic_len == strlen(ctx->topic_cfg) &&
            strncmp(event->topic, ctx->topic_cfg, (size_t)event->topic_len) == 0) {
            on_cfg(ctx, event->data, event->data_len);
        } else {
            on_command(ctx, event->topic, event->topic_len, event->data, event->data_len);
        }
        break;

    case MQTT_EVENT_ERROR:
        if (event->error_handle != NULL &&
            event->error_handle->error_type == MQTT_ERROR_TYPE_CONNECTION_REFUSED) {
            ESP_LOGE(TAG, "broker refused the connection (return code %d). The stored "
                          "credential is not accepted; this board needs a new enrollment.",
                     event->error_handle->connect_return_code);
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
    snprintf(s_ctx.topic_log, sizeof(s_ctx.topic_log), TOPIC_ROOT "/%s/up/log", id);
    snprintf(s_ctx.topic_ack, sizeof(s_ctx.topic_ack), TOPIC_ROOT "/%s/up/ack", id);
    snprintf(s_ctx.topic_cfg, sizeof(s_ctx.topic_cfg), TOPIC_ROOT "/%s/dn/cfg", id);
    snprintf(s_ctx.topic_dn, sizeof(s_ctx.topic_dn), TOPIC_ROOT "/%s/dn/#", id);

    const esp_mqtt_client_config_t config = {
        .broker = {
            .address.uri = cfg->mqtt_uri,
            .verification.crt_bundle_attach = esp_crt_bundle_attach,
        },
        .credentials = {
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
                .retain = 1,
            },
            .disable_clean_session = true,
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

    const bool claim_boot = claim_boot_classify();
    if (claim_boot) {
        s_selftest_registered = ff_selftest_freeze();
        ff_identity_note_selftest(s_selftest_registered);
        ff_identity_note_safe_window(ff_safe_window_freeze());
    }
    ff_sleep_freeze(cfg);
    ff_sec_init(cfg, cred);

    if (claim_boot) {
        ff_txn_init();
        (void)ff_txn_last_reboot_cmd(s_reboot_cmd_id, sizeof(s_reboot_cmd_id));
        classify_txn();
        portENTER_CRITICAL(&s_decide_lock);
        s_boot_classified = true;
        portEXIT_CRITICAL(&s_decide_lock);
    }

    ESP_LOGI(TAG, "connecting to %s as %s", cfg->mqtt_uri, id);
    esp_err_t err = esp_mqtt_client_start(s_ctx.client);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot start the mqtt client: %s", esp_err_to_name(err));
        return err;
    }

    while (true) {
        vTaskDelay(portMAX_DELAY);
    }
}

bool ff_mqtt_is_connected(void) { return s_ctx.connected && s_acked_this_connection; }

const char *ff_mqtt_sleep_blocker(void)
{
    if (!s_checked_in) {
        return "not checked in with the fleet yet";
    }
    if (s_rollback_decided || s_rollback_waiting) {
        return "a rollback is decided";
    }
    if (s_cmd_reboot_pending) {
        return "a reboot is pending";
    }
    if (s_txn.kind != TXN_NONE) {
        return "a transaction is not closed";
    }
    if (s_stay_awake) {
        return "held awake by dn/cfg";
    }
    if (!ff_sec_synced()) {
        return "secrets sync in progress";
    }
    if (esp_mqtt_client_get_outbox_size(s_ctx.client) > 0) {
        return "messages await their PUBACK";
    }
    return NULL;
}

void ff_mqtt_set_offline_mode(bool offline)
{
    if (offline && (pending_verify() || s_txn.kind == TXN_BOOTED_NEW)) {
        (void)ff_mqtt_confirm_offline();
    }
}

esp_err_t ff_mqtt_confirm_offline(void)
{
    if (!s_boot_classified) {
        if (claim_boot_classify()) {
            s_selftest_registered = ff_selftest_freeze();
            ff_identity_note_selftest(s_selftest_registered);
            ff_identity_note_safe_window(ff_safe_window_freeze());
            ff_txn_init();
            (void)ff_txn_last_reboot_cmd(s_reboot_cmd_id, sizeof(s_reboot_cmd_id));
            classify_txn();
            portENTER_CRITICAL(&s_decide_lock);
            s_boot_classified = true;
            portEXIT_CRITICAL(&s_decide_lock);
        }
    }

    if (!pending_verify() && s_txn.kind != TXN_BOOTED_NEW) {
        return ESP_OK;
    }
    if (s_rollback_decided) {
        return ESP_ERR_INVALID_STATE;
    }
    if (!s_selftest_registered) {
        return ESP_ERR_INVALID_STATE;
    }
    if (s_txn.kind == TXN_CONFIRMING) {
        (void)ff_txn_set_detail(s_txn.cmd_id, DETAIL_SELFTEST_TIMED_OUT);
    }
    return ff_selftest_run(selftest_done);
}
