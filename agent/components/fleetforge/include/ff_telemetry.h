/*
 * ff_telemetry — numbers your firmware reports to the fleet, on `up/telemetry`.
 *
 * This is the MAKER API (R4-fw-2, DECISIONS 2026-10-15): a header a firmware's own code
 * calls. The stock agent has no application and therefore never includes it; the two worked
 * examples (examples/basic_idf/main/main.c, and examples/Basic through Fleetforge.metric())
 * call every function it declares. Like the rest of include/ it is ADDITIVE-ONLY from the
 * release that first ships it: a firmware built on it goes to boards nobody can reach again.
 *
 *     ff_telemetry("battery_v", 3.71);
 *
 * What a call does: it records one sample (name, value, and the time on the device's
 * monotonic clock) in a RAM ring of the newest 64 samples. The session sends the ring as
 * `{"dropped":0,"samples":[{"age_ms":...,"name":"...","value":...}, ...]}`, oldest first:
 * at most one payload every 5 s while connected, and back to back right after a (re)connect.
 * `age_ms` is how long ago the sample was taken, and the server stamps it `receipt time -
 * age_ms`, so a sample taken while the board was offline arrives with its original timing
 * and the board needs no wall clock. QoS 0, not retained: a sample is not worth a
 * retransmit. A reboot loses the samples not yet sent.
 *
 * The contract:
 *
 *  - `name`: 1-31 characters of `a-z`, `0-9` and `_`, starting with a letter (a valid
 *    Prometheus metric name). Names starting `ff_` are reserved for the agent's own metrics
 *    and refused.
 *  - `value`: any finite double. NaN and +/-inf are refused.
 *  - Returns ESP_OK when the sample was recorded, including when the ring was full and the
 *    oldest buffered sample made room for it (the next payload's `dropped` counts that).
 *    Returns ESP_ERR_INVALID_ARG, recording nothing, for a NULL or invalid name or a
 *    non-finite value.
 *  - Callable from any task, at any time: before fleetforge_start() / Fleetforge.begin(),
 *    while enrolling, while disconnected. NOT from an ISR.
 *  - It never blocks, never allocates and never logs.
 */

#pragma once

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Record one sample of the metric `name`. See the file header for the contract. */
esp_err_t ff_telemetry(const char *name, double value);

#ifdef __cplusplus
}
#endif
