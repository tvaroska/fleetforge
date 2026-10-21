/*
 * ff_log — see include/ff_log.h (R4-fw-3, R4-fw-4, OTEL-fw-1).
 *
 * Mirrors ff_telemetry.c's lock and flush structure:
 *   - A static ring of the newest 32 lines under a portMUX spinlock held for copies only.
 *   - A 4-line magic-and-CRC-guarded RTC_NOINIT_ATTR ring so lines recorded right before a
 *     panic or watchdog reset survive the reset and are queued into the RAM ring when the
 *     recovery boot initializes or brings the session up.
 *   - At most one QoS 0, non-retained publish per 5 s after the connect drain.
 */

#include "ff_log.h"
#include "ff_log_internal.h"

#include <inttypes.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#include "esp_attr.h"
#include "esp_log.h"
#include "esp_rom_crc.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static const char *TAG = "ff-log";

#define FF_LOG_CAPACITY 32
#define FF_LOG_TEXT_MAX 200
#define FF_LOG_PAYLOAD_MAX 1800
#define FF_LOG_FLUSH_MS 5000
#define FF_LOG_TASK_STACK 4096
#define FF_LOG_TASK_PRIO 4

/* R4-fw-4: crash-surviving tail in RTC slow memory. */
#define FF_LOG_RTC_MAGIC 0x46464C47u
#define FF_LOG_RTC_LINES 4

typedef struct {
    char level;
    char text[FF_LOG_TEXT_MAX + 1];
} ff_log_rtc_slot_t;

typedef struct {
    uint32_t magic;
    uint32_t head;
    ff_log_rtc_slot_t slots[FF_LOG_RTC_LINES];
    uint32_t crc;
} ff_log_rtc_t;

static RTC_NOINIT_ATTR ff_log_rtc_t s_rtc;
static bool s_rtc_checked;

typedef struct {
    uint64_t seq;
    int64_t at_us;
    char level;
    char text[FF_LOG_TEXT_MAX + 1];
} ff_log_entry_t;

static portMUX_TYPE s_lock = portMUX_INITIALIZER_UNLOCKED;
static ff_log_entry_t s_ring[FF_LOG_CAPACITY];
static uint64_t s_head;
static uint64_t s_unsent;
static uint32_t s_dropped;

static esp_mqtt_client_handle_t s_client;
static char s_topic[96];
static volatile bool s_connected;
static TaskHandle_t s_task;
static char s_payload[FF_LOG_PAYLOAD_MAX];

static uint32_t rtc_crc(void)
{
    return esp_rom_crc32_le(0, (const uint8_t *)&s_rtc, offsetof(ff_log_rtc_t, crc));
}

static bool is_crash_reset(esp_reset_reason_t reason)
{
    switch (reason) {
    case ESP_RST_PANIC:
    case ESP_RST_INT_WDT:
    case ESP_RST_TASK_WDT:
    case ESP_RST_WDT:
        return true;
    default:
        return false;
    }
}

static char normalize_level(char level)
{
    switch (level) {
    case 'E':
    case 'W':
    case 'I':
    case 'D':
    case 'V':
        return level;
    default:
        return 'I';
    }
}

static void sanitize_text(char *buf)
{
    for (size_t i = 0; buf[i] != '\0'; i++) {
        unsigned char c = (unsigned char)buf[i];
        if (c < 0x20 || c == 0x7f) {
            buf[i] = ' ';
        }
    }
}

static void push_ring_locked(char level, const char *text, int64_t now_us)
{
    ff_log_entry_t *slot = &s_ring[s_head % FF_LOG_CAPACITY];
    if (s_head - s_unsent >= FF_LOG_CAPACITY) {
        s_unsent = s_head - FF_LOG_CAPACITY + 1;
        if (s_dropped < UINT32_MAX) {
            s_dropped++;
        }
    }
    slot->seq = s_head;
    slot->at_us = now_us;
    slot->level = level;
    strlcpy(slot->text, text, sizeof(slot->text));
    s_head++;
}

static void recover_rtc_if_needed_locked(int64_t now_us)
{
    if (s_rtc_checked) {
        return;
    }
    s_rtc_checked = true;

    const bool valid = s_rtc.magic == FF_LOG_RTC_MAGIC && s_rtc.crc == rtc_crc() &&
                       s_rtc.head <= 0x10000000u;
    const esp_reset_reason_t reason = esp_reset_reason();
    if (valid && is_crash_reset(reason) && s_rtc.head > 0) {
        const uint32_t count =
            s_rtc.head < FF_LOG_RTC_LINES ? s_rtc.head : (uint32_t)FF_LOG_RTC_LINES;
        const uint32_t start = s_rtc.head - count;
        for (uint32_t i = 0; i < count; i++) {
            const ff_log_rtc_slot_t *saved = &s_rtc.slots[(start + i) % FF_LOG_RTC_LINES];
            if (saved->text[0] != '\0') {
                char recovered[FF_LOG_TEXT_MAX + 1];
                snprintf(recovered, sizeof(recovered), "[prev-boot] %s", saved->text);
                push_ring_locked(normalize_level(saved->level), recovered, now_us);
            }
        }
    }
    memset(&s_rtc, 0, sizeof(s_rtc));
    s_rtc.magic = FF_LOG_RTC_MAGIC;
    s_rtc.crc = rtc_crc();
}

esp_err_t ff_logf(char level, const char *fmt, ...)
{
    if (fmt == NULL) {
        return ESP_ERR_INVALID_ARG;
    }
    char formatted[FF_LOG_TEXT_MAX + 1];
    va_list args;
    va_start(args, fmt);
    int n = vsnprintf(formatted, sizeof(formatted), fmt, args);
    va_end(args);
    if (n <= 0) {
        return ESP_ERR_INVALID_ARG;
    }
    formatted[FF_LOG_TEXT_MAX] = '\0';
    sanitize_text(formatted);
    if (formatted[0] == '\0') {
        return ESP_ERR_INVALID_ARG;
    }

    const char norm = normalize_level(level);
    const int64_t now_us = esp_timer_get_time();

    portENTER_CRITICAL(&s_lock);
    recover_rtc_if_needed_locked(now_us);
    push_ring_locked(norm, formatted, now_us);
    ff_log_rtc_slot_t *rtc_slot = &s_rtc.slots[s_rtc.head % FF_LOG_RTC_LINES];
    rtc_slot->level = norm;
    strlcpy(rtc_slot->text, formatted, sizeof(rtc_slot->text));
    s_rtc.head++;
    s_rtc.crc = rtc_crc();
    portEXIT_CRITICAL(&s_lock);

    return ESP_OK;
}

/* Append a JSON-escaped string into dst[pos..cap-1]. Returns new pos or 0 if out of room. */
static size_t append_json_escaped(char *dst, size_t pos, size_t cap, const char *src)
{
    for (size_t i = 0; src[i] != '\0'; i++) {
        char c = src[i];
        if (c == '"' || c == '\\') {
            if (pos + 2 >= cap) {
                return 0;
            }
            dst[pos++] = '\\';
            dst[pos++] = c;
        } else {
            if (pos + 1 >= cap) {
                return 0;
            }
            dst[pos++] = c;
        }
    }
    dst[pos] = '\0';
    return pos;
}

static bool publish_one(void)
{
    if (!s_connected || s_client == NULL) {
        return false;
    }

    const int64_t now_us = esp_timer_get_time();
    portENTER_CRITICAL(&s_lock);
    recover_rtc_if_needed_locked(now_us);
    if (s_head > s_unsent && s_head - s_unsent > FF_LOG_CAPACITY) {
        s_unsent = s_head - FF_LOG_CAPACITY;
    }
    uint64_t seq = s_unsent;
    const uint64_t head = s_head;
    const uint32_t dropped = s_dropped;
    portEXIT_CRITICAL(&s_lock);

    if (seq >= head) {
        return false;
    }

    int head_len = snprintf(s_payload, sizeof(s_payload), "{\"dropped\":%" PRIu32 ",\"lines\":[",
                            dropped);
    if (head_len <= 0 || (size_t)head_len >= sizeof(s_payload) - 4) {
        return false;
    }
    size_t pos = (size_t)head_len;
    uint64_t last_seq = seq;
    size_t packed = 0;

    while (seq < head) {
        ff_log_entry_t entry;
        memset(&entry, 0, sizeof(entry));
        bool have = false;
        portENTER_CRITICAL(&s_lock);
        if (seq < s_unsent) {
            seq = s_unsent;
        }
        if (seq < head && seq < s_head) {
            entry = s_ring[seq % FF_LOG_CAPACITY];
            have = true;
        }
        portEXIT_CRITICAL(&s_lock);
        if (!have) {
            break;
        }

        char prefix[64];
        int64_t age_ms = (now_us - entry.at_us) / 1000;
        if (age_ms < 0) {
            age_ms = 0;
        }
        int plen = snprintf(prefix, sizeof(prefix), "%s{\"age_ms\":%" PRId64 ",\"level\":\"%c\",\"text\":\"",
                            packed > 0 ? "," : "", age_ms, entry.level);
        if (plen <= 0) {
            break;
        }
        const size_t save = pos;
        if (pos + (size_t)plen + 8 >= sizeof(s_payload)) {
            break;
        }
        memcpy(s_payload + pos, prefix, (size_t)plen);
        pos += (size_t)plen;
        size_t next_pos = append_json_escaped(s_payload, pos, sizeof(s_payload) - 4, entry.text);
        if (next_pos == 0 || next_pos + 4 >= sizeof(s_payload)) {
            pos = save;
            s_payload[pos] = '\0';
            break;
        }
        pos = next_pos;
        s_payload[pos++] = '"';
        s_payload[pos++] = '}';
        s_payload[pos] = '\0';
        last_seq = entry.seq;
        seq = entry.seq + 1;
        packed++;
    }

    if (packed == 0) {
        return false;
    }
    s_payload[pos++] = ']';
    s_payload[pos++] = '}';
    s_payload[pos] = '\0';

    int id = esp_mqtt_client_publish(s_client, s_topic, s_payload, (int)pos, 0, 0);
    if (id < 0) {
        return false;
    }

    portENTER_CRITICAL(&s_lock);
    if (last_seq + 1 > s_unsent) {
        s_unsent = last_seq + 1;
    }
    if (s_dropped >= dropped) {
        s_dropped -= dropped;
    } else {
        s_dropped = 0;
    }
    portEXIT_CRITICAL(&s_lock);

    ESP_LOGI(TAG, "publish %s (qos 0, no retain, %u lines, dropped %" PRIu32 ", %u B)", s_topic,
             (unsigned)packed, dropped, (unsigned)pos);
    return true;
}

static void log_flush_task(void *arg)
{
    (void)arg;
    while (true) {
        if (ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(FF_LOG_FLUSH_MS)) > 0) {
            while (s_connected && publish_one()) {
            }
        } else if (s_connected) {
            (void)publish_one();
        }
    }
}

void ff_log_session_up(esp_mqtt_client_handle_t client, const char *topic)
{
    s_client = client;
    strlcpy(s_topic, topic, sizeof(s_topic));
    s_connected = true;
    if (s_task == NULL) {
        if (xTaskCreate(log_flush_task, "ff_log", FF_LOG_TASK_STACK, NULL, FF_LOG_TASK_PRIO,
                        &s_task) != pdPASS) {
            ESP_LOGE(TAG, "cannot start the log flush task");
            s_task = NULL;
            return;
        }
    }
    xTaskNotifyGive(s_task);
}

void ff_log_session_down(void)
{
    s_connected = false;
}

bool ff_log_drained(void)
{
    bool drained;
    portENTER_CRITICAL(&s_lock);
    drained = s_head == s_unsent;
    portEXIT_CRITICAL(&s_lock);
    return drained;
}
