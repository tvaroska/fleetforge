"""The frozen content-addressed key scheme (S0-infra-4) — pure functions, no I/O.

Separate from `tests/test_object_store.py` (645 lines, organised by adapter) because
these are the key *rules*, the same shape as the `resolve_key` block at the top of that
file: no network, no database, no SDK.

The load-bearing one is `test_blob_key_is_store_relative`. `design/artifacts.md` writes
the layout as the absolute path `fleetforge/blobs/sha256/<hex>`; a `blob_key` that
returned that would write `fleetforge/fleetforge/blobs/…` in production — and would only
be noticed by someone reading a bucket listing.
"""

import hashlib

import pytest

from fleetforge.storage import (
    BLOB_CACHE_CONTROL,
    BLOB_PREFIX,
    ObjectKeyError,
    blob_key,
    digest_bytes,
    parse_blob_key,
    put_blob,
    resolve_key,
)
from tests.conftest import MemoryObjectStore

# Two fixtures on purpose: the obviously-synthetic one reads well in failure output, and
# a real hashlib digest proves nothing depends on the string being all one character.
DIGEST = "a" * 64
REAL_PAYLOAD = b"fleetforge-agent-app.bin"
REAL_DIGEST = hashlib.sha256(REAL_PAYLOAD).hexdigest()


@pytest.mark.parametrize("digest", [DIGEST, REAL_DIGEST])
def test_blob_key_round_trips(digest: str) -> None:
    assert parse_blob_key(blob_key(digest)) == digest


@pytest.mark.parametrize("digest", [DIGEST, REAL_DIGEST])
def test_blob_key_is_store_relative(digest: str) -> None:
    """THE regression guard: the key carries no `fleetforge/`; the store's prefix does.

    Both deployments must land on one layout — `fleetforge/blobs/sha256/<hex>` in the
    shared GCS bucket, `blobs/sha256/<hex>` in the dedicated MinIO bucket — and that is
    only true if the key itself is prefix-free.
    """
    key = blob_key(digest)
    assert key == f"blobs/sha256/{digest}"
    assert key.startswith("blobs/")
    assert "fleetforge" not in key
    assert resolve_key("fleetforge/", key) == f"fleetforge/blobs/sha256/{digest}"
    assert resolve_key("", key) == f"blobs/sha256/{digest}"


def test_digest_bytes_is_the_lowercase_hex_sha256() -> None:
    assert digest_bytes(REAL_PAYLOAD) == REAL_DIGEST
    assert digest_bytes(b"") == hashlib.sha256(b"").hexdigest()


@pytest.mark.parametrize(
    ("key", "why"),
    [
        ("secrets/fleetforge.env", "another tenant of the shared bucket"),
        ("podcasts/x.mp3", "another tenant of the shared bucket"),
        (f"firmware/{DIGEST}.bin", "the old directory layout"),
        (f"fleetforge/{BLOB_PREFIX}{DIGEST}", "double-prefixed — the store-relative bug"),
        (f"blobs/sha1/{DIGEST[:40]}", "a different digest algorithm"),
        ("blobs/sha256/", "prefix with no digest"),
        (f"blobs/sha256/{'a' * 63}", "63 hex characters"),
        (f"blobs/sha256/{'a' * 65}", "65 hex characters"),
        (f"blobs/sha256/{DIGEST.upper()}", "uppercase — two spellings of one object"),
        (f"blobs/sha256/{DIGEST}/app.bin", "nested below the blob"),
        (f"blobs/sha256/{DIGEST}.bin", "a suffix the digest does not have"),
        (f"/blobs/sha256/{DIGEST}", "absolute"),
        (f"../blobs/sha256/{DIGEST}", "escaping"),
        ("", "empty"),
    ],
)
def test_parse_blob_key_refuses_anything_outside_the_blob_prefix(key: str, why: str) -> None:
    """A key outside `fleetforge/blobs/` is refused — the TODO line's named criterion."""
    with pytest.raises(ObjectKeyError):
        parse_blob_key(key)


@pytest.mark.parametrize(
    ("digest", "why"),
    [
        (DIGEST.upper(), "uppercase is rejected, never lowercased"),
        ("a" * 63, "too short"),
        ("a" * 65, "too long"),
        ("g" * 64, "not hex"),
        ("", "empty"),
        (f"{'a' * 32}/{'a' * 31}", "a separator inside the digest"),
        (f"{DIGEST}\n", "a trailing newline — `$` alone would match before it"),
    ],
)
def test_blob_key_refuses_a_bad_digest(digest: str, why: str) -> None:
    with pytest.raises(ObjectKeyError):
        blob_key(digest)


def test_blob_key_does_not_normalise_an_uppercase_digest() -> None:
    """Stated as its own test because "helpfully" lowercasing is the tempting bug."""
    with pytest.raises(ObjectKeyError):
        blob_key(REAL_DIGEST.upper())
    # And the lowercase spelling is still the one and only key.
    assert blob_key(REAL_DIGEST) == f"{BLOB_PREFIX}{REAL_DIGEST}"


async def test_put_blob_derives_the_key_and_marks_it_immutable() -> None:
    store = MemoryObjectStore()
    key = await put_blob(store, REAL_PAYLOAD)
    assert key == f"{BLOB_PREFIX}{REAL_DIGEST}"
    (recorded_key, data, content_type, cache_control) = store.puts[0]
    assert recorded_key == key
    assert data == REAL_PAYLOAD
    assert content_type == "application/octet-stream"
    assert cache_control == BLOB_CACHE_CONTROL
    assert BLOB_CACHE_CONTROL == "public, max-age=31536000, immutable"


async def test_put_blob_accepts_a_matching_expected_digest() -> None:
    store = MemoryObjectStore()
    assert await put_blob(store, REAL_PAYLOAD, expected_digest=REAL_DIGEST) == (
        f"{BLOB_PREFIX}{REAL_DIGEST}"
    )
    assert len(store.puts) == 1


@pytest.mark.parametrize("expected", [DIGEST, REAL_DIGEST.upper(), "nope"])
async def test_put_blob_refuses_a_mismatching_expected_digest(expected: str) -> None:
    """A client-supplied digest is checked, never trusted — and nothing is written."""
    store = MemoryObjectStore()
    with pytest.raises(ObjectKeyError):
        await put_blob(store, REAL_PAYLOAD, expected_digest=expected)
    assert store.puts == []


async def test_put_blob_passes_a_content_type_through() -> None:
    store = MemoryObjectStore()
    await put_blob(store, REAL_PAYLOAD, content_type="application/json")
    assert store.puts[0][2] == "application/json"


def test_a_put_blob_key_survives_resolve_key_under_both_prefixes() -> None:
    """The scheme has to be legal input to the security control, not merely adjacent."""
    key = blob_key(REAL_DIGEST)
    for prefix in ("fleetforge/", ""):
        resolved = resolve_key(prefix, key)
        assert resolved.endswith(REAL_DIGEST)
        assert resolved.startswith(prefix)
