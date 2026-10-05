"""`fleetforge.announce_fields`: store, never reject; never log the value.

Pure unit tests — no database. The edges that call these (the announce and the enroll
body) are covered in `test_ingestor.py` and `test_enroll.py`; this file pins the rules
themselves, including the three gotchas that would otherwise lose an announce: a NUL,
a lone surrogate, and `bool` being an `int`.
"""

import json
import logging

import pytest

from fleetforge.announce_fields import (
    FLASH_CHIP_SIZE_MAX,
    KNOWN_NETWORKS_MAX,
    SSID_MAX_BYTES,
    normalize_flash_chip_size,
    normalize_known_networks,
    normalize_partition_table_sha256,
    normalize_rollback_capable,
    normalize_ssid,
)
from tests.conftest import capture_logs

DEVICE_ID = "a4cf12b3de90"


@pytest.mark.parametrize(
    "value",
    [
        "shed",
        "é" * 16,  # exactly 32 bytes, 16 characters
        "a" * SSID_MAX_BYTES,
        " leading and trailing ",  # stored exactly as written, never stripped
        "Shed-5G",  # never case-folded
    ],
)
def test_a_usable_ssid_is_stored_exactly(value: str) -> None:
    assert normalize_ssid(value) == value


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "é" * 17,  # 34 bytes, only 17 characters
        "a" * (SSID_MAX_BYTES + 1),
        "a\x00b",  # PostgreSQL refuses NUL in TEXT and the transaction rolls back
        "tab\there",
        "line\nbreak",
        json.loads('"\\ud800"'),  # a lone surrogate: json accepts it, UTF-8 cannot encode it
        42,
        ["x"],
        {"ssid": "shed"},
        True,
        False,
    ],
)
def test_an_unusable_ssid_becomes_none(value: object) -> None:
    assert normalize_ssid(value) is None


@pytest.mark.parametrize("value", [0, 1, 4, KNOWN_NETWORKS_MAX])
def test_a_plausible_count_is_stored(value: int) -> None:
    assert normalize_known_networks(value) == value


@pytest.mark.parametrize(
    "value",
    [None, True, False, -1, KNOWN_NETWORKS_MAX + 1, 2.0, "2", "two", [2], 10**12],
)
def test_an_implausible_count_becomes_none(value: object) -> None:
    assert normalize_known_networks(value) is None


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (42, "not a string"),
        ("a" * 33, "33 bytes > 32"),
        ("secret\x00net", "control character"),
        (json.loads('"\\ud800x"'), "not valid unicode"),
    ],
)
def test_a_malformed_ssid_logs_the_field_and_reason_never_the_value(
    value: object, reason: str
) -> None:
    with capture_logs() as records:
        assert normalize_ssid(value, device_id=DEVICE_ID) is None

    assert len(records) == 1, "a malformed value must say so, once"
    message = records[0].getMessage()
    assert "ssid" in message
    assert reason in message
    assert DEVICE_ID in message
    assert str(value) not in message
    assert repr(value) not in message
    assert value not in records[0].args


def test_a_malformed_ssid_value_never_appears_in_the_log() -> None:
    """The plain case, stated plainly: an SSID is the operator's data."""
    ssid = "my-home-network-name-is-too-long-by-far"
    with capture_logs() as records:
        assert normalize_ssid(ssid, device_id=DEVICE_ID) is None

    assert records, "something must be logged, or the next assertion is vacuous"
    for record in records:
        assert ssid not in record.getMessage()
        assert ssid not in str(record.args)


@pytest.mark.parametrize(("value", "reason"), [(-1, "out of range"), ("2", "not an integer")])
def test_a_malformed_count_logs_the_field_and_reason_never_the_value(
    value: object, reason: str
) -> None:
    with capture_logs() as records:
        assert normalize_known_networks(value) is None

    assert len(records) == 1
    message = records[0].getMessage()
    assert "known_networks" in message
    assert reason in message
    assert str(value) not in str(records[0].args)


def test_not_reported_logs_nothing() -> None:
    """`None` / absent is the spec's "not reported", not a malformed value."""
    with capture_logs() as records:
        assert normalize_ssid(None, device_id=DEVICE_ID) is None
        assert normalize_ssid("", device_id=DEVICE_ID) is None
        assert normalize_known_networks(None, device_id=DEVICE_ID) is None
        assert normalize_ssid("shed", device_id=DEVICE_ID) == "shed"
        assert normalize_known_networks(2, device_id=DEVICE_ID) == 2

    assert records == []


# --- the board measurements (R2b-be-6) ---------------------------------------

# The `ab-4m-v1` fingerprint (spec → *Partition layouts*).
SHA = "1fa67e6bbd034e434d04e9d6f4f52bbe899361602cd498573eb3bde97d1559ed"


@pytest.mark.parametrize("value", [4194304, 1, FLASH_CHIP_SIZE_MAX])
def test_a_positive_flash_chip_size_is_stored(value: int) -> None:
    assert normalize_flash_chip_size(value) == value


def test_the_flash_ceiling_is_the_agents_uint32() -> None:
    assert FLASH_CHIP_SIZE_MAX == 2**32 - 1


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (True, "not an integer"),  # `True` is an `int` in Python
        (False, "not an integer"),
        (4194304.0, "not an integer"),
        ("4194304", "not an integer"),
        ([4194304], "not an integer"),
        (0, "out of range"),
        (-1, "out of range"),
        (2**32, "out of range"),
        (2**63, "out of range"),  # past int64: asyncpg would raise and lose the announce
        (2**64, "out of range"),
    ],
)
def test_an_unusable_flash_chip_size_logs_the_reason_never_the_value(
    value: object, reason: str
) -> None:
    with capture_logs() as records:
        assert normalize_flash_chip_size(value, device_id=DEVICE_ID) is None

    assert len(records) == 1, "a malformed value must say so, once"
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "flash_chip_size" in message
    assert f"({reason})" in message
    assert DEVICE_ID in message
    assert records[0].args == (DEVICE_ID, "flash_chip_size", reason), "never the value"
    if len(str(value)) > 3:
        assert str(value) not in message


def test_sha_is_stored_exactly() -> None:
    assert normalize_partition_table_sha256(SHA) == SHA


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (SHA.upper(), "not 64 lowercase hex characters"),  # never lowercased
        (SHA[:63], "not 64 lowercase hex characters"),
        (SHA + "0", "not 64 lowercase hex characters"),
        ("g" * 64, "not 64 lowercase hex characters"),
        (SHA + "\n", "not 64 lowercase hex characters"),  # `re.match(...$)` would accept it
        ("\u0660" * 64, "not 64 lowercase hex characters"),  # ARABIC-INDIC DIGIT ZERO: `\d`
        ("a" * 63 + "\x00", "not 64 lowercase hex characters"),  # NUL: PG refuses it in TEXT
        (SHA[:63] + json.loads('"\\ud800"'), "not 64 lowercase hex characters"),
        ("", "not 64 lowercase hex characters"),
        (123, "not a string"),
        ([SHA], "not a string"),
    ],
)
def test_an_unusable_sha_logs_the_reason_never_the_value(value: object, reason: str) -> None:
    with capture_logs() as records:
        assert normalize_partition_table_sha256(value, device_id=DEVICE_ID) is None

    assert len(records) == 1
    assert records[0].levelno == logging.INFO
    message = records[0].getMessage()
    assert "partition_table_sha256" in message
    assert f"({reason})" in message
    assert DEVICE_ID in message
    assert records[0].args == (DEVICE_ID, "partition_table_sha256", reason), "never the value"
    if len(str(value)) > 3:
        assert str(value) not in message


@pytest.mark.parametrize("value", [True, False])
def test_a_boolean_rollback_capable_is_stored_as_it_is(value: bool) -> None:
    assert normalize_rollback_capable(value) is value


@pytest.mark.parametrize("value", [1, 0, "true", "false", [], {}, "yes"])
def test_anything_but_a_boolean_rollback_capable_is_none(value: object) -> None:
    with capture_logs() as records:
        assert normalize_rollback_capable(value) is None

    assert len(records) == 1
    message = records[0].getMessage()
    assert "rollback_capable" in message
    assert "(not a boolean)" in message
    assert "device ? announced" in message, "no device id: the log says `?`"


def test_unknown_measurements_log_nothing() -> None:
    """`None` / absent is the spec's "unknown", not a malformed value."""
    with capture_logs() as records:
        assert normalize_flash_chip_size(None, device_id=DEVICE_ID) is None
        assert normalize_partition_table_sha256(None, device_id=DEVICE_ID) is None
        assert normalize_rollback_capable(None, device_id=DEVICE_ID) is None
        assert normalize_flash_chip_size(4194304, device_id=DEVICE_ID) == 4194304
        assert normalize_partition_table_sha256(SHA, device_id=DEVICE_ID) == SHA
        assert normalize_rollback_capable(False, device_id=DEVICE_ID) is False

    assert records == []
