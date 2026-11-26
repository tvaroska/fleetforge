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


def _extract_ota_sig_ladder() -> str:
    """Extract the real signature preprocessor ladder and key-slot helpers from `ff_ota.c`."""
    raw = FF_OTA_C.read_text(encoding="utf-8")
    start = raw.index("#define FF_OTA_DEV_PRIMARY_PUBKEY_PEM")
    end = raw.index("/* The URL the artifact link redirects to.", start)
    return raw[start:end]


def _compile_sig_ladder_case(
    tmp_path: Path,
    name: str,
    prelude_c: str,
) -> subprocess.CompletedProcess[bytes]:
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc must be available to compile host C verification tests"
    ladder = _extract_ota_sig_ladder()
    src = f"""
#include <stdbool.h>
#include <stddef.h>
#include <string.h>

{prelude_c}
{ladder}

bool exported_requires_sig(void) {{ return ff_ota_requires_signature(); }}
bool exported_uses_dev_key(void) {{ return ff_ota_uses_dev_signing_key(); }}
"""
    so_path = tmp_path / f"libff_ladder_{name}.so"
    return subprocess.run(  # noqa: S603
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
        input=src.encode("utf-8"),
        capture_output=True,
    )


def test_the_signature_ladder_under_each_define_set(tmp_path: Path) -> None:
    """D-23 & D-24: Compile `ff_ota.c`'s un-overridden `#define` ladder & real `ff_ota_uses_dev_signing_key()`."""
    custom_a = (
        '"-----BEGIN PUBLIC KEY-----\\n'
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEAAAAAAAAAAAAAAAAAAAAAAAAAAAA\\n"
        "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA==\\n"
        '-----END PUBLIC KEY-----\\n"'
    )
    custom_b = (
        '"-----BEGIN PUBLIC KEY-----\\n'
        "MFkwEwYHKoZIzj0CAQYIKoZIzj0DAQcDQgAEBBBBBBBBBBBBBBBBBBBBBBBBBBBB\\n"
        "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB==\\n"
        '-----END PUBLIC KEY-----\\n"'
    )

    # 1. Default build (no defines): signature required by default, dev keys active (`M2a`, `M2b`).
    p_default = _compile_sig_ladder_case(tmp_path, "default", "")
    assert p_default.returncode == 0, p_default.stderr.decode("utf-8", errors="replace")
    lib_default = ctypes.CDLL(str(tmp_path / "libff_ladder_default.so"))
    lib_default.exported_requires_sig.restype = ctypes.c_bool
    lib_default.exported_uses_dev_key.restype = ctypes.c_bool
    assert lib_default.exported_requires_sig() is True
    assert lib_default.exported_uses_dev_key() is True

    # 2. Explicit dev opt-out `CONFIG_FF_OTA_REQUIRE_SIGNATURE=0` (non-prod): disables requirement.
    p_optout = _compile_sig_ladder_case(
        tmp_path, "optout", "#define CONFIG_FF_OTA_REQUIRE_SIGNATURE 0\n"
    )
    assert p_optout.returncode == 0, p_optout.stderr.decode("utf-8", errors="replace")
    lib_optout = ctypes.CDLL(str(tmp_path / "libff_ladder_optout.so"))
    lib_optout.exported_requires_sig.restype = ctypes.c_bool
    assert lib_optout.exported_requires_sig() is False

    # 3. D-23 regression: Kconfig `string` options default to `""`; under CONFIG_FF_OTA_PRODUCTION_BUILD=1
    #    the `#error` MUST fire instead of silently compiling with dev keys.
    p_kconfig_empty = _compile_sig_ladder_case(
        tmp_path,
        "kconfig_empty",
        "#define CONFIG_FF_OTA_PRODUCTION_BUILD 1\n"
        '#define CONFIG_FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM ""\n'
        '#define CONFIG_FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM ""\n',
    )
    assert p_kconfig_empty.returncode != 0
    assert b"FF_OTA_PRODUCTION_BUILD" in p_kconfig_empty.stderr

    # 4. Single-slot override under production build MUST fail compile.
    p_single = _compile_sig_ladder_case(
        tmp_path,
        "single_slot",
        f"#define FF_OTA_PRODUCTION_BUILD 1\n#define FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM {custom_a}\n",
    )
    assert p_single.returncode != 0

    # 5. Empty-string macro overrides under production build MUST fail `_Static_assert`.
    p_empty_macros = _compile_sig_ladder_case(
        tmp_path,
        "empty_macros",
        "#define FF_OTA_PRODUCTION_BUILD 1\n"
        '#define FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM ""\n'
        '#define FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM ""\n',
    )
    assert p_empty_macros.returncode != 0

    # 6. Production build with both custom PEMs forces signature=1 even if CONFIG_FF_OTA_REQUIRE_SIGNATURE=0.
    p_prod_ok = _compile_sig_ladder_case(
        tmp_path,
        "prod_ok",
        f"#define CONFIG_FF_OTA_PRODUCTION_BUILD 1\n"
        f"#define CONFIG_FF_OTA_REQUIRE_SIGNATURE 0\n"
        f"#define FF_OTA_SIGNING_PUBKEY_PRIMARY_PEM {custom_a}\n"
        f"#define FF_OTA_SIGNING_PUBKEY_SECONDARY_PEM {custom_b}\n",
    )
    assert p_prod_ok.returncode == 0, p_prod_ok.stderr.decode("utf-8", errors="replace")
    lib_prod_ok = ctypes.CDLL(str(tmp_path / "libff_ladder_prod_ok.so"))
    lib_prod_ok.exported_requires_sig.restype = ctypes.c_bool
    lib_prod_ok.exported_uses_dev_key.restype = ctypes.c_bool
    assert lib_prod_ok.exported_requires_sig() is True
    assert lib_prod_ok.exported_uses_dev_key() is False

    # 7. EMBED_TXTFILES path (`FF_OTA_EMBEDDED_PUBKEYS=1`) with two custom PEM buffers compiles & clears dev check.
    p_embed = _compile_sig_ladder_case(
        tmp_path,
        "embed_ok",
        f"#define FF_OTA_PRODUCTION_BUILD 1\n"
        f"#define FF_OTA_EMBEDDED_PUBKEYS 1\n"
        f"const char _binary_ff_ota_pubkey_primary_pem_start[] = {custom_a};\n"
        f"const char _binary_ff_ota_pubkey_secondary_pem_start[] = {custom_b};\n",
    )
    assert p_embed.returncode == 0, p_embed.stderr.decode("utf-8", errors="replace")
    lib_embed = ctypes.CDLL(str(tmp_path / "libff_ladder_embed_ok.so"))
    lib_embed.exported_requires_sig.restype = ctypes.c_bool
    lib_embed.exported_uses_dev_key.restype = ctypes.c_bool
    assert lib_embed.exported_requires_sig() is True
    assert lib_embed.exported_uses_dev_key() is False


def test_cargo_and_dock_paths_pin_their_digest_and_signature_checks(tmp_path: Path) -> None:
    """D-24: Compile & execute `run_cargo_ota`, `ff_ota_dock_stage`, `ff_mqtt_confirm_offline`, and `secrets_retry_timer_cb`."""
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc must be available to compile host C verification tests"

    ota_raw = FF_OTA_C.read_text(encoding="utf-8")
    mqtt_raw = FF_MQTT_C.read_text(encoding="utf-8")
    sec_raw = (COMPONENT_SRC / "ff_sec.c").read_text(encoding="utf-8")

    # Pin download truncation / size mismatch checks in `ota_task` (`ff_ota.c:1092, 1107`).
    ota_task_code = _ota_task()
    assert "if (!esp_https_ota_is_complete_data_received(handle))" in ota_task_code
    assert 'fail(cmd, "truncated download");' in ota_task_code
    assert "if (cmd->size != 0 && got != cmd->size)" in ota_task_code
    assert 'fail(cmd, "size mismatch");' in ota_task_code

    fn_hex = extract_c_function(ota_raw, "hex_encode")
    fn_cargo = extract_c_function(ota_raw, "run_cargo_ota")
    fn_dock = extract_c_function(ota_raw, "ff_ota_dock_stage")
    fn_confirm_offline = extract_c_function(mqtt_raw, "ff_mqtt_confirm_offline")
    fn_sec_retry = extract_c_function(sec_raw, "secrets_retry_timer_cb")

    harness_c = f"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_INVALID_SIZE 0x104
#define ESP_ERR_NOT_SUPPORTED 0x106
#define ESP_ERR_INVALID_CRC 0x109
#define OTA_RESOLVED_URL_MAX 128
#define OTA_READBACK_CHUNK 64
#define OTA_HTTP_TIMEOUT_MS 1000
#define OTA_HTTP_RX_BUFFER 64
#define OTA_HTTP_TX_BUFFER 64
#define OTA_MAX_RETRIES 1
#define OTA_RETRY_BASE_MS 1
#define HTTP_METHOD_GET 0
#define FF_STATUS_STAGING 1
#define FF_STATUS_DOWNLOADING 2
#define FF_STATUS_VERIFYING 3
#define FF_STATUS_STAGED 4
#define FF_STATUS_APPLYING 5
#define FF_STATUS_REBOOTING 6
#define FF_STATUS_AWAITING_SAFE_WINDOW 7
#define FF_STATUS_PCT_NONE -1
#define FF_DETAIL_APPLY_WAITS "waits"
#define WHY_REJECTED "rejected"
#define pdMS_TO_TICKS(ms) (ms)

typedef int portMUX_TYPE;
#define portENTER_CRITICAL(l) ((void)(l))
#define portEXIT_CRITICAL(l) ((void)(l))
#define SECRETS_MAX_RETRY_ROUNDS 5

typedef struct {{
    char cmd_id[64];
    char url[128];
    char sha256[65];
    char sig[160];
    char type[32];
    char version[32];
    uint32_t size;
    bool apply_now;
    uint32_t confirm_timeout_s;
}} ff_ota_cmd_t;

typedef struct {{ uint32_t address; uint32_t size; char label[17]; }} esp_partition_t;
typedef uint32_t esp_ota_handle_t;
typedef esp_err_t (*ff_cargo_write_fn_t)(size_t offset, const uint8_t *data, size_t len, void *ctx);
typedef esp_err_t (*ff_cargo_finish_fn_t)(size_t total, const char *sha256, const char *ver, void *ctx);
typedef struct {{
    char type[32];
    ff_cargo_write_fn_t write_cb;
    ff_cargo_finish_fn_t finish_cb;
    void *ctx;
}} ff_cargo_handler_t;

typedef struct {{
    const char *url; int method; void *crt_bundle_attach; int timeout_ms;
    bool keep_alive_enable; int buffer_size; int buffer_size_tx;
}} esp_http_client_config_t;
typedef struct {{ size_t pos; }} mock_http_t;
typedef mock_http_t *esp_http_client_handle_t;

static void *esp_crt_bundle_attach = NULL;
static portMUX_TYPE s_cancel_lock = 0;
static volatile bool s_running = false;
static char g_last_fail[64];
static int g_finish_calls = 0;
static int g_set_boot_calls = 0;
static bool g_sig_ok = true;
static uint8_t g_computed_digest[32];
static uint8_t g_http_body[16] = {{1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16}};
static mock_http_t g_http_client;
static esp_partition_t g_slot = {{ 0x200000, 1966080, "ota_1" }};

static bool cancel_requested(void) {{ return false; }}
static void vTaskDelay(uint32_t t) {{ (void)t; }}
static void report_cancelled(const ff_ota_cmd_t *c) {{ (void)c; }}
static void fail(const ff_ota_cmd_t *c, const char *d) {{
    (void)c; snprintf(g_last_fail, sizeof(g_last_fail), "%s", d ? d : "");
}}
static void ff_mqtt_publish_status(const char *id, int st, int pct, const char *d) {{
    (void)id; (void)st; (void)pct; (void)d;
}}
static void ff_mqtt_complete_cargo(const char *id, const char *t, const char *v) {{
    (void)id; (void)t; (void)v;
}}
static esp_err_t resolve_artifact_url(const char *u, char *out) {{
    snprintf(out, OTA_RESOLVED_URL_MAX, "%s", u); return ESP_OK;
}}
static esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *c) {{
    (void)c; g_http_client.pos = 0; return &g_http_client;
}}
static esp_err_t esp_http_client_open(esp_http_client_handle_t c, int l) {{ (void)c; (void)l; return ESP_OK; }}
static int64_t esp_http_client_fetch_headers(esp_http_client_handle_t c) {{ (void)c; return sizeof(g_http_body); }}
static int g_http_status = 200;
static int g_write_calls = 0;
static int esp_http_client_get_status_code(esp_http_client_handle_t c) {{ (void)c; return g_http_status; }}
static esp_err_t esp_http_client_set_header(esp_http_client_handle_t c, const char *k, const char *v) {{
    (void)c; (void)k; (void)v; return ESP_OK;
}}
static esp_err_t esp_http_client_close(esp_http_client_handle_t c) {{ (void)c; return ESP_OK; }}
static esp_err_t esp_http_client_cleanup(esp_http_client_handle_t c) {{ (void)c; return ESP_OK; }}
static bool esp_http_client_is_complete_data_received(esp_http_client_handle_t c) {{
    return c->pos >= sizeof(g_http_body);
}}
static int esp_http_client_read(esp_http_client_handle_t c, char *b, int len) {{
    size_t rem = sizeof(g_http_body) - c->pos;
    if (rem == 0) return 0;
    size_t n = rem < (size_t)len ? rem : (size_t)len;
    memcpy(b, g_http_body + c->pos, n);
    c->pos += n;
    return (int)n;
}}
typedef struct {{ int dummy; }} mbedtls_sha256_context;
static void mbedtls_sha256_init(mbedtls_sha256_context *c) {{ c->dummy = 0; }}
static int mbedtls_sha256_starts(mbedtls_sha256_context *c, int i) {{ (void)c; (void)i; return 0; }}
static int mbedtls_sha256_update(mbedtls_sha256_context *c, const uint8_t *d, size_t l) {{ (void)c; (void)d; (void)l; return 0; }}
static int mbedtls_sha256_finish(mbedtls_sha256_context *c, uint8_t out[32]) {{
    (void)c; memcpy(out, g_computed_digest, 32); return 0;
}}
static void mbedtls_sha256_free(mbedtls_sha256_context *c) {{ (void)c; }}
static bool verify_artifact_signature(const uint8_t d[32], const char *s, const char **out) {{
    (void)d; (void)s;
    if (!g_sig_ok) {{ *out = "signature invalid"; return false; }}
    return true;
}}
static esp_err_t dummy_write_cb(size_t o, const uint8_t *d, size_t l, void *ctx) {{
    (void)o; (void)d; (void)l; (void)ctx; g_write_calls++; return ESP_OK;
}}
static esp_err_t dummy_finish_cb(size_t t, const char *s, const char *v, void *ctx) {{
    (void)t; (void)s; (void)v; (void)ctx; g_finish_calls++; return ESP_OK;
}}
static ff_cargo_handler_t g_cargo = {{ "mcu_fw", dummy_write_cb, dummy_finish_cb, NULL }};
static const ff_cargo_handler_t *find_cargo_handler(const char *t) {{
    return strcmp(t, "mcu_fw") == 0 ? &g_cargo : NULL;
}}
static const esp_partition_t *choose_target_slot(const ff_ota_cmd_t *c) {{ (void)c; return &g_slot; }}
static esp_err_t esp_ota_begin(const esp_partition_t *p, size_t l, esp_ota_handle_t *h) {{ (void)p; (void)l; *h = 1; return ESP_OK; }}
static esp_err_t esp_ota_write(esp_ota_handle_t h, const void *d, size_t l) {{ (void)h; (void)d; (void)l; return ESP_OK; }}
static esp_err_t esp_ota_abort(esp_ota_handle_t h) {{ (void)h; return ESP_OK; }}
static esp_err_t esp_ota_end(esp_ota_handle_t h) {{ (void)h; return ESP_OK; }}
static esp_err_t esp_ota_set_boot_partition(const esp_partition_t *p) {{ (void)p; g_set_boot_calls++; return ESP_OK; }}
static bool restore_boot_partition(const char *w) {{ (void)w; return true; }}
static esp_err_t ff_txn_save(const char *id, uint32_t a, uint32_t t) {{ (void)id; (void)a; (void)t; return ESP_OK; }}
static esp_err_t ff_txn_clear_if(const char *id) {{ (void)id; return ESP_OK; }}
static bool ff_safe_window_is_safe(void) {{ return true; }}
static esp_err_t partition_digest(const esp_partition_t *p, size_t l, char out[65], uint8_t raw[32]) {{
    (void)p; (void)l;
    memcpy(raw, g_computed_digest, 32);
    static const char HEX[] = "0123456789abcdef";
    for (int i = 0; i < 32; i++) {{ out[i*2] = HEX[raw[i]>>4]; out[i*2+1] = HEX[raw[i]&0xf]; }}
    out[64] = '\\0';
    return ESP_OK;
}}

{fn_hex}
{fn_cargo}
{fn_dock}

/* Stubs for ff_mqtt_confirm_offline (`ff_mqtt.c:2365`) */
#define TXN_BOOTED_NEW 1
#define TXN_CONFIRMING 2
#define DETAIL_SELFTEST_TIMED_OUT "timeout"
typedef struct {{ int kind; char cmd_id[32]; }} txn_t;
static portMUX_TYPE s_decide_lock = 0;
static volatile bool s_boot_classified = true;
static bool claim_boot_classify(void) {{ return false; }}
static volatile bool s_rollback_decided = false;
static bool s_selftest_registered = true;
static txn_t s_txn = {{ TXN_BOOTED_NEW, "cmd-1" }};
static char s_reboot_cmd_id[32];
static bool ff_selftest_freeze(void) {{ return true; }}
static void ff_identity_note_selftest(bool v) {{ (void)v; }}
static bool ff_safe_window_freeze(void) {{ return true; }}
static void ff_identity_note_safe_window(bool v) {{ (void)v; }}
static void ff_txn_init(void) {{}}
static esp_err_t ff_txn_last_reboot_cmd(char *b, size_t l) {{ (void)b; (void)l; return ESP_OK; }}
static void classify_txn(void) {{}}
static bool pending_verify(void) {{ return true; }}
static esp_err_t ff_txn_set_detail(const char *id, const char *d) {{ (void)id; (void)d; return ESP_OK; }}
static void selftest_done(bool ok, const char *r) {{ (void)ok; (void)r; }}
static esp_err_t ff_selftest_run(void (*cb)(bool, const char *)) {{ (void)cb; return ESP_OK; }}

{fn_confirm_offline}

/* Stubs for secrets_retry_timer_cb (`ff_sec.c:117`) */
static portMUX_TYPE s_sec_lock = 0;
static volatile bool s_sync_in_flight = false;
static volatile uint32_t s_local_rev = 0;
static volatile uint32_t s_target_rev = 2;
static volatile uint32_t s_retry_rounds = 0;
static int g_start_sync_calls = 0;
static void start_sync_task(void) {{ g_start_sync_calls++; }}

{fn_sec_retry}

int test_cargo_case(bool bad_sha, bool bad_sig, int http_status, char *fail_out, size_t cap) {{
    memset(g_computed_digest, 0xaa, 32);
    g_sig_ok = !bad_sig;
    g_http_status = http_status;
    g_write_calls = 0;
    g_finish_calls = 0;
    g_last_fail[0] = '\\0';
    ff_ota_cmd_t cmd = {{0}};
    snprintf(cmd.cmd_id, sizeof(cmd.cmd_id), "c1");
    snprintf(cmd.url, sizeof(cmd.url), "https://e.test/x");
    snprintf(cmd.type, sizeof(cmd.type), "mcu_fw");
    snprintf(cmd.version, sizeof(cmd.version), "1.0");
    cmd.size = sizeof(g_http_body);
    memset(cmd.sha256, bad_sha ? 'b' : 'a', 64);
    cmd.sha256[64] = '\\0';
    run_cargo_ota(&cmd);
    snprintf(fail_out, cap, "%s", g_last_fail);
    return g_finish_calls;
}}

int test_dock_case(bool is_cargo, bool bad_sha, bool bad_sig, char *fail_out, size_t cap) {{
    memset(g_computed_digest, 0xaa, 32);
    g_sig_ok = !bad_sig;
    g_finish_calls = 0;
    g_set_boot_calls = 0;
    g_last_fail[0] = '\\0';
    ff_ota_cmd_t cmd = {{0}};
    snprintf(cmd.cmd_id, sizeof(cmd.cmd_id), "d1");
    snprintf(cmd.type, sizeof(cmd.type), "%s", is_cargo ? "mcu_fw" : "app");
    snprintf(cmd.version, sizeof(cmd.version), "1.0");
    cmd.size = sizeof(g_http_body);
    memset(cmd.sha256, bad_sha ? 'b' : 'a', 64);
    cmd.sha256[64] = '\\0';
    (void)ff_ota_dock_stage(&cmd, g_http_body, sizeof(g_http_body));
    snprintf(fail_out, cap, "%s", g_last_fail);
    return is_cargo ? g_finish_calls : g_set_boot_calls;
}}

int test_confirm_offline_rollback_guard(bool rollback_decided) {{
    s_boot_classified = true;
    s_selftest_registered = true;
    s_rollback_decided = rollback_decided;
    return ff_mqtt_confirm_offline();
}}

int test_sec_retry_cap(uint32_t initial_rounds) {{
    s_sync_in_flight = false;
    s_local_rev = 0;
    s_target_rev = 2;
    s_retry_rounds = initial_rounds;
    g_start_sync_calls = 0;
    secrets_retry_timer_cb(NULL);
    return g_start_sync_calls;
}}
"""
    so_path = tmp_path / "libff_cargo_dock_mutants.so"
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
    lib.test_cargo_case.argtypes = [
        ctypes.c_bool,
        ctypes.c_bool,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.test_cargo_case.restype = ctypes.c_int
    lib.test_dock_case.argtypes = [
        ctypes.c_bool,
        ctypes.c_bool,
        ctypes.c_bool,
        ctypes.c_char_p,
        ctypes.c_size_t,
    ]
    lib.test_dock_case.restype = ctypes.c_int
    lib.test_confirm_offline_rollback_guard.argtypes = [ctypes.c_bool]
    lib.test_confirm_offline_rollback_guard.restype = ctypes.c_int
    lib.test_sec_retry_cap.argtypes = [ctypes.c_uint32]
    lib.test_sec_retry_cap.restype = ctypes.c_int

    buf = ctypes.create_string_buffer(64)
    # Cargo OTA: non-200 initial GET, bad sha256, or bad sig MUST NOT invoke finish_cb; valid invokes once.
    assert lib.test_cargo_case(False, False, 403, buf, 64) == 0
    assert buf.value == b"cannot open the artifact"
    assert lib.test_cargo_case(True, False, 200, buf, 64) == 0
    assert buf.value == b"sha256 mismatch"
    assert lib.test_cargo_case(False, True, 200, buf, 64) == 0
    assert buf.value == b"signature invalid"
    assert lib.test_cargo_case(False, False, 200, buf, 64) == 1

    # Declaration order check: `s_decide_lock` must be declared before `claim_boot_classify` in `ff_mqtt.c`.
    lock_pos = mqtt_raw.index("static portMUX_TYPE s_decide_lock")
    claim_pos = mqtt_raw.index("static bool claim_boot_classify(void)")
    assert lock_pos < claim_pos, "s_decide_lock must be declared before claim_boot_classify()"

    # Dock cargo & dock app: bad sha256 or bad sig MUST NOT invoke finish_cb / set_boot_partition.
    for is_cargo in (True, False):
        assert lib.test_dock_case(is_cargo, True, False, buf, 64) == 0
        assert buf.value == b"sha256 mismatch"
        assert lib.test_dock_case(is_cargo, False, True, buf, 64) == 0
        assert buf.value == b"signature invalid"
        assert lib.test_dock_case(is_cargo, False, False, buf, 64) == 1

    # `ff_mqtt_confirm_offline` refuses when `s_rollback_decided` is true (`ff_mqtt.c:2365`).
    assert lib.test_confirm_offline_rollback_guard(True) != 0
    assert lib.test_confirm_offline_rollback_guard(False) == 0

    # `secrets_retry_timer_cb` stops at `SECRETS_MAX_RETRY_ROUNDS` (`ff_sec.c:117`).
    assert lib.test_sec_retry_cap(4) == 1
    assert lib.test_sec_retry_cap(5) == 0


def test_improv_ble_and_mqtt_parse_hardening(tmp_path: Path) -> None:
    """D-8 & D-15: Host-compiled Improv BLE auth gate + firmware MQTT/cfg parse bounds."""
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc must be available to compile host C verification tests"

    wifi_raw = (COMPONENT_SRC / "ff_net_wifi.c").read_text(encoding="utf-8")
    mqtt_raw = FF_MQTT_C.read_text(encoding="utf-8")
    cfg_raw = (COMPONENT_SRC / "ff_cfg.c").read_text(encoding="utf-8")

    # D-8 static invariants: encrypted write flag + link check + window rate cap + flashed-first priority.
    assert "BLE_GATT_CHR_F_WRITE | BLE_GATT_CHR_F_WRITE_ENC" in wifi_raw
    assert "desc.sec_state.encrypted" in wifi_raw
    assert "IMPROV_BLE_MAX_OPENS_PER_BOOT" in wifi_raw
    assert "IMPROV_BLE_COOLDOWN_US" in wifi_raw
    assert "static uint8_t s_improv_state = IMPROV_STATE_AUTH_REQUIRED;" in wifi_raw
    overlay_body = extract_c_function(wifi_raw, "load_nvs_net_overlay")
    assert overlay_body.index("cfg->nets[i]") < overlay_body.index("nvs_open(")

    # D-15 static invariants: total_data_len guard, bounded size cast, hb_s cap, shared s_decide_lock.
    assert "event->total_data_len != event->data_len" in mqtt_raw
    assert "artifact size invalid" in mqtt_raw
    assert "out->hb_s > 86400" in cfg_raw
    assert "ctx->cfg->hb_s <= 86400" in mqtt_raw
    confirm_cb = extract_c_function(mqtt_raw, "confirm_timeout_cb")
    assert "session_confirmed = s_ctx.session_confirmed;" in confirm_cb
    assert confirm_cb.index("portENTER_CRITICAL(&s_decide_lock)") < confirm_cb.index(
        "s_ctx.session_confirmed"
    )
    assert "s_venue_offline" not in mqtt_raw

    # Compile real `improv_rpc_fail`, `improv_handle_rpc_command`, and `ff_net_wifi_improv_authorize` under host gcc.
    fn_rpc_fail = extract_c_function(wifi_raw, "improv_rpc_fail")
    fn_rpc = extract_c_function(wifi_raw, "improv_handle_rpc_command")
    fn_auth = extract_c_function(wifi_raw, "ff_net_wifi_improv_authorize")
    harness_c = f"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <string.h>

typedef int esp_err_t;
typedef int portMUX_TYPE;
#define portMUX_INITIALIZER_UNLOCKED 0
#define portENTER_CRITICAL(m) ((void)(m))
#define portEXIT_CRITICAL(m) ((void)(m))
#define ESP_OK 0
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_INVALID_STATE 0x103
#define ESP_ERR_INVALID_CRC 0x109
#define ESP_ERR_NOT_SUPPORTED 0x106
#define IMPROV_STATE_AUTH_REQUIRED 0x01
#define IMPROV_STATE_AUTHORIZED 0x02
#define IMPROV_STATE_PROVISIONING 0x03
#define IMPROV_ERR_NONE 0x00
#define IMPROV_ERR_INVALID_RPC 0x01
#define IMPROV_ERR_UNKNOWN_RPC 0x02
#define IMPROV_ERR_NOT_AUTHORIZED 0x04
#define IMPROV_CMD_WIFI_SETTINGS 0x01
#define IMPROV_CMD_IDENTIFY 0x02
#define WIFI_EVENT 0
#define FF_WIFI_EVENT_IMPROV_CRED 1
#define ESP_LOGI(tag, ...) ((void)(tag))

static const char *TAG = "test";
static volatile bool s_improv_active = true;
static volatile bool s_venue_offline = false;
static volatile bool s_improv_ble_enabled = true;
static portMUX_TYPE s_improv_lock = portMUX_INITIALIZER_UNLOCKED;
static uint8_t s_improv_state = IMPROV_STATE_AUTH_REQUIRED;
static uint8_t s_improv_error = IMPROV_ERR_NONE;
static char s_improv_ssid[33];
static char s_improv_sec[65];
static int g_saved_calls = 0;

static void improv_ble_notify_status(void) {{}}
static esp_err_t ff_net_wifi_save_nvs(const char *s, const char *p) {{
    (void)s; (void)p; g_saved_calls++; return ESP_OK;
}}
static esp_err_t esp_event_post(int base, int id, const void *d, size_t l, int t) {{
    (void)base; (void)id; (void)d; (void)l; (void)t; return ESP_OK;
}}

{fn_rpc_fail}
{fn_rpc}
{fn_auth}

int run_rpc_test(bool active, bool offline, bool authorize_first, uint8_t cmd, int *out_saved, uint8_t *out_err) {{
    s_improv_active = active;
    s_venue_offline = offline;
    s_improv_ble_enabled = true;
    s_improv_state = IMPROV_STATE_AUTH_REQUIRED;
    s_improv_error = IMPROV_ERR_NONE;
    g_saved_calls = 0;
    if (authorize_first) {{
        ff_net_wifi_improv_authorize();
    }}
    uint8_t pkt[16];
    size_t len = 0;
    if (cmd == IMPROV_CMD_IDENTIFY) {{
        pkt[0] = IMPROV_CMD_IDENTIFY; pkt[1] = 0; pkt[2] = IMPROV_CMD_IDENTIFY; len = 3;
    }} else {{
        pkt[0] = IMPROV_CMD_WIFI_SETTINGS; pkt[1] = 6;
        pkt[2] = 2; pkt[3] = 'O'; pkt[4] = 'K';
        pkt[5] = 2; pkt[6] = 'p'; pkt[7] = 'w';
        uint8_t sum = 0;
        for (size_t i = 0; i < 8; i++) sum = (uint8_t)(sum + pkt[i]);
        pkt[8] = sum;
        len = 9;
    }}
    int rc = improv_handle_rpc_command(pkt, len);
    *out_saved = g_saved_calls;
    *out_err = s_improv_error;
    return rc;
}}
"""
    so_path = tmp_path / "libff_improv.so"
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
    lib.run_rpc_test.argtypes = [
        ctypes.c_bool,
        ctypes.c_bool,
        ctypes.c_bool,
        ctypes.c_uint8,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint8),
    ]
    lib.run_rpc_test.restype = ctypes.c_int

    saved = ctypes.c_int(0)
    err = ctypes.c_uint8(0)
    # Without physical/serial authorization, WIFI_SETTINGS is refused with IMPROV_ERR_NOT_AUTHORIZED.
    assert (
        lib.run_rpc_test(True, False, False, 0x01, ctypes.byref(saved), ctypes.byref(err)) == 0x103
    )
    assert saved.value == 0
    assert err.value == 0x04
    # Identify remains allowed while awaiting authorization.
    assert lib.run_rpc_test(True, False, False, 0x02, ctypes.byref(saved), ctypes.byref(err)) == 0
    assert saved.value == 0
    # After explicit authorization, WIFI_SETTINGS succeeds and saves once.
    assert lib.run_rpc_test(True, False, True, 0x01, ctypes.byref(saved), ctypes.byref(err)) == 0
    assert saved.value == 1
    assert err.value == 0x00
    # Inactive (post-timeout) or venue-offline window refuses WIFI_SETTINGS even after authorize().
    assert (
        lib.run_rpc_test(False, False, True, 0x01, ctypes.byref(saved), ctypes.byref(err)) == 0x103
    )
    assert saved.value == 0
    assert lib.run_rpc_test(True, True, True, 0x01, ctypes.byref(saved), ctypes.byref(err)) == 0x103
    assert saved.value == 0
