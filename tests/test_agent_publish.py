"""`just agent-publish` — the write half of S0-infra-6, against an in-memory store.

The property that matters most is the **round trip**: what `publish_bundle` writes is
exactly what `load_catalog` can serve. Everything else here is one of the three ways this
scheme can be got wrong and nobody notice for a year:

* the index written as a blob (`immutable` metadata on the one mutable object);
* the manifest re-serialised instead of uploaded verbatim (a digest nothing reproduces);
* a rollback pointed at bytes that were never published and never verified.
"""

import json
from pathlib import Path

import pytest

from fleetforge.firmware import (
    DEFAULT_INDEX_KEY,
    MAX_SUPERSEDED,
    AgentBundleError,
    AgentIndex,
    load_bundle_dir,
    load_catalog,
)
from fleetforge.firmware.publish import publish_bundle, read_index, rollback, write_index
from fleetforge.storage.blobs import BLOB_CACHE_CONTROL, BLOB_PREFIX, blob_key, digest_bytes
from tests.conftest import MemoryObjectStore
from tests.test_firmware_catalog import PART_FILES, write_bundle


def bump(bundle_dir: Path, version: str) -> Path:
    """Re-stamp a bundle's `agent_version`, so two publishes are distinguishable."""
    manifest = json.loads((bundle_dir / "manifest.json").read_text())
    manifest["agent_version"] = version
    (bundle_dir / "manifest.json").write_text(json.dumps(manifest))
    return bundle_dir


class TestPublish:
    async def test_every_part_lands_at_its_own_digest_marked_immutable(
        self, tmp_path: Path
    ) -> None:
        store = MemoryObjectStore()
        bundle_dir = write_bundle(tmp_path, "esp32")
        result = await publish_bundle(store, load_bundle_dir(bundle_dir))

        for name, filename in PART_FILES.items():
            payload = (bundle_dir / filename).read_bytes()
            key = blob_key(digest_bytes(payload))
            assert store.objects[key] == payload, name
            assert key in result.part_keys
        # 4 parts + 1 manifest, all under the frozen blob prefix and all immutable.
        blob_puts = [put for put in store.puts if put[0].startswith(BLOB_PREFIX)]
        assert len(blob_puts) == 5
        assert {put[3] for put in blob_puts} == {BLOB_CACHE_CONTROL}

    async def test_the_manifest_is_uploaded_verbatim(self, tmp_path: Path) -> None:
        """The recorded digest must be the digest of the file `agent-verify` checked."""
        store = MemoryObjectStore()
        bundle_dir = write_bundle(tmp_path, "esp32")
        raw = (bundle_dir / "manifest.json").read_bytes()
        result = await publish_bundle(store, load_bundle_dir(bundle_dir))

        assert result.manifest_sha256 == digest_bytes(raw)
        assert store.objects[blob_key(result.manifest_sha256)] == raw

    async def test_the_index_is_not_a_blob_and_is_never_cached(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))

        index_puts = [put for put in store.puts if put[0] == DEFAULT_INDEX_KEY]
        assert len(index_puts) == 1
        (key, _, content_type, cache_control) = index_puts[0]
        assert not key.startswith(BLOB_PREFIX), "the index is mutable; it is not a blob"
        assert not key.startswith("fleetforge/"), (
            "keys are store-relative; the prefix is the store's"
        )
        assert content_type == "application/json"
        assert cache_control == "no-store"

    async def test_the_index_names_the_published_bundle(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        result = await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))
        index = await read_index(store)
        entry = index.entry_for("esp32")
        assert entry is not None
        assert entry.manifest_sha256 == result.manifest_sha256
        assert entry.partition_layout == "ab-4m-v1"
        assert entry.agent_version == "0.1.0"
        assert entry.superseded == []

    async def test_two_targets_coexist(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32c6")))
        index = await read_index(store)
        assert [entry.target for entry in index.bundles] == ["esp32", "esp32c6"]

    async def test_republishing_the_same_bundle_is_idempotent(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        bundle = load_bundle_dir(write_bundle(tmp_path, "esp32"))
        await publish_bundle(store, bundle)
        before = dict(store.objects)
        await publish_bundle(store, bundle)

        assert store.objects == before, "content addressing makes a re-publish a no-op"
        index = await read_index(store)
        entry = index.entry_for("esp32")
        assert entry is not None
        assert entry.superseded == [], "a bundle must not supersede itself"

    async def test_a_new_version_supersedes_the_old_one(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        bundle_dir = write_bundle(tmp_path, "esp32")
        first = await publish_bundle(store, load_bundle_dir(bundle_dir))
        second = await publish_bundle(store, load_bundle_dir(bump(bundle_dir, "0.2.0")))

        entry = (await read_index(store)).entry_for("esp32")
        assert entry is not None
        assert entry.manifest_sha256 == second.manifest_sha256
        assert entry.agent_version == "0.2.0"
        assert [item.manifest_sha256 for item in entry.superseded] == [first.manifest_sha256]
        # Nothing is ever deleted: the old bundle stays flashable.
        assert blob_key(first.manifest_sha256) in store.objects

    async def test_the_rollback_menu_is_capped(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        bundle_dir = write_bundle(tmp_path, "esp32")
        for n in range(MAX_SUPERSEDED + 5):
            await publish_bundle(store, load_bundle_dir(bump(bundle_dir, f"0.0.{n}")))

        entry = (await read_index(store)).entry_for("esp32")
        assert entry is not None
        assert len(entry.superseded) == MAX_SUPERSEDED
        assert entry.superseded[0].agent_version == f"0.0.{MAX_SUPERSEDED + 3}", "newest first"

    async def test_publish_then_load_catalog_round_trips(self, tmp_path: Path) -> None:
        """The regression guard the whole task rests on."""
        store = MemoryObjectStore()
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32c6")))

        catalog = await load_catalog(store)
        assert catalog.targets == ("esp32", "esp32c6")
        bundle = catalog.bundle("esp32")
        assert bundle is not None
        app = bundle.part("app")
        assert app is not None
        assert store.objects[app.blob_key] == (tmp_path / "esp32" / "app.bin").read_bytes()

    async def test_an_unreadable_index_is_not_overwritten_blind(self, tmp_path: Path) -> None:
        """Clobbering an index we could not parse would unpublish every other target."""
        store = MemoryObjectStore()
        await store.put(DEFAULT_INDEX_KEY, b"{not json")
        with pytest.raises(AgentBundleError):
            await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))


class TestRollback:
    async def test_rollback_to_a_superseded_digest_flips_the_entry(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        bundle_dir = write_bundle(tmp_path, "esp32")
        first = await publish_bundle(store, load_bundle_dir(bundle_dir))
        second = await publish_bundle(store, load_bundle_dir(bump(bundle_dir, "0.2.0")))

        index = await rollback(store, "esp32", first.manifest_sha256)
        entry = index.entry_for("esp32")
        assert entry is not None
        assert entry.manifest_sha256 == first.manifest_sha256
        assert entry.agent_version == "0.1.0"
        assert second.manifest_sha256 in {item.manifest_sha256 for item in entry.superseded}

        # And the catalog serves the older build, with no restart and no rebuild.
        catalog = await load_catalog(store)
        bundle = catalog.bundle("esp32")
        assert bundle is not None
        assert bundle.agent_version == "0.1.0"

    async def test_rollback_to_an_unknown_digest_is_refused(self, tmp_path: Path) -> None:
        """An arbitrary digest would point the index at bytes nobody ever verified."""
        store = MemoryObjectStore()
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))
        with pytest.raises(AgentBundleError, match="not a previously published bundle"):
            await rollback(store, "esp32", "d" * 64)

    async def test_rollback_to_the_current_digest_is_refused(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        result = await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, "esp32")))
        with pytest.raises(AgentBundleError, match="already current"):
            await rollback(store, "esp32", result.manifest_sha256)

    async def test_rollback_of_an_unpublished_target_is_refused(self) -> None:
        store = MemoryObjectStore()
        await write_index(store, AgentIndex())
        with pytest.raises(AgentBundleError, match="nothing is published"):
            await rollback(store, "esp32", "d" * 64)

    async def test_rollback_refuses_a_digest_whose_blob_is_gone(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        bundle_dir = write_bundle(tmp_path, "esp32")
        first = await publish_bundle(store, load_bundle_dir(bundle_dir))
        await publish_bundle(store, load_bundle_dir(bump(bundle_dir, "0.2.0")))
        await store.delete(blob_key(first.manifest_sha256))

        with pytest.raises(AgentBundleError, match="no longer in the store"):
            await rollback(store, "esp32", first.manifest_sha256)
