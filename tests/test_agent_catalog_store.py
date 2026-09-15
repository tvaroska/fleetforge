"""`firmware/catalog.py` — reading the published catalog back out of the store.

The read side's contract is narrower than it looks, and every clause of it is a test here:

* **absent index ≠ broken store.** `ObjectNotFound` on the index is "nothing published
  yet" and an empty catalog; anything else the transport does is a named fault that
  propagates, because "we cannot see what is published" and "nothing is published" send
  an operator to different places.
* **one bad entry does not take the others down** — the rule the filesystem loader had,
  kept.
* **a failing refresh never serves the previous snapshot.** A snapshot whose parts cannot
  be fetched is a manifest we cannot honour, which is the one thing `spec/standards.md`
  rules out.
"""

import asyncio
import json
from pathlib import Path

import pytest

from fleetforge.firmware import DEFAULT_INDEX_KEY, CatalogCache, load_bundle_dir, load_catalog
from fleetforge.firmware.publish import publish_bundle, read_index, write_index
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectStoreError
from tests.conftest import MemoryObjectStore, capture_logs
from tests.test_firmware_catalog import warnings_in, write_bundle


async def published(store: MemoryObjectStore, tmp_path: Path, *targets: str) -> None:
    """Publish real, verified bundles — the same path `just agent-publish` takes."""
    for target in targets:
        await publish_bundle(store, load_bundle_dir(write_bundle(tmp_path, target)))


async def corrupt_published_manifest(
    store: MemoryObjectStore, target: str, overrides: dict[str, object]
) -> None:
    """Rewrite a published manifest blob in place, keeping the index pointing at it.

    Only something *other* than the CLI can produce this — which is exactly why the
    catalog re-checks a manifest it did not verify itself.
    """
    index = await read_index(store)
    entry = index.entry_for(target)
    assert entry is not None
    key = blob_key(entry.manifest_sha256)
    manifest = json.loads(store.objects[key])
    manifest.update(overrides)
    store.objects[key] = json.dumps(manifest).encode()


class TestLoadCatalog:
    async def test_an_absent_index_is_an_empty_catalog(self) -> None:
        store = MemoryObjectStore()
        with capture_logs() as records:
            catalog = await load_catalog(store)
        assert catalog.targets == ()
        assert bool(catalog) is False
        assert warnings_in(records), "nothing published must warn, not pass silently"

    async def test_the_happy_path_serves_both_targets(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32", "esp32c6")

        catalog = await load_catalog(store)
        assert catalog.keys == (("esp32", "ab-4m-v1"), ("esp32c6", "ab-4m-v1"))
        assert catalog.layouts_for("esp32") == ("ab-4m-v1",)
        bundle = catalog.bundle("esp32c6")
        assert bundle is not None
        assert bundle.manifest_sha256
        bootloader = bundle.part("bootloader")
        assert bootloader is not None
        assert bootloader.offset == 0, "the C6 bootloader sits at 0x0, not 0x1000"
        assert bootloader.blob_key == blob_key(bootloader.sha256)
        assert bootloader.etag == f'"sha256-{bootloader.sha256}"'

    async def test_a_missing_manifest_blob_drops_only_that_target(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32", "esp32c6")
        entry = (await read_index(store)).entry_for("esp32c6")
        assert entry is not None
        await store.delete(blob_key(entry.manifest_sha256))

        with capture_logs() as records:
            catalog = await load_catalog(store)

        assert catalog.targets == ("esp32",)
        assert any("esp32c6" in message for message in warnings_in(records))

    @pytest.mark.parametrize(
        ("overrides", "needle"),
        [
            ({"partition_layout": "ab-2m-v0"}, "ab-2m-v0"),
            ({"ota_slot_size": 1048576}, "ota_slot_size"),
            ({"target": "esp32c3"}, "esp32"),
        ],
    )
    async def test_a_manifest_that_disagrees_with_the_protocol_is_dropped(
        self, tmp_path: Path, overrides: dict[str, object], needle: str
    ) -> None:
        """Checked at publish AND here: the store can be written to by something else."""
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        await corrupt_published_manifest(store, "esp32", overrides)

        with capture_logs() as records:
            catalog = await load_catalog(store)
        assert catalog.targets == ()
        assert any(needle in message for message in warnings_in(records))

    async def test_the_same_fixture_loads_when_it_is_not_corrupted(self, tmp_path: Path) -> None:
        """Vacuity guard for the three cases above."""
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        with capture_logs() as records:
            catalog = await load_catalog(store)
        assert catalog.targets == ("esp32",)
        assert warnings_in(records) == []

    async def test_an_unparseable_published_manifest_is_dropped(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        entry = (await read_index(store)).entry_for("esp32")
        assert entry is not None
        store.objects[blob_key(entry.manifest_sha256)] = b"{not json"

        with capture_logs() as records:
            catalog = await load_catalog(store)
        assert catalog.targets == ()
        assert any("esp32" in message for message in warnings_in(records))

    @pytest.mark.parametrize("payload", [b"{not json", b'{"schema": 2, "bundles": []}', b"[]"])
    async def test_a_malformed_index_is_empty_and_never_raises(self, payload: bytes) -> None:
        """The API must still start and still answer honestly on a hand-broken index."""
        store = MemoryObjectStore()
        await store.put(DEFAULT_INDEX_KEY, payload)
        with capture_logs() as records:
            catalog = await load_catalog(store)
        assert catalog.targets == ()
        assert warnings_in(records)

    async def test_a_duplicate_index_key_is_dropped_first_wins(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        index = await read_index(store)
        index.bundles.append(index.bundles[0].model_copy(update={"agent_version": "9.9.9"}))
        await write_index(store, index)

        with capture_logs() as records:
            catalog = await load_catalog(store)
        assert catalog.targets == ("esp32",)
        assert warnings_in(records)

    async def test_an_unreachable_store_propagates(self) -> None:
        """The named fault. Flattening this into an empty catalog is the bug."""
        store = MemoryObjectStore()
        store.fail_with = ObjectStoreError("minio refused the connection")
        with pytest.raises(ObjectStoreError):
            await load_catalog(store)

    async def test_a_manifest_fetch_failure_propagates_rather_than_dropping_a_target(
        self, tmp_path: Path
    ) -> None:
        """A readable index and an unreachable blob is still a named fault, not a drop."""

        class BlobsUnreachable(MemoryObjectStore):
            async def get(self, key: str) -> bytes:
                if key.startswith("blobs/"):
                    raise ObjectStoreError("minio went away mid-read")
                return await super().get(key)

        store = BlobsUnreachable()
        await published(store, tmp_path, "esp32")
        with pytest.raises(ObjectStoreError):
            await load_catalog(store)

    async def test_a_custom_index_key_is_honoured(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await publish_bundle(
            store, load_bundle_dir(write_bundle(tmp_path, "esp32")), index_key="agent/other.json"
        )
        assert (await load_catalog(store, index_key="agent/other.json")).targets == ("esp32",)
        assert (await load_catalog(store)).targets == ()


class TestCatalogCache:
    async def test_two_reads_inside_the_ttl_cost_one_index_read(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        cache = CatalogCache(ttl_s=60.0)

        store.gets.clear()
        await cache.get(store)
        first = store.gets.count(DEFAULT_INDEX_KEY)
        await cache.get(store)
        assert first == 1
        assert store.gets.count(DEFAULT_INDEX_KEY) == 1

    async def test_a_zero_ttl_reads_the_index_every_time(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        cache = CatalogCache(ttl_s=0.0)

        store.gets.clear()
        await cache.get(store)
        await cache.get(store)
        assert store.gets.count(DEFAULT_INDEX_KEY) == 2

    async def test_a_burst_of_concurrent_gets_costs_one_index_read(self, tmp_path: Path) -> None:
        """A flash pulls four parts at once; that must not be four index reads."""
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        cache = CatalogCache(ttl_s=60.0)

        store.gets.clear()
        await asyncio.gather(*(cache.get(store) for _ in range(5)))
        assert store.gets.count(DEFAULT_INDEX_KEY) == 1

    async def test_a_new_publish_appears_after_the_ttl_with_nothing_restarted(
        self, tmp_path: Path
    ) -> None:
        """The criterion the feature exists for, in miniature."""
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        cache = CatalogCache(ttl_s=0.0)
        assert (await cache.get(store)).targets == ("esp32",)

        await published(store, tmp_path, "esp32c6")
        assert (await cache.get(store)).targets == ("esp32", "esp32c6")

    async def test_a_failing_refresh_raises_and_does_not_serve_the_old_snapshot(
        self, tmp_path: Path
    ) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        cache = CatalogCache(ttl_s=0.0)
        assert (await cache.get(store)).targets == ("esp32",)

        store.fail_with = ObjectStoreError("minio refused the connection")
        with pytest.raises(ObjectStoreError):
            await cache.get(store)
        # And again: the failure itself is not cached either.
        with pytest.raises(ObjectStoreError):
            await cache.get(store)

        store.fail_with = None
        assert (await cache.get(store)).targets == ("esp32",)

    async def test_clear_forgets_the_snapshot(self, tmp_path: Path) -> None:
        store = MemoryObjectStore()
        await published(store, tmp_path, "esp32")
        cache = CatalogCache(ttl_s=3600.0)
        await cache.get(store)
        cache.clear()

        store.gets.clear()
        await cache.get(store)
        assert store.gets.count(DEFAULT_INDEX_KEY) == 1
