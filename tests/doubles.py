"""Shared test doubles and simulator harnesses (`T-11`).

Consolidates `StubSimRunner`, `StubBuildRunner`, `FakeMqttClient`, `FakeDynsecSession`,
`RecordingProvisioner`, `FakeFirebaseAuthClient`, and pure simulator session helpers so test
modules never import across `tests.test_*`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import time
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, Literal

from fleetforge.auth.firebase import (
    FirebaseAuthClient,
    FirebaseAuthError,
    FirebaseIdentity,
    FirebaseInvalidCredentials,
)
from fleetforge.broker import BrokerProvisioningError
from fleetforge.broker.dynsec import DynsecResponse, DynsecSession, parse_response
from fleetforge.builds import BuildError, LogSink
from fleetforge.clock import now_utc
from fleetforge.lib_marker import MAGIC, find_marker
from fleetforge.merged_image import detect_merged
from fleetforge.simulation import SimResult, SimStatus
from fleetforge.simulator.device import (
    LINK_FAST,
    SAFE_WINDOW_AUTO,
    STAGE_WALK,
    STATUS,
    DeviceIdentity,
    LinkProfile,
    StageRunner,
    run_session,
    up_topic,
)
from fleetforge.simulator.state import Credential

ESP_IMAGE_MAGIC = 0xE9
MIN_ESP_APP_BYTES = 32

SECRET = "brk-pw-2f9d1c-DO-NOT-LOG"  # noqa: S105
FIRMWARE = b"\xe9\x06\x02\x20" + b"firmware bytes, opaque to the board" * 29
FIRMWARE_SHA256 = hashlib.sha256(FIRMWARE).hexdigest()
SIGNED_URL = (
    f"https://storage.invalid/blobs/sha256/{FIRMWARE_SHA256}"
    "?X-Goog-Signature=deadbeefdeadbeef&X-Goog-Expires=1800"
)


class StubSimRunner:
    """Explicit test double for `SimRunner` (never shipped in `src/fleetforge`)."""

    runner_name = "test-stub-sim"

    async def simulate(
        self,
        *,
        sha256: str,
        target: str,
        image: bytes,
        partition_layout: str | None = None,
        artifact_type: str = "app",
    ) -> SimResult:
        _ = (sha256, partition_layout)
        started = time.monotonic()
        ran_at = now_utc()

        if artifact_type != "app":
            return SimResult(
                status=SimStatus.SKIPPED,
                detail=f"non-app cargo ({artifact_type}) does not boot directly",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if len(image) < MIN_ESP_APP_BYTES or image[0] != ESP_IMAGE_MAGIC:
            return SimResult(
                status=SimStatus.FAILED,
                detail=(
                    f"invalid {target} app boot header: expected 0xE9 magic and >= {MIN_ESP_APP_BYTES} bytes"
                ),
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if detect_merged(image) is not None:
            return SimResult(
                status=SimStatus.FAILED,
                detail="merged full-flash image cannot boot from an OTA app slot",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if b"FF_SIM_BOOT_CRASH" in image or b"FF_SIM_PANIC" in image:
            return SimResult(
                status=SimStatus.FAILED,
                detail="test boot panic marker detected",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        if b"FF_SIM_SELFTEST_FAIL" in image:
            return SimResult(
                status=SimStatus.FAILED,
                detail="test self-test failure marker detected",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        marker = find_marker(image)
        if marker is None:
            return SimResult(
                status=SimStatus.FAILED,
                detail="missing FleetForge OTA library marker",
                ran_at=ran_at,
                runner=self.runner_name,
                duration_ms=int((time.monotonic() - started) * 1000),
            )

        return SimResult(
            status=SimStatus.PASSED,
            detail=f"stub simulated {target} image (libfleetforge {marker.lib_version})",
            ran_at=ran_at,
            runner=self.runner_name,
            duration_ms=int((time.monotonic() - started) * 1000),
        )


class StubBuildRunner:
    """Explicit test double for `BuildRunner` (never shipped in `src/fleetforge`)."""

    async def compile_source(
        self,
        *,
        work_dir: Path,
        target: str,
        partition_layout: str,
        idf_image: str,
        on_log: LogSink | None = None,
    ) -> bytes:
        if on_log is not None:
            await on_log(f"[1/3] Configuring target {target} ({partition_layout}) on {idf_image}\n")

        sources = sorted(p for p in work_dir.rglob("*") if p.is_file())
        if not sources:
            raise BuildError("repository checkout contains no source files")

        combined = hashlib.sha256()
        for src in sources:
            content = src.read_bytes()
            if b"FF_BUILD_FAIL" in content:
                if on_log is not None:
                    await on_log(f"error: compilation failed in {src.relative_to(work_dir)}\n")
                raise BuildError(f"compile error in {src.relative_to(work_dir)}")
            combined.update(content)

        if on_log is not None:
            await on_log(f"[2/3] Compiling {len(sources)} source file(s) for {target}\n")
            await on_log("[3/3] Linking ESP-IDF app image with libfleetforge marker\n")

        marker = MAGIC + b"\x01\x00\x00\x00" + b"0.6.0\x00".ljust(32, b"\x00") + b"\x00" * 12
        meta = f"{target}:{partition_layout}:{idf_image}:{combined.hexdigest()}".encode("ascii")
        return b"\xe9\x00\x02\x20" + marker + meta + (b"\x00" * 64)


class FakeMqttClient:
    """Records MQTT publishes, or raises a scripted failure on connect."""

    def __init__(self, fail_with: Exception | None = None) -> None:
        self.fail_with = fail_with
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.entered = 0

    def __call__(self) -> FakeMqttClient:
        return self

    async def __aenter__(self) -> FakeMqttClient:
        self.entered += 1
        if self.fail_with is not None:
            raise self.fail_with
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def publish(self, topic: str, payload: bytes, *, qos: int, retain: bool) -> None:
        self.published.append((topic, payload, qos, retain))


class FakeDynsecSession:
    """Records `$CONTROL/dynamic-security/v1` commands and answers from scripted replies."""

    def __init__(
        self,
        replies: list[bytes | Exception],
        *,
        clear_fail_with: Exception | None = None,
    ) -> None:
        self.replies = list(replies)
        self.commands: list[dict[str, Any]] = []
        self.cleared_retained: list[tuple[str, str]] = []
        self.clear_fail_with = clear_fail_with

    async def command(
        self, payload: dict[str, Any], *, expect: str, correlation: str
    ) -> DynsecResponse:
        self.commands.append(payload)
        if not self.replies:
            raise BrokerProvisioningError(f"dynsec connection closed while awaiting {expect}")
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        body = json.loads(reply)
        body["responses"][0]["correlationData"] = correlation
        return parse_response(json.dumps(body).encode(), expect=expect, correlation=correlation)

    async def clear_retained_up_topics(self, device_id: str, password: str) -> None:
        if self.clear_fail_with is not None:
            raise self.clear_fail_with
        self.cleared_retained.append((device_id, password))

    def __call__(self) -> FakeDynsecSession:
        return self

    async def __aenter__(self) -> DynsecSession:
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None


class RecordingProvisioner:
    """`BrokerProvisioner` double recording `ensure_client` and `remove_client` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.removed: list[str] = []
        self.result = True
        self.error: Exception | None = None

    async def ensure_client(self, device_id: str, password: str) -> bool:
        self.calls.append((device_id, password))
        if self.error is not None:
            raise self.error
        return self.result

    async def remove_client(self, device_id: str) -> bool:
        self.removed.append(device_id)
        if self.error is not None:
            raise self.error
        return self.result


class FakeFirebaseAuthClient:
    """Port-checked `FirebaseAuthClient` double matching `LiveFirebaseAuthClient` contracts."""

    def __init__(self) -> None:
        self.accounts: dict[str, tuple[str, FirebaseIdentity]] = {}
        self.tokens: dict[str, FirebaseIdentity] = {}
        self.oob_codes: dict[str, str] = {}
        self.disabled_emails: set[str] = set()
        self.valid_since: dict[str, int] = {}
        self.reset_emails_sent: list[str] = []
        self.continue_urls: list[str | None] = []
        self.accounts_created: list[str] = []
        self.fail_create = False
        self.fail_reset = False

    def register(
        self,
        *,
        email: str,
        password: str,
        uid: str,
        id_token: str,
        display_name: str | None = None,
        email_verified: bool = True,
        auth_time: int | None = None,
        disabled: bool = False,
        valid_since: int | None = None,
    ) -> None:
        key = email.strip().lower()
        resolved_auth_time = auth_time if auth_time is not None else int(time.time()) - 30
        ident = FirebaseIdentity(
            uid=uid,
            email=key,
            email_verified=email_verified,
            display_name=display_name,
            auth_time=resolved_auth_time,
        )
        self.accounts[key] = (password, ident)
        self.tokens[id_token] = ident
        if disabled:
            self.disabled_emails.add(key)
        else:
            self.disabled_emails.discard(key)
        if valid_since is not None:
            self.valid_since[key] = valid_since
        else:
            self.valid_since.pop(key, None)

    async def verify_id_token(
        self,
        token: str,
        project_id: str,
        *,
        api_key: str | None = None,
        check_revoked: bool = True,
    ) -> FirebaseIdentity:
        _ = project_id
        if check_revoked and not api_key:
            raise FirebaseAuthError("revocation check needs FIREBASE_WEB_API_KEY")
        ident = self.tokens.get(token)
        if ident is None:
            raise FirebaseInvalidCredentials("bad token")
        if not check_revoked:
            return ident
        if ident.email in self.disabled_emails:
            raise FirebaseInvalidCredentials("Firebase account is disabled")
        cutoff = self.valid_since.get(ident.email)
        if cutoff is not None and (ident.auth_time is None or ident.auth_time < cutoff):
            raise FirebaseInvalidCredentials("Firebase ID token has been revoked")
        account_entry = self.accounts.get(ident.email)
        live_verified = account_entry[1].email_verified if account_entry is not None else True
        return dataclasses.replace(
            ident,
            email_verified=ident.email_verified and live_verified,
        )

    async def sign_in_with_password(
        self,
        email: str,
        password: str,
        *,
        api_key: str,
        project_id: str | None = None,
    ) -> FirebaseIdentity:
        _ = (api_key, project_id)
        key = email.strip().lower()
        entry = self.accounts.get(key)
        if entry is None or entry[0] != password or key in self.disabled_emails:
            raise FirebaseInvalidCredentials("bad email or password")
        return entry[1]

    async def create_account(self, email: str, *, api_key: str) -> bool:
        _ = api_key
        if self.fail_create:
            raise FirebaseAuthError("signUp failed")
        key = email.strip().lower()
        self.accounts_created.append(key)
        if key in self.accounts:
            return False
        placeholder = FirebaseIdentity(uid=f"fb-uid-{uuid.uuid4().hex}", email=key)
        self.accounts[key] = (uuid.uuid4().hex, placeholder)
        return True

    async def send_password_reset(
        self, email: str, *, api_key: str, continue_url: str | None = None
    ) -> None:
        _ = api_key
        if self.fail_reset:
            raise FirebaseAuthError("sendOobCode failed")
        key = email.strip().lower()
        if key not in self.accounts:
            raise FirebaseInvalidCredentials("EMAIL_NOT_FOUND")
        self.reset_emails_sent.append(key)
        self.continue_urls.append(continue_url)
        self.oob_codes[f"oob-for-{key}"] = key

    async def reset_password(
        self,
        oob_code: str,
        new_password: str,
        *,
        api_key: str,
        project_id: str | None = None,
    ) -> FirebaseIdentity:
        _ = (api_key, project_id)
        email = self.oob_codes.pop(oob_code.strip(), None)
        if email is None or email not in self.accounts or email in self.disabled_emails:
            raise FirebaseInvalidCredentials("INVALID_OOB_CODE")
        _, old_ident = self.accounts[email]
        verified_ident = FirebaseIdentity(
            uid=old_ident.uid,
            email=old_ident.email,
            email_verified=True,
            display_name=old_ident.display_name,
            auth_time=int(time.time()),
        )
        self.accounts[email] = (new_password, verified_ident)
        return verified_ident


def _assert_firebase_port_conformance() -> FirebaseAuthClient:
    return FakeFirebaseAuthClient()


def credential_for(device_id: str, password: str = SECRET) -> Credential:
    return Credential(
        device_id=device_id,
        mqtt_username=device_id,
        mqtt_password=password,
        api_base="http://localhost:8080",
        enrolled_at="2026-01-01T00:00:00Z",
    )


class FakeMessage:
    """Minimal `aiomqtt.Message` stand-in with `.topic.value` and `.payload`."""

    def __init__(self, topic: str, payload: bytes) -> None:
        self.topic = type("Topic", (), {"value": topic})()
        self.payload = payload


async def _drain(inbox: asyncio.Queue[FakeMessage]) -> AsyncIterator[FakeMessage]:
    while True:
        yield await inbox.get()


class FakeClient:
    """Recording stand-in for `aiomqtt.Client` used by simulator unit suites."""

    def __init__(self) -> None:
        self.published: list[tuple[str, bytes, int, bool]] = []
        self.subscribed: list[tuple[str, int]] = []
        self.calls: list[str] = []
        self.enters = 0
        self.inbox: asyncio.Queue[FakeMessage] = asyncio.Queue()
        self.messages = _drain(self.inbox)

    async def __aenter__(self) -> FakeClient:
        self.enters += 1
        return self

    async def __aexit__(self, *_exc: object) -> bool:
        return False

    async def subscribe(self, topic: str, qos: int = 0) -> None:
        self.subscribed.append((topic, qos))
        self.calls.append(f"subscribe {topic}")

    async def publish(
        self, topic: str, payload: bytes = b"", qos: int = 0, retain: bool = False
    ) -> None:
        self.published.append((topic, bytes(payload), qos, retain))
        self.calls.append(f"publish {topic}")


def transcript() -> tuple[list[str], Any]:
    lines: list[str] = []
    return lines, lines.append


async def drive_session(
    device: DeviceIdentity,
    *,
    awake_s: float = 0.05,
    goodbye: bool = True,
    step: Any = None,
    client: FakeClient | None = None,
) -> FakeClient:
    fake = client or FakeClient()
    await run_session(
        device,
        credential_for(device.device_id),
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        link=LinkProfile(LINK_FAST),
        heartbeat_interval_s=0.01,
        awake_s=awake_s,
        goodbye=goodbye,
        step=step or (lambda _message: None),
    )
    return fake


def stage_command(
    *,
    cmd_id: str = "cmd-stage-1",
    url: str = SIGNED_URL,
    sha256: str = FIRMWARE_SHA256,
    version: str = "1.5.0",
    apply: str = "auto",
    size: int = len(FIRMWARE),
) -> bytes:
    return json.dumps(
        {
            "id": cmd_id,
            "type": "stage",
            "artifact": {
                "url": url,
                "sha256": sha256,
                "size": size,
                "version": version,
            },
            "apply": apply,
            "confirm_timeout_s": 300,
        }
    ).encode()


class FakeHttpResponse:
    """Context-managed chunked HTTP body reader for `urllib.request.urlopen` tests."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    def __enter__(self) -> FakeHttpResponse:
        return self

    def __exit__(self, *_exc: object) -> Literal[False]:
        return False

    def read(self, size: int = -1) -> bytes:
        chunk = self._data[self._offset :] if size < 0 else self._data[self._offset :][:size]
        self._offset += len(chunk)
        return chunk


async def run_stage(
    body: bytes,
    *,
    safe_window: str = SAFE_WINDOW_AUTO,
    device: DeviceIdentity | None = None,
    repeats: int = 1,
    awake_s: float = 0.3,
) -> tuple[StageRunner, FakeClient, list[str]]:
    identity = device or DeviceIdentity(device_id="a4cf12b3de90", fw_version="1.4.2")
    fake = FakeClient()
    for _ in range(repeats):
        fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{identity.device_id}/dn/cmd", body))
    stage = StageRunner(identity=identity, link=LinkProfile(LINK_FAST), safe_window=safe_window)
    lines, step = transcript()
    await run_session(
        identity,
        credential_for(identity.device_id),
        client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
        link=LinkProfile(LINK_FAST),
        heartbeat_interval_s=10.0,
        awake_s=awake_s,
        stage=stage,
        step=step,
    )
    return stage, fake, lines


def statuses(fake: FakeClient, device_id: str = "a4cf12b3de90") -> list[dict[str, Any]]:
    return [
        json.loads(payload)
        for topic, payload, _, _ in fake.published
        if topic == up_topic(device_id, STATUS)
    ]


def channel_payloads(fake: FakeClient, device_id: str, channel: str) -> list[dict[str, Any]]:
    return [
        json.loads(payload)
        for topic, payload, _, _ in fake.published
        if topic == up_topic(device_id, channel)
    ]


def session_factory(
    device_id: str, first_command: bytes | None = None
) -> tuple[list[FakeClient], Any]:
    clients: list[FakeClient] = []

    def factory() -> FakeClient:
        fake = FakeClient()
        if not clients and first_command is not None:
            fake.inbox.put_nowait(FakeMessage(f"ff/v1/d/{device_id}/dn/cmd", first_command))
        clients.append(fake)
        return fake

    return clients, factory


async def wait_until(predicate: Any, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("the simulator never reached the expected state")
        await asyncio.sleep(0.01)


async def drive_until(runner: Any, predicate: Any, stop: asyncio.Event) -> None:
    task = asyncio.create_task(runner)
    try:
        await wait_until(predicate)
    finally:
        stop.set()
        await asyncio.wait_for(task, timeout=5.0)


async def one_session(
    stage: StageRunner,
    fake: FakeClient,
    *,
    awake_s: float | None = 0.1,
    stop: asyncio.Event | None = None,
) -> list[str]:
    lines, step = transcript()
    await asyncio.wait_for(
        run_session(
            stage.identity,
            credential_for(stage.identity.device_id),
            client_factory=lambda: fake,  # type: ignore[arg-type,return-value]
            link=LinkProfile(LINK_FAST),
            heartbeat_interval_s=10.0,
            awake_s=awake_s,
            stop=stop,
            stage=stage,
            step=step,
        ),
        timeout=5.0,
    )
    return lines


async def applied_stage(
    *, version: str = "1.5.0", capabilities: tuple[str, ...] = (), **kwargs: Any
) -> StageRunner:
    stage = StageRunner(
        identity=DeviceIdentity(
            device_id="a4cf12b3de90", fw_version="1.4.2", capabilities=capabilities
        ),
        link=LinkProfile(LINK_FAST),
        **kwargs,
    )
    first = FakeClient()
    first.inbox.put_nowait(
        FakeMessage("ff/v1/d/a4cf12b3de90/dn/cmd", stage_command(version=version))
    )
    await one_session(stage, first, awake_s=None)
    assert stage.reboot.is_set(), "the apply did not reboot"
    assert [report["state"] for report in statuses(first)] == list(STAGE_WALK)
    stage.reboot.clear()
    return stage
