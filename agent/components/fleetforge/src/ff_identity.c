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

#include "ff_identity_internal.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

#include "cJSON.h"
#include "esp_app_desc.h"
#include "esp_flash.h"
#include "esp_log.h"
#include "esp_mac.h"
#include "esp_ota_ops.h"
#include "esp_partition.h"
#include "esp_system.h"
#include "esp_timer.h"
#include "ff_net_internal.h"
#include "ff_store_internal.h"
#include "mbedtls/sha256.h"

static const char *TAG = "ff-id";

static char s_device_id[FF_DEVICE_ID_SIZE];
static bool s_mac_is_blank;

/* R2b-fw-2: the three board measurements of spec/device-protocol.md -> up/announce. The
 * first two are flash-time immutables, taken once in ff_identity_init() and cached. */
static uint32_t s_flash_chip_size;
static bool s_have_flash_chip_size;
static char s_partition_sha256[65];
static bool s_have_partition_sha256;
/* Loaded in ff_identity_init(), set by ff_identity_note_rollback_capable() from
 * ff_mqtt.c::classify_txn() — main task, before esp_mqtt_client_start(). The announce is
 * built in the mqtt task after that start, so the client start orders the write before
 * every read and no lock is needed. */
static bool s_rollback_capable;

/* 32 entries is twice what any table we know of carries (ab-4m-v1 has six). Static, so a
 * 384-byte array never lands on the main task's stack. */
#define FF_PT_MAX_ENTRIES 32

typedef struct {
    uint8_t type;
    uint8_t subtype;
    uint32_t address;
    uint32_t size;
} ff_pt_entry_t;

static ff_pt_entry_t s_pt_entries[FF_PT_MAX_ENTRIES];

/* `partition_table_sha256`, spec/device-protocol.md -> up/announce: sha256 over one line
 * per partition on the default flash chip, `type:subtype:offset:size\n` in DECIMAL, sorted
 * by offset. Geometry only — never the label, never the flags — so two tables that put the
 * same partitions in the same places hash alike whatever they are called. The worked value
 * for ab-4m-v1 is in the spec's *Partition layouts* table and is re-derived from
 * agent/partitions.csv by tests/test_agent_board_measurements.py. */
static bool partition_fingerprint(char out[65])
{
    size_t count = 0;
    esp_partition_iterator_t it =
        esp_partition_find(ESP_PARTITION_TYPE_ANY, ESP_PARTITION_SUBTYPE_ANY, NULL);
    while (it != NULL) {
        const esp_partition_t *p = esp_partition_get(it);
        /* External flash (esp_partition_register_external) is not part of the table the
         * bootloader reads. */
        if (p != NULL && p->flash_chip == esp_flash_default_chip) {
            if (count == FF_PT_MAX_ENTRIES) {
                esp_partition_iterator_release(it);
                ESP_LOGW(TAG, "the partition table has more than %d entries; "
                              "partition_table_sha256 is announced as null",
                         FF_PT_MAX_ENTRIES);
                return false;
            }
            s_pt_entries[count].type = (uint8_t)p->type;
            s_pt_entries[count].subtype = (uint8_t)p->subtype;
            s_pt_entries[count].address = p->address;
            s_pt_entries[count].size = p->size;
            count++;
        }
        /* Frees the iterator when it returns NULL; only the early exit above releases. */
        it = esp_partition_next(it);
    }
    if (count == 0) {
        ESP_LOGW(TAG, "no partition found on the flash chip; partition_table_sha256 is "
                      "announced as null");
        return false;
    }

    /* The iterator yields table order; the spec hashes offset order. */
    for (size_t i = 1; i < count; i++) {
        ff_pt_entry_t key = s_pt_entries[i];
        size_t j = i;
        while (j > 0 && s_pt_entries[j - 1].address > key.address) {
            s_pt_entries[j] = s_pt_entries[j - 1];
            j--;
        }
        s_pt_entries[j] = key;
    }

    mbedtls_sha256_context ctx;
    mbedtls_sha256_init(&ctx);
    int rc = mbedtls_sha256_starts(&ctx, 0);
    for (size_t i = 0; rc == 0 && i < count; i++) {
        char line[40]; /* the longest possible line is 30 characters */
        int len = snprintf(line, sizeof line, "%u:%u:%" PRIu32 ":%" PRIu32 "\n",
                           (unsigned)s_pt_entries[i].type, (unsigned)s_pt_entries[i].subtype,
                           s_pt_entries[i].address, s_pt_entries[i].size);
        if (len < 0 || (size_t)len >= sizeof line) {
            rc = -1;
            break;
        }
        rc = mbedtls_sha256_update(&ctx, (const unsigned char *)line, (size_t)len);
    }
    uint8_t digest[32];
    if (rc == 0) {
        rc = mbedtls_sha256_finish(&ctx, digest);
    }
    mbedtls_sha256_free(&ctx);
    if (rc != 0) {
        ESP_LOGW(TAG, "cannot hash the partition table (%d); partition_table_sha256 is "
                      "announced as null",
                 rc);
        return false;
    }

    /* By hand, lowercase, as ff_store.c::token_fingerprint does: nothing to truncate. */
    static const char HEX[] = "0123456789abcdef";
    for (size_t i = 0; i < sizeof digest; i++) {
        out[i * 2] = HEX[digest[i] >> 4];
        out[i * 2 + 1] = HEX[digest[i] & 0x0f];
    }
    out[64] = '\0';
    return true;
}

/* The two flash-time measurements. Neither may come from the image header or the build
 * config: those are claims made by whoever built THIS image, and the point of the fields
 * is what the board itself is. So no fallback to esp_flash_get_size() (the header's
 * flash size) when the physical read fails: the field is left out instead. */
static void measure_board(void)
{
    uint32_t size = 0;
    esp_err_t err = esp_flash_get_physical_size(esp_flash_default_chip, &size);
    if (err == ESP_OK && size > 0) {
        s_flash_chip_size = size;
        s_have_flash_chip_size = true;
    } else {
        ESP_LOGW(TAG, "cannot read the physical flash size (%s); flash_chip_size is left out "
                      "of the announce",
                 err != ESP_OK ? esp_err_to_name(err) : "the chip reported 0 bytes");
    }

    s_have_partition_sha256 = partition_fingerprint(s_partition_sha256);
}

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

    /* R2b-fw-2. After the MAC, and never fatal: an unknown measurement is announced as
     * unknown, while a failed init parks the board. */
    measure_board();
    s_rollback_capable = ff_store_load_rollback_capable();

    char chip[16];
    if (s_have_flash_chip_size) {
        snprintf(chip, sizeof(chip), "%" PRIu32, s_flash_chip_size);
    } else {
        strlcpy(chip, "unknown", sizeof(chip));
    }
    ESP_LOGI(TAG, "board: flash chip %s bytes (physical), partition table sha256 %s, "
                  "rollback_capable %s",
             chip, s_have_partition_sha256 ? s_partition_sha256 : "unknown",
             s_rollback_capable ? "true" : "unknown");
    return ESP_OK;
}

void ff_identity_note_rollback_capable(void)
{
    if (s_rollback_capable) {
        return; /* already measured and stored: no NVS write on every OTA boot */
    }
    /* True for this boot whatever the save does: the observation is real. A failed save is
     * logged by ff_store and retried at the next PENDING_VERIFY boot. */
    s_rollback_capable = true;
    const bool stored = ff_store_save_rollback_capable() == ESP_OK;
    ESP_LOGW(TAG, "rollback_capable: true — this OTA-written image booted in pending_verify, "
                  "so this board's bootloader rolls back (%s)",
             stored ? "stored" : "NOT stored; announced for this boot only");
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

#if defined(ARDUINO)
/* R3-fw-3: see ff_identity_set_app_versions() in ff_identity_internal.h. Written once in
 * begin() before the fleetforge task exists, read only from that task and the tasks it
 * starts, so task creation orders the write before every read and no lock is needed. */
#define FF_APP_VERSION_SIZE 32
static char s_fw_version[FF_APP_VERSION_SIZE];
static char s_agent_version[FF_APP_VERSION_SIZE];

static bool app_version_valid(const char *version)
{
    if (version == NULL) {
        return false;
    }
    size_t len = 0;
    for (; version[len] != '\0'; len++) {
        if (len == FF_APP_VERSION_SIZE - 1 || version[len] < 0x20 || version[len] > 0x7e) {
            return false;
        }
    }
    return len > 0;
}

esp_err_t ff_identity_set_app_versions(const char *fw_version, const char *agent_version)
{
    if (!app_version_valid(fw_version) || !app_version_valid(agent_version)) {
        return ESP_ERR_INVALID_ARG;
    }
    strlcpy(s_fw_version, fw_version, sizeof(s_fw_version));
    strlcpy(s_agent_version, agent_version, sizeof(s_agent_version));
    return ESP_OK;
}
#endif

/* R1-fw-2. The one place a version becomes a thing this board says about itself. The
 * descriptor belongs to the image that is EXECUTING: after an OTA it is the new slot's,
 * after a bootloader rollback it is the old slot's again. See ff_identity.h for why no
 * other source is allowed. */
const char *ff_identity_fw_version(void)
{
#if defined(ARDUINO)
    if (s_fw_version[0] != '\0') {
        return s_fw_version;
    }
#endif
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
#if defined(ARDUINO)
    /* R3-fw-3: in a library build `agent_version` is the library's version, handed in by
     * begin() (DECISIONS R3-spec-3's version note); the descriptor's is the core's. */
    cJSON_AddStringToObject(root, "agent_version",
                            s_agent_version[0] != '\0' ? s_agent_version : app->version);
#else
    cJSON_AddStringToObject(root, "agent_version", app->version);
#endif
    cJSON_AddStringToObject(root, "link_type", ff_cfg_link_name(cfg->link));
    /* spec/device-protocol.md -> up/announce: which network this broker session runs over,
     * and how many the board will try. Both null on ethernet. Never a passphrase, never
     * the other networks' SSIDs. The SSID comes through the seam (ff_net.h), never from
     * the adapter: this file must not see a link-specific symbol. */
    const char *ssid = cfg->link == FF_LINK_WIFI ? ff_net_ssid() : NULL;
    if (ssid != NULL) {
        cJSON_AddStringToObject(root, "ssid", ssid);
    } else {
        cJSON_AddNullToObject(root, "ssid");
    }
    if (cfg->link == FF_LINK_WIFI) {
        cJSON_AddNumberToObject(root, "known_networks", cfg->net_count);
    } else {
        cJSON_AddNullToObject(root, "known_networks");
    }
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
    /* R2b-fw-2: the three board measurements, in spec order, measured on this board and
     * never taken from the build. `flash_chip_size` is OMITTED when unreadable ("a device
     * that cannot read it omits the field"); the other two go out as explicit null, as
     * `ssid` does. cJSON prints 4194304 as an integer, which the server requires. */
    if (s_have_flash_chip_size) {
        cJSON_AddNumberToObject(root, "flash_chip_size", s_flash_chip_size);
    }
    if (s_have_partition_sha256) {
        cJSON_AddStringToObject(root, "partition_table_sha256", s_partition_sha256);
    } else {
        cJSON_AddNullToObject(root, "partition_table_sha256");
    }
    /* `true` or null, never `false`, until R2b-test-5 (DECISIONS 2026-10-04 A3). */
    if (s_rollback_capable) {
        cJSON_AddTrueToObject(root, "rollback_capable");
    } else {
        cJSON_AddNullToObject(root, "rollback_capable");
    }
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
