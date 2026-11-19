"""Simulated UGV edge-relay gateway & `ffdelta-v1` delta OTA client (`V3-1`, `V3-2`).

Pure stdlib + `fleetforge.identity` / `fleetforge.simulator.device` ONLY (`test_simulator.py`
import-purity rule: zero imports from `fleetforge.config`, `fleetforge.db`, `fleetforge.api`,
`fleetforge.broker`, `sqlalchemy`, `fastapi`, `pydantic`, or `httpx`).
"""

from __future__ import annotations

import hashlib
import http.server
import re
import struct
import threading
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field

from fleetforge.identity import DEVICE_ID_RE
from fleetforge.simulator.device import (
    DEFAULT_SIGNING_PUBKEY_RING,
    _verify_artifact_sig,
)
from fleetforge.simulator.errors import SimulatorError

_ARTIFACT_BIN_RE = re.compile(r"\A/v1/artifact/([0-9a-f]{64})/bin\Z")

DELTA_MAGIC = b"FFDLTA01"
DELTA_HEADER_BYTES = 76
OP_END = 0x00
OP_COPY = 0x01
OP_INSERT = 0x02


def apply_ffdelta_v1(base: bytes, patch: bytes, *, expected_to_sha256: str) -> bytes:
    """Device-side `ffdelta-v1` patch verifier and reconstructor (`V3-2`)."""
    if len(patch) < DELTA_HEADER_BYTES + 1 or patch[:8] != DELTA_MAGIC:
        raise SimulatorError("invalid ffdelta-v1 patch header")
    from_digest = patch[8:40]
    to_digest = patch[40:72]
    (target_size,) = struct.unpack(">I", patch[72:76])

    if to_digest.hex() != expected_to_sha256:
        raise SimulatorError("delta header to_sha256 mismatch")
    if hashlib.sha256(base).digest() != from_digest:
        raise SimulatorError("active slot sha256 does not match delta from_sha256")

    out = bytearray()
    pos = DELTA_HEADER_BYTES
    saw_end = False
    while pos < len(patch):
        op = patch[pos]
        pos += 1
        if op == OP_END:
            saw_end = True
            break
        if op == OP_COPY:
            if pos + 8 > len(patch):
                raise SimulatorError("truncated OP_COPY")
            src_off, length = struct.unpack(">II", patch[pos : pos + 8])
            pos += 8
            if length == 0 or src_off + length > len(base) or len(out) + length > target_size:
                raise SimulatorError("OP_COPY out of bounds")
            out.extend(base[src_off : src_off + length])
        elif op == OP_INSERT:
            if pos + 4 > len(patch):
                raise SimulatorError("truncated OP_INSERT")
            (length,) = struct.unpack(">I", patch[pos : pos + 4])
            pos += 4
            if length == 0 or pos + length > len(patch) or len(out) + length > target_size:
                raise SimulatorError("OP_INSERT out of bounds")
            out.extend(patch[pos : pos + length])
            pos += length
        else:
            raise SimulatorError(f"unknown delta opcode 0x{op:02x}")

    if not saw_end or pos != len(patch) or len(out) != target_size:
        raise SimulatorError("delta stream ended with size mismatch")
    reconstructed = bytes(out)
    if hashlib.sha256(reconstructed).hexdigest() != expected_to_sha256:
        raise SimulatorError("reconstructed target sha256 mismatch")
    return reconstructed


def fetch_and_apply_stage(
    stage_cmd: dict[str, object],
    *,
    active_slot_bytes: bytes | None = None,
    pubkey_ring: tuple[str, ...] = DEFAULT_SIGNING_PUBKEY_RING,
    require_signature: bool = True,
    urlopen_fn: Callable[[str], bytes] | None = None,
) -> tuple[bytes, int, bool]:
    """Execute a `stage` download (preferring `artifact.delta` when `active_slot_bytes` is given).

    Returns `(verified_image_bytes, wire_bytes_downloaded, used_delta)`.
    """
    artifact = stage_cmd.get("artifact")
    if not isinstance(artifact, dict):
        raise SimulatorError("stage command missing artifact object")
    expected_sha = str(artifact.get("sha256") or "")
    sig = artifact.get("sig")

    def _get_bytes(url: str) -> bytes:
        if urlopen_fn is not None:
            return urlopen_fn(url)
        with urllib.request.urlopen(url, timeout=15.0) as resp:  # noqa: S310
            return bytes(resp.read())

    delta_obj = artifact.get("delta")
    if isinstance(delta_obj, dict) and active_slot_bytes is not None:
        delta_url = str(delta_obj.get("url") or "")
        patch_sha = str(delta_obj.get("patch_sha256") or "")
        from_sha = str(delta_obj.get("from_sha256") or "")
        if hashlib.sha256(active_slot_bytes).hexdigest() != from_sha:
            raise SimulatorError("active slot digest does not match stage delta.from_sha256")
        patch_bytes = _get_bytes(delta_url)
        if hashlib.sha256(patch_bytes).hexdigest() != patch_sha:
            raise SimulatorError("downloaded delta patch sha256 mismatch")
        reconstructed = apply_ffdelta_v1(
            active_slot_bytes,
            patch_bytes,
            expected_to_sha256=expected_sha,
        )
        used_delta = True
        downloaded_len = len(patch_bytes)
    else:
        full_url = str(artifact.get("url") or "")
        reconstructed = _get_bytes(full_url)
        if hashlib.sha256(reconstructed).hexdigest() != expected_sha:
            raise SimulatorError("downloaded artifact sha256 mismatch")
        used_delta = False
        downloaded_len = len(reconstructed)

    if isinstance(sig, str):
        if not _verify_artifact_sig(expected_sha, sig, pubkey_ring):
            raise SimulatorError("artifact ECDSA P-256 signature verification failed")
    elif require_signature:
        raise SimulatorError("artifact signature missing")

    return reconstructed, downloaded_len, used_delta


@dataclass
class EdgeRelayGateway:
    """Simulated mobile UGV edge-relay HTTP artifact cache for child drones (`V3-1`).

    Pulls each content-addressed `sha256` blob at most once via `upstream_fetcher(path_and_query)`
    (or HTTP `upstream_base_url`), verifies its SHA-256 digest before caching, and serves all
    subsequent child drone requests from local memory (including when disconnected from uplink).
    """

    gateway_id: str
    upstream_base_url: str = ""
    upstream_fetcher: Callable[[str], bytes] | None = None
    upstream_online: bool = True
    cache: dict[str, bytes] = field(default_factory=dict)
    upstream_fetches: int = 0
    child_hits: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _server: http.server.ThreadingHTTPServer | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        if not DEVICE_ID_RE.match(self.gateway_id):
            raise SimulatorError(f"gateway_id {self.gateway_id!r} must be 12 lowercase hex digits")

    @property
    def base_url(self) -> str:
        if self._server is None:
            raise SimulatorError("EdgeRelayGateway HTTP server has not been started")
        host, port = self._server.server_address[:2]
        host_str = host.decode("ascii") if isinstance(host, bytes) else str(host)
        return f"http://{host_str}:{port}"

    def cached_digests(self) -> list[str]:
        with self._lock:
            return sorted(self.cache.keys())

    def preload(self, sha256: str, data: bytes) -> None:
        if hashlib.sha256(data).hexdigest() != sha256:
            raise SimulatorError("refusing to cache corrupt blob in EdgeRelayGateway")
        with self._lock:
            self.cache[sha256] = data

    def resolve_or_pull(self, sha256: str, path_and_query: str) -> bytes:
        with self._lock:
            existing = self.cache.get(sha256)
            if existing is not None:
                self.child_hits += 1
                return existing

        if not self.upstream_online:
            raise OSError("UGV gateway is disconnected from upstream and artifact is not cached")

        if self.upstream_fetcher is not None:
            fetched = self.upstream_fetcher(path_and_query)
        elif self.upstream_base_url:
            target_url = f"{self.upstream_base_url.rstrip('/')}{path_and_query}"
            with urllib.request.urlopen(target_url, timeout=15.0) as resp:  # noqa: S310
                fetched = resp.read()
        else:
            raise OSError("no upstream configured for EdgeRelayGateway cache miss")

        if hashlib.sha256(fetched).hexdigest() != sha256:
            raise OSError("upstream returned corrupt bytes failing sha256 check")

        with self._lock:
            if sha256 not in self.cache:
                self.cache[sha256] = fetched
                self.upstream_fetches += 1
            self.child_hits += 1
            return self.cache[sha256]

    def start(self) -> str:
        gw = self

        class _Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                parsed = urllib.parse.urlsplit(self.path)
                match = _ARTIFACT_BIN_RE.match(parsed.path)
                if match is None:
                    self.send_error(404, "not found")
                    return
                sha256 = match.group(1)
                query = f"?{parsed.query}" if parsed.query else ""
                try:
                    blob = gw.resolve_or_pull(sha256, f"{parsed.path}{query}")
                except OSError as exc:
                    self.send_error(502, str(exc))
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/octet-stream")
                self.send_header("Content-Length", str(len(blob)))
                self.end_headers()
                self.wfile.write(blob)

            def log_message(self, _fmt: str, *_args: object) -> None:
                return

        self._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()
        return self.base_url

    def close(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
