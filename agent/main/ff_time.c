/*
 * ff_time — see ff_time.h for why this runs before the first TLS handshake.
 *
 * Uses `esp_netif_sntp`, the IDF wrapper that owns the lwIP SNTP singleton, rather than
 * poking `sntp_*` directly: it is the API that survives IDF's periodic reshuffles of the
 * underlying lwIP options, and it provides the blocking wait this file needs.
 */

#include "ff_time.h"

#include <string.h>
#include <sys/time.h>
#include <time.h>

#include "esp_log.h"
#include "esp_netif_sntp.h"
#include "freertos/FreeRTOS.h"

static const char *TAG = "ff-time";

/* The year that separates "SNTP worked" from "still at epoch 0". Any value >= this is
 * plausible; the agent deliberately does not try to judge how plausible, because a board
 * has no second opinion to check the NTP server against. */
#define FF_TIME_SANE_YEAR 2024

bool ff_time_is_sane(void)
{
    time_t now = time(NULL);
    struct tm tm_now;
    gmtime_r(&now, &tm_now);
    return (tm_now.tm_year + 1900) >= FF_TIME_SANE_YEAR;
}

void ff_time_iso8601(char *out, size_t len)
{
    if (out == NULL || len == 0) {
        return;
    }
    time_t now = time(NULL);
    struct tm tm_now;
    gmtime_r(&now, &tm_now);
    if (strftime(out, len, "%Y-%m-%dT%H:%M:%SZ", &tm_now) == 0) {
        out[0] = '\0';
    }
}

esp_err_t ff_time_sync(const char *server, uint32_t timeout_ms)
{
    char before[24] = {0};
    char after[24] = {0};
    ff_time_iso8601(before, sizeof(before));

    if (server == NULL || server[0] == '\0') {
        /* A deliberate configuration (`--ntp ""`), so not an error — but say what it
         * costs, because the failure it causes appears three layers away as a TLS
         * handshake error nobody will connect back to this line. */
        ESP_LOGW(TAG, "no ntp server configured: the clock stays at %s. Any https:// or "
                      "mqtts:// endpoint WILL fail its certificate validity check.",
                 before);
        return ESP_ERR_INVALID_STATE;
    }

    esp_sntp_config_t config = ESP_NETIF_SNTP_DEFAULT_CONFIG(server);
    esp_err_t err = esp_netif_sntp_init(&config);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot start sntp against %s: %s", server, esp_err_to_name(err));
        return err;
    }

    err = esp_netif_sntp_sync_wait(pdMS_TO_TICKS(timeout_ms));
    ff_time_iso8601(after, sizeof(after));
    if (err != ESP_OK) {
        /* Not fatal: a plaintext lab setup (mqtt:// + http://) works fine at epoch 0, and
         * refusing to boot would make the emulator useless. The caller logs on. */
        ESP_LOGW(TAG, "sntp: no answer from %s within %u ms; clock is still %s",
                 server, (unsigned)timeout_ms, after);
        return ESP_ERR_TIMEOUT;
    }

    /* The before/after pair in one line: this is the evidence that the jump happened and
     * the timestamp everything afterwards (TLS validity, enrolled_at) is judged against. */
    ESP_LOGI(TAG, "sntp: %s -> %s (via %s)", before, after, server);
    if (!ff_time_is_sane()) {
        ESP_LOGW(TAG, "sntp answered but the clock is still before %d — TLS will fail",
                 FF_TIME_SANE_YEAR);
    }
    return ESP_OK;
}
