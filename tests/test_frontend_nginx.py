"""The production nginx must let a firmware image through to `POST /v1/artifact`.

nginx's default `client_max_body_size` is 1m and `frontend/nginx.conf` once did not set
it, so any image over 1 MiB got nginx's own HTML 413 and never reached the API. The old
upload runbook only worked because the esp32s3 agent (998,672 B) happens to fit; the c6
agent (1,106,384 B) and any user build near the 1,966,080 B OTA slot did not. The dev
stack hides this completely (the Vite proxy has no limit), so nothing but this file
notices a tidy-up that drops the override. Read as text: there is no nginx in the suite.
"""

import re
from pathlib import Path

from fleetforge.firmware.manifest import BUILTIN_LAYOUTS
from fleetforge.partition_profiles import MAX_PROFILE_SLOT_SIZE

CONF = (Path(__file__).resolve().parent.parent / "frontend" / "nginx.conf").read_text()

_UNITS = {"": 1, "k": 1024, "m": 1024 * 1024}


def _block(opening: str) -> str:
    """The body of the `location` whose header is exactly `opening`, braces matched."""
    start = CONF.index(opening)
    depth = 0
    for i in range(CONF.index("{", start), len(CONF)):
        if CONF[i] == "{":
            depth += 1
        elif CONF[i] == "}":
            depth -= 1
            if depth == 0:
                return CONF[start : i + 1]
    raise AssertionError(f"unterminated block: {opening}")


def _body_limit(block: str) -> int | None:
    match = re.search(r"^\s*client_max_body_size\s+(\d+)([kKmM]?)\s*;", block, re.MULTILINE)
    if match is None:
        return None
    return int(match.group(1)) * _UNITS[match.group(2).lower()]


def test_the_upload_route_takes_at_least_one_ota_slot() -> None:
    block = _block("location = /v1/artifact")
    limit = _body_limit(block)
    assert limit is not None, "no client_max_body_size: nginx falls back to 1m"
    assert limit >= max(p.ota_slot_size for p in BUILTIN_LAYOUTS.values())
    # R3-be-2 D8: an operator profile's slot may be up to MAX_PROFILE_SLOT_SIZE, and an upload
    # that size must reach the API rather than nginx's own HTML 413.
    assert limit >= MAX_PROFILE_SLOT_SIZE
    assert "proxy_pass http://api:8000" in block


def test_the_general_api_block_caps_body_size_below_artifact_uploads() -> None:
    """D-11: `/v1/` sets an explicit bounded `client_max_body_size` <= 2 MiB."""
    limit = _body_limit(_block("location /v1/ {"))
    assert limit is not None, "/v1/ should set an explicit client_max_body_size"
    assert 1024 * 1024 <= limit <= 2 * 1024 * 1024


def test_security_headers_cover_server_assets_and_shell_and_artifact_logs_omit_query() -> None:
    """D-11: HSTS + security headers at server scope (and repeated alongside Cache-Control),
    and `/v1/artifact/` access logging strips signed query strings via `$uri`."""
    required = (
        'add_header Strict-Transport-Security "max-age=63072000; includeSubDomains" always;',
        'add_header X-Content-Type-Options "nosniff" always;',
        'add_header X-Frame-Options "DENY" always;',
        'add_header Referrer-Policy "strict-origin-when-cross-origin" always;',
        "add_header Content-Security-Policy ",
    )
    server_header_region = CONF[CONF.index("server {") : CONF.index("location = /nginx-health")]
    for block_name, block in (
        ("server", server_header_region),
        ("location /assets/", _block("location /assets/ {")),
        ("location /", _block("location / {")),
    ):
        for header in required:
            assert header in block, f"missing {header!r} in {block_name}"

    assert "log_format json_combined_no_query" in CONF
    assert "$request_method $uri $server_protocol" in CONF
    art_dl = _block("location ^~ /v1/artifact/ {")
    assert "json_combined_no_query" in art_dl
    assert "proxy_pass http://api:8000" in art_dl


def _css_block(css: str, header: str) -> str:
    start = css.index(header)
    depth = 0
    for i in range(css.index("{", start), len(css)):
        if css[i] == "{":
            depth += 1
        elif css[i] == "}":
            depth -= 1
            if depth == 0:
                return css[start : i + 1]
    raise AssertionError(f"unterminated CSS block: {header}")


def test_workbench_fleet_table_scroll_containment_and_sticky_header_clearance() -> None:
    """`R-095` (`missing_unit_case`): prevent Fleet table overflow under `.device-inspector`
    or sticky `.status-strip` pointer interception in Chromium Playwright viewports."""
    repo_root = Path(__file__).resolve().parent.parent
    css = (repo_root / "frontend" / "src" / "index.css").read_text(encoding="utf-8")
    fleet_tsx = (repo_root / "frontend" / "src" / "FleetView.tsx").read_text(encoding="utf-8")

    root_block = _css_block(css, ":root {")
    assert "scroll-padding-top:" in root_block

    assert '<div className="fleet-table-scroll">' in fleet_tsx
    scroll_block = _css_block(css, ".fleet-table-scroll {")
    assert "overflow-x: auto;" in scroll_block

    card_block = _css_block(css, ".workbench-master .console-card {")
    assert "min-width: 0;" in card_block
    assert "overflow-x: visible;" in card_block

    assert "min-width: 0;" in _css_block(css, ".workbench-master,\n.workbench-inspector-col {")
    assert "max-width:" in _css_block(css, "[data-testid='deploy-cell'] select {")
    assert "@media (min-width: 1100px)" not in css
    wide_split = _css_block(css, "@media (min-width: 1520px) {")
    assert ".workbench-split" in wide_split
    assert "position: sticky;" in wide_split
