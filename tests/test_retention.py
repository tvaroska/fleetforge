"""Orphan artifact retention safety (`A-4`, `A-30`, `A-31`).

Verifies that:
1. `retention.sweep` never deletes `artifacts` rows when `store=None` OR when `prune_orphans=False`
   (the default).
2. With `prune_orphans=True` and a configured `ObjectStore` (e.g. `ingestor.main.sweep_once`),
   orphan `user_firmware` and `delta_patch` rows are atomically deleted and committed in Postgres
   BEFORE `store.delete` is invoked, and a single transient blob-delete failure does not block
   pruning other orphan blobs.
"""

from __future__ import annotations

import datetime as dt

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, async_sessionmaker

from fleetforge import retention
from fleetforge.ingestor import main as ingestor_main
from fleetforge.storage.blobs import blob_key
from fleetforge.storage.objectstore import ObjectNotFound, ObjectStoreError
from tests.conftest import MemoryObjectStore


async def _seed_orphan_artifact(
    engine: AsyncEngine,
    sha256: str,
    *,
    kind: str = "user_firmware",
) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO artifacts (sha256, kind, target, size_bytes, partition_layout, artifact_type)
                VALUES (:sha256, :kind, 'esp32', 4096, 'ab-4m-v1', :atype)
                ON CONFLICT (sha256) DO NOTHING
                """
            ),
            {
                "sha256": sha256,
                "kind": kind,
                "atype": "delta_patch" if kind == "delta_patch" else "app",
            },
        )
        await session.commit()


async def _artifact_exists(engine: AsyncEngine, sha256: str) -> bool:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        found = await session.scalar(
            text("SELECT sha256 FROM artifacts WHERE sha256 = :sha256"),
            {"sha256": sha256},
        )
        return found is not None


async def test_orphan_rows_survive_when_no_store_is_configured(
    engine: AsyncEngine, clean_tables: None
) -> None:
    """A-4 / A-30 / A-31: `store=None` and `prune_orphans=False` preserve orphan rows;
    `sweep_once` (`prune_orphans=True`) commits DB deletes for both `user_firmware` and
    `delta_patch` before unlinking blobs, tolerating individual blob-delete failures."""
    sha_fail_blob = "a" * 64
    sha_prune = "b" * 64
    sha_delta_orphan = "c" * 64
    await _seed_orphan_artifact(engine, sha_fail_blob)
    await _seed_orphan_artifact(engine, sha_prune)
    await _seed_orphan_artifact(engine, sha_delta_orphan, kind="delta_patch")

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)

    # `store=None`: orphan rows MUST survive.
    async with sessionmaker() as session:
        res_no_store = await retention.sweep(session, now=now, store=None, prune_orphans=True)
        await session.commit()
    assert res_no_store.artifacts == 0
    assert await _artifact_exists(engine, sha_fail_blob) is True
    assert await _artifact_exists(engine, sha_prune) is True
    assert await _artifact_exists(engine, sha_delta_orphan) is True

    await ingestor_main.sweep_once(sessionmaker, store=None)
    assert await _artifact_exists(engine, sha_fail_blob) is True

    # `prune_orphans=False` (default) WITH a store: orphan rows & blobs MUST survive!
    class SelectiveFailingStore(MemoryObjectStore):
        async def delete(self, key: str) -> None:
            if key == blob_key(sha_fail_blob):
                raise ObjectStoreError("simulated S3 503 during delete")
            await super().delete(key)

    store = SelectiveFailingStore()
    await store.put(blob_key(sha_fail_blob), b"blob-keep")
    await store.put(blob_key(sha_prune), b"blob-prune")
    await store.put(blob_key(sha_delta_orphan), b"delta-orphan")

    async with sessionmaker() as session:
        res_default = await retention.sweep(session, now=now, store=store)
        await session.commit()
    assert res_default.artifacts == 0
    assert await _artifact_exists(engine, sha_prune) is True
    assert await store.get(blob_key(sha_prune)) == b"blob-prune"

    # `sweep_once` sets `prune_orphans=True`: DB rows commit deleted BEFORE blob unlink,
    # and a transient error deleting `sha_fail_blob` does not stop `sha_prune` or `sha_delta_orphan`.
    await ingestor_main.sweep_once(sessionmaker, store=store)
    assert await _artifact_exists(engine, sha_fail_blob) is False
    assert await _artifact_exists(engine, sha_prune) is False
    assert await _artifact_exists(engine, sha_delta_orphan) is False
    assert await store.get(blob_key(sha_fail_blob)) == b"blob-keep"
    with pytest.raises(ObjectNotFound):
        await store.get(blob_key(sha_prune))
    with pytest.raises(ObjectNotFound):
        await store.get(blob_key(sha_delta_orphan))
