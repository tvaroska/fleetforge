"""A download that stops making progress fails (R2-fw-5) — firmware tripwires, as text.

No host test can execute `agent/components/fleetforge/src/*.c`; the QEMU run (store proxy blackholed mid-download:
`failed` / `download stalled` 60-80 s after the last byte, otadata unchanged, the next
deploy runs) is the proof. What CAN be held here, on every `just test`, are the orderings
that make that true. Each one fails silently: the board stays safe on its old image, and
simply never takes another update until somebody power-cycles it.

* the stall budget is a bounded wall-clock number, at least two read timeouts long;
* the clock starts after `esp_https_ota_begin()` and before the perform loop, so the wait
  for the first body byte counts;
* the stall check runs before the loop's size-less `continue`, so a command without a size
  is guarded too;
* the stall branch aborts and never finishes, never writes otadata or the transaction
  record, and never logs the URL;
* the stall branch comes before the `download failed` branch (after the break `err` is still
  IN_PROGRESS, which would otherwise be misreported);
* `done:` still clears `s_running`, which is what frees the update slot.

Same idiom as `test_agent_txn.py`: comments are stripped, because they quote the very
spellings these tests forbid.
"""

import ctypes
import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from tests.agent_src import COMPONENT_SRC, find_c_function, read_c_family
from tests.agent_test_helpers import _code, _function_body

PERFORM_LOOP = "while ((err = esp_https_ota_perform(handle))"


def _ota_task() -> str:
    return _function_body(_code(read_c_family("ff_ota")), "ota_task")


def _define(source: str, name: str) -> int:
    match = re.search(rf"^#define {name} (\d+)\s*$", source, flags=re.MULTILINE)
    assert match is not None, f"no #define {name}"
    return int(match.group(1))


def _perform_loop_body(task: str) -> str:
    """The body of the perform while-loop, by brace matching."""
    start = task.index(PERFORM_LOOP)
    open_brace = task.index("{", start)
    depth = 0
    for index in range(open_brace, len(task)):
        if task[index] == "{":
            depth += 1
        elif task[index] == "}":
            depth -= 1
            if depth == 0:
                return task[open_brace + 1 : index]
    raise AssertionError("unbalanced braces in the perform loop")


def _stall_branch(task: str) -> str:
    start = task.index("if (stalled)")
    return task[start : task.index("goto done", start)]


def test_the_stall_budget_is_bounded_and_longer_than_two_reads() -> None:
    source = _code(
        (COMPONENT_SRC / "ff_ota_internal.h").read_text(encoding="utf-8")
        + "\n"
        + read_c_family("ff_ota")
    )
    stall = _define(source, "OTA_STALL_MS")
    timeout = _define(source, "OTA_HTTP_TIMEOUT_MS")
    assert stall >= 2 * timeout, "one slow read must never trip the guard"
    # A budget that drifts towards "forever" is the bug this task fixed.
    assert stall <= 120_000
    assert "_Static_assert(OTA_STALL_MS >= 2 * OTA_HTTP_TIMEOUT_MS" in source


def test_the_clock_starts_after_begin_and_before_the_loop() -> None:
    task = _ota_task()
    clock = task.index("esp_timer_get_time(")
    assert task.index("esp_https_ota_begin(") < clock < task.index(PERFORM_LOOP)


def test_a_sizeless_command_cannot_skip_the_stall_check() -> None:
    body = _perform_loop_body(_ota_task())
    assert "continue" in body
    assert body.index("esp_timer_get_time(") < body.index("continue")
    assert body.index("stalled = true") < body.index("continue")


def test_the_stall_branch_aborts_and_touches_nothing_else() -> None:
    branch = _stall_branch(_ota_task())
    assert "esp_https_ota_abort(handle)" in branch
    assert 'fail(cmd, "download stalled")' in branch
    for forbidden in (
        "ff_txn_save",
        "esp_https_ota_finish",
        "restore_boot_partition",
        "esp_ota_set_boot_partition",
        "cmd->url",
        "resolved",
    ):
        assert forbidden not in branch, forbidden


def test_a_stall_is_not_misreported_as_a_failed_download() -> None:
    task = _ota_task()
    assert task.index("if (stalled)") < task.index('fail(cmd, "download failed")')


def test_done_still_frees_the_update_slot() -> None:
    task = _ota_task()
    assert "s_running = false" in task[task.index("done:") :]


def test_a_resume_requests_the_rest_of_the_image(tmp_path: Path) -> None:
    """D-22: resuming a >200 KiB app OTA mid-stream sends open-ended `bytes=<offset>-` and reassembles the full image."""
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc is required for host firmware resume tests"

    raw = read_c_family("ff_ota")
    assert "OTA_RANGE_CHUNK_BYTES" not in raw
    assert "max_http_request_size" not in raw
    _, fn_resume = find_c_function("resume_range_perform")

    image = bytes((i * 131 + 17) % 256 for i in range(262_144))
    drop_offset = 70_000

    harness_c = f"""
#include <inttypes.h>
#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

typedef int esp_err_t;
#define ESP_OK 0
#define ESP_FAIL -1
#define ESP_ERR_INVALID_RESPONSE 0x108
#define ESP_ERR_HTTPS_OTA_IN_PROGRESS 0x9001
#define OTA_MAX_RETRIES 3
#define OTA_RETRY_BASE_MS 1000
#define OTA_STALL_MS 60000
#define pdMS_TO_TICKS(ms) (ms)

typedef void *esp_https_ota_handle_t;
typedef void *esp_http_client_handle_t;
typedef struct {{
    char cmd_id[64];
    uint32_t size;
}} ff_ota_cmd_t;

static const char *TAG = "ff-ota-test";
#define ESP_LOGW(tag, fmt, ...) do {{ (void)(tag); if (0) {{ (void)printf(fmt, ##__VA_ARGS__); }} }} while (0)
static esp_http_client_handle_t s_ota_http_client = (void *)1;
static char s_resume_range_hdr[48];
static char g_last_range_header[48];
static const uint8_t *g_origin_image = NULL;
static uint8_t *g_flash_slot = NULL;
static int g_image_len_read = 0;
static int g_http_cursor = 0;
static int g_total_image_size = 0;
static int g_range_end_limit = 0;
static int g_close_calls = 0;

static bool cancel_requested(void) {{ return false; }}
static void vTaskDelay(uint32_t ticks) {{ (void)ticks; }}
static int64_t esp_timer_get_time(void) {{ return 1000000; }}
static int esp_https_ota_get_image_len_read(esp_https_ota_handle_t h) {{
    (void)h;
    return g_image_len_read;
}}
static esp_err_t esp_http_client_close(esp_http_client_handle_t c) {{
    (void)c;
    g_close_calls++;
    return ESP_OK;
}}
static esp_err_t esp_http_client_set_header(esp_http_client_handle_t c, const char *k, const char *v) {{
    (void)c;
    if (strcmp(k, "Range") == 0) {{
        snprintf(g_last_range_header, sizeof(g_last_range_header), "%s", v);
        int start = 0, end = -1;
        if (sscanf(v, "bytes=%d-%d", &start, &end) == 2 && end >= start) {{
            g_http_cursor = start;
            g_range_end_limit = end + 1;
        }} else if (sscanf(v, "bytes=%d-", &start) == 1 && start >= 0) {{
            g_http_cursor = start;
            g_range_end_limit = g_total_image_size;
        }}
    }}
    return ESP_OK;
}}
static esp_err_t esp_http_client_open(esp_http_client_handle_t c, int len) {{ (void)c; (void)len; return ESP_OK; }}
static int64_t esp_http_client_fetch_headers(esp_http_client_handle_t c) {{ (void)c; return g_range_end_limit - g_http_cursor; }}
static int esp_http_client_get_status_code(esp_http_client_handle_t c) {{ (void)c; return 206; }}
static esp_err_t esp_https_ota_perform(esp_https_ota_handle_t h) {{
    (void)h;
    int remaining = g_range_end_limit - g_http_cursor;
    if (remaining <= 0) return ESP_OK;
    int step = remaining > 16384 ? 16384 : remaining;
    memcpy(g_flash_slot + g_image_len_read, g_origin_image + g_http_cursor, (size_t)step);
    g_http_cursor += step;
    g_image_len_read += step;
    return g_image_len_read < g_range_end_limit ? ESP_ERR_HTTPS_OTA_IN_PROGRESS : ESP_OK;
}}

{fn_resume}

int run_resume_case(const uint8_t *origin, uint8_t *flash_out, int start_offset, int total_size,
                    char *range_out, size_t range_cap, int *final_len) {{
    g_origin_image = origin;
    g_flash_slot = flash_out;
    memcpy(g_flash_slot, origin, (size_t)start_offset);
    g_image_len_read = start_offset;
    g_http_cursor = start_offset;
    g_total_image_size = total_size;
    g_range_end_limit = total_size;
    g_last_range_header[0] = '\\0';
    g_close_calls = 0;
    ff_ota_cmd_t cmd = {{ "cmd-resume", (uint32_t)total_size }};
    int last_len = start_offset;
    int64_t last_progress = 0;
    bool stalled = false;
    bool cancel_seen = false;
    esp_err_t rc = resume_range_perform((void *)1, &cmd, &last_len, &last_progress, &stalled, &cancel_seen);
    snprintf(range_out, range_cap, "%s", g_last_range_header);
    *final_len = g_image_len_read;
    return rc;
}}
"""
    so_path = tmp_path / "libff_resume.so"
    proc = subprocess.run(  # noqa: S603
        [
            gcc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
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
    lib.run_resume_case.argtypes = [
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.POINTER(ctypes.c_int),
    ]
    lib.run_resume_case.restype = ctypes.c_int

    flash_buf = ctypes.create_string_buffer(len(image))
    range_buf = ctypes.create_string_buffer(48)
    final_len = ctypes.c_int(0)
    rc = lib.run_resume_case(
        image,
        flash_buf,
        drop_offset,
        len(image),
        range_buf,
        48,
        ctypes.byref(final_len),
    )
    assert rc == 0
    assert range_buf.value == f"bytes={drop_offset}-".encode("ascii")
    assert final_len.value == len(image)
    assert len(image) > 200 * 1024
    assert hashlib.sha256(flash_buf.raw).hexdigest() == hashlib.sha256(image).hexdigest()


def test_cargo_resume_closes_client_on_unexpected_200(tmp_path: Path) -> None:
    """D-22: `run_cargo_ota` closes the HTTP client on an unexpected 200 retry instead of corrupting storage."""
    gcc = shutil.which("gcc")
    assert gcc is not None, "gcc is required for host cargo resume tests"

    _, fn_hex = find_c_function("hex_encode")
    _, fn_cargo = find_c_function("run_cargo_ota")

    payload = bytes((i * 31 + 7) % 256 for i in range(12_288))
    expected_sha = hashlib.sha256(payload).hexdigest()

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
#define OTA_RESOLVED_URL_MAX 256
#define OTA_READBACK_CHUNK 4096
#define OTA_HTTP_TIMEOUT_MS 20000
#define OTA_HTTP_RX_BUFFER 4096
#define OTA_HTTP_TX_BUFFER 2048
#define OTA_MAX_RETRIES 3
#define OTA_RETRY_BASE_MS 1000
#define HTTP_METHOD_GET 0
#define FF_STATUS_DOWNLOADING 1
#define FF_STATUS_VERIFYING 2
#define FF_STATUS_STAGED 3
#define FF_STATUS_APPLYING 4
#define FF_STATUS_PCT_NONE -1
#define pdMS_TO_TICKS(ms) (ms)

typedef struct {{
    char cmd_id[64];
    char url[128];
    char sha256[65];
    char sig[160];
    char type[32];
    char version[32];
    uint32_t size;
}} ff_ota_cmd_t;

typedef esp_err_t (*ff_cargo_write_fn_t)(size_t offset, const uint8_t *data, size_t len, void *ctx);
typedef esp_err_t (*ff_cargo_finish_fn_t)(size_t total, const char *sha256, const char *ver, void *ctx);
typedef struct {{
    char type[32];
    ff_cargo_write_fn_t write_cb;
    ff_cargo_finish_fn_t finish_cb;
    void *ctx;
}} ff_cargo_handler_t;

typedef struct {{
    const char *url;
    int method;
    void *crt_bundle_attach;
    int timeout_ms;
    bool keep_alive_enable;
    int buffer_size;
    int buffer_size_tx;
}} esp_http_client_config_t;

typedef struct {{
    bool open;
    int open_count;
    int status_code;
    size_t cursor;
    size_t drop_at;
    bool dropped_once;
}} mock_http_t;
typedef mock_http_t *esp_http_client_handle_t;

static void *esp_crt_bundle_attach = NULL;
static const uint8_t *g_src = NULL;
static size_t g_src_len = 0;
static uint8_t g_written[16384];
static size_t g_written_max = 0;
static int g_unexpected_200_writes = 0;
static int g_close_on_200 = 0;
static char g_last_fail[64];
static bool g_completed = false;
static uint8_t g_expected_digest[32];

static bool cancel_requested(void) {{ return false; }}
static void vTaskDelay(uint32_t ticks) {{ (void)ticks; }}
static void report_cancelled(const ff_ota_cmd_t *c) {{ (void)c; }}
static void fail(const ff_ota_cmd_t *c, const char *d) {{
    (void)c;
    snprintf(g_last_fail, sizeof(g_last_fail), "%s", d ? d : "");
}}
static void ff_mqtt_publish_status(const char *id, int st, int pct, const char *d) {{
    (void)id; (void)st; (void)pct; (void)d;
}}
static void ff_mqtt_complete_cargo(const char *id, const char *t, const char *v) {{
    (void)id; (void)t; (void)v;
    g_completed = true;
}}
static esp_err_t resolve_artifact_url(const char *url, char *out) {{
    snprintf(out, OTA_RESOLVED_URL_MAX, "%s", url);
    return ESP_OK;
}}

typedef struct {{ uint8_t buf[16384]; size_t len; }} mbedtls_sha256_context;
static void mbedtls_sha256_init(mbedtls_sha256_context *c) {{ memset(c, 0, sizeof(*c)); }}
static int mbedtls_sha256_starts(mbedtls_sha256_context *c, int is224) {{ (void)is224; c->len = 0; return 0; }}
static int mbedtls_sha256_update(mbedtls_sha256_context *c, const uint8_t *d, size_t l) {{
    if (c->len + l > sizeof(c->buf)) return -1;
    memcpy(c->buf + c->len, d, l);
    c->len += l;
    return 0;
}}
static int mbedtls_sha256_finish(mbedtls_sha256_context *c, uint8_t out[32]) {{
    if (c->len == g_src_len && memcmp(c->buf, g_src, g_src_len) == 0) {{
        memcpy(out, g_expected_digest, 32);
    }} else {{
        memset(out, 0xEE, 32);
    }}
    return 0;
}}
static void mbedtls_sha256_free(mbedtls_sha256_context *c) {{ (void)c; }}
static bool verify_artifact_signature(const uint8_t d[32], const char *s, const char **out) {{
    (void)d; (void)s; (void)out; return true;
}}

static mock_http_t g_client;
static esp_http_client_handle_t esp_http_client_init(const esp_http_client_config_t *cfg) {{
    (void)cfg;
    memset(&g_client, 0, sizeof(g_client));
    g_client.drop_at = 4096;
    return &g_client;
}}
static esp_err_t esp_http_client_set_header(esp_http_client_handle_t c, const char *k, const char *v) {{
    (void)k;
    unsigned off = 0;
    if (sscanf(v, "bytes=%u-", &off) == 1) {{
        c->cursor = off;
    }}
    return ESP_OK;
}}
static esp_err_t esp_http_client_open(esp_http_client_handle_t c, int len) {{
    (void)len;
    c->open = true;
    c->open_count++;
    if (c->open_count == 1) {{
        c->status_code = 200;
        c->cursor = 0;
    }} else if (c->open_count == 2) {{
        /* Simulate origin ignoring Range and replying 200 OK from byte 0! */
        c->status_code = 200;
        c->cursor = 0;
    }} else {{
        c->status_code = 206;
    }}
    return ESP_OK;
}}
static int64_t esp_http_client_fetch_headers(esp_http_client_handle_t c) {{
    return c->open ? (int64_t)(g_src_len - c->cursor) : -1;
}}
static int esp_http_client_get_status_code(esp_http_client_handle_t c) {{ return c->status_code; }}
static esp_err_t esp_http_client_close(esp_http_client_handle_t c) {{
    if (c->open && c->open_count == 2 && c->status_code == 200) {{
        g_close_on_200++;
    }}
    c->open = false;
    return ESP_OK;
}}
static esp_err_t esp_http_client_cleanup(esp_http_client_handle_t c) {{ (void)c; return ESP_OK; }}
static bool esp_http_client_is_complete_data_received(esp_http_client_handle_t c) {{
    return c->open && c->cursor >= g_src_len;
}}
static int esp_http_client_read(esp_http_client_handle_t c, char *buf, int len) {{
    if (!c->open) return -1;
    if (c->open_count == 2 && c->status_code == 200) {{
        /* Buggy caller did not close the unexpected 200 response: serve byte 0! */
        g_unexpected_200_writes++;
        int n = len < (int)g_src_len ? len : (int)g_src_len;
        memcpy(buf, g_src, (size_t)n);
        c->cursor += (size_t)n;
        return n;
    }}
    if (!c->dropped_once && c->cursor >= c->drop_at) {{
        c->dropped_once = true;
        return -1;
    }}
    size_t rem = g_src_len - c->cursor;
    if (rem == 0) return 0;
    size_t n = rem < (size_t)len ? rem : (size_t)len;
    if (!c->dropped_once && c->cursor + n > c->drop_at) {{
        n = c->drop_at - c->cursor;
    }}
    memcpy(buf, g_src + c->cursor, n);
    c->cursor += n;
    return (int)n;
}}

static esp_err_t test_write_cb(size_t offset, const uint8_t *data, size_t len, void *ctx) {{
    (void)ctx;
    if (offset + len > sizeof(g_written)) return ESP_FAIL;
    memcpy(g_written + offset, data, len);
    if (offset + len > g_written_max) g_written_max = offset + len;
    return ESP_OK;
}}
static esp_err_t test_finish_cb(size_t total, const char *sha, const char *ver, void *ctx) {{
    (void)total; (void)sha; (void)ver; (void)ctx;
    return ESP_OK;
}}
static ff_cargo_handler_t g_handler = {{ "mcu_fw", test_write_cb, test_finish_cb, NULL }};
static const ff_cargo_handler_t *find_cargo_handler(const char *t) {{
    return strcmp(t, "mcu_fw") == 0 ? &g_handler : NULL;
}}

{fn_hex}
{fn_cargo}

int run_cargo_200_case(const uint8_t *src, size_t len, const uint8_t digest[32], const char *sha_hex,
                       int *close_on_200, int *bad_reads, int *completed, uint8_t *out_buf) {{
    g_src = src;
    g_src_len = len;
    memcpy(g_expected_digest, digest, 32);
    memset(g_written, 0, sizeof(g_written));
    g_written_max = 0;
    g_unexpected_200_writes = 0;
    g_close_on_200 = 0;
    g_last_fail[0] = '\\0';
    g_completed = false;

    ff_ota_cmd_t cmd = {{0}};
    snprintf(cmd.cmd_id, sizeof(cmd.cmd_id), "cmd-cargo");
    snprintf(cmd.url, sizeof(cmd.url), "https://example.test/cargo.bin");
    snprintf(cmd.sha256, sizeof(cmd.sha256), "%s", sha_hex);
    snprintf(cmd.type, sizeof(cmd.type), "mcu_fw");
    snprintf(cmd.version, sizeof(cmd.version), "1.0.0");
    cmd.size = (uint32_t)len;

    run_cargo_ota(&cmd);
    *close_on_200 = g_close_on_200;
    *bad_reads = g_unexpected_200_writes;
    *completed = g_completed ? 1 : 0;
    memcpy(out_buf, g_written, len);
    return (int)g_written_max;
}}
"""
    so_path = tmp_path / "libff_cargo_resume.so"
    proc = subprocess.run(  # noqa: S603
        [
            gcc,
            "-O2",
            "-Wall",
            "-Wextra",
            "-Werror",
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
    lib.run_cargo_200_case.argtypes = [
        ctypes.c_char_p,
        ctypes.c_size_t,
        ctypes.c_char_p,
        ctypes.c_char_p,
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.POINTER(ctypes.c_int),
        ctypes.c_char_p,
    ]
    lib.run_cargo_200_case.restype = ctypes.c_int

    close_on_200 = ctypes.c_int(0)
    bad_reads = ctypes.c_int(0)
    completed = ctypes.c_int(0)
    out_buf = ctypes.create_string_buffer(len(payload))
    written = lib.run_cargo_200_case(
        payload,
        len(payload),
        hashlib.sha256(payload).digest(),
        expected_sha.encode("ascii"),
        ctypes.byref(close_on_200),
        ctypes.byref(bad_reads),
        ctypes.byref(completed),
        out_buf,
    )
    assert close_on_200.value == 1, "run_cargo_ota must close HTTP client on unexpected 200 retry"
    assert bad_reads.value == 0, "unexpected 200 body must never be streamed into cargo storage"
    assert completed.value == 1
    assert written == len(payload)
    assert out_buf.raw == payload
