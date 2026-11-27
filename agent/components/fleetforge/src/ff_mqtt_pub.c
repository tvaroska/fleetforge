/*
 * ff_mqtt_pub — outbound MQTT publishing: connect sequence, heartbeat task, up/status, up/ack, cargo.
 */

#define FF_MQTT_INTERNAL_IMPL
#include "ff_mqtt_internal.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "ff_identity_internal.h"
#include "ff_log_internal.h"
#include "ff_progress.h"
#include "ff_telemetry_internal.h"

static const char *TAG = "ff-mqtt";

static void heartbeat_task(void *arg)
{
    ff_mqtt_ctx_t *ctx = (ff_mqtt_ctx_t *)arg;
    const int64_t boot_us = esp_timer_get_time();
    const uint32_t hb_s =
        (ctx->cfg->hb_s > 0 && ctx->cfg->hb_s <= 86400) ? (uint32_t)ctx->cfg->hb_s : 60U;
    while (true) {
        vTaskDelay(pdMS_TO_TICKS(hb_s * 1000U));
        uint32_t uptime_s = (uint32_t)((esp_timer_get_time() - boot_us) / 1000000);
        char *payload = ff_identity_heartbeat_json(uptime_s);
        if (payload == NULL) {
            continue;
        }
        int id = esp_mqtt_client_publish(ctx->client, ctx->topic_hb, payload, 0, QOS, 0);
        ESP_LOGI(TAG, "publish %s (qos 1, no retain, msg_id %d, uptime %" PRIu32 " s)",
                 ctx->topic_hb, id, uptime_s);
        free(payload);
    }
}

void on_connected(ff_mqtt_ctx_t *ctx)
{
    ESP_LOGI(TAG, "mqtt connected as %s (%s)", ff_device_id(), ctx->cfg->mqtt_uri);

    ff_progress_report(FF_PROGRESS_MQTT_CONNECTED, NULL);

    s_acked_this_connection = false;

    int id = esp_mqtt_client_subscribe(ctx->client, ctx->topic_dn, QOS);
    ESP_LOGI(TAG, "subscribe %s (msg_id %d)", ctx->topic_dn, id);

    char *announce = ff_identity_announce_json(ctx->cfg);
    if (announce != NULL) {
        ctx->announce_msg_id =
            esp_mqtt_client_publish(ctx->client, ctx->topic_announce, announce, 0, QOS, 1);
        ESP_LOGI(TAG, "publish %s (qos 1, retain, msg_id %d)", ctx->topic_announce,
                 ctx->announce_msg_id);
#if FF_ROLLBACK_TEST
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
    report_txn_on_connect();

    if (ctx->heartbeat == NULL) {
        if (xTaskCreate(heartbeat_task, "ff_hb", 4096, ctx, 4, &ctx->heartbeat) != pdPASS) {
            ESP_LOGE(TAG, "cannot start the heartbeat task");
            ctx->heartbeat = NULL;
        } else {
            ESP_LOGI(TAG, "heartbeat every %d s -> %s (not retained)", ctx->cfg->hb_s,
                     ctx->topic_hb);
        }
    }

    ff_telemetry_session_up(ctx->client, ctx->topic_telemetry);
    ff_log_session_up(ctx->client, ctx->topic_log);
}

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
        ESP_LOGE(TAG, "cannot report %s for %s: there is no mqtt session yet", state, cmd_id);
        return;
    }
    char *payload = build_status_payload(cmd_id, state, pct, detail);
    if (payload == NULL) {
        return;
    }

    int msg_id = esp_mqtt_client_publish(s_ctx.client, s_ctx.topic_status, payload, 0, QOS, 1);
    ESP_LOGI(TAG, "publish %s (qos 1, retain, msg_id %d) cmd_id=%s state=%s pct=%d",
             s_ctx.topic_status, msg_id, cmd_id, state, pct);
    free(payload);
}

int enqueue_status(const char *cmd_id, const char *state, int pct, const char *detail)
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
    ESP_LOGI(TAG, "publish %s (qos 1, retain, queued, msg_id %d) cmd_id=%s state=%s pct=%d",
             s_ctx.topic_status, msg_id, cmd_id, state, pct);
    free(payload);
    return msg_id;
}

void enqueue_ack(const char *id, const char *type, const char *result, const char *cmd_id,
                 const char *detail)
{
    if (s_ctx.client == NULL) {
        ESP_LOGE(TAG, "cannot answer command %s: there is no mqtt session yet", id);
        return;
    }
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        ESP_LOGE(TAG, "out of memory building the answer to command %s", id);
        return;
    }
    cJSON_AddStringToObject(root, "id", id);
    if (type != NULL) {
        cJSON_AddStringToObject(root, "type", type);
    } else {
        cJSON_AddNullToObject(root, "type");
    }
    cJSON_AddStringToObject(root, "result", result);
    if (cmd_id != NULL) {
        cJSON_AddStringToObject(root, "cmd_id", cmd_id);
    } else {
        cJSON_AddNullToObject(root, "cmd_id");
    }
    if (detail != NULL) {
        cJSON_AddStringToObject(root, "detail", detail);
    } else {
        cJSON_AddNullToObject(root, "detail");
    }
    char *payload = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    if (payload == NULL) {
        ESP_LOGE(TAG, "out of memory serialising the answer to command %s", id);
        return;
    }
    int msg_id = esp_mqtt_client_enqueue(s_ctx.client, s_ctx.topic_ack, payload, 0, QOS, 0, true);
    ESP_LOGI(TAG, "publish %s (qos 1, no retain, queued, msg_id %d) id=%s type=%s result=%s "
                  "cmd_id=%s detail=%s",
             s_ctx.topic_ack, msg_id, id, type != NULL ? type : "null", result,
             cmd_id != NULL ? cmd_id : "null", detail != NULL ? detail : "null");
    free(payload);
}

esp_err_t ff_mqtt_register_cargo_type(const char *type)
{
    return ff_identity_note_cargo_type(type);
}

void ff_mqtt_complete_cargo(const char *cmd_id, const char *type, const char *version)
{
    if (cmd_id == NULL || cmd_id[0] == '\0' || type == NULL || type[0] == '\0') {
        return;
    }
    if (version != NULL && version[0] != '\0') {
        (void)ff_identity_set_sub_version(type, version);
        if (s_ctx.client != NULL && s_ctx.cfg != NULL) {
            char *announce = ff_identity_announce_json(s_ctx.cfg);
            if (announce != NULL) {
                (void)esp_mqtt_client_publish(s_ctx.client, s_ctx.topic_announce, announce, 0, QOS,
                                              1);
                free(announce);
            }
        }
    }
    ff_mqtt_publish_status(cmd_id, FF_STATUS_CONFIRMING, FF_STATUS_PCT_NONE, NULL);
    ff_mqtt_publish_status(cmd_id, FF_STATUS_CONFIRMED, 100, NULL);
}
