/*
 * Fleetforge basic (ESP-IDF) — a Morse-code blinker that updates itself over the air.
 *
 * fleetforge_start() FIRST, then your firmware. Ship partitions.csv and sdkconfig.defaults
 * unchanged (layout ab-4m-v1) and flash the board's ff_cfg blob at 0x12000 (README.md).
 */
#include <ctype.h>
#include <stdbool.h>
#include <stdio.h>

#include "driver/gpio.h"
#include "ff_identity.h"
#include "fleetforge_start.h"
#include "freertos/FreeRTOS.h"
#include "freertos/task.h"

#ifndef MORSE_MESSAGE
#define MORSE_MESSAGE "SOS" /* what the LED blinks: change it to see an update land */
#endif
#ifndef LED_GPIO
/* An ESP32 DevKit's LED. The S3 DevKitC-1's is an RGB LED a plain GPIO cannot light: on
 * that board the "morse:" log line is the indicator. */
#define LED_GPIO 2
#endif
#define UNIT_MS 200 /* one dot; a dash is three */

static const char *const CODES[36] = { /* A-Z, then 0-9 */
    ".-", "-...", "-.-.", "-..", ".", "..-.", "--.", "....", "..", ".---", "-.-", ".-..",
    "--", "-.", "---", ".--.", "--.-", ".-.", "...", "-", "..-", "...-", ".--", "-..-",
    "-.--", "--..", "-----", ".----", "..---", "...--", "....-", ".....", "-....", "--...",
    "---..", "----.",
};

static void wait_units(int units) { vTaskDelay(pdMS_TO_TICKS(units * UNIT_MS)); }

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
    gpio_reset_pin(LED_GPIO);
    gpio_set_direction(LED_GPIO, GPIO_MODE_OUTPUT);

    /* Your firmware. It keeps blinking whether or not the board ever reaches the network. */
    while (true) {
        printf("morse: %s (firmware %s)\n", MORSE_MESSAGE, ff_identity_fw_version());
        for (const char *letter = MORSE_MESSAGE; *letter != '\0'; letter++) {
            blink(*letter);
        }
        wait_units(4); /* the gap before the message repeats (7 units in all) */
    }
}
