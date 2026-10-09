// Fleetforge Basic — the smallest sketch that enrolls, heartbeats and takes OTA updates.
//
// Ship this folder's partitions.csv with your sketch, unchanged (layout ab-4m-arduino-v1),
// and flash the board's ff_cfg blob (agent/tools/ff_cfg.py) at 0x3D0000.
//
// The library owns the network link, the clock and the OTA confirm decision:
//   - do not call WiFi.begin(), ETH.begin() or configTime();
//   - do not define verifyRollbackLater() or verifyOta();
//   - do not call esp_ota_mark_app_valid_cancel_rollback().
// An OTA'd image is confirmed once it reaches the fleet, and rolls back if it does not.

#include <Fleetforge.h>

// This firmware's version: what the fleet dashboard shows. Override with -DFW_VERSION=...
#ifndef FW_VERSION
#define FW_VERSION "1.0.0"
#endif

void setup() {
  Fleetforge.begin(FW_VERSION);  // FIRST: arms the rollback timer before anything else
  Serial.begin(115200);
}

void loop() {
  Serial.printf("basic: running firmware %s\n", FW_VERSION);
  delay(10000);
}
