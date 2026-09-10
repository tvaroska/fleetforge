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

#include "ff_mqtt.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crt_bundle.h"
#include "esp_log.h"
#include "esp_ota_ops.h"
#include "esp_timer.h"
#include "ff_identity.h"
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

/* spec/prd.md -> Requirements & targets -> Timing: "confirm timeout 300 s". */
#define CONFIRM_TIMEOUT_S 300

static const char PRESENCE_ONLINE[] = "{\"online\":true}";
static const char PRESENCE_OFFLINE[] = "{\"online\":false}";

typedef struct {
    const ff_cfg_t *cfg;
    esp_mqtt_client_handle_t client;
    char topic_announce[TOPIC_MAX];
    char topic_presence[TOPIC_MAX];
    char topic_hb[TOPIC_MAX];
    char topic_dn[TOPIC_MAX];
    int announce_msg_id;   /* the id whose PUBACK means "this board did its job" */
    bool session_confirmed; /* the announce has been acknowledged at least once */
    TaskHandle_t heartbeat; /* NULL until the first successful connect */
    char last_command_id[64];
} ff_mqtt_ctx_t;

static ff_mqtt_ctx_t s_ctx;

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
 *
 * Do not try to prove these by forcing the partition state; R2 owns the live test. */

static bool pending_verify(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (running == NULL || esp_ota_get_state_partition(running, &state) != ESP_OK) {
        return false;
    }
    return state == ESP_OTA_IMG_PENDING_VERIFY;
}

static void confirm_this_image(void)
{
    if (!pending_verify()) {
        return; /* the normal R0 path: nothing to confirm, nothing to roll back */
    }
    esp_err_t err = esp_ota_mark_app_valid_cancel_rollback();
    if (err == ESP_OK) {
        ESP_LOGW(TAG, "this image was written by OTA and is now CONFIRMED: the broker "
                      "accepted us and the retained announce was acknowledged");
    } else {
        ESP_LOGE(TAG, "cannot confirm this OTA image (%s) — the bootloader will roll back "
                      "on the next reset",
                 esp_err_to_name(err));
    }
}

/* The negative branch. Runs once, CONFIRM_TIMEOUT_S after boot, and only for an image the
 * bootloader is still waiting on. A board that cannot reach its fleet with a new image is
 * a board that must go back to the one that worked — unattended, because by definition
 * nobody can reach it to help. */
static void confirm_timeout_cb(void *arg)
{
    (void)arg;
    if (s_ctx.session_confirmed || !pending_verify()) {
        return;
    }
    ESP_LOGE(TAG, "no working session %d s after an OTA boot — marking this image invalid "
                  "and rolling back to the previous slot",
             CONFIRM_TIMEOUT_S);
    esp_ota_mark_app_invalid_rollback_and_reboot(); /* does not return */
}

static void arm_confirm_timeout(void)
{
    if (!pending_verify()) {
        /* Inert at R0 by design; the line is here so the log tells the truth about which
         * branch a future OTA board took. */
        ESP_LOGD(TAG, "not an OTA boot: no confirm timer armed");
        return;
    }
    const esp_timer_create_args_t args = {
        .callback = confirm_timeout_cb,
        .name = "ff_confirm",
    };
    esp_timer_handle_t timer = NULL;
    if (esp_timer_create(&args, &timer) == ESP_OK) {
        ESP_ERROR_CHECK(esp_timer_start_once(timer, (uint64_t)CONFIRM_TIMEOUT_S * 1000000ULL));
        ESP_LOGW(TAG, "OTA boot: %d s to reach the fleet or roll back", CONFIRM_TIMEOUT_S);
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
        free(announce);
    } else {
        ESP_LOGE(TAG, "out of memory building the announce payload");
    }

    id = esp_mqtt_client_publish(ctx->client, ctx->topic_presence, PRESENCE_ONLINE,
                                 (int)strlen(PRESENCE_ONLINE), QOS, 1);
    ESP_LOGI(TAG, "publish %s (qos 1, retain, msg_id %d) %s", ctx->topic_presence, id,
             PRESENCE_ONLINE);

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
}

/* R0 is connect-only: a command is acknowledged in the log and executed by nobody. Saying
 * so on every message is the point — an operator who sends a command must be able to see
 * that it arrived and that this agent will not act on it, rather than watching it vanish. */
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

    ESP_LOGI(TAG, "%.*s id=%s type=%s — logged, not executed (R0 is connect-only)", topic_len,
             topic, id != NULL ? id : "?", type);
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
            confirm_this_image();
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

    /* Armed before the connect attempt, so an image that can never reach its broker still
     * rolls back on schedule rather than waiting for a connection that never comes. */
    arm_confirm_timeout();

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
