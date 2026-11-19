"""Shared secret key contract between Python API schema and C firmware (`D-7`)."""

from __future__ import annotations

import ctypes
import re
import shutil
import subprocess
from pathlib import Path

from fleetforge.api.schemas import SECRET_KEY_PATTERN
from tests.agent_src import COMPONENT_SRC, extract_c_function

FF_SEC_C = COMPONENT_SRC / "ff_sec.c"


def _compile_sec_harness(tmp_path: Path) -> ctypes.CDLL:
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc must be available to compile ff_sec.c contract harness"

    sec_raw = FF_SEC_C.read_text(encoding="utf-8")
    fn_valid = extract_c_function(sec_raw, "valid_secret_key")
    fn_store = extract_c_function(sec_raw, "store_secrets_json")

    harness_c = f"""
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

#define FF_SEC_NAMESPACE "ff_sec"
#define FF_SEC_KEY_REV "_rev"
#define SECRETS_MAX_STALE_KEYS 32

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_ERR_INVALID_ARG 0x102
#define ESP_ERR_NOT_FOUND 0x105

static const char *TAG = "ff-sec-test";
#define ESP_LOGE(tag, fmt, ...) ((void)(tag))
static const char *esp_err_to_name(esp_err_t e) {{ (void)e; return "err"; }}

static size_t ff_test_strlcpy(char *dst, const char *src, size_t siz) {{
    size_t slen = strlen(src);
    if (siz > 0) {{
        size_t copy = slen >= siz ? siz - 1 : slen;
        memcpy(dst, src, copy);
        dst[copy] = '\\0';
    }}
    return slen;
}}
#undef strlcpy
#define strlcpy ff_test_strlcpy

/* Minimal cJSON stub for object iteration */
typedef struct cJSON {{
    struct cJSON *next;
    struct cJSON *child;
    int is_obj;
    int is_str;
    const char *string;
    const char *valuestring;
}} cJSON;

static bool cJSON_IsObject(const cJSON *item) {{ return item != NULL && item->is_obj != 0; }}
static bool cJSON_IsString(const cJSON *item) {{ return item != NULL && item->is_str != 0; }}
static bool cJSON_HasObjectItem(const cJSON *obj, const char *key) {{
    for (const cJSON *c = obj ? obj->child : NULL; c != NULL; c = c->next) {{
        if (c->string && strcmp(c->string, key) == 0) return true;
    }}
    return false;
}}
#define cJSON_ArrayForEach(element, array) \\
    for (element = (array != NULL) ? (array)->child : NULL; element != NULL; element = element->next)

/* Minimal NVS stub recording written count */
typedef int nvs_handle_t;
#define NVS_READWRITE 1
#define NVS_TYPE_STR 2
typedef void *nvs_iterator_t;
typedef struct {{ char key[16]; }} nvs_entry_info_t;

static int g_nvs_writes = 0;
static uint32_t g_nvs_rev = 0;

static esp_err_t nvs_open(const char *ns, int mode, nvs_handle_t *out) {{
    (void)ns; (void)mode; *out = 1; return ESP_OK;
}}
static void nvs_close(nvs_handle_t h) {{ (void)h; }}
static esp_err_t nvs_set_str(nvs_handle_t h, const char *k, const char *v) {{
    (void)h; (void)k; (void)v; g_nvs_writes++; return ESP_OK;
}}
static esp_err_t nvs_set_u32(nvs_handle_t h, const char *k, uint32_t v) {{
    (void)h; (void)k; g_nvs_rev = v; return ESP_OK;
}}
static esp_err_t nvs_commit(nvs_handle_t h) {{ (void)h; return ESP_OK; }}
static esp_err_t nvs_entry_find_in_handle(nvs_handle_t h, int t, nvs_iterator_t *it) {{
    (void)h; (void)t; *it = NULL; return ESP_ERR_NOT_FOUND;
}}
static void nvs_entry_info(nvs_iterator_t it, nvs_entry_info_t *info) {{ (void)it; info->key[0] = '\\0'; }}
static esp_err_t nvs_entry_next(nvs_iterator_t *it) {{ *it = NULL; return ESP_ERR_NOT_FOUND; }}
static void nvs_release_iterator(nvs_iterator_t it) {{ (void)it; }}
static esp_err_t nvs_erase_key(nvs_handle_t h, const char *k) {{ (void)h; (void)k; return ESP_OK; }}

{fn_valid}
{fn_store}

bool exported_valid_secret_key(const char *key) {{
    return valid_secret_key(key);
}}

int exported_store_two_keys(const char *k1, const char *v1, const char *k2, const char *v2,
                            int k2_is_str, uint32_t rev, int *writes_out, uint32_t *rev_out) {{
    g_nvs_writes = 0;
    g_nvs_rev = 0;
    cJSON item2 = {{ NULL, NULL, 0, k2_is_str, k2, v2 }};
    cJSON item1 = {{ &item2, NULL, 0, 1, k1, v1 }};
    cJSON root = {{ NULL, &item1, 1, 0, NULL, NULL }};
    esp_err_t err = store_secrets_json(&root, rev);
    *writes_out = g_nvs_writes;
    *rev_out = g_nvs_rev;
    return (int)err;
}}
"""
    so_path = tmp_path / "libff_sec.so"
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
    lib.exported_valid_secret_key.argtypes = [ctypes.c_char_p]
    lib.exported_valid_secret_key.restype = ctypes.c_bool
    lib.exported_store_two_keys.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_uint32),
    ]
    lib.exported_store_two_keys.restype = ctypes.c_int
    return lib


def test_server_key_pattern_equals_firmware_rule(tmp_path: Path) -> None:
    """D-7: `SECRET_KEY_PATTERN` and C `valid_secret_key` accept/reject the exact same keys,
    and `store_secrets_json` fails closed without advancing `_rev` if any entry is invalid."""
    lib = _compile_sec_harness(tmp_path)
    py_re = re.compile(SECRET_KEY_PATTERN)

    candidates = [
        "",
        "a",
        "Z",
        "0",
        "api_url",
        "api.url",
        "v1.key-2_ok",
        "A1._-b2._-c3456",  # 15 chars (max)
        "A1._-b2._-c34567",  # 16 chars (too long)
        "_rev",
        "_leading",
        ".leading",
        "-leading",
        "trailing.",
        "trailing-",
        "trailing_",
        "has space",
        "slash/key",
        "colon:key",
        "semi;key",
        "dollar$key",
        "utf8\xc3\xa9",
    ]
    for key in candidates:
        py_ok = bool(py_re.fullmatch(key))
        c_ok = bool(lib.exported_valid_secret_key(key.encode("utf-8", errors="surrogateescape")))
        assert py_ok == c_ok, f"mismatch on key {key!r}: server={py_ok}, firmware={c_ok}"

    # Valid dotted + hyphenated keys store cleanly and advance `_rev`.
    writes = ctypes.c_int(0)
    rev_out = ctypes.c_uint32(0)
    rc_ok = lib.exported_store_two_keys(
        b"api.url",
        b"https://api.example.com",
        b"mqtt-token_1",
        b"s3cr3t",
        1,
        7,
        ctypes.byref(writes),
        ctypes.byref(rev_out),
    )
    assert rc_ok == 0
    assert writes.value == 2
    assert rev_out.value == 7

    # Any invalid key (`_rev`) aborts the entire sync (`ESP_ERR_INVALID_ARG`) with 0 writes.
    rc_bad_key = lib.exported_store_two_keys(
        b"api.url",
        b"https://api.example.com",
        b"_rev",
        b"spoof",
        1,
        8,
        ctypes.byref(writes),
        ctypes.byref(rev_out),
    )
    assert rc_bad_key == 0x102
    assert writes.value == 0
    assert rev_out.value == 0

    # Any non-string JSON value also aborts the sync with 0 writes.
    rc_bad_val = lib.exported_store_two_keys(
        b"api.url",
        b"https://api.example.com",
        b"timeout.ms",
        b"",
        0,
        9,
        ctypes.byref(writes),
        ctypes.byref(rev_out),
    )
    assert rc_bad_val == 0x102
    assert writes.value == 0
    assert rev_out.value == 0
