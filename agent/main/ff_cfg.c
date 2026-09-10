/*
 * ff_cfg — read the flash-time configuration blob. See ff_cfg.h for the format and
 * agent/tools/ff_cfg.py for the encoder this mirrors.
 *
 * Two naming rules that look arbitrary and are not:
 *
 *  - The Wi-Fi keys are `ssid` and `psk`, NEVER `wifi_ssid` / `wifi_password`.
 *    tests/test_agent_partitions.py::test_agent_holds_no_credential greps every file under
 *    agent/ for those spellings next to a quoted value, because that is exactly what a
 *    credential baked into firmware looks like. The tripwire is deliberately blunt (its
 *    character class spans newlines), so a JSON key name in a C string is enough to trip
 *    it. Renaming the keys to match it is free; relaxing the tripwire is not.
 *  - Unknown keys are IGNORED, never rejected. That is what makes the format additive in
 *    the same way spec/device-protocol.md -> Evolution rules makes the wire format
 *    additive: a newer flasher may write a key this agent has never heard of, and the
 *    board must still boot. It is the one property that cannot be added later, because
 *    the fielded agent is the one doing the ignoring.
 */

#include "ff_cfg.h"

#include <inttypes.h>
#include <stdlib.h>
#include <string.h>

#include "cJSON.h"
#include "esp_crc.h"
#include "esp_log.h"
#include "esp_partition.h"

static const char *TAG = "ff-cfg";

/* Little-endian, packed, byte-for-byte the header agent/tools/ff_cfg.py writes. The
 * static assert is the third leg of the contract (python struct / this struct / the
 * FF_CFG_HEADER_SIZE constant the tests retype). */
typedef struct __attribute__((packed)) {
    char magic[4];
    uint16_t version;
    uint16_t reserved;
    uint32_t payload_len;
    uint32_t crc32;
} ff_cfg_header_t;

_Static_assert(sizeof(ff_cfg_header_t) == FF_CFG_HEADER_SIZE, "ff_cfg header must be 16 bytes");
_Static_assert(FF_CFG_MAX_PAYLOAD == FF_CFG_PARTITION_SIZE - FF_CFG_HEADER_SIZE,
               "ff_cfg payload capacity must be the partition minus the header");

/* Copy a JSON string field into a fixed buffer, or fail. A truncated URL is worse than a
 * missing one: it produces a connection attempt to somewhere that is not the server. */
static esp_err_t copy_string(const cJSON *root, const char *key, char *dest, size_t cap,
                             bool required)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (item == NULL || cJSON_IsNull(item)) {
        if (required) {
            ESP_LOGE(TAG, "config has no '%s' — the agent has no compiled-in default for it", key);
            return ESP_ERR_INVALID_ARG;
        }
        dest[0] = '\0';
        return ESP_OK;
    }
    if (!cJSON_IsString(item)) {
        ESP_LOGE(TAG, "config key '%s' is not a string", key);
        return ESP_ERR_INVALID_ARG;
    }
    const char *value = item->valuestring;
    size_t length = strlen(value);
    if (length >= cap) {
        ESP_LOGE(TAG, "config key '%s' is %u bytes, over the %u-byte field", key,
                 (unsigned)length, (unsigned)(cap - 1));
        return ESP_ERR_INVALID_SIZE;
    }
    memcpy(dest, value, length + 1);
    return ESP_OK;
}

/* An integer field, or `fallback` when it is absent. A present-but-wrong type is an error
 * rather than a silent fallback, so a flasher bug surfaces on the first board. */
static esp_err_t copy_int(const cJSON *root, const char *key, int *dest, int fallback)
{
    const cJSON *item = cJSON_GetObjectItemCaseSensitive(root, key);
    if (item == NULL || cJSON_IsNull(item)) {
        *dest = fallback;
        return ESP_OK;
    }
    if (!cJSON_IsNumber(item)) {
        ESP_LOGE(TAG, "config key '%s' is not a number", key);
        return ESP_ERR_INVALID_ARG;
    }
    *dest = item->valueint;
    return ESP_OK;
}

static esp_err_t parse_payload(const char *payload, size_t length, ff_cfg_t *out)
{
    cJSON *root = cJSON_ParseWithLength(payload, length);
    if (root == NULL) {
        ESP_LOGE(TAG, "config payload is not JSON");
        return ESP_ERR_INVALID_ARG;
    }
    esp_err_t err = ESP_OK;
    if (!cJSON_IsObject(root)) {
        ESP_LOGE(TAG, "config payload is JSON but not an object");
        err = ESP_ERR_INVALID_ARG;
        goto done;
    }

    memset(out, 0, sizeof(*out));

    /* api_base and mqtt_uri are the two the board cannot invent. */
    if ((err = copy_string(root, "api_base", out->api_base, sizeof(out->api_base), true)) != ESP_OK)
        goto done;
    if ((err = copy_string(root, "mqtt_uri", out->mqtt_uri, sizeof(out->mqtt_uri), true)) != ESP_OK)
        goto done;
    if ((err = copy_string(root, "token", out->token, sizeof(out->token), false)) != ESP_OK)
        goto done;
    if ((err = copy_string(root, "ssid", out->ssid, sizeof(out->ssid), false)) != ESP_OK)
        goto done;
    if ((err = copy_string(root, "psk", out->psk, sizeof(out->psk), false)) != ESP_OK)
        goto done;
    if ((err = copy_string(root, "power", out->power, sizeof(out->power), false)) != ESP_OK)
        goto done;

    /* `ntp` is the one field where "absent" and "empty" differ: absent means "use the
     * default pool", empty means "the operator turned SNTP off deliberately" — which is
     * the lever that proves spec/device-protocol.md -> Clock, SNTP before TLS. */
    const cJSON *ntp = cJSON_GetObjectItemCaseSensitive(root, "ntp");
    if (ntp == NULL || cJSON_IsNull(ntp)) {
        strncpy(out->ntp, FF_CFG_DEFAULT_NTP, sizeof(out->ntp) - 1);
    } else if ((err = copy_string(root, "ntp", out->ntp, sizeof(out->ntp), false)) != ESP_OK) {
        goto done;
    }

    if (out->power[0] == '\0') {
        strncpy(out->power, FF_CFG_DEFAULT_POWER, sizeof(out->power) - 1);
    }

    if ((err = copy_int(root, "hb_s", &out->hb_s, FF_CFG_DEFAULT_HB_S)) != ESP_OK)
        goto done;
    if ((err = copy_int(root, "wake_s", &out->wake_s, 0)) != ESP_OK)
        goto done;
    if (out->hb_s <= 0) {
        ESP_LOGE(TAG, "config hb_s must be positive, not %d", out->hb_s);
        err = ESP_ERR_INVALID_ARG;
        goto done;
    }

    /* Default wifi: the fleet this ships for is Wi-Fi, and `ethernet` only exists because
     * the QEMU harness has an OpenCores NIC (ff_net_openeth.c). */
    char link[16];
    if ((err = copy_string(root, "link", link, sizeof(link), false)) != ESP_OK)
        goto done;
    if (link[0] == '\0' || strcmp(link, "wifi") == 0) {
        out->link = FF_LINK_WIFI;
    } else if (strcmp(link, "ethernet") == 0) {
        out->link = FF_LINK_ETHERNET;
    } else {
        ESP_LOGE(TAG, "config link '%s' is neither wifi nor ethernet", link);
        err = ESP_ERR_INVALID_ARG;
        goto done;
    }

done:
    cJSON_Delete(root);
    return err;
}

esp_err_t ff_cfg_load(ff_cfg_t *out)
{
    if (out == NULL) {
        return ESP_ERR_INVALID_ARG;
    }

    const esp_partition_t *part = esp_partition_find_first(
        ESP_PARTITION_TYPE_DATA, (esp_partition_subtype_t)FF_CFG_PARTITION_SUBTYPE,
        FF_CFG_PARTITION_LABEL);
    if (part == NULL) {
        ESP_LOGE(TAG, "no '%s' partition: this board carries a superseded partition layout "
                      "and cannot be configured (no OTA can add one)",
                 FF_CFG_PARTITION_LABEL);
        return ESP_ERR_NOT_FOUND;
    }
    if (part->size < FF_CFG_PARTITION_SIZE) {
        ESP_LOGE(TAG, "'%s' is %" PRIu32 " bytes, smaller than the %d-byte blob format",
                 FF_CFG_PARTITION_LABEL, part->size, FF_CFG_PARTITION_SIZE);
        return ESP_ERR_INVALID_SIZE;
    }

    ff_cfg_header_t header;
    esp_err_t err = esp_partition_read(part, 0, &header, sizeof(header));
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read '%s': %s", FF_CFG_PARTITION_LABEL, esp_err_to_name(err));
        return err;
    }

    if (memcmp(header.magic, FF_CFG_MAGIC, sizeof(header.magic)) != 0) {
        const uint8_t erased[4] = {0xFF, 0xFF, 0xFF, 0xFF};
        if (memcmp(header.magic, erased, sizeof(erased)) == 0) {
            ESP_LOGE(TAG, "%s is erased: this board has never been configured. Flash a config "
                          "blob at 0x%" PRIx32 " (agent/tools/ff_cfg.py, or the R0-fe-3 flasher).",
                     FF_CFG_PARTITION_LABEL, part->address);
            return ESP_ERR_NOT_SUPPORTED;
        }
        ESP_LOGE(TAG, "%s has no %s magic (got %02x %02x %02x %02x)", FF_CFG_PARTITION_LABEL,
                 FF_CFG_MAGIC, (uint8_t)header.magic[0], (uint8_t)header.magic[1],
                 (uint8_t)header.magic[2], (uint8_t)header.magic[3]);
        return ESP_ERR_INVALID_ARG;
    }
    if (header.version != FF_CFG_VERSION) {
        ESP_LOGE(TAG, "%s is version %u, this agent reads version %d — the flasher and the "
                      "firmware are from different releases",
                 FF_CFG_PARTITION_LABEL, header.version, FF_CFG_VERSION);
        return ESP_ERR_INVALID_VERSION;
    }
    if (header.reserved != 0) {
        ESP_LOGE(TAG, "%s reserved field is %u, not 0", FF_CFG_PARTITION_LABEL, header.reserved);
        return ESP_ERR_INVALID_ARG;
    }
    if (header.payload_len == 0 || header.payload_len > FF_CFG_MAX_PAYLOAD) {
        ESP_LOGE(TAG, "%s payload_len %" PRIu32 " is outside 1…%d", FF_CFG_PARTITION_LABEL,
                 header.payload_len, FF_CFG_MAX_PAYLOAD);
        return ESP_ERR_INVALID_SIZE;
    }

    /* One heap buffer, freed before the network comes up: the payload is ~200 bytes but
     * the format allows 4080, and 4 KB of stack in app_main is not free. */
    char *payload = malloc(header.payload_len + 1);
    if (payload == NULL) {
        return ESP_ERR_NO_MEM;
    }
    err = esp_partition_read(part, FF_CFG_HEADER_SIZE, payload, header.payload_len);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read the %s payload: %s", FF_CFG_PARTITION_LABEL,
                 esp_err_to_name(err));
        free(payload);
        return err;
    }
    payload[header.payload_len] = '\0';

    /* Byte-for-byte zlib.crc32 (the encoder's CRC): esp_rom_crc32_le already applies the
     * standard pre- and post-inversion internally, so a SEED OF ZERO — not ~0 — is what
     * matches `zlib.crc32(payload)`. Getting that backwards mismatches on every correctly
     * written blob and reads as "the flasher is broken". */
    uint32_t crc = esp_crc32_le(0, (const uint8_t *)payload, header.payload_len);
    if (crc != header.crc32) {
        ESP_LOGE(TAG, "%s: crc32 mismatch (header 0x%08" PRIx32 ", payload 0x%08" PRIx32 ") — "
                      "the blob was written partially or has been corrupted in flash",
                 FF_CFG_PARTITION_LABEL, header.crc32, crc);
        free(payload);
        return ESP_ERR_INVALID_CRC;
    }

    err = parse_payload(payload, header.payload_len, out);
    free(payload);
    if (err != ESP_OK) {
        return err;
    }

    ESP_LOGI(TAG, "ff_cfg v%d loaded (crc ok), %" PRIu32 " byte payload from 0x%" PRIx32,
             FF_CFG_VERSION, header.payload_len, part->address);
    return ESP_OK;
}

const char *ff_cfg_link_name(ff_link_t link)
{
    /* The exact strings spec/device-protocol.md prints for `link_type` in up/announce. */
    return link == FF_LINK_ETHERNET ? "ethernet" : "wifi";
}

void ff_cfg_log(const ff_cfg_t *cfg)
{
    ESP_LOGI(TAG, "  api_base  %s", cfg->api_base);
    ESP_LOGI(TAG, "  mqtt_uri  %s", cfg->mqtt_uri);
    ESP_LOGI(TAG, "  link      %s%s%s", ff_cfg_link_name(cfg->link),
             cfg->link == FF_LINK_WIFI ? ", ssid " : "",
             cfg->link == FF_LINK_WIFI ? cfg->ssid : "");
    ESP_LOGI(TAG, "  ntp       %s", cfg->ntp[0] ? cfg->ntp : "(disabled)");
    ESP_LOGI(TAG, "  hb_s      %d", cfg->hb_s);
    ESP_LOGI(TAG, "  power     %s (wake_s %d)", cfg->power, cfg->wake_s);
    /* Lengths only. A serial console is shoulder-surfable and often logged to a file. */
    ESP_LOGI(TAG, "  secrets   token %u chars, passphrase %u chars (never printed)",
             (unsigned)strlen(cfg->token), (unsigned)strlen(cfg->psk));
}
