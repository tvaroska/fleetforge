"""Orphan artifact retention safety (`A-4`).

Verifies that `retention.sweep` and `ingestor.main.sweep_once` never delete `artifacts`
rows when no `ObjectStore` is configured, and delete an orphan `artifacts` row ONLY after
its corresponding blob delete succeeds.
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


async def _seed_orphan_artifact(engine: AsyncEngine, sha256: str) -> None:
    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    async with sessionmaker() as session:
        await session.execute(
            text(
                """
                INSERT INTO artifacts (sha256, kind, target, size_bytes, partition_layout, artifact_type)
                VALUES (:sha256, 'user_firmware', 'esp32', 4096, 'ab-4m-v1', 'app')
                ON CONFLICT (sha256) DO NOTHING
                """
            ),
            {"sha256": sha256},
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
    """A-4: without an `ObjectStore`, neither `retention.sweep` nor `sweep_once` deletes
    orphan `artifacts` rows; with a store, rows are deleted ONLY after `store.delete` succeeds."""
    sha_keep = "a" * 64
    sha_prune = "b" * 64
    await _seed_orphan_artifact(engine, sha_keep)
    await _seed_orphan_artifact(engine, sha_prune)

    sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
    now = dt.datetime.now(dt.UTC)

    # 1. `store=None`: orphan `artifacts` rows MUST survive so blobs never leak unreferenced.
    async with sessionmaker() as session:
        res_no_store = await retention.sweep(session, now=now, store=None)
        await session.commit()
    assert res_no_store.artifacts == 0
    assert await _artifact_exists(engine, sha_keep) is True
    assert await _artifact_exists(engine, sha_prune) is True

    await ingestor_main.sweep_once(sessionmaker, store=None)
    assert await _artifact_exists(engine, sha_keep) is True
    assert await _artifact_exists(engine, sha_prune) is True

    # 2. `store` fails on `sha_keep` and succeeds on `sha_prune`: ONLY `sha_prune` row is deleted.
    class SelectiveFailingStore(MemoryObjectStore):
        async def delete(self, key: str) -> None:
            if key == blob_key(sha_keep):
                raise ObjectStoreError("simulated S3 503 during delete")
            await super().delete(key)

    store = SelectiveFailingStore()
    await store.put(blob_key(sha_keep), b"blob-keep")
    await store.put(blob_key(sha_prune), b"blob-prune")

    await ingestor_main.sweep_once(sessionmaker, store=store)
    assert await _artifact_exists(engine, sha_keep) is True
    assert await _artifact_exists(engine, sha_prune) is False
    assert await store.get(blob_key(sha_keep)) == b"blob-keep"
    with pytest.raises(ObjectNotFound):
        await store.get(blob_key(sha_prune))
