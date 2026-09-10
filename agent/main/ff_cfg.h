/*
 * ff_cfg — the flash-time configuration blob, and the only thing that differs between
 * two boards flashed from the same bundle.
 *
 * One 4 KB `data`/0x40 partition at 0x12000 (agent/partitions.csv, FROZEN at R0) holds a
 * 16-byte header and a compact JSON payload. The full format, and the reasons for it, are
 * in agent/tools/ff_cfg.py — the encoder this reader is the counterpart of. The browser
 * flasher (R0-fe-3) is the second encoder; tests/test_ff_cfg.py cross-checks the constants
 * below against that file so a rename on one side fails on this box and not on a bench.
 *
 * The constants are RETYPED here rather than generated. A tripwire that imports the value
 * it guards proves nothing, and the partition geometry is a flash-time immutable
 * (CRITICAL.md): getting it wrong is a physical recall, not a patch.
 */

#pragma once

#include <stdbool.h>
#include <stdint.h>

#include "esp_err.h"

#ifdef __cplusplus
extern "C" {
#endif

/* Header (agent/tools/ff_cfg.py): magic, u16 version, u16 reserved, u32 payload_len,
 * u32 crc32 — 16 bytes, little-endian, then the payload, then 0xff to the end. */
#define FF_CFG_MAGIC "FFCF"
#define FF_CFG_VERSION 1
#define FF_CFG_HEADER_SIZE 16
#define FF_CFG_PARTITION_SIZE 4096
#define FF_CFG_MAX_PAYLOAD 4080

/* The partition this lives in, exactly as agent/partitions.csv declares it. Looked up by
 * label AND subtype so a future table that reuses the name for something else fails to
 * match rather than being parsed as a config. */
#define FF_CFG_PARTITION_LABEL "ff_cfg"
#define FF_CFG_PARTITION_SUBTYPE 0x40

/* Field capacities. Generous but bounded: everything lands in a fixed struct so no part of
 * the boot path allocates, and a payload that would overflow one is a loud rejection rather
 * than a silent truncation to half a URL. */
#define FF_CFG_MAX_URI 160
#define FF_CFG_MAX_TOKEN 128
#define FF_CFG_MAX_SSID 33 /* IEEE 802.11: 32 octets + NUL */
#define FF_CFG_MAX_PSK 65  /* WPA2: 63 characters + NUL */
#define FF_CFG_MAX_NTP 64
#define FF_CFG_MAX_POWER 16

/* spec/prd.md -> Requirements & targets -> Timing. Retyped, with the spec named, because
 * CRITICAL.md marks that table as the thing downstream code resolves against. */
#define FF_CFG_DEFAULT_HB_S 60
#define FF_CFG_DEFAULT_NTP "pool.ntp.org"
#define FF_CFG_DEFAULT_POWER "always_on"

/* The link seam's two implementations (ff_net.h). Lives here, not in ff_net.h, because
 * ff_cfg is the lowest layer and the config is what selects the adapter — ff_net.h
 * includes this header rather than the other way round. */
typedef enum {
    FF_LINK_WIFI = 0,
    FF_LINK_ETHERNET = 1,
} ff_link_t;

typedef struct {
    char api_base[FF_CFG_MAX_URI];  /* origin serving /v1; the scheme selects TLS */
    char mqtt_uri[FF_CFG_MAX_URI];  /* mqtt:// or mqtts://; the scheme selects TLS */
    char token[FF_CFG_MAX_TOKEN];   /* the single-use enrollment token; "" once used up */
    char ssid[FF_CFG_MAX_SSID];     /* link=wifi. NEVER spelled wifi_ssid — see ff_cfg.c */
    char psk[FF_CFG_MAX_PSK];       /* link=wifi */
    char ntp[FF_CFG_MAX_NTP];       /* "" means: do not sync, and TLS will fail */
    char power[FF_CFG_MAX_POWER];   /* always_on | sleepy — reported in up/announce */
    ff_link_t link;
    int hb_s;   /* heartbeat seconds */
    int wake_s; /* expected_wake_interval_s; 0 = absent (reported as null) */
} ff_cfg_t;

/*
 * Read, validate and parse the ff_cfg partition into `out`.
 *
 * Fails LOUDLY and TERMINALLY: on any error the caller must idle, never fall back to a
 * compiled-in default. A board that boots with a bad config and does nothing is diagnosable
 * from its serial log; one that invents a server URL connects somewhere unexpected and is
 * not.
 *
 * Returns ESP_ERR_NOT_FOUND (no such partition), ESP_ERR_INVALID_CRC, ESP_ERR_INVALID_VERSION,
 * ESP_ERR_INVALID_SIZE, ESP_ERR_INVALID_ARG (not an object / missing a required key) or
 * ESP_ERR_NOT_SUPPORTED (erased: never configured). Every one of them has already been
 * logged by the time it returns.
 */
esp_err_t ff_cfg_load(ff_cfg_t *out);

/* "wifi" | "ethernet" — the string reported as `link_type` in up/announce. */
const char *ff_cfg_link_name(ff_link_t link);

/* Log the config the board is running with. Never prints the token or the passphrase:
 * the serial console is the one place a field engineer's credential leaks from. */
void ff_cfg_log(const ff_cfg_t *cfg);

#ifdef __cplusplus
}
#endif
