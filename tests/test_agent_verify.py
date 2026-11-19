"""Verify before the boot switch (R2-fw-1, D-4, T-3) — parser-level call-site guards & host-compiled C tests."""

from __future__ import annotations

import base64
import ctypes
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from fleetforge.signing import DEFAULT_PRIMARY_PUBLIC_KEY_PEM
from tests.agent_src import COMPONENT_SRC, c_call_sites, extract_c_function
from tests.test_agent_txn import FF_MQTT_C, FF_OTA_C, _function_body
from tests.test_ff_cfg import _code

PARENTHESISED_GUARDED_RE = re.compile(
    r"\(\s*(?:esp_ota_set_boot_partition|ff_txn_save|ff_selftest_run"
    r"|ff_selftest_freeze|restore_boot_partition)\s*\)\s*\("
)


def _ota_task() -> str:
    return _function_body(_code(FF_OTA_C), "ota_task")


def test_the_digest_is_checked_before_the_boot_pointer_moves() -> None:
    task = _ota_task()
    assert task.count("partition_digest(") == 1, "one read-back, not one per side of finish()"
    assert task.index("partition_digest(") < task.index("esp_https_ota_finish(")


def test_a_mismatch_aborts_and_never_touches_the_boot_partition() -> None:
    task = _ota_task()
    start = task.index("if (strcmp(digest, cmd->sha256) != 0)")
    branch = task[start : task.index("goto done", start)]
    assert "esp_https_ota_abort(handle)" in branch
    assert "restore_boot_partition" not in branch
    assert "esp_https_ota_finish" not in branch
    assert "strcasecmp(digest" not in _code(FF_OTA_C)


def test_the_boot_partition_is_set_only_by_the_undo() -> None:
    raw_ota = FF_OTA_C.read_text(encoding="utf-8")
    assert c_call_sites(raw_ota, "esp_ota_set_boot_partition") == [
        "ff_ota_dock_stage",
        "restore_boot_partition",
    ]


def test_no_parenthesised_call_bypasses_a_guard() -> None:
    """D-4: zero parenthesised `(fn)(...)` calls in component C, and `c_call_sites` catches them."""
    for path in sorted(COMPONENT_SRC.glob("*.c")):
        text = path.read_text(encoding="utf-8")
        assert PARENTHESISED_GUARDED_RE.search(text) is None, (
            f"parenthesised guarded call in {path.name}"
        )

    synthetic = """
    static void sneaky(void) {
        (esp_ota_set_boot_partition)(target);
        ((ff_selftest_run))(cb);
    }
    """
    assert c_call_sites(synthetic, "esp_ota_set_boot_partition") == ["sneaky"]
    assert c_call_sites(synthetic, "ff_selftest_run") == ["sneaky"]

    aliased = """
    static void via_ptr(void) {
        void *fn = esp_ota_set_boot_partition;
        (void)fn;
    }
    """
    with pytest.raises(AssertionError, match="function-pointer alias"):
        c_call_sites(aliased, "esp_ota_set_boot_partition")


def test_flash_encryption_is_a_compile_error() -> None:
    assert re.search(r"#if CONFIG_SECURE_FLASH_ENC_ENABLED\s*\n\s*#error ", _code(FF_OTA_C))


def test_the_slot_size_is_checked_before_anything_is_fetched() -> None:
    task = _ota_task()
    guard = task.index("cmd->size > target->size")
    assert guard < task.index("resolve_artifact_url(") < task.index("esp_https_ota_begin(")


def test_a_malformed_digest_is_refused_at_the_seam() -> None:
    mqtt = _code(FF_MQTT_C)
    stage = _function_body(mqtt, "on_stage")
    assert stage.index("is_lowercase_sha256(") < stage.index("ff_ota_start(")
    assert '"artifact sha256 malformed"' in stage
    validator = _function_body(mqtt, "is_lowercase_sha256")
    for forbidden in ("tolower", "toupper", "isxdigit"):
        assert forbidden not in validator, forbidden


def _compile_verify_harness(tmp_path: Path, *, prod_build: bool = False) -> ctypes.CDLL:
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc must be available to compile host C verification tests"

    ota_raw = FF_OTA_C.read_text(encoding="utf-8")
    mqtt_raw = FF_MQTT_C.read_text(encoding="utf-8")

    # Pin exact condition polarities in `ota_task` and `ff_ota_dock_stage` so 1-line flips fail.
    ota_code = _code(FF_OTA_C)
    assert "if (strcmp(digest, cmd->sha256) != 0)" in _function_body(ota_code, "ota_task")
    assert "if (!verify_artifact_signature(digest_raw, cmd->sig, &sig_detail))" in _function_body(
        ota_code, "ota_task"
    )
    assert "if (strcmp(digest, cmd->sha256) != 0)" in _function_body(ota_code, "ff_ota_dock_stage")
    assert "if (!verify_artifact_signature(digest_raw, cmd->sig, &sig_detail))" in _function_body(
        ota_code, "ff_ota_dock_stage"
    )

    fn_sha256 = extract_c_function(mqtt_raw, "is_lowercase_sha256")
    fn_pending = extract_c_function(mqtt_raw, "pending_verify")
    fn_ota_state = extract_c_function(ota_raw, "ota_state_name")
    fn_choose_slot = extract_c_function(ota_raw, "choose_target_slot")
    fn_verify_sig = extract_c_function(ota_raw, "verify_artifact_signature")

    prod_define = "#define FF_OTA_PRODUCTION_BUILD 1\n" if prod_build else ""
    dev_pem_escaped = DEFAULT_PRIMARY_PUBLIC_KEY_PEM.replace("\n", "\\n")

    harness_c = f"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

{prod_define}
#define FF_OTA_REQUIRE_SIGNATURE 1

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_NOT_FOUND 0x105

typedef enum {{
    ESP_OTA_IMG_NEW = 0,
    ESP_OTA_IMG_PENDING_VERIFY = 1,
    ESP_OTA_IMG_VALID = 2,
    ESP_OTA_IMG_INVALID = 3,
    ESP_OTA_IMG_ABORTED = 4,
    ESP_OTA_IMG_UNDEFINED = 5,
}} esp_ota_img_states_t;

#define ESP_PARTITION_TYPE_APP 0x00
#define ESP_PARTITION_SUBTYPE_APP_OTA_MIN 0x10
#define ESP_PARTITION_SUBTYPE_APP_OTA_MAX 0x20

typedef struct {{
    int type;
    int subtype;
    uint32_t address;
    uint32_t size;
    char label[17];
}} esp_partition_t;

typedef struct {{
    char cmd_id[64];
    char sha256[65];
    char sig[160];
}} ff_ota_cmd_t;

static const char *TAG = "ff-test";
#define ESP_LOGI(tag, fmt, ...) ((void)(tag))
#define ESP_LOGW(tag, fmt, ...) ((void)(tag))
#define ESP_LOGE(tag, fmt, ...) ((void)(tag))
static const char *esp_err_to_name(esp_err_t e) {{ (void)e; return "err"; }}

static esp_partition_t g_slots[2] = {{
    {{ ESP_PARTITION_TYPE_APP, 0x10, 0x20000, 1966080, "ota_0" }},
    {{ ESP_PARTITION_TYPE_APP, 0x11, 0x200000, 1966080, "ota_1" }},
}};
static const esp_partition_t *g_running = &g_slots[0];
static const esp_partition_t *g_boot = &g_slots[0];
static const esp_partition_t *g_next = &g_slots[1];
static esp_err_t g_state_err = ESP_OK;
static esp_ota_img_states_t g_running_state = ESP_OTA_IMG_VALID;
static char g_last_fail[128];

static const esp_partition_t *esp_ota_get_running_partition(void) {{ return g_running; }}
static const esp_partition_t *esp_ota_get_boot_partition(void) {{ return g_boot; }}
static const esp_partition_t *esp_ota_get_next_update_partition(const esp_partition_t *p) {{
    (void)p;
    return g_next;
}}
static esp_err_t esp_ota_get_state_partition(const esp_partition_t *p, esp_ota_img_states_t *st) {{
    (void)p;
    if (g_state_err != ESP_OK) return g_state_err;
    *st = g_running_state;
    return ESP_OK;
}}
static void fail(const ff_ota_cmd_t *cmd, const char *detail) {{
    (void)cmd;
    snprintf(g_last_fail, sizeof(g_last_fail), "%s", detail ? detail : "");
}}

/* Minimal mbedTLS stubs controlled by deterministic test vectors */
typedef struct {{ int slot_ok; }} mbedtls_pk_context;
#define MBEDTLS_MD_SHA256 1
static bool g_dev_key_in_ring = false;
static int g_pk_verify_rc = 0;
static uint8_t g_expected_digest[32];
static const char *s_signing_pubkey_ring[2] = {{ "{dev_pem_escaped}", "{dev_pem_escaped}" }};
static const char *signing_pubkey_slot(size_t i) {{ return s_signing_pubkey_ring[i]; }}
static bool ff_ota_uses_dev_signing_key(void) {{ return g_dev_key_in_ring; }}
static void mbedtls_pk_init(mbedtls_pk_context *ctx) {{ ctx->slot_ok = 0; }}
static void mbedtls_pk_free(mbedtls_pk_context *ctx) {{ ctx->slot_ok = 0; }}
static int mbedtls_pk_parse_public_key(mbedtls_pk_context *ctx, const unsigned char *key, size_t len) {{
    (void)key; (void)len;
    ctx->slot_ok = 1;
    return 0;
}}
static int mbedtls_pk_verify(mbedtls_pk_context *ctx, int md, const uint8_t *hash, size_t hlen,
                             const uint8_t *sig, size_t slen) {{
    (void)ctx; (void)md; (void)sig; (void)slen;
    if (hlen != 32 || memcmp(hash, g_expected_digest, 32) != 0) {{
        return -1;
    }}
    return g_pk_verify_rc;
}}

static int b64_val(unsigned char c) {{
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}}
static int mbedtls_base64_decode(unsigned char *dst, size_t dlen, size_t *olen,
                                 const unsigned char *src, size_t slen) {{
    if (slen == 0 || (slen % 4) != 0) return -1;
    size_t out = 0;
    for (size_t i = 0; i < slen; i += 4) {{
        int a = b64_val(src[i]);
        int b = b64_val(src[i + 1]);
        int c = src[i + 2] == '=' ? 0 : b64_val(src[i + 2]);
        int d = src[i + 3] == '=' ? 0 : b64_val(src[i + 3]);
        if (a < 0 || b < 0 || c < 0 || d < 0) return -1;
        uint32_t v = ((uint32_t)a << 18) | ((uint32_t)b << 12) | ((uint32_t)c << 6) | (uint32_t)d;
        if (out >= dlen) return -1;
        dst[out++] = (unsigned char)((v >> 16) & 0xFF);
        if (src[i + 2] != '=') {{
            if (out >= dlen) return -1;
            dst[out++] = (unsigned char)((v >> 8) & 0xFF);
        }}
        if (src[i + 3] != '=') {{
            if (out >= dlen) return -1;
            dst[out++] = (unsigned char)(v & 0xFF);
        }}
    }}
    *olen = out;
    return 0;
}}

{fn_sha256}
{fn_pending}
{fn_ota_state}
{fn_choose_slot}
{fn_verify_sig}

bool exported_is_lowercase_sha256(const char *s) {{ return is_lowercase_sha256(s); }}

bool exported_pending_verify(int state, int err) {{
    g_running = &g_slots[0];
    g_running_state = (esp_ota_img_states_t)state;
    g_state_err = err;
    return pending_verify();
}}

int exported_choose_slot(int running_state, int boot_idx, int next_idx, char *fail_out, size_t cap) {{
    g_last_fail[0] = '\\0';
    g_running = &g_slots[0];
    g_running_state = (esp_ota_img_states_t)running_state;
    g_state_err = ESP_OK;
    g_boot = boot_idx >= 0 ? &g_slots[boot_idx] : NULL;
    g_next = next_idx >= 0 ? &g_slots[next_idx] : NULL;
    ff_ota_cmd_t cmd = {{0}};
    snprintf(cmd.cmd_id, sizeof(cmd.cmd_id), "cmd-1");
    const esp_partition_t *res = choose_target_slot(&cmd);
    snprintf(fail_out, cap, "%s", g_last_fail);
    if (res == NULL) return -1;
    return res == &g_slots[1] ? 1 : 0;
}}

bool exported_verify_sig(const uint8_t digest[32], const uint8_t expected[32], const char *sig,
                         int pk_rc, bool dev_key, char *detail_out, size_t cap) {{
    memcpy(g_expected_digest, expected, 32);
    g_pk_verify_rc = pk_rc;
    g_dev_key_in_ring = dev_key;
    const char *detail = "";
    bool ok = verify_artifact_signature(digest, sig, &detail);
    snprintf(detail_out, cap, "%s", detail ? detail : "");
    return ok;
}}
"""
    so_path = tmp_path / ("libff_verify_prod.so" if prod_build else "libff_verify.so")
    proc = subprocess.run(  # noqa: S603
        [
            gcc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
            "-Wno-unused-variable",
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
    lib.exported_is_lowercase_sha256.argtypes = [ctypes.c_char_p]
    lib.exported_is_lowercase_sha256.restype = ctypes.c_bool
    lib.exported_pending_verify.argtypes = [ctypes.c_int, ctypes.c_int]
    lib.exported_pending_verify.restype = ctypes.c_bool
    lib.exported_choose_slot.argtypes = [
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.exported_choose_slot.restype = ctypes.c_int
    lib.exported_verify_sig.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_bool,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.exported_verify_sig.restype = ctypes.c_bool
    return lib


def test_the_mismatch_branch_is_taken_on_inequality(tmp_path: Path) -> None:
    """T-3: Compile and execute firmware verification, SHA-256, pending_verify, and slot selection."""
    lib = _compile_verify_harness(tmp_path, prod_build=False)
    prod_lib = _compile_verify_harness(tmp_path, prod_build=True)

    # 1. `is_lowercase_sha256`: exact 64 lowercase hex passes; inequality/uppercase/short/long fail.
    assert lib.exported_is_lowercase_sha256(b"a" * 64) is True
    assert lib.exported_is_lowercase_sha256(b"0123456789abcdef" * 4) is True
    assert lib.exported_is_lowercase_sha256(b"A" * 64) is False
    assert lib.exported_is_lowercase_sha256(b"a" * 63) is False
    assert lib.exported_is_lowercase_sha256(b"a" * 65) is False
    assert lib.exported_is_lowercase_sha256(b"g" * 64) is False

    # 2. `pending_verify`: true ONLY on ESP_OTA_IMG_PENDING_VERIFY (1) with ESP_OK (0).
    assert lib.exported_pending_verify(1, 0) is True
    assert lib.exported_pending_verify(2, 0) is False  # VALID
    assert lib.exported_pending_verify(0, 0) is False  # NEW
    assert lib.exported_pending_verify(1, -1) is False  # read error

    # 3. `choose_target_slot`: chooses spare slot (1) when VALID & boot==running; refuses mismatches.
    detail_buf = ctypes.create_string_buffer(128)
    assert lib.exported_choose_slot(2, 0, 1, detail_buf, 128) == 1
    assert lib.exported_choose_slot(1, 0, 1, detail_buf, 128) == -1
    assert b"not confirmed yet" in detail_buf.value
    assert lib.exported_choose_slot(2, 1, 1, detail_buf, 128) == -1
    assert b"already staged" in detail_buf.value
    assert lib.exported_choose_slot(2, 0, 0, detail_buf, 128) == -1
    assert b"no spare ota slot" in detail_buf.value

    # 4. `verify_artifact_signature`: passes on matching digest + rc==0; fails on inequality or bad sig.
    der_sig = b"\x30\x44" + b"\x02\x20" + (b"\x11" * 32) + b"\x02\x20" + (b"\x22" * 32)
    valid_sig = b"ecdsa-p256-sha256:" + base64.b64encode(der_sig)
    digest_a = b"\xaa" * 32
    digest_b = b"\xbb" * 32

    assert lib.exported_verify_sig(digest_a, digest_a, valid_sig, 0, False, detail_buf, 128) is True
    # Digest inequality -> mismatch branch taken (`signature invalid`, returns False)
    assert (
        lib.exported_verify_sig(digest_a, digest_b, valid_sig, 0, False, detail_buf, 128) is False
    )
    assert detail_buf.value == b"signature invalid"
    # Non-zero mbedtls_pk_verify return -> returns False
    assert (
        lib.exported_verify_sig(digest_a, digest_a, valid_sig, -1, False, detail_buf, 128) is False
    )
    assert detail_buf.value == b"signature invalid"
    # Missing signature under FF_OTA_REQUIRE_SIGNATURE=1 -> returns False
    assert lib.exported_verify_sig(digest_a, digest_a, b"", 0, False, detail_buf, 128) is False
    assert detail_buf.value == b"signature missing"
    # Production build refuses dev key in ring (`D-1 / A-8`)
    assert (
        prod_lib.exported_verify_sig(digest_a, digest_a, valid_sig, 0, True, detail_buf, 128)
        is False
    )
    assert detail_buf.value == b"production ring carries dev key"
