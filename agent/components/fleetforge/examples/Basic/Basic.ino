// Fleetforge Basic — a Morse-code blinker that updates itself over the air (README.md).
//
// Ship this folder's partitions.csv with your sketch, unchanged (layout ab-4m-arduino-v1),
// and flash the board's ff_cfg blob (agent/tools/ff_cfg.py) at 0x3D0000.
// The library owns the network link, the clock and the OTA confirm decision:
//   - do not call WiFi.begin(), ETH.begin() or configTime();
//   - do not define verifyRollbackLater() or verifyOta();
//   - do not call esp_ota_mark_app_valid_cancel_rollback().

#include <Fleetforge.h>

#ifndef FW_VERSION
#define FW_VERSION "1.0.0"  // what the fleet dashboard shows: change it for every build
#endif
#ifndef MORSE_MESSAGE
#define MORSE_MESSAGE "SOS"  // what the LED blinks: change it to see an update land
#endif
#ifdef LED_BUILTIN
static const uint8_t LED_PIN = LED_BUILTIN;  // the board's own LED (S3 DevKitC-1: the RGB LED)
#else
static const uint8_t LED_PIN = 2;  // the LED of most ESP32 DevKits
#endif

static const unsigned UNIT_MS = 200;  // one dot; a dash is three
static const char *const CODES[36] = {  // A-Z, then 0-9
  ".-", "-...", "-.-.", "-..", ".", "..-.", "--.", "....", "..", ".---", "-.-", ".-..",
  "--", "-.", "---", ".--.", "--.-", ".-.", "...", "-", "..-", "...-", ".--", "-..-",
  "-.--", "--..", "-----", ".----", "..---", "...--", "....-", ".....", "-....", "--...",
  "---..", "----.",
};

static void blink(char letter) {
  int c = toupper((unsigned char)letter);
  const char *code = isalpha(c) ? CODES[c - 'A'] : isdigit(c) ? CODES[26 + c - '0'] : nullptr;
  if (code == nullptr) {  // a space, or anything Morse has no code for: a word gap
    delay(4 * UNIT_MS);
    return;
  }
  for (; *code != '\0'; code++) {
    digitalWrite(LED_PIN, HIGH);
    delay((*code == '-' ? 3 : 1) * UNIT_MS);
    digitalWrite(LED_PIN, LOW);
    delay(UNIT_MS);  // the gap inside a letter
  }
  delay(2 * UNIT_MS);  // the gap between letters (3 units in all)
}

void setup() {
  Fleetforge.begin(FW_VERSION);  // FIRST: arms the rollback timer, then runs in its own task
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
}

// Your firmware. It keeps blinking whether or not the board ever reaches the network.
void loop() {
  Serial.printf("morse: %s (firmware %s)\n", MORSE_MESSAGE, FW_VERSION);
  for (const char *letter = MORSE_MESSAGE; *letter != '\0'; letter++) {
    blink(*letter);
  }
  delay(4 * UNIT_MS);  // the gap before the message repeats (7 units in all)
}
