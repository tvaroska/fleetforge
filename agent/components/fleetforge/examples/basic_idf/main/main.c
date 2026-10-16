/* Fleetforge basic (ESP-IDF) — a Morse-code blinker that updates itself over the air.
 * fleetforge_start() FIRST, then your firmware. Ship partitions.csv and sdkconfig.defaults
 * unchanged (layout ab-4m-v1) and flash the board's ff_cfg blob at 0x12000 (README.md). */
#include <ctype.h>
#include <stdio.h>
#include "driver/gpio.h"
#include "ff_identity.h"
#include "ff_selftest.h"
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
    ".-", "-...", "-.-.", "-..", ".", "..-.", "--.", "....", "..", ".---", "-.-", ".-..",
    "--", "-.", "---", ".--.", "--.-", ".-.", "...", "-", "..-", "...-", ".--", "-..-",
    "-.--", "--..", "-----", ".----", "..---", "...--", "....-", ".....", "-....", "--...",
    "---..", "----.",
};

static void wait_units(int units) { vTaskDelay(pdMS_TO_TICKS(units * UNIT_MS)); }

static ff_selftest_result_t selftest(void *ctx, char *reason, size_t reason_len)
{
    (void)ctx, (void)reason, (void)reason_len;
    return FF_SELFTEST_PENDING; /* asked once, after an update reaches the fleet; see below */
}

static void blink(char letter)
{
    int c = toupper((unsigned char)letter);
    const char *code = isalpha(c) ? CODES[c - 'A'] : isdigit(c) ? CODES[26 + c - '0'] : NULL;
    if (code == NULL) { /* a space, or anything Morse has no code for: a word gap */
        wait_units(4);
        return;
    }
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
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);
    /* Your firmware. It keeps blinking whether or not the board ever reaches the network. */
    for (unsigned loops = 1;; loops++) {
        printf("morse: %s (firmware %s)\n", MORSE_MESSAGE, ff_identity_fw_version());
        ff_telemetry("morse_loops", loops); /* sent to the fleet on up/telemetry */
        for (const char *letter = MORSE_MESSAGE; *letter != '\0'; letter++) {
            blink(*letter);
        }
        ff_selftest_report(true, NULL); /* a whole message blinked: this firmware works */
        wait_units(4); /* the gap before the message repeats (7 units in all) */
    }
}
