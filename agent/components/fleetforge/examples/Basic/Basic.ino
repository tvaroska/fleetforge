// Fleetforge Basic — a Morse-code blinker that updates itself over the air (README.md).
// Ship this folder's partitions.csv with your sketch, unchanged (layout ab-4m-arduino-v1),
// and flash the board's ff_cfg blob (agent/tools/ff_cfg.py) at 0x3D0000.
// The library owns the network link, the clock and the OTA confirm decision: do not call
// WiFi.begin(), ETH.begin(), configTime() or esp_ota_mark_app_valid_cancel_rollback(), and
// do not define verifyRollbackLater() or verifyOta().

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
static unsigned loops = 0;  // messages blinked since boot: the metric below
static volatile bool betweenMessages = false;  // true in the gap after a whole message
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

static void selfTest() {}  // asked once, after an update reaches the fleet: loop() answers
static bool safeNow() { return betweenMessages; }  // reboot only between messages, never mid-word

void setup() {
  Fleetforge.begin(FW_VERSION);  // FIRST: arms the rollback timer, then runs in its own task
  Fleetforge.onSelfTest(selfTest);  // keep an update only if loop() still runs (below)
  Fleetforge.onSafeWindow(safeNow);  // an update or a rollback reboots only when safeNow() agrees
  Serial.begin(115200);
  pinMode(LED_PIN, OUTPUT);
}

// Your firmware. It keeps blinking whether or not the board ever reaches the network.
void loop() {
  betweenMessages = false;  // a message starts: no reboot until it is done
  Serial.printf("morse: %s (firmware %s)\n", MORSE_MESSAGE, FW_VERSION);
  Fleetforge.metric("morse_loops", ++loops);  // sent to the fleet on up/telemetry
  for (const char *letter = MORSE_MESSAGE; *letter != '\0'; letter++) {
    blink(*letter);
  }
  betweenMessages = true;  // the whole message blinked: a waiting reboot may happen now
  Fleetforge.selfTestDone(true);  // a whole message blinked: this firmware works
  delay(4 * UNIT_MS);  // the gap before the message repeats (7 units in all)
}
