/*
 * ff_identity — the eFuse MAC, and the payloads built from it.
 *
 * Three things worth knowing before editing this file:
 *
 * 1. **The id is never normalised, only formatted once.** `src/fleetforge/identity.py`'s
 *    rule is "never normalise, always reject": the server refuses anything that is not 12
 *    lowercase hex digits rather than lowercasing it, because `mqtt_username == device_id`
 *    and the ACL binds to the exact string. The agent's job is therefore to emit the
 *    canonical form at exactly one place — the `%02x` below — and nowhere else.
 * 2. **There is no device_id override in ff_cfg, on purpose.** `device_id` IS the eFuse MAC
 *    by contract. An override would be a footgun that eventually reaches real hardware and
 *    lets two boards claim one identity; to give an emulated board a distinct id, patch the
 *    eFuse image (agent/tools/qemu_image.py --efuse), not the firmware.
 * 3. **The enroll body is the announce payload with a token added.** Not a copy of it —
 *    the same cJSON object, extended. spec/device-protocol.md step 2 requires the flat
 *    shape `{token, …announce}`; api/schemas.py::EnrollRequest is the server reading the
 *    same sentence. A nested `{"token":…, "identity":{…}}` would be a fleet recall.
 */

#include "ff_identity.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ff_net.h"

static const char *TAG = "ff-id";

static char s_device_id[FF_DEVICE_ID_SIZE];
static bool s_mac_is_blank;

esp_err_t ff_identity_init(void)
{
    uint8_t mac[6] = {0};
    esp_err_t err = esp_read_mac(mac, ESP_MAC_EFUSE_FACTORY);
    if (err != ESP_OK) {
        ESP_LOGE(TAG, "cannot read the eFuse MAC: %s", esp_err_to_name(err));
        return err;
    }

    snprintf(s_device_id, sizeof(s_device_id), "%02x%02x%02x%02x%02x%02x", mac[0], mac[1], mac[2],
             mac[3], mac[4], mac[5]);

    /* QEMU's default eFuse image is almost all zero bytes, so an emulated board reads
     * 00:00:00:00:00:00 — a legal DEVICE_ID_RE value that enrolls perfectly well. Say so
     * out loud: on real silicon this can only mean blank eFuses, i.e. an unprogrammed or
     * counterfeit part, and every such board would collide on one identity. */
    s_mac_is_blank = (mac[0] | mac[1] | mac[2] | mac[3] | mac[4] | mac[5]) == 0;
    if (s_mac_is_blank) {
        ESP_LOGW(TAG, "eFuse MAC is all zeros — this is an emulator, never a board. Every "
                      "such device claims device_id %s and they would collide in the fleet.",
                 s_device_id);
    }

    ESP_LOGI(TAG, "device_id %s", s_device_id);
    return ESP_OK;
}

const char *ff_device_id(void)
{
    return s_device_id;
}

bool ff_identity_mac_is_blank(void)
{
    return s_mac_is_blank;
}

/* The running slot's real size, which is what an OTA actually has to fit into. Reported
 * instead of the compiled-in FF_OTA_SLOT_SIZE so the number the server performs its
 * capability check against comes from the flashed partition table, not from a constant
 * that could disagree with it. They are asserted equal here rather than assumed. */
static uint32_t running_slot_size(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    if (running == NULL) {
        return FF_OTA_SLOT_SIZE;
    }
    if (running->size != FF_OTA_SLOT_SIZE) {
        ESP_LOGW(TAG, "the running slot is %" PRIu32 " bytes but %s promises %d — this board "
                      "carries a partition layout the server does not know",
                 running->size, FF_PARTITION_LAYOUT, FF_OTA_SLOT_SIZE);
    }
    return running->size;
}

/* True unless the bootloader is still waiting for this image to confirm itself. Reported
 * in up/hb as `boot_ok`; at R0 it is always true on a serially flashed board (ff_mqtt.c
 * explains why), and it becomes load-bearing the moment R2 ships OTA. */
static bool boot_ok(void)
{
    const esp_partition_t *running = esp_ota_get_running_partition();
    esp_ota_img_states_t state = ESP_OTA_IMG_UNDEFINED;
    if (running == NULL || esp_ota_get_state_partition(running, &state) != ESP_OK) {
        return true;
    }
    return state != ESP_OTA_IMG_PENDING_VERIFY;
}

/* R1-fw-2. The one place a version becomes a thing this board says about itself. The
 * descriptor belongs to the image that is EXECUTING: after an OTA it is the new slot's,
 * after a bootloader rollback it is the old slot's again. See ff_identity.h for why no
 * other source is allowed. */
const char *ff_identity_fw_version(void)
{
    return esp_app_get_description()->version;
}

/* One announce object, keys in the order spec/device-protocol.md prints them. There is no
 * `ts` field, here or anywhere: "the server timestamps by its own receipt time". */
static cJSON *announce_object(const ff_cfg_t *cfg)
{
    const esp_app_desc_t *app = esp_app_get_description();
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }

    cJSON_AddNumberToObject(root, "proto", FF_PROTO_VERSION);
    cJSON_AddStringToObject(root, "device_id", s_device_id);
    /* CONFIG_IDF_TARGET is the same spelling the bundle manifest and the flasher use. */
    cJSON_AddStringToObject(root, "platform_type", CONFIG_IDF_TARGET);
    /* At R0 the agent IS the application, so the two versions are the same string. They
     * are separate fields because from R1 the agent is a component inside a user firmware
     * and only `fw_version` moves. */
    cJSON_AddStringToObject(root, "fw_version", ff_identity_fw_version());
    cJSON_AddStringToObject(root, "agent_version", app->version);
    cJSON_AddStringToObject(root, "link_type", ff_cfg_link_name(cfg->link));
    cJSON_AddStringToObject(root, "power_class", cfg->power);
    if (cfg->wake_s > 0) {
        cJSON_AddNumberToObject(root, "expected_wake_interval_s", cfg->wake_s);
    } else {
        cJSON_AddNullToObject(root, "expected_wake_interval_s");
    }
    /* Reserved for V3's gateway hierarchy; a v1 board is always its own root. */
    cJSON_AddNullToObject(root, "parent_device_id");
    cJSON_AddStringToObject(root, "partition_layout", FF_PARTITION_LAYOUT);
    cJSON_AddNumberToObject(root, "ota_slot_size", running_slot_size());
    /* `["ota"]` since R1-fw-1: this agent downloads through the signed URL, writes the
     * inactive slot, verifies the digest and reboots into it (ff_ota.c). The list is a
     * claim about what the board can be asked to DO, so it stays as short as the truth —
     * it was empty at R0 for exactly the same reason.
     *
     * It is also load-bearing server-side: `POST /v1/devices/{id}/deploy` answers 409 to a
     * device whose `capabilities` does not contain `ota`, so an agent that performs OTA
     * but does not announce it is a fleet that cannot be deployed to, with no error
     * anywhere near the cause. */
    cJSON *capabilities = cJSON_CreateArray();
    cJSON_AddItemToArray(capabilities, cJSON_CreateString("ota"));
    cJSON_AddItemToObject(root, "capabilities", capabilities);
    return root;
}

char *ff_identity_announce_json(const ff_cfg_t *cfg)
{
    cJSON *root = announce_object(cfg);
    if (root == NULL) {
        return NULL;
    }
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json;
}

char *ff_identity_enroll_body(const ff_cfg_t *cfg)
{
    cJSON *root = announce_object(cfg);
    if (root == NULL) {
        return NULL;
    }
    /* Prepended, so the body reads `{"token":…, "proto":1, …}` exactly as the spec prints
     * it. Order carries no meaning to a JSON parser; it carries a lot to a human diffing
     * a capture against the spec. */
    cJSON_AddItemToObject(root, "token", cJSON_CreateString(cfg->token));
    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json;
}

char *ff_identity_heartbeat_json(uint32_t uptime_s)
{
    cJSON *root = cJSON_CreateObject();
    if (root == NULL) {
        return NULL;
    }
    cJSON_AddStringToObject(root, "fw_version", ff_identity_fw_version());
    cJSON_AddNumberToObject(root, "uptime_s", uptime_s);

    /* An Ethernet (or, later, a cellular) board has no RSSI. It reports **null** rather
     * than a plausible-looking number: a sentinel like 0 or -127 in a health field is a
     * lie the dashboard cannot distinguish from a real reading. Proposed as an additive
     * clarification to spec/device-protocol.md -> up/hb. */
    int rssi = 0;
    if (ff_net_rssi(&rssi)) {
        cJSON_AddNumberToObject(root, "rssi", rssi);
    } else {
        cJSON_AddNullToObject(root, "rssi");
    }

    cJSON_AddNumberToObject(root, "free_heap", esp_get_free_heap_size());
    cJSON_AddBoolToObject(root, "boot_ok", boot_ok());

    char *json = cJSON_PrintUnformatted(root);
    cJSON_Delete(root);
    return json;
}
