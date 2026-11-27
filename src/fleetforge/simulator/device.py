"""What a simulated board *is* on the wire: identity, topics, and lifecycle loops.

Everything across `board_state.py`, `dock_ops.py`, `stage_runner.py`, `mqtt_session.py`,
and `device.py` is a server-independent reading of `spec/device-protocol.md` (enforced by
`tests/test_simulator.py`'s AST import-purity guard).

Six wire properties encoded across the simulator:

1. **QoS/retain matrix:** `announce`, `presence`, and `status` are QoS 1 retained; `hb` and
   `ack` are QoS 1 non-retained; `telemetry` is QoS 0 non-retained.
2. **Retained LWT:** Last Will publishes retained `{"online":false}` on `up/presence`.
3. **Persistent session:** `clean_session=False` with `identifier=device_id` drains queued
   `dn/#` commands across reconnects and deep-sleep wakes.
4. **Explicit clean goodbye:** Clean `DISCONNECT` packets do not trigger Mosquitto's LWT,
   so graceful shutdowns publish retained `{"online":false}` (`--crash-after` exercises the
   real LWT via `os._exit`).
5. **Fresh client per wake:** `aiomqtt.Client` is non-reentrant, so `mqtt_client_factory`
   constructs a new client on every reconnect or sleepy wake.
6. **Transcript observability:** Every publish logs its topic, QoS, and retain flag so ACL
   drops below MQTT v5 remain diagnosable.

Simulator fidelity boundaries vs ESP-IDF hardware (`D-2`, `D-5`, `D-6`, `D-10`):

* **`D-2` (HTTP Range resume):** Firmware opens a plain `GET` through `esp_https_ota`
  (never sending an unsigned `HEAD` pre-request that S3/GCS GET-presigned URLs reject with
  `403`) and `resume_range_perform()` re-opens with open-ended `Range: bytes=<offset>-`
  while streaming directly into the inactive flash slot. The simulator (`_fetch`) buffers
  downloads in memory via `urllib.request`, sends open-ended `Range: bytes=<offset>-` after
  any mid-stream `OSError`, and clears the partial buffer if an upstream ignores `Range`
  and answers `200` from byte `0`.
* **`D-5` (Buffered manual-reboot dock staging):** Both `ff_ota_dock_stage()` and
  `StageRunner.dock_stage()` verify SHA-256 + ECDSA P-256 over an in-memory buffer and stop
  at `staged` without auto-rebooting; the caller owns safe-window checks and triggers the
  reboot explicitly (`esp_restart()` on hardware vs `StageRunner.dock_reboot_staged()`).
* **`D-6` (Timing, session drop, and crypto boundaries):** Artifact downloads run to
  completion via `asyncio.to_thread` before the next MQTT message is read, so `cancel`
  meets `staged` / `awaiting_safe_window` rather than mid-transfer; `--confirm-timeout` is
  configured per board rather than clamped per `stage` command, and confirm-timeout /
  self-test rollbacks do not block on `--safe-window`; simulated reboots exit `async with
  client:` cleanly instead of dropping TCP ungracefully; and MQTT TLS uses the host trust
  store (`aiomqtt.TLSParameters()`) while detached ECDSA P-256 artifact signatures verify
  against `DEFAULT_SIGNING_PUBKEY_RING` via `cryptography`.
"""

from __future__ import annotations

import asyncio
import time

import aiomqtt

from fleetforge.simulator.board_state import (
    _SHA256_HEX as _SHA256_HEX,
)
from fleetforge.simulator.board_state import (
    _SIG_PREFIX as _SIG_PREFIX,
)
from fleetforge.simulator.board_state import (
    _SIG_RE as _SIG_RE,
)
from fleetforge.simulator.board_state import (
    ACK as ACK,
)
from fleetforge.simulator.board_state import (
    ACK_ACCEPTED as ACK_ACCEPTED,
)
from fleetforge.simulator.board_state import (
    ACK_CANCEL_FIRST as ACK_CANCEL_FIRST,
)
from fleetforge.simulator.board_state import (
    ACK_CMD_ID_MISSING as ACK_CMD_ID_MISSING,
)
from fleetforge.simulator.board_state import (
    ACK_DEFERRED as ACK_DEFERRED,
)
from fleetforge.simulator.board_state import (
    ACK_DETAILS as ACK_DETAILS,
)
from fleetforge.simulator.board_state import (
    ACK_NO_IMAGE as ACK_NO_IMAGE,
)
from fleetforge.simulator.board_state import (
    ACK_NOT_CONFIRMED as ACK_NOT_CONFIRMED,
)
from fleetforge.simulator.board_state import (
    ACK_NOT_IMPLEMENTED as ACK_NOT_IMPLEMENTED,
)
from fleetforge.simulator.board_state import (
    ACK_NOTHING_STAGED as ACK_NOTHING_STAGED,
)
from fleetforge.simulator.board_state import (
    ACK_NOTHING_TO_CANCEL as ACK_NOTHING_TO_CANCEL,
)
from fleetforge.simulator.board_state import (
    ACK_REFUSED as ACK_REFUSED,
)
from fleetforge.simulator.board_state import (
    ACK_UPDATE_IN_PROGRESS as ACK_UPDATE_IN_PROGRESS,
)
from fleetforge.simulator.board_state import (
    ALL_BUILTIN_CAPABILITIES as ALL_BUILTIN_CAPABILITIES,
)
from fleetforge.simulator.board_state import (
    ANNOUNCE as ANNOUNCE,
)
from fleetforge.simulator.board_state import (
    APPLY_AUTO as APPLY_AUTO,
)
from fleetforge.simulator.board_state import (
    APPLY_ON_COMMAND as APPLY_ON_COMMAND,
)
from fleetforge.simulator.board_state import (
    BASE_AGENT_CAPABILITIES as BASE_AGENT_CAPABILITIES,
)
from fleetforge.simulator.board_state import (
    CAPABILITY_SELFTEST as CAPABILITY_SELFTEST,
)
from fleetforge.simulator.board_state import (
    COMMAND as COMMAND,
)
from fleetforge.simulator.board_state import (
    COMMAND_APPLY as COMMAND_APPLY,
)
from fleetforge.simulator.board_state import (
    COMMAND_CANCEL as COMMAND_CANCEL,
)
from fleetforge.simulator.board_state import (
    COMMAND_REBOOT as COMMAND_REBOOT,
)
from fleetforge.simulator.board_state import (
    COMMAND_ROLLBACK as COMMAND_ROLLBACK,
)
from fleetforge.simulator.board_state import (
    COMMAND_STAGE as COMMAND_STAGE,
)
from fleetforge.simulator.board_state import (
    CONFIRM_AUTO as CONFIRM_AUTO,
)
from fleetforge.simulator.board_state import (
    CONFIRM_MODES as CONFIRM_MODES,
)
from fleetforge.simulator.board_state import (
    CONFIRM_NEVER as CONFIRM_NEVER,
)
from fleetforge.simulator.board_state import (
    CONFIRM_WALK as CONFIRM_WALK,
)
from fleetforge.simulator.board_state import (
    DEFAULT_AGENT_CAPABILITIES as DEFAULT_AGENT_CAPABILITIES,
)
from fleetforge.simulator.board_state import (
    DEFAULT_AWAKE_S as DEFAULT_AWAKE_S,
)
from fleetforge.simulator.board_state import (
    DEFAULT_CONFIRM_TIMEOUT_S as DEFAULT_CONFIRM_TIMEOUT_S,
)
from fleetforge.simulator.board_state import (
    DEFAULT_HEARTBEAT_INTERVAL_S as DEFAULT_HEARTBEAT_INTERVAL_S,
)
from fleetforge.simulator.board_state import (
    DEFAULT_SIGNING_PUBKEY_RING as DEFAULT_SIGNING_PUBKEY_RING,
)
from fleetforge.simulator.board_state import (
    DETAIL_APPLY_WAITS as DETAIL_APPLY_WAITS,
)
from fleetforge.simulator.board_state import (
    DETAIL_ROLLBACK_WAITS as DETAIL_ROLLBACK_WAITS,
)
from fleetforge.simulator.board_state import (
    DOWNLOAD_CHUNK_BYTES as DOWNLOAD_CHUNK_BYTES,
)
from fleetforge.simulator.board_state import (
    DOWNLOAD_TIMEOUT_S as DOWNLOAD_TIMEOUT_S,
)
from fleetforge.simulator.board_state import (
    HEARTBEAT as HEARTBEAT,
)
from fleetforge.simulator.board_state import (
    KEEPALIVE_S as KEEPALIVE_S,
)
from fleetforge.simulator.board_state import (
    LINK_FAST as LINK_FAST,
)
from fleetforge.simulator.board_state import (
    LINK_PROFILES as LINK_PROFILES,
)
from fleetforge.simulator.board_state import (
    LINK_SLOW as LINK_SLOW,
)
from fleetforge.simulator.board_state import (
    OPTIONAL_AGENT_CAPABILITIES as OPTIONAL_AGENT_CAPABILITIES,
)
from fleetforge.simulator.board_state import (
    POWER_CLASSES as POWER_CLASSES,
)
from fleetforge.simulator.board_state import (
    PRESENCE as PRESENCE,
)
from fleetforge.simulator.board_state import (
    PRESENCE_OFFLINE as PRESENCE_OFFLINE,
)
from fleetforge.simulator.board_state import (
    PRESENCE_ONLINE as PRESENCE_ONLINE,
)
from fleetforge.simulator.board_state import (
    QOS as QOS,
)
from fleetforge.simulator.board_state import (
    RESET_REASON_SW as RESET_REASON_SW,
)
from fleetforge.simulator.board_state import (
    RETURNED_TO as RETURNED_TO,
)
from fleetforge.simulator.board_state import (
    ROLLBACK_WALK as ROLLBACK_WALK,
)
from fleetforge.simulator.board_state import (
    ROLLED_BACK_BY_COMMAND as ROLLED_BACK_BY_COMMAND,
)
from fleetforge.simulator.board_state import (
    SAFE_WINDOW_AUTO as SAFE_WINDOW_AUTO,
)
from fleetforge.simulator.board_state import (
    SAFE_WINDOW_FILE_PREFIX as SAFE_WINDOW_FILE_PREFIX,
)
from fleetforge.simulator.board_state import (
    SAFE_WINDOW_HOLD as SAFE_WINDOW_HOLD,
)
from fleetforge.simulator.board_state import (
    SAFE_WINDOW_MODES as SAFE_WINDOW_MODES,
)
from fleetforge.simulator.board_state import (
    SAFE_WINDOW_POLL_S as SAFE_WINDOW_POLL_S,
)
from fleetforge.simulator.board_state import (
    SELFTEST_FAIL_REASON as SELFTEST_FAIL_REASON,
)
from fleetforge.simulator.board_state import (
    SELFTEST_FAILED as SELFTEST_FAILED,
)
from fleetforge.simulator.board_state import (
    SELFTEST_PASSED as SELFTEST_PASSED,
)
from fleetforge.simulator.board_state import (
    SELFTEST_TIMED_OUT as SELFTEST_TIMED_OUT,
)
from fleetforge.simulator.board_state import (
    SIGNING_PUBKEY_PRIMARY_PEM as SIGNING_PUBKEY_PRIMARY_PEM,
)
from fleetforge.simulator.board_state import (
    SIGNING_PUBKEY_SECONDARY_PEM as SIGNING_PUBKEY_SECONDARY_PEM,
)
from fleetforge.simulator.board_state import (
    SLOT_LABELS as SLOT_LABELS,
)
from fleetforge.simulator.board_state import (
    SLOW_ENROLL_EXTRA_S as SLOW_ENROLL_EXTRA_S,
)
from fleetforge.simulator.board_state import (
    SLOW_MAX_DELAY_S as SLOW_MAX_DELAY_S,
)
from fleetforge.simulator.board_state import (
    SLOW_MIN_DELAY_S as SLOW_MIN_DELAY_S,
)
from fleetforge.simulator.board_state import (
    STAGE_WALK as STAGE_WALK,
)
from fleetforge.simulator.board_state import (
    STATE_APPLYING as STATE_APPLYING,
)
from fleetforge.simulator.board_state import (
    STATE_AWAITING_SAFE_WINDOW as STATE_AWAITING_SAFE_WINDOW,
)
from fleetforge.simulator.board_state import (
    STATE_CANCELLED as STATE_CANCELLED,
)
from fleetforge.simulator.board_state import (
    STATE_CONFIRMED as STATE_CONFIRMED,
)
from fleetforge.simulator.board_state import (
    STATE_CONFIRMING as STATE_CONFIRMING,
)
from fleetforge.simulator.board_state import (
    STATE_DOWNLOADING as STATE_DOWNLOADING,
)
from fleetforge.simulator.board_state import (
    STATE_FAILED as STATE_FAILED,
)
from fleetforge.simulator.board_state import (
    STATE_REBOOTING as STATE_REBOOTING,
)
from fleetforge.simulator.board_state import (
    STATE_ROLLED_BACK as STATE_ROLLED_BACK,
)
from fleetforge.simulator.board_state import (
    STATE_ROLLING_BACK as STATE_ROLLING_BACK,
)
from fleetforge.simulator.board_state import (
    STATE_STAGED as STATE_STAGED,
)
from fleetforge.simulator.board_state import (
    STATE_STAGING as STATE_STAGING,
)
from fleetforge.simulator.board_state import (
    STATE_VERIFYING as STATE_VERIFYING,
)
from fleetforge.simulator.board_state import (
    STATUS as STATUS,
)
from fleetforge.simulator.board_state import (
    TELEMETRY as TELEMETRY,
)
from fleetforge.simulator.board_state import (
    TOPIC_ROOT as TOPIC_ROOT,
)
from fleetforge.simulator.board_state import (
    VERB_COMMANDS as VERB_COMMANDS,
)
from fleetforge.simulator.board_state import (
    ClientFactory as ClientFactory,
)
from fleetforge.simulator.board_state import (
    DeviceIdentity as DeviceIdentity,
)
from fleetforge.simulator.board_state import (
    LinkProfile as LinkProfile,
)
from fleetforge.simulator.board_state import (
    PendingConfirm as PendingConfirm,
)
from fleetforge.simulator.board_state import (
    Step as Step,
)
from fleetforge.simulator.board_state import (
    _is_valid_artifact_sig as _is_valid_artifact_sig,
)
from fleetforge.simulator.board_state import (
    _paced_publish as _paced_publish,
)
from fleetforge.simulator.board_state import (
    _silent as _silent,
)
from fleetforge.simulator.board_state import (
    _verify_artifact_sig as _verify_artifact_sig,
)
from fleetforge.simulator.board_state import (
    check_safe_window as check_safe_window,
)
from fleetforge.simulator.board_state import (
    derive_device_id as derive_device_id,
)
from fleetforge.simulator.board_state import (
    dn_filter as dn_filter,
)
from fleetforge.simulator.board_state import (
    encode as encode,
)
from fleetforge.simulator.board_state import (
    redact_url as redact_url,
)
from fleetforge.simulator.board_state import (
    redacted_command as redacted_command,
)
from fleetforge.simulator.board_state import (
    up_topic as up_topic,
)
from fleetforge.simulator.board_state import (
    will_for as will_for,
)
from fleetforge.simulator.dock_ops import _fetch as _fetch
from fleetforge.simulator.errors import SimulatorError as SimulatorError
from fleetforge.simulator.mqtt_session import (
    _command_loop as _command_loop,
)
from fleetforge.simulator.mqtt_session import (
    _heartbeat_loop as _heartbeat_loop,
)
from fleetforge.simulator.mqtt_session import (
    _safe_window_loop as _safe_window_loop,
)
from fleetforge.simulator.mqtt_session import (
    _sampler_loop as _sampler_loop,
)
from fleetforge.simulator.mqtt_session import (
    _sleep_until as _sleep_until,
)
from fleetforge.simulator.mqtt_session import (
    _start_sampler as _start_sampler,
)
from fleetforge.simulator.mqtt_session import (
    _stop_sampler as _stop_sampler,
)
from fleetforge.simulator.mqtt_session import (
    _telemetry_loop as _telemetry_loop,
)
from fleetforge.simulator.mqtt_session import (
    run_session as run_session,
)
from fleetforge.simulator.stage_runner import StageRunner as StageRunner
from fleetforge.simulator.state import Credential
from fleetforge.simulator.telemetry import TelemetryBuffer

RECONNECT_INITIAL_DELAY_S = 1.0
RECONNECT_MAX_DELAY_S = 30.0
REBOOT_DELAY_S = 2.0


async def run_always_on(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    stop: asyncio.Event,
    endpoint: str = "broker",
    safe_window: str = SAFE_WINDOW_AUTO,
    confirm: str = CONFIRM_AUTO,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
    broken_marker: str | None = None,
    selftest_fail_marker: str | None = None,
    telemetry_every_s: float | None = None,
    step: Step = _silent,
) -> None:
    """One session, forever, reconnecting with capped exponential backoff."""
    telemetry, sampler = _start_sampler(telemetry_every_s, stop)
    try:
        await _always_on(
            device,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            stop=stop,
            endpoint=endpoint,
            safe_window=safe_window,
            confirm=confirm,
            confirm_timeout_s=confirm_timeout_s,
            broken_marker=broken_marker,
            selftest_fail_marker=selftest_fail_marker,
            telemetry=telemetry,
            step=step,
        )
    finally:
        await _stop_sampler(sampler)


async def _always_on(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    stop: asyncio.Event,
    endpoint: str,
    safe_window: str,
    confirm: str,
    confirm_timeout_s: float,
    broken_marker: str | None,
    selftest_fail_marker: str | None,
    telemetry: TelemetryBuffer | None,
    step: Step,
) -> None:
    boot_monotonic = time.monotonic()
    connected = asyncio.Event()
    stage = StageRunner(
        identity=device,
        link=link,
        safe_window=safe_window,
        confirm=confirm,
        confirm_timeout_s=confirm_timeout_s,
        broken_marker=broken_marker,
        selftest_fail_marker=selftest_fail_marker,
    )
    delay = RECONNECT_INITIAL_DELAY_S
    while not stop.is_set():
        try:
            await run_session(
                stage.identity,
                credential,
                client_factory=client_factory,
                link=link,
                heartbeat_interval_s=heartbeat_interval_s,
                stop=stop,
                connected=connected,
                boot_monotonic=boot_monotonic,
                endpoint=endpoint,
                stage=stage,
                telemetry=telemetry,
                step=step,
            )
        except (aiomqtt.MqttError, OSError) as exc:
            if not connected.is_set() or stop.is_set():
                raise
            step(f"reconnect broker error ({exc}); retrying in {delay:.0f}s")
        else:
            if stop.is_set():
                return
            if stage.reboot.is_set():
                stage.reboot.clear()
                stage.seen.clear()
                boot_monotonic = time.monotonic()
                if telemetry is not None:
                    telemetry.clear()
                step(f"boot     back in {REBOOT_DELAY_S:.0f}s on {stage.identity.fw_version}")
                await _sleep_until(stop, REBOOT_DELAY_S)
                delay = RECONNECT_INITIAL_DELAY_S
                continue
            step(f"reconnect the broker closed the session; retrying in {delay:.0f}s")
            delay = RECONNECT_INITIAL_DELAY_S
        await _sleep_until(stop, delay)
        delay = min(delay * 2, RECONNECT_MAX_DELAY_S)


async def run_sleepy(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    wake_interval_s: float,
    awake_s: float,
    stop: asyncio.Event,
    endpoint: str = "broker",
    safe_window: str = SAFE_WINDOW_AUTO,
    confirm: str = CONFIRM_AUTO,
    confirm_timeout_s: float = DEFAULT_CONFIRM_TIMEOUT_S,
    broken_marker: str | None = None,
    selftest_fail_marker: str | None = None,
    telemetry_every_s: float | None = None,
    step: Step = _silent,
) -> None:
    """wake -> connect -> announce/presence/hb -> drain `dn/` -> disconnect -> sleep -> repeat."""
    telemetry, sampler = _start_sampler(telemetry_every_s, stop)
    try:
        await _sleepy(
            device,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            wake_interval_s=wake_interval_s,
            awake_s=awake_s,
            stop=stop,
            endpoint=endpoint,
            safe_window=safe_window,
            confirm=confirm,
            confirm_timeout_s=confirm_timeout_s,
            broken_marker=broken_marker,
            selftest_fail_marker=selftest_fail_marker,
            telemetry=telemetry,
            step=step,
        )
    finally:
        await _stop_sampler(sampler)


async def _sleepy(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float,
    wake_interval_s: float,
    awake_s: float,
    stop: asyncio.Event,
    endpoint: str,
    safe_window: str,
    confirm: str,
    confirm_timeout_s: float,
    broken_marker: str | None,
    selftest_fail_marker: str | None,
    telemetry: TelemetryBuffer | None,
    step: Step,
) -> None:
    boot_monotonic = time.monotonic()
    stage = StageRunner(
        identity=device,
        link=link,
        safe_window=safe_window,
        confirm=confirm,
        confirm_timeout_s=confirm_timeout_s,
        broken_marker=broken_marker,
        selftest_fail_marker=selftest_fail_marker,
    )
    while not stop.is_set():
        step(f"wake     staying up {awake_s:.0f}s")
        await run_session(
            stage.identity,
            credential,
            client_factory=client_factory,
            link=link,
            heartbeat_interval_s=heartbeat_interval_s,
            awake_s=awake_s,
            goodbye=False,
            stop=stop,
            boot_monotonic=boot_monotonic,
            endpoint=endpoint,
            stage=stage,
            telemetry=telemetry,
            step=step,
        )
        if stop.is_set():
            return
        if stage.reboot.is_set():
            stage.reboot.clear()
            stage.seen.clear()
            boot_monotonic = time.monotonic()
            if telemetry is not None:
                telemetry.clear()
            step(f"boot     rebooted onto {stage.identity.fw_version}")
        step(f"sleep    {wake_interval_s:.0f}s until the next wake")
        await _sleep_until(stop, wake_interval_s)


def mqtt_client_factory(
    host: str, port: int, credential: Credential, device_id: str, tls: bool = False
) -> ClientFactory:
    """Build a fresh `aiomqtt.Client` per session."""

    def factory() -> aiomqtt.Client:
        return aiomqtt.Client(
            hostname=host,
            port=port,
            identifier=device_id,
            username=credential.mqtt_username,
            password=credential.mqtt_password,
            will=will_for(device_id),
            clean_session=False,
            keepalive=KEEPALIVE_S,
            tls_params=aiomqtt.TLSParameters() if tls else None,
        )

    return factory
