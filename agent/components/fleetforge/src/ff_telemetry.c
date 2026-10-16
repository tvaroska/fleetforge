/*
 * ff_telemetry — the `up/telemetry` ring and its flush task (R4-fw-2). See
 * include/ff_telemetry.h for the maker API and ff_telemetry_internal.h for the session
 * hooks ff_mqtt.c calls.
 *
 * Four properties, each one a way to get this silently wrong:
 *
 * 1. NO WALL CLOCK. A sample carries the time on esp_timer (monotonic, from boot), and the
 *    payload carries `age_ms` = payload build time - sample time. The server stamps
 *    `receipt time - age_ms` (spec/device-protocol.md -> Clock; docs/SWARM.md 5.1). A board
 *    whose SNTP never worked still reports correctly timed samples.
 * 2. PUBLISH, THEN COMMIT. A payload is built from the oldest unsent sample without
 *    removing anything; only a publish esp-mqtt accepted moves `s_unsent` past it. A
 *    publish that fails (the session dropped under us) loses nothing: the same samples go
 *    out after the next connect, with ages measured at THAT build.
 * 3. ff_telemetry() NEVER LOGS, never allocates and never blocks. R4-fw-3 will forward log
 *    lines: a metric call that logs could recurse through that forwarder. The spinlock is
 *    held for struct copies only — no snprintf, no ESP_LOG, no publish inside it.
 * 4. QoS 0, NOT RETAINED (the spec's Topics table). A retained sample would be replayed to
 *    the ingestor on every reconnect, and QoS 1 would grow the outbox with the backlog.
 *
 * The JSON is written by hand, not with cJSON: names are validated to [a-z0-9_], so nothing
 * needs escaping, and the exact size of every entry is known before it is appended, which
 * is what keeps a payload at FF_TELEMETRY_PAYLOAD_MAX bytes or less without a heap print.
 */

#include "ff_telemetry_internal.h"

#include <inttypes.h>
#include <math.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "esp_log.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-tel";

/* The newest 64 samples are kept while the board cannot publish: 3.5 KB of BSS. At one
 * sample per 7 s (the worked examples) that is a 7-minute outage before `dropped` > 0. */
#define FF_TELEMETRY_CAPACITY 64
/* Matches `ingestor/protocol.py::TELEMETRY_NAME_RE` ({0,30} after the first letter). */
#define FF_TELEMETRY_NAME_MAX 31
/* Under the spec's 2048 B, and small enough that the whole MQTT packet (topic
 * `ff/v1/d/<12hex>/up/telemetry`, 33 B, plus the fixed header) fits esp-mqtt's default
 * 1024 B buffer: no reliance on its large-message path. */
#define FF_TELEMETRY_PAYLOAD_MAX 900
/* At most one payload per 5 s while connected; a (re)connect drains back to back. */
#define FF_TELEMETRY_FLUSH_MS 5000
/* snprintf with %g is the deepest call; the heartbeat task does cJSON in the same size. */
#define FF_TELEMETRY_STACK 4096
#define FF_TELEMETRY_PRIO 4

typedef struct {
    uint32_t seq;
    int64_t t_us; /* esp_timer_get_time() when recorded */
    double value;
    char name[FF_TELEMETRY_NAME_MAX + 1];
} sample_t;

/* Sample `k` lives at `k % FF_TELEMETRY_CAPACITY`. Sequence numbers wrap; every
 * comparison is an unsigned difference. Guarded by s_lock. */
static sample_t s_ring[FF_TELEMETRY_CAPACITY];
static uint32_t s_head;    /* seq of the next sample written */
static uint32_t s_unsent;  /* seq of the oldest sample not yet published */
static uint32_t s_dropped; /* samples displaced since the last successful publish */
static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;

/* The session, written by the esp-mqtt task (ff_mqtt.c) and read by the flush task. */
static esp_mqtt_client_handle_t s_client;
static const char *s_topic;
static volatile bool s_connected;
static TaskHandle_t s_task;

/* Only the flush task touches it: static, to keep that task's stack small. */
static char s_payload[FF_TELEMETRY_PAYLOAD_MAX + 1];

/* The name rule, or 0: 1-31 of [a-z0-9_], first a letter, never the reserved `ff_`. */
static size_t valid_name_len(const char *name)
{
    if (name == NULL || name[0] < 'a' || name[0] > 'z' || strncmp(name, "ff_", 3) == 0) {
        return 0;
    }
    size_t len = 0;
    for (; name[len] != '\0'; len++) {
        const char c = name[len];
        if (len >= FF_TELEMETRY_NAME_MAX ||
            !((c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '_')) {
            return 0;
        }
    }
    return len;
}

esp_err_t ff_telemetry(const char *name, double value)
{
    const size_t len = valid_name_len(name);
    if (len == 0 || !isfinite(value)) {
        return ESP_ERR_INVALID_ARG;
    }
    const int64_t now_us = esp_timer_get_time();

    portENTER_CRITICAL(&s_lock);
    if (s_head - s_unsent >= FF_TELEMETRY_CAPACITY) {
        /* Full: the newest samples are the useful ones after a long outage. */
        s_unsent++;
        s_dropped++;
    }
    sample_t *slot = &s_ring[s_head % FF_TELEMETRY_CAPACITY];
    slot->seq = s_head;
    slot->t_us = now_us;
    slot->value = value;
    memcpy(slot->name, name, len + 1);
    s_head++;
    portEXIT_CRITICAL(&s_lock);
    return ESP_OK;
}

/* cJSON's round-trip rule: 15 significant digits, or 17 when 15 do not read back as the
 * same double. An integral value prints as `42`. `value` is finite (checked at record). */
static void format_value(double value, char *out, size_t len)
{
    snprintf(out, len, "%.15g", value);
    if (strtod(out, NULL) != value) {
        snprintf(out, len, "%.17g", value);
    }
}

/* Build one payload in s_payload from the oldest unsent sample. Returns how many samples it
 * holds (0: nothing to send), with the last one's seq and the `dropped` it reported. */
static int build_payload(int *len_out, uint32_t *last_seq, uint32_t *dropped_reported)
{
    portENTER_CRITICAL(&s_lock);
    uint32_t seq = s_unsent;
    const uint32_t head = s_head; /* samples recorded after now_us below wait for the next */
    const uint32_t dropped = s_dropped;
    portEXIT_CRITICAL(&s_lock);
    if (seq == head) {
        return 0;
    }
    /* Once per payload, after the head snapshot: every age in it is measured at the same
     * instant, and none is negative. */
    const int64_t now_us = esp_timer_get_time();

    int len = snprintf(s_payload, sizeof(s_payload), "{\"dropped\":%" PRIu32 ",\"samples\":[",
                       dropped);
    int count = 0;
    while (true) {
        sample_t copy;
        memset(&copy, 0, sizeof(copy));
        bool have = false;
        portENTER_CRITICAL(&s_lock);
        if ((int32_t)(seq - s_unsent) < 0) {
            seq = s_unsent; /* overwritten while we formatted: skip to what is still there */
        }
        if ((int32_t)(seq - head) < 0) {
            copy = s_ring[seq % FF_TELEMETRY_CAPACITY];
            have = true;
        }
        portEXIT_CRITICAL(&s_lock);
        if (!have) {
            break;
        }

        char value_text[32];
        format_value(copy.value, value_text, sizeof(value_text));
        char entry[160];
        const int n = snprintf(entry, sizeof(entry),
                               "%s{\"age_ms\":%" PRId64 ",\"name\":\"%s\",\"value\":%s}",
                               count > 0 ? "," : "", (now_us - copy.t_us) / 1000, copy.name,
                               value_text);
        /* +2 for the closing `]}`. The first entry always fits: it is at most ~110 B. */
        if (n <= 0 || n >= (int)sizeof(entry) || len + n + 2 > FF_TELEMETRY_PAYLOAD_MAX) {
            break;
        }
        memcpy(s_payload + len, entry, (size_t)n);
        len += n;
        count++;
        *last_seq = copy.seq;
        seq = copy.seq + 1;
    }
    if (count == 0) {
        return 0;
    }
    memcpy(s_payload + len, "]}", 3);
    *len_out = len + 2;
    *dropped_reported = dropped;
    return count;
}

/* Publish one payload. True when one was published (there may be more to drain); false
 * when there was nothing to send or the publish failed (everything stays buffered). */
static bool publish_one(void)
{
    int len = 0;
    uint32_t last_seq = 0;
    uint32_t dropped_reported = 0;
    const int count = build_payload(&len, &last_seq, &dropped_reported);
    if (count == 0) {
        return false;
    }
    /* QoS 0, retain 0: property 4. */
    const int id = esp_mqtt_client_publish(s_client, s_topic, s_payload, len, 0, 0);
    if (id < 0) {
        ESP_LOGW(TAG, "publish %s failed (%d); %d samples stay buffered", s_topic, id, count);
        return false;
    }

    portENTER_CRITICAL(&s_lock);
    if ((int32_t)(last_seq + 1 - s_unsent) > 0) {
        s_unsent = last_seq + 1;
    }
    s_dropped -= s_dropped < dropped_reported ? s_dropped : dropped_reported;
    portEXIT_CRITICAL(&s_lock);

    ESP_LOGI(TAG, "publish %s (qos 0, no retain, %d samples, dropped %" PRIu32 ", %d B)", s_topic,
             count, dropped_reported, len);
    return true;
}

/* One task for the life of the process, like the heartbeat: esp-mqtt reconnects underneath
 * it. A notify is a (re)connect: drain. A timeout is the 5 s tick: at most one payload. */
static void flush_task(void *arg)
{
    (void)arg;
    while (true) {
        const bool drain = ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(FF_TELEMETRY_FLUSH_MS)) > 0;
        do {
            if (!s_connected || !publish_one()) {
                break;
            }
        } while (drain);
    }
}

void ff_telemetry_session_up(esp_mqtt_client_handle_t client, const char *topic)
{
    s_client = client;
    s_topic = topic;
    s_connected = true;
    if (s_task == NULL) {
        if (xTaskCreate(flush_task, "ff_tel", FF_TELEMETRY_STACK, NULL, FF_TELEMETRY_PRIO,
                        &s_task) != pdPASS) {
            ESP_LOGE(TAG, "cannot start the telemetry task; samples stay buffered");
            s_task = NULL;
            return;
        }
    }
    xTaskNotifyGive(s_task);
}

void ff_telemetry_session_down(void)
{
    s_connected = false;
}

bool ff_telemetry_drained(void)
{
    portENTER_CRITICAL(&s_lock);
    const bool drained = s_head == s_unsent;
    portEXIT_CRITICAL(&s_lock);
    return drained;
}
