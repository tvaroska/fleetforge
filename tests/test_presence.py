"""The derived-presence rule. Pure — no database, no clock.

Every case here is a way the dashboard lies: an `always_on` board whose LWT fired but
that still reads online, a dead e-paper frame that never ages out, or a sleepy board
marked offline because its own LWT was believed.
"""

import datetime as dt

import pytest

from fleetforge.db.models import Device
from fleetforge.presence import is_online

NOW = dt.datetime(2026, 9, 8, 12, 0, tzinfo=dt.UTC)
TOLERANCE = 2.5
WAKE_S = 600  # 10 minutes; 2.5 × => the boundary is exactly 25 minutes


def device(**overrides: object) -> Device:
    """A `Device` built in memory — never inserted, so a CHECK cannot get in the way."""
    values: dict[str, object] = {
        "device_id": "a4cf12b3de90",
        "platform_type": "esp32c6",
        "link_type": "wifi",
        "power_class": "always_on",
    }
    values.update(overrides)
    return Device(**values)


@pytest.mark.parametrize(
    ("presence_reported", "expected"),
    [(True, True), (False, False), (None, False)],
)
def test_always_on_follows_reported_presence(
    presence_reported: bool | None, expected: bool
) -> None:
    """The retained `up/presence` value is authoritative; NULL means never heard from."""
    subject = device(presence_reported=presence_reported, last_seen=NOW)
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is expected


def test_always_on_ignores_a_fresh_last_seen() -> None:
    """A board whose LWT fired is offline even though a message arrived a second ago."""
    subject = device(presence_reported=False, last_seen=NOW - dt.timedelta(seconds=1))
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is False


@pytest.mark.parametrize(
    ("age_minutes", "expected"),
    [
        (0, True),
        (20, True),
        (24, True),
        (25, False),  # the boundary is strict `<` — assert it so nobody "fixes" it to `<=`
        (26, False),
    ],
)
def test_sleepy_ages_out_at_the_tolerance_boundary(age_minutes: int, expected: bool) -> None:
    subject = device(
        power_class="sleepy",
        expected_wake_interval_s=WAKE_S,
        last_seen=NOW - dt.timedelta(minutes=age_minutes),
    )
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is expected


def test_sleepy_ignores_reported_presence() -> None:
    """The LWT fires on every normal sleep and means nothing (spec/device-protocol.md).

    This is the bug the spec calls out: believing it would mark a healthy e-paper frame
    offline for the whole of every sleep cycle.
    """
    subject = device(
        power_class="sleepy",
        expected_wake_interval_s=WAKE_S,
        presence_reported=False,
        last_seen=NOW - dt.timedelta(minutes=1),
    )
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is True


@pytest.mark.parametrize("interval", [None, 0, -1])
def test_sleepy_without_a_usable_interval_is_offline(interval: int | None) -> None:
    """The CHECK makes this unreachable; a TypeError in the message loop would be worse."""
    subject = device(
        power_class="sleepy",
        expected_wake_interval_s=interval,
        last_seen=NOW,
        presence_reported=True,
    )
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is False


def test_sleepy_never_seen_is_offline() -> None:
    subject = device(power_class="sleepy", expected_wake_interval_s=WAKE_S, last_seen=None)
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is False


def test_unknown_power_class_is_offline_and_does_not_raise() -> None:
    """`power_class` outside the CHECK leaves presence undefined — report offline, loudly."""
    subject = device(power_class="hibernating", last_seen=NOW, presence_reported=True)
    assert is_online(subject, now=NOW, tolerance=TOLERANCE) is False
