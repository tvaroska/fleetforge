/* Fleetforge basic (ESP-IDF): a Morse blinker updated over the air. fleetforge_start() FIRST;
 * ship partitions.csv + sdkconfig.defaults unchanged (ab-4m-v1), ff_cfg at 0x12000 (README.md) */
#include <ctype.h>
#include <stdio.h>
#include "driver/gpio.h"
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
#ifndef MORSE_MESSAGE
#define MORSE_MESSAGE "SOS" /* what the LED blinks: change it to see an update land */
#endif
#ifndef LED_GPIO
#define LED_GPIO 2 /* an ESP32 DevKit's LED; an S3 DevKitC-1's is RGB: watch the log there */
#endif
#define UNIT_MS 200 /* one dot; a dash is three */
static const char *const CODES[36] = { /* A-Z, then 0-9 */
    ".-", "-...", "-.-.", "-..", ".", "..-.", "--.", "....", "..", ".---", "-.-", ".-..", "--",
    "-.", "---", ".--.", "--.-", ".-.", "...", "-", "..-", "...-", ".--", "-..-", "-.--", "--..",
    "-----", ".----", "..---", "...--", "....-", ".....", "-....", "--...", "---..", "----.",
};
static volatile bool s_idle; /* true in the gap after a whole message: see app_main */
static void wait_units(int units) { vTaskDelay(pdMS_TO_TICKS(units * UNIT_MS)); }
static bool between_messages(void *ctx) { (void)ctx; return s_idle; } /* the safe window */
static ff_selftest_result_t selftest(void *ctx, char *reason, size_t len) /* asked once, after */
{ (void)ctx, (void)reason, (void)len; return FF_SELFTEST_PENDING; } /* an update lands: see below */
static void blink(char letter)
{
    int c = toupper((unsigned char)letter);
    const char *code = isalpha(c) ? CODES[c - 'A'] : isdigit(c) ? CODES[26 + c - '0'] : NULL;
    if (code == NULL) { wait_units(4); return; } /* a space, or no Morse code: a word gap */
    for (; *code != '\0'; code++) {
        gpio_set_level(LED_GPIO, 1);
        wait_units(*code == '-' ? 3 : 1);
        gpio_set_level(LED_GPIO, 0);
        wait_units(1); /* the gap inside a letter */
    }
    wait_units(2); /* the gap between letters (3 units in all) */
}
void app_main(void)
{
    fleetforge_start(); /* FIRST: arms the rollback timer, then runs in its own task */
    ff_set_selftest(selftest, NULL); /* keep an update only if the loop below still runs */
    ff_set_safe_window_cb(between_messages, NULL); /* reboot only between two messages */
    ff_set_power_class(FF_POWER_ALWAYS_ON, 0); /* never sleeps; a battery board: FF_POWER_SLEEPY */
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    for (unsigned loops = 1;; loops++) { /* your firmware: it blinks, network or not */
        s_idle = false; /* a message starts: no reboot until it is done */
        char api_key[64] = {0};
        bool has_key = ff_get_secret("api_key", api_key, sizeof(api_key)) == ESP_OK;
        printf("morse: %s (firmware %s)\n", MORSE_MESSAGE, ff_identity_fw_version());
        printf("fleet: %s, %s\n", ff_is_connected() ? "connected" : "not connected",
               ff_is_ready_for_sleep() ? "could sleep now" : "must stay awake");
        ff_logf('I', "morse: %s (loop %u, key=%s)", MORSE_MESSAGE, loops, has_key ? "set" : "none");
        ff_telemetry("morse_loops", loops); /* sent to the fleet on up/telemetry */
        for (const char *letter = MORSE_MESSAGE; *letter != '\0'; letter++) { blink(*letter); }
        s_idle = true; /* the whole message blinked: a waiting reboot may happen now */
        ff_selftest_report(true, NULL); /* a whole message blinked: this firmware works */
        wait_units(4); /* the gap before the message repeats (7 units in all) */
    }
}
