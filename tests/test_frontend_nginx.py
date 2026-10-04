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

from fleetforge.firmware.manifest import SUPPORTED_LAYOUTS

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
    assert limit >= max(SUPPORTED_LAYOUTS.values())
    assert "proxy_pass http://api:8000" in block


def test_the_general_api_block_keeps_the_default_body_limit() -> None:
    # Public enroll/login endpoints have no reason to accept megabytes.
    assert _body_limit(_block("location /v1/ {")) is None
