"""Single connected MQTT session (`run_session`) and concurrent worker loops.

Encodes the ordered subscribe -> retained announce -> retained presence -> boot outcome ->
heartbeat/command/telemetry/safe-window worker loops -> clean goodbye sequence.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable

import aiomqtt

from fleetforge.simulator.board_state import (
    ANNOUNCE,
    DEFAULT_HEARTBEAT_INTERVAL_S,
    HEARTBEAT,
    PRESENCE,
    PRESENCE_OFFLINE,
    PRESENCE_ONLINE,
    QOS,
    SAFE_WINDOW_POLL_S,
    TELEMETRY,
    ClientFactory,
    DeviceIdentity,
    LinkProfile,
    Step,
    _paced_publish,
    _silent,
    dn_filter,
    encode,
    up_topic,
)
from fleetforge.simulator.stage_runner import StageRunner
from fleetforge.simulator.state import Credential
from fleetforge.simulator.telemetry import TELEMETRY_FLUSH_S, TELEMETRY_QOS, TelemetryBuffer


async def _sleep_until(stop: asyncio.Event, seconds: float) -> None:
    """Sleep, waking immediately if `stop` is set."""
    with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(stop.wait(), timeout=seconds)


async def _heartbeat_loop(
    client: aiomqtt.Client,
    device: DeviceIdentity,
    link: LinkProfile,
    interval_s: float,
    boot_monotonic: float,
    step: Step,
    boot_ok: Callable[[], bool] = lambda: True,
) -> None:
    """One `up/hb` every `interval_s`, starting immediately. Never retained."""
    while True:
        uptime_s = int(time.monotonic() - boot_monotonic)
        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, HEARTBEAT),
            encode(device.heartbeat(uptime_s, boot_ok=boot_ok())),
            retain=False,
            step=step,
        )
        await asyncio.sleep(interval_s)


async def _telemetry_loop(
    client: aiomqtt.Client, device: DeviceIdentity, buffer: TelemetryBuffer, step: Step
) -> None:
    """`ff_telemetry.c::flush_task`: drain on connect, then at most one payload per 5 s."""
    topic = up_topic(device.device_id, TELEMETRY)

    async def publish_one() -> bool:
        built = buffer.next_payload()
        if built is None:
            return False
        payload, last_seq, dropped = built
        await client.publish(topic, payload, qos=TELEMETRY_QOS, retain=False)
        buffer.commit(last_seq, dropped)
        samples = payload.count(b'"age_ms":')
        step(
            f"publish  {topic} (qos {TELEMETRY_QOS}, no retain, "
            f"{samples} samples, dropped {dropped}, {len(payload)} B)"
        )
        return True

    while await publish_one():
        pass
    while True:
        await asyncio.sleep(TELEMETRY_FLUSH_S)
        await publish_one()


async def _sampler_loop(buffer: TelemetryBuffer, every_s: float, stop: asyncio.Event) -> None:
    """Sample `sim_counter` = 1, 2, 3, ... every `every_s`, connected or not."""
    start = time.monotonic()
    counter = 0
    while not stop.is_set():
        counter += 1
        buffer.record("sim_counter", counter)
        await _sleep_until(stop, max(start + counter * every_s - time.monotonic(), 0.0))


def _start_sampler(
    every_s: float | None, stop: asyncio.Event
) -> tuple[TelemetryBuffer | None, asyncio.Task[None] | None]:
    if every_s is None:
        return None, None
    buffer = TelemetryBuffer()
    return buffer, asyncio.create_task(_sampler_loop(buffer, every_s, stop))


async def _stop_sampler(task: asyncio.Task[None] | None) -> None:
    if task is not None:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def _safe_window_loop(client: aiomqtt.Client, stage: StageRunner, step: Step) -> None:
    """Poll a `file:` window every `SAFE_WINDOW_POLL_S` and release what waits on it."""
    while True:
        await stage.release_if_safe(client, step)
        await asyncio.sleep(SAFE_WINDOW_POLL_S)


async def _command_loop(client: aiomqtt.Client, stage: StageRunner, step: Step) -> None:
    """Drain `dn/#` and hand every message to `StageRunner`."""
    async for message in client.messages:
        await stage.handle(client, message, step)


async def run_session(
    device: DeviceIdentity,
    credential: Credential,
    *,
    client_factory: ClientFactory,
    link: LinkProfile,
    heartbeat_interval_s: float = DEFAULT_HEARTBEAT_INTERVAL_S,
    awake_s: float | None = None,
    goodbye: bool = True,
    stop: asyncio.Event | None = None,
    connected: asyncio.Event | None = None,
    boot_monotonic: float | None = None,
    endpoint: str = "broker",
    stage: StageRunner | None = None,
    telemetry: TelemetryBuffer | None = None,
    step: Step = _silent,
) -> None:
    """One connected session: subscribe, announce, be present, beat, then leave."""
    stop = stop if stop is not None else asyncio.Event()
    boot_monotonic = boot_monotonic if boot_monotonic is not None else time.monotonic()
    stage = stage if stage is not None else StageRunner(identity=device, link=link)

    client = client_factory()
    async with client:
        if connected is not None:
            connected.set()
        step(
            f"connect  {endpoint} as {credential.mqtt_username} "
            f"(client_id={device.device_id}, clean_session=False, will=retained "
            f"{PRESENCE_OFFLINE.decode()} on up/presence)"
        )
        await client.subscribe(dn_filter(device.device_id), qos=QOS)
        step(f"subscribe {dn_filter(device.device_id)} (qos {QOS})")

        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, ANNOUNCE),
            encode(device.announce()),
            retain=True,
            step=step,
        )
        await _paced_publish(
            client,
            link,
            up_topic(device.device_id, PRESENCE),
            PRESENCE_ONLINE,
            retain=True,
            step=step,
        )
        confirm_timer = await stage.on_boot(client, step)

        workers = [
            asyncio.create_task(
                _heartbeat_loop(
                    client,
                    device,
                    link,
                    heartbeat_interval_s,
                    boot_monotonic,
                    step,
                    boot_ok=lambda: stage.boot_ok,
                )
            ),
            asyncio.create_task(_command_loop(client, stage, step)),
        ]
        if telemetry is not None:
            workers.append(asyncio.create_task(_telemetry_loop(client, device, telemetry, step)))
        if stage.window_can_open:
            workers.append(asyncio.create_task(_safe_window_loop(client, stage, step)))

        sentinels: list[asyncio.Task[object]] = [
            asyncio.create_task(stop.wait()),
            asyncio.create_task(stage.reboot.wait()),
        ]
        if confirm_timer is not None:
            sentinels.append(confirm_timer)
        try:
            done, _ = await asyncio.wait(
                [*workers, *sentinels], timeout=awake_s, return_when=asyncio.FIRST_COMPLETED
            )
            for task in done:
                if task is confirm_timer:
                    task.result()
                elif task not in sentinels:
                    task.result()
        finally:
            for task in [*workers, *sentinels]:
                task.cancel()
            await asyncio.gather(*workers, *sentinels, return_exceptions=True)

        if stage.reboot.is_set():
            step(f"reboot   restarting into fw_version {stage.identity.fw_version}")
        elif goodbye:
            await client.publish(
                up_topic(device.device_id, PRESENCE), PRESENCE_OFFLINE, qos=QOS, retain=True
            )
            step(
                f"goodbye  {up_topic(device.device_id, PRESENCE)} "
                f"{PRESENCE_OFFLINE.decode()} (qos {QOS}, retain)"
            )
