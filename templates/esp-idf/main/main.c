/* {{APP_NAME}}: ESP-IDF firmware managed over the air by Fleetforge (project {{PROJECT_SLUG}}).
 *
 * fleetforge_start() MUST be the first statement of app_main(); partitions.csv and the
 * rollback lines in sdkconfig.defaults define layout ab-4m-v1 and must ship unchanged.
 * See AGENTS.md for the full C API and DRAM guardrails.
 */

#include <stdbool.h>
#include <stdio.h>
#include <string.h>

#include "esp_heap_caps.h"
#include "ff_identity.h"
#include "ff_log.h"
#include "ff_safe_window.h"
#include "ff_sec.h"
#include "ff_selftest.h"
#include "ff_sleep.h"
#include "ff_telemetry.h"
#include "fleetforge_start.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

static volatile bool s_busy;

/* Hold OTA/rollback reboots while s_busy is true (e.g. during a display refresh or flash write). */
static bool is_safe_window(void *ctx)
{
    (void)ctx;
    return !s_busy;
}

#define MIN_HEALTHY_INTERNAL_HEAP_BYTES 16384U

/* Defer OTA confirmation until the main loop verifies heap headroom and identity, then calls ff_selftest_report(). */
static ff_selftest_result_t on_selftest(void *ctx, char *reason, size_t len)
{
    (void)ctx;
    size_t free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);
    if (free_internal < MIN_HEALTHY_INTERNAL_HEAP_BYTES) {
        snprintf(reason, len, "internal heap low (%u B)", (unsigned)free_internal);
        return FF_SELFTEST_FAIL;
    }
    const char *ver = ff_identity_fw_version();
    if (ver == NULL || ver[0] == '\0') {
        snprintf(reason, len, "empty firmware version");
        return FF_SELFTEST_FAIL;
    }
    return FF_SELFTEST_PENDING;
}

void app_main(void)
{
    fleetforge_start(); /* FIRST: arms the rollback timer, then runs the agent in its own task */
    ff_set_selftest(on_selftest, NULL);
    ff_set_safe_window_cb(is_safe_window, NULL);
    ff_set_power_class(FF_POWER_ALWAYS_ON, 0); /* For battery boards: FF_POWER_SLEEPY, wake_s */

    for (unsigned cycle = 1;; cycle++) {
        s_busy = true;

        char api_url[128] = {0};
        bool has_url = (ff_get_secret("api_url", api_url, sizeof(api_url)) == ESP_OK);

        printf("{{APP_NAME}}: cycle=%u fw=%s connected=%d api_url=%s\n",
               cycle,
               ff_identity_fw_version(),
               ff_is_connected(),
               has_url ? "configured" : "unset");

        ff_logf('I', "{{APP_NAME}} cycle=%u fw=%s api_url=%s",
                cycle,
                ff_identity_fw_version(),
                has_url ? "configured" : "unset");
        ff_telemetry("app_cycles", (double)cycle);

        s_busy = false;
        ff_selftest_report(true, NULL); /* Main loop completed a healthy iteration */

        vTaskDelay(pdMS_TO_TICKS(10000));
    }
}
