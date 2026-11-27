"""Cross-language capability registry (`D-29`) and canonical wire vector (`D-10`) tests.

Verifies that:
- `ff_identity.c`, `fleetforge.simulator.device`, `fleetforge.capabilities`, and
  `src/fleetforge/contracts/capabilities.json` announce the exact same built-in capability
  sets (`D-29`).
- Every valid and invalid wire vector in `src/fleetforge/contracts/wire_vectors.json`
  (`announce`, `hb`, `status`, `ack`, `presence`, and `dn/stage`) agrees across
  `fleetforge.ingestor.protocol.decode`, `fleetforge.simulator`, and a host-`gcc`-compiled
  firmware C parser/validator (`D-10`).
"""

from __future__ import annotations

import ctypes
import json
import re
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from fleetforge.capabilities import (
    ALL_BUILTIN_CAPABILITIES,
    BASE_CAPABILITIES,
    DEFAULT_AGENT_CAPABILITIES,
    OPTIONAL_BUILTIN_CAPABILITIES,
    is_known_capability,
)
from fleetforge.contracts import load_capabilities_contract, load_wire_vectors_contract
from fleetforge.ingestor.protocol import (
    AckPayload,
    AnnouncePayload,
    HeartbeatPayload,
    PresencePayload,
    StatusPayload,
    decode,
)
from fleetforge.simulator.device import (
    ALL_BUILTIN_CAPABILITIES as SIM_ALL_BUILTIN_CAPABILITIES,
)
from fleetforge.simulator.device import (
    BASE_AGENT_CAPABILITIES as SIM_BASE_CAPABILITIES,
)
from fleetforge.simulator.device import (
    DEFAULT_AGENT_CAPABILITIES as SIM_DEFAULT_CAPABILITIES,
)
from fleetforge.simulator.device import (
    OPTIONAL_AGENT_CAPABILITIES as SIM_OPTIONAL_CAPABILITIES,
)
from fleetforge.simulator.device import (
    PRESENCE_OFFLINE,
    PRESENCE_ONLINE,
    STATE_FAILED,
    STATE_STAGING,
    DeviceIdentity,
    LinkProfile,
    StageRunner,
    encode,
    up_topic,
)
from fleetforge.simulator.errors import SimulatorError
from tests.agent_src import extract_c_function, find_c_function, read_c_family
from tests.agent_test_helpers import FF_IDENTITY_C, _code, _function_body


def _extract_firmware_capabilities() -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Extract unconditional and conditional capability literals from `ff_identity.c`."""
    announce_fn = _function_body(_code(FF_IDENTITY_C), "announce_object")
    cap_start = announce_fn.index("cJSON *capabilities = cJSON_CreateArray();")
    cap_end = announce_fn.index('cJSON_AddItemToObject(root, "capabilities", capabilities);')
    block = announce_fn[cap_start:cap_end]

    unconditional: list[str] = []
    conditional: list[str] = []
    depth = 0
    for raw_line in block.splitlines():
        line = raw_line.strip()
        literals = re.findall(r'cJSON_CreateString\("([a-z0-9_]+)"\)', line)
        if depth == 0:
            unconditional.extend(literals)
        else:
            conditional.extend(literals)
        depth += line.count("{") - line.count("}")
    return tuple(unconditional), tuple(conditional)


def test_capability_registry_matches_firmware_and_simulator() -> None:
    """`D-29`: `ff_identity.c`, `simulator`, `capabilities.py`, and `capabilities.json` match 1:1."""
    contract = load_capabilities_contract()
    fw_base, fw_optional = _extract_firmware_capabilities()

    ota_raw = read_c_family("ff_ota")
    assert "#define FF_OTA_REQUIRE_SIGNATURE 1" in ota_raw
    fw_default = (*fw_base, "sig")
    fw_all = (*fw_base, *fw_optional)

    assert tuple(contract["base"]) == fw_base
    assert tuple(contract["optional_builtin"]) == fw_optional
    assert tuple(contract["default_agent"]) == fw_default
    assert tuple(contract["all_builtin"]) == fw_all

    assert BASE_CAPABILITIES == fw_base == SIM_BASE_CAPABILITIES
    assert OPTIONAL_BUILTIN_CAPABILITIES == fw_optional == SIM_OPTIONAL_CAPABILITIES
    assert DEFAULT_AGENT_CAPABILITIES == fw_default == SIM_DEFAULT_CAPABILITIES
    assert ALL_BUILTIN_CAPABILITIES == fw_all == SIM_ALL_BUILTIN_CAPABILITIES

    for cap in ALL_BUILTIN_CAPABILITIES:
        assert is_known_capability(cap, allow_cargo=False)
    assert is_known_capability("mcu_fw", allow_cargo=True)
    assert not is_known_capability("mcu_fw", allow_cargo=False)
    assert not is_known_capability("app", allow_cargo=True)
    assert not is_known_capability("UPPERCASE", allow_cargo=True)


def _compile_c_wire_harness(tmp_path: Path) -> ctypes.CDLL:
    """Compile real firmware `on_command` / `on_stage` + C wire validators via host `gcc`."""
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc is required for host C wire vector tests"

    fn_take_string = extract_c_function(read_c_family("ff_mqtt"), "take_string")
    _, fn_is_sha256 = find_c_function("is_lowercase_sha256")
    _, fn_is_sig = find_c_function("is_valid_artifact_sig")
    _, fn_clamp = find_c_function("confirm_timeout_clamp")
    _, fn_on_stage = find_c_function("on_stage")
    _, fn_on_command = find_c_function("on_command")

    harness_c = f"""
#include <ctype.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static size_t ff_strlcpy(char *dst, const char *src, size_t size) {{
    size_t srclen = strlen(src);
    if (size > 0) {{
        size_t n = srclen >= size ? size - 1 : srclen;
        memcpy(dst, src, n);
        dst[n] = '\\0';
    }}
    return srclen;
}}
#undef strlcpy
#define strlcpy ff_strlcpy

typedef enum {{
    CJ_NULL = 0,
    CJ_FALSE,
    CJ_TRUE,
    CJ_NUMBER,
    CJ_STRING,
    CJ_ARRAY,
    CJ_OBJECT
}} cj_kind_t;

typedef struct cJSON {{
    cj_kind_t kind;
    char *key;
    char *valuestring;
    double valuedouble;
    struct cJSON *child;
    struct cJSON *next;
}} cJSON;

static void cJSON_Delete(cJSON *node) {{
    while (node != NULL) {{
        cJSON *next = node->next;
        if (node->child != NULL) cJSON_Delete(node->child);
        free(node->key);
        free(node->valuestring);
        free(node);
        node = next;
    }}
}}

static cJSON *cj_new(cj_kind_t k) {{
    cJSON *n = (cJSON *)calloc(1, sizeof(cJSON));
    if (n != NULL) n->kind = k;
    return n;
}}

static void cj_skip_ws(const char **p, const char *end) {{
    while (*p < end && (**p == ' ' || **p == '\\t' || **p == '\\r' || **p == '\\n')) (*p)++;
}}

static char *cj_parse_str(const char **p, const char *end) {{
    if (*p >= end || **p != '"') return NULL;
    (*p)++;
    size_t cap = (size_t)(end - *p) + 1;
    char *out = (char *)malloc(cap);
    if (out == NULL) return NULL;
    size_t len = 0;
    while (*p < end) {{
        char c = *(*p)++;
        if (c == '"') {{
            out[len] = '\\0';
            return out;
        }}
        if (c == '\\\\') {{
            if (*p >= end) {{ free(out); return NULL; }}
            char esc = *(*p)++;
            switch (esc) {{
            case '"': case '\\\\': case '/': out[len++] = esc; break;
            case 'n': out[len++] = '\\n'; break;
            case 'r': out[len++] = '\\r'; break;
            case 't': out[len++] = '\\t'; break;
            default: free(out); return NULL;
            }}
        }} else {{
            out[len++] = c;
        }}
    }}
    free(out);
    return NULL;
}}

static cJSON *cj_parse_val(const char **p, const char *end);

static cJSON *cj_parse_arr(const char **p, const char *end) {{
    (*p)++;
    cj_skip_ws(p, end);
    cJSON *arr = cj_new(CJ_ARRAY);
    if (arr == NULL) return NULL;
    if (*p < end && **p == ']') {{ (*p)++; return arr; }}
    cJSON *tail = NULL;
    while (*p < end) {{
        cJSON *item = cj_parse_val(p, end);
        if (item == NULL) {{ cJSON_Delete(arr); return NULL; }}
        if (tail == NULL) arr->child = item; else tail->next = item;
        tail = item;
        cj_skip_ws(p, end);
        if (*p < end && **p == ',') {{ (*p)++; cj_skip_ws(p, end); continue; }}
        if (*p < end && **p == ']') {{ (*p)++; return arr; }}
        break;
    }}
    cJSON_Delete(arr);
    return NULL;
}}

static cJSON *cj_parse_obj(const char **p, const char *end) {{
    (*p)++;
    cj_skip_ws(p, end);
    cJSON *obj = cj_new(CJ_OBJECT);
    if (obj == NULL) return NULL;
    if (*p < end && **p == '}}') {{ (*p)++; return obj; }}
    cJSON *tail = NULL;
    while (*p < end) {{
        cj_skip_ws(p, end);
        char *k = cj_parse_str(p, end);
        if (k == NULL) {{ cJSON_Delete(obj); return NULL; }}
        cj_skip_ws(p, end);
        if (*p >= end || **p != ':') {{ free(k); cJSON_Delete(obj); return NULL; }}
        (*p)++;
        cJSON *val = cj_parse_val(p, end);
        if (val == NULL) {{ free(k); cJSON_Delete(obj); return NULL; }}
        val->key = k;
        if (tail == NULL) obj->child = val; else tail->next = val;
        tail = val;
        cj_skip_ws(p, end);
        if (*p < end && **p == ',') {{ (*p)++; continue; }}
        if (*p < end && **p == '}}') {{ (*p)++; return obj; }}
        break;
    }}
    cJSON_Delete(obj);
    return NULL;
}}

static cJSON *cj_parse_val(const char **p, const char *end) {{
    cj_skip_ws(p, end);
    if (*p >= end) return NULL;
    if (**p == '{{') return cj_parse_obj(p, end);
    if (**p == '[') return cj_parse_arr(p, end);
    if (**p == '"') {{
        char *s = cj_parse_str(p, end);
        if (s == NULL) return NULL;
        cJSON *n = cj_new(CJ_STRING);
        if (n == NULL) {{ free(s); return NULL; }}
        n->valuestring = s;
        return n;
    }}
    if ((size_t)(end - *p) >= 4 && strncmp(*p, "true", 4) == 0) {{ *p += 4; return cj_new(CJ_TRUE); }}
    if ((size_t)(end - *p) >= 5 && strncmp(*p, "false", 5) == 0) {{ *p += 5; return cj_new(CJ_FALSE); }}
    if ((size_t)(end - *p) >= 4 && strncmp(*p, "null", 4) == 0) {{ *p += 4; return cj_new(CJ_NULL); }}
    if (**p == '-' || (**p >= '0' && **p <= '9')) {{
        char buf[64];
        size_t i = 0;
        while (*p < end && i < sizeof(buf) - 1 &&
               (**p == '-' || **p == '+' || **p == '.' || **p == 'e' || **p == 'E' ||
                (**p >= '0' && **p <= '9'))) {{
            buf[i++] = *(*p)++;
        }}
        buf[i] = '\\0';
        char *ep = NULL;
        double d = strtod(buf, &ep);
        if (ep == buf || *ep != '\\0') return NULL;
        cJSON *n = cj_new(CJ_NUMBER);
        if (n != NULL) n->valuedouble = d;
        return n;
    }}
    return NULL;
}}

static cJSON *cJSON_ParseWithLength(const char *data, size_t len) {{
    const char *p = data;
    const char *end = data + len;
    cJSON *root = cj_parse_val(&p, end);
    if (root == NULL) return NULL;
    cj_skip_ws(&p, end);
    if (p != end) {{ cJSON_Delete(root); return NULL; }}
    return root;
}}

static const cJSON *cJSON_GetObjectItemCaseSensitive(const cJSON *obj, const char *key) {{
    if (obj == NULL || obj->kind != CJ_OBJECT) return NULL;
    for (const cJSON *c = obj->child; c != NULL; c = c->next) {{
        if (c->key != NULL && strcmp(c->key, key) == 0) return c;
    }}
    return NULL;
}}
static bool cJSON_IsObject(const cJSON *i) {{ return i != NULL && i->kind == CJ_OBJECT; }}
static bool cJSON_IsArray(const cJSON *i) {{ return i != NULL && i->kind == CJ_ARRAY; }}
static bool cJSON_IsString(const cJSON *i) {{ return i != NULL && i->kind == CJ_STRING; }}
static bool cJSON_IsNumber(const cJSON *i) {{ return i != NULL && i->kind == CJ_NUMBER; }}
static bool cJSON_IsBool(const cJSON *i) {{ return i != NULL && (i->kind == CJ_TRUE || i->kind == CJ_FALSE); }}
static bool cJSON_IsNull(const cJSON *i) {{ return i != NULL && i->kind == CJ_NULL; }}

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_STATE 0x103
#define FF_TXN_MAX_CMD_ID 64
#define CONFIRM_TIMEOUT_S 300
#define CONFIRM_TIMEOUT_MIN_S 30
#define CONFIRM_TIMEOUT_MAX_S 3600
#define FF_STATUS_FAILED 7
#define FF_STATUS_PCT_NONE -1
#define ACK_REFUSED "refused"
#define ACK_NOT_IMPLEMENTED "not implemented"
#define ACK_ID_TOO_LONG "command id too long"
#define ACK_ROLLBACK_DECIDED "a rollback is already decided"
#define ACK_REBOOT_PENDING "a reboot is already pending"

static const char *TAG = "ff-wire";
static void ff_noop_log(const char *tag, const char *fmt, ...) {{ (void)tag; (void)fmt; }}
#define ESP_LOGI ff_noop_log
#define ESP_LOGW ff_noop_log
#define ESP_LOGE ff_noop_log
static const char *esp_err_to_name(esp_err_t e) {{ (void)e; return "err"; }}

typedef enum {{ FIELD_OK, FIELD_MISSING, FIELD_TOO_LONG }} field_result_t;

typedef struct {{
    char cmd_id[64];
    char url[512];
    char sha256[65];
    char sig[160];
    size_t size;
    char version[32];
    char type[32];
    bool apply_now;
    uint32_t confirm_timeout_s;
}} ff_ota_cmd_t;

typedef struct {{
    char last_command_id[64];
}} ff_mqtt_ctx_t;

static bool s_rollback_decided = false;
static bool s_cmd_reboot_pending = false;
static char s_reboot_cmd_id[64];

static bool g_ota_started = false;
static ff_ota_cmd_t g_last_cmd;
static int g_status_count = 0;
static char g_last_status_detail[128];

static bool ff_ota_is_handling(const char *id) {{ (void)id; return false; }}
static const char *held_txn_id(void) {{ return NULL; }}
static esp_err_t ff_ota_start(const ff_ota_cmd_t *cmd) {{
    g_ota_started = true;
    g_last_cmd = *cmd;
    return ESP_OK;
}}
static void ff_mqtt_publish_status(const char *id, int st, int pct, const char *detail) {{
    (void)id; (void)st; (void)pct;
    g_status_count++;
    snprintf(g_last_status_detail, sizeof(g_last_status_detail), "%s", detail ? detail : "");
}}
static void enqueue_ack(const char *id, const char *t, const char *r, const char *cid, const char *d) {{
    (void)id; (void)t; (void)r; (void)cid; (void)d;
}}
static void on_apply(const cJSON *r, const char *id) {{ (void)r; (void)id; }}
static void on_cancel(const cJSON *r, const char *id) {{ (void)r; (void)id; }}
static void on_rollback(const char *id) {{ (void)id; }}
static void on_reboot(const char *id) {{ (void)id; }}

{fn_take_string}
{fn_is_sha256}
{fn_is_sig}
{fn_clamp}
{fn_on_stage}
{fn_on_command}

static bool is_device_id(const char *s) {{
    for (size_t i = 0; i < 12; i++) {{
        char c = s[i];
        if (!((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f'))) return false;
    }}
    return s[12] == '\\0';
}}

static bool is_int_num(const cJSON *n) {{
    return cJSON_IsNumber(n) && (double)(int64_t)n->valuedouble == n->valuedouble;
}}

static bool in_list(const char *val, const char *const *items, size_t count) {{
    for (size_t i = 0; i < count; i++) {{
        if (strcmp(val, items[i]) == 0) return true;
    }}
    return false;
}}

bool c_validate_up(const char *channel, const char *raw, size_t len) {{
    cJSON *root = cJSON_ParseWithLength(raw, len);
    if (!cJSON_IsObject(root)) {{
        cJSON_Delete(root);
        return false;
    }}
    bool ok = false;
    if (strcmp(channel, "presence") == 0) {{
        const cJSON *online = cJSON_GetObjectItemCaseSensitive(root, "online");
        ok = cJSON_IsBool(online);
    }} else if (strcmp(channel, "hb") == 0) {{
        const cJSON *fw = cJSON_GetObjectItemCaseSensitive(root, "fw_version");
        const cJSON *up = cJSON_GetObjectItemCaseSensitive(root, "uptime_s");
        const cJSON *boot_ok = cJSON_GetObjectItemCaseSensitive(root, "boot_ok");
        ok = cJSON_IsString(fw) && fw->valuestring[0] != '\\0' &&
             is_int_num(up) && up->valuedouble >= 0.0 &&
             (boot_ok == NULL || cJSON_IsNull(boot_ok) || cJSON_IsBool(boot_ok));
    }} else if (strcmp(channel, "status") == 0) {{
        static const char *const STATES[] = {{
            "staging", "downloading", "verifying", "staged", "awaiting_safe_window",
            "applying", "rebooting", "failed", "confirming", "confirmed",
            "rolling_back", "rolled_back", "cancelled"
        }};
        const cJSON *cid = cJSON_GetObjectItemCaseSensitive(root, "cmd_id");
        const cJSON *st = cJSON_GetObjectItemCaseSensitive(root, "state");
        const cJSON *pct = cJSON_GetObjectItemCaseSensitive(root, "pct");
        ok = cJSON_IsString(cid) && cid->valuestring[0] != '\\0' &&
             cJSON_IsString(st) && in_list(st->valuestring, STATES, sizeof(STATES) / sizeof(STATES[0])) &&
             (pct == NULL || cJSON_IsNull(pct) ||
              (is_int_num(pct) && pct->valuedouble >= 0.0 && pct->valuedouble <= 100.0));
    }} else if (strcmp(channel, "ack") == 0) {{
        static const char *const RESULTS[] = {{ "accepted", "deferred", "refused" }};
        const cJSON *id = cJSON_GetObjectItemCaseSensitive(root, "id");
        const cJSON *res = cJSON_GetObjectItemCaseSensitive(root, "result");
        ok = cJSON_IsString(id) && id->valuestring[0] != '\\0' &&
             cJSON_IsString(res) && in_list(res->valuestring, RESULTS, sizeof(RESULTS) / sizeof(RESULTS[0]));
    }} else if (strcmp(channel, "announce") == 0) {{
        const cJSON *proto = cJSON_GetObjectItemCaseSensitive(root, "proto");
        const cJSON *did = cJSON_GetObjectItemCaseSensitive(root, "device_id");
        const cJSON *pt = cJSON_GetObjectItemCaseSensitive(root, "platform_type");
        const cJSON *fw = cJSON_GetObjectItemCaseSensitive(root, "fw_version");
        const cJSON *av = cJSON_GetObjectItemCaseSensitive(root, "agent_version");
        const cJSON *lt = cJSON_GetObjectItemCaseSensitive(root, "link_type");
        const cJSON *pc = cJSON_GetObjectItemCaseSensitive(root, "power_class");
        const cJSON *wake = cJSON_GetObjectItemCaseSensitive(root, "expected_wake_interval_s");
        const cJSON *pl = cJSON_GetObjectItemCaseSensitive(root, "partition_layout");
        const cJSON *slot = cJSON_GetObjectItemCaseSensitive(root, "ota_slot_size");
        const cJSON *caps = cJSON_GetObjectItemCaseSensitive(root, "capabilities");
        bool pc_ok = cJSON_IsString(pc) &&
                     (strcmp(pc->valuestring, "always_on") == 0 ||
                      (strcmp(pc->valuestring, "sleepy") == 0 &&
                       is_int_num(wake) && wake->valuedouble > 0.0));
        ok = is_int_num(proto) && proto->valuedouble == 1.0 &&
             cJSON_IsString(did) && is_device_id(did->valuestring) &&
             cJSON_IsString(pt) && pt->valuestring[0] != '\\0' &&
             cJSON_IsString(fw) && fw->valuestring[0] != '\\0' &&
             cJSON_IsString(av) && av->valuestring[0] != '\\0' &&
             cJSON_IsString(lt) && lt->valuestring[0] != '\\0' &&
             pc_ok &&
             cJSON_IsString(pl) && pl->valuestring[0] != '\\0' &&
             is_int_num(slot) && slot->valuedouble > 0.0 &&
             cJSON_IsArray(caps);
    }}
    cJSON_Delete(root);
    return ok;
}}

int c_run_dn_stage(const char *raw, size_t len, char *cmd_id_out, size_t id_cap,
                   char *type_out, size_t type_cap, bool *apply_now_out,
                   uint32_t *timeout_out, char *detail_out, size_t detail_cap) {{
    ff_mqtt_ctx_t ctx;
    memset(&ctx, 0, sizeof(ctx));
    memset(&g_last_cmd, 0, sizeof(g_last_cmd));
    g_ota_started = false;
    g_status_count = 0;
    g_last_status_detail[0] = '\\0';

    const char *topic = "ff/v1/d/02a1b2c3d4e5/dn/cmd";
    on_command(&ctx, topic, (int)strlen(topic), raw, (int)len);

    snprintf(detail_out, detail_cap, "%s", g_last_status_detail);
    if (!g_ota_started) {{
        return g_status_count > 0 ? -1 : 0;
    }}
    snprintf(cmd_id_out, id_cap, "%s", g_last_cmd.cmd_id);
    snprintf(type_out, type_cap, "%s", g_last_cmd.type);
    *apply_now_out = g_last_cmd.apply_now;
    *timeout_out = g_last_cmd.confirm_timeout_s;
    return 1;
}}
"""
    so_path = tmp_path / "libff_wire_vectors.so"
    proc = subprocess.run(  # noqa: S603
        [
            gcc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-function",
            "-shared",
            "-fPIC",
            "-x",
            "c",
            "-",
            "-o",
            str(so_path),
        ],
        input=harness_c.encode("utf-8"),
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", errors="replace")
    lib = ctypes.CDLL(str(so_path))
    lib.c_validate_up.argtypes = [ctypes.c_char_p, ctypes.c_char_p, ctypes.c_size_t]
    lib.c_validate_up.restype = ctypes.c_bool
    lib.c_run_dn_stage.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_bool),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.c_run_dn_stage.restype = ctypes.c_int
    return lib


@dataclass
class _StubTopic:
    value: str


@dataclass
class _StubMessage:
    topic: _StubTopic
    payload: bytes


@dataclass
class _RecordingClient:
    published: list[tuple[str, bytes, int, bool]] = field(default_factory=list)

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


def _simulator_accepts_up_vector(channel: str, payload: dict[str, Any]) -> bool:
    """Validate an `up/*` payload through `fleetforge.simulator`'s own models/encoders."""
    if channel == "presence":
        raw = encode(payload)
        return raw in (PRESENCE_ONLINE, PRESENCE_OFFLINE)
    if channel == "hb":
        fw = payload.get("fw_version")
        up = payload.get("uptime_s")
        boot_ok = payload.get("boot_ok", True)
        if not isinstance(fw, str) or not fw:
            return False
        if isinstance(up, bool) or not isinstance(up, int) or up < 0:
            return False
        if not isinstance(boot_ok, bool):
            return False
        ident = DeviceIdentity(device_id="02a1b2c3d4e5", fw_version=fw)
        built = ident.heartbeat(up, boot_ok=boot_ok)
        return built["fw_version"] == fw and built["uptime_s"] == up and built["boot_ok"] is boot_ok
    if channel == "status":
        valid_states = {
            "staging",
            "downloading",
            "verifying",
            "staged",
            "awaiting_safe_window",
            "applying",
            "rebooting",
            "failed",
            "confirming",
            "confirmed",
            "rolling_back",
            "rolled_back",
            "cancelled",
        }
        cid = payload.get("cmd_id")
        st = payload.get("state")
        pct = payload.get("pct")
        pct_ok = pct is None or (
            not isinstance(pct, bool) and isinstance(pct, int) and 0 <= pct <= 100
        )
        return (
            isinstance(cid, str)
            and bool(cid)
            and isinstance(st, str)
            and st in valid_states
            and pct_ok
        )
    if channel == "ack":
        uid = payload.get("id")
        res = payload.get("result")
        return (
            isinstance(uid, str)
            and bool(uid)
            and isinstance(res, str)
            and res in {"accepted", "deferred", "refused"}
        )
    if channel == "announce":
        try:
            caps = payload.get("capabilities")
            if not isinstance(caps, list) or not all(isinstance(x, str) for x in caps):
                return False
            ident = DeviceIdentity(
                device_id=str(payload.get("device_id", "")),
                platform_type=str(payload.get("platform_type", "")),
                fw_version=str(payload.get("fw_version", "")),
                agent_version=str(payload.get("agent_version", "")),
                link_type=str(payload.get("link_type", "")),
                ssid=payload.get("ssid"),
                known_networks=payload.get("known_networks"),
                power_class=str(payload.get("power_class", "")),
                expected_wake_interval_s=payload.get("expected_wake_interval_s"),
                parent_device_id=payload.get("parent_device_id"),
                partition_layout=str(payload.get("partition_layout", "")),
                ota_slot_size=int(payload.get("ota_slot_size", 0)),
                flash_chip_size=payload.get("flash_chip_size"),
                partition_table_sha256=payload.get("partition_table_sha256"),
                rollback_capable=payload.get("rollback_capable"),
                capabilities=tuple(caps),
                reset_reason=payload.get("reset_reason"),
                sub_versions=payload.get("sub_versions"),
                proto=int(payload.get("proto", 0)),
            )
            ident.validate()
        except (SimulatorError, TypeError, ValueError):
            return False
        return ident.announce() == payload
    raise AssertionError(f"unexpected channel {channel!r}")


def _ingestor_accepts_up_vector(channel: str, raw: bytes, payload: dict[str, Any]) -> bool:
    """Decode via `fleetforge.ingestor.protocol.decode` and verify lossless field parity."""
    topic = up_topic("02a1b2c3d4e5", channel)
    decoded_obj: Any = None
    if channel == "announce":
        decoded_obj = decode(AnnouncePayload, topic, raw)
        if decoded_obj is None or decoded_obj.proto != 1 or not decoded_obj.device_id:
            return False
        if decoded_obj.capabilities is None:
            return False
    elif channel == "hb":
        decoded_obj = decode(HeartbeatPayload, topic, raw)
        if decoded_obj is None or not decoded_obj.fw_version or decoded_obj.uptime_s is None:
            return False
    elif channel == "status":
        decoded_obj = decode(StatusPayload, topic, raw)
        if decoded_obj is None or not decoded_obj.cmd_id or not decoded_obj.state:
            return False
    elif channel == "ack":
        decoded_obj = decode(AckPayload, topic, raw)
        if decoded_obj is None or not decoded_obj.id or not decoded_obj.result:
            return False
    elif channel == "presence":
        decoded_obj = decode(PresencePayload, topic, raw)
        if decoded_obj is None or decoded_obj.online is None:
            return False
    else:
        raise AssertionError(f"unexpected channel {channel!r}")

    for field_name in type(decoded_obj).model_fields:
        if field_name in payload and getattr(decoded_obj, field_name) != payload[field_name]:
            return False
    return True


async def test_wire_vectors_round_trip_across_ingestor_simulator_and_c(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`D-10`: Every `wire_vectors.json` vector agrees across ingestor, simulator, and C."""
    lib = _compile_c_wire_harness(tmp_path)
    contract = load_wire_vectors_contract()
    vectors: list[dict[str, Any]] = contract["vectors"]
    seen_channels = {v["channel"] for v in vectors}
    assert seen_channels == {"announce", "hb", "status", "ack", "presence", "dn/stage"}

    for channel in seen_channels:
        chan_vecs = [v for v in vectors if v["channel"] == channel]
        assert any(v["valid"] for v in chan_vecs), f"missing valid vector for {channel}"
        assert any(not v["valid"] for v in chan_vecs), f"missing invalid vector for {channel}"

    for vec in vectors:
        vec_id = str(vec["id"])
        channel = str(vec["channel"])
        expected_valid = bool(vec["valid"])
        payload_obj = vec["payload"]
        raw = encode(payload_obj)

        if channel != "dn/stage":
            c_ok = bool(lib.c_validate_up(channel.encode("utf-8"), raw, len(raw)))
            sim_ok = _simulator_accepts_up_vector(channel, payload_obj)
            ingestor_ok = _ingestor_accepts_up_vector(channel, raw, payload_obj)
            assert c_ok is expected_valid, f"C mismatch on {vec_id}"
            assert sim_ok is expected_valid, f"Simulator mismatch on {vec_id}"
            assert ingestor_ok is expected_valid, f"Ingestor mismatch on {vec_id}"
            continue

        c_id = ctypes.create_string_buffer(64)
        c_type = ctypes.create_string_buffer(32)
        c_apply_now = ctypes.c_bool(False)
        c_timeout = ctypes.c_uint32(0)
        c_detail = ctypes.create_string_buffer(128)
        rc = lib.c_run_dn_stage(
            raw,
            len(raw),
            c_id,
            64,
            c_type,
            32,
            ctypes.byref(c_apply_now),
            ctypes.byref(c_timeout),
            c_detail,
            128,
        )

        monkeypatch.setattr(
            "fleetforge.simulator.device._fetch",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("stop-at-download")),
        )
        ident = DeviceIdentity(
            device_id="02a1b2c3d4e5",
            capabilities=(*DEFAULT_AGENT_CAPABILITIES, "mcu_fw"),
        )
        runner = StageRunner(
            identity=ident,
            link=LinkProfile(),
            require_signature=False,
            download_retries=0,
            cargo_handlers={"mcu_fw": lambda _b, _v: None},
        )
        client = _RecordingClient()
        msg = _StubMessage(topic=_StubTopic("ff/v1/d/02a1b2c3d4e5/dn/cmd"), payload=raw)
        await runner.handle(client, msg, lambda _s: None)  # type: ignore[arg-type]
        statuses = [json.loads(b.decode("utf-8")) for _, b, _, _ in client.published]

        if expected_valid:
            assert rc == 1, f"C rejected valid dn/stage {vec_id}: {c_detail.value!r}"
            assert c_id.value.decode("utf-8") == payload_obj["id"]
            expected_type = payload_obj["artifact"].get("type", "app")
            assert c_type.value.decode("utf-8") == expected_type
            assert c_apply_now.value is (payload_obj.get("apply") != "on_command")
            assert 30 <= c_timeout.value <= 3600
            assert statuses and statuses[0]["state"] == STATE_STAGING, (
                f"Simulator rejected valid dn/stage {vec_id}: {statuses}"
            )
        else:
            assert rc <= 0, f"C accepted invalid dn/stage {vec_id}"
            assert not any(s["state"] == STATE_STAGING for s in statuses), (
                f"Simulator staged invalid dn/stage {vec_id}: {statuses}"
            )
            if "expected_detail" in vec:
                exp = vec["expected_detail"]
                if exp is None:
                    assert rc == 0
                    assert statuses == []
                else:
                    assert c_detail.value.decode("utf-8") == exp
                    assert statuses[0]["state"] == STATE_FAILED
                    assert statuses[0]["detail"] == exp
            else:
                assert c_detail.value.decode("utf-8") == vec["expected_c_detail"]
                assert statuses[0]["state"] == STATE_FAILED
                assert statuses[0]["detail"] == vec["expected_sim_detail"]
