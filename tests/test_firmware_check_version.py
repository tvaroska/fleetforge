"""`just agent-check-published` — does the store serve the agent this checkout builds?

S0-infra-10: prod's flasher served 0.3.2 / 0.2.0 for eleven days while the repo moved to
0.4.5 and nothing said so. The comparison is pure (`firmware/index.py`); the CLI turns it
into rows, a STALE line on stderr and an exit code.
"""

import pytest

from fleetforge.firmware import AgentIndex, AgentIndexEntry, compare_published_versions
from fleetforge.firmware import __main__ as cli
from fleetforge.firmware.publish import write_index
from tests.conftest import MemoryObjectStore

DIGEST = "a" * 64


def entry(target: str, version: str, layout: str = "ab-4m-v1") -> AgentIndexEntry:
    return AgentIndexEntry(
        target=target, partition_layout=layout, agent_version=version, manifest_sha256=DIGEST
    )


def index_of(*entries: AgentIndexEntry) -> AgentIndex:
    return AgentIndex(bundles=list(entries))


def statuses(index: AgentIndex, expected: str, targets: list[str] | None = None) -> list[str]:
    return [r.status for r in compare_published_versions(index, expected, targets)]


class TestComparePublishedVersions:
    def test_equal_is_current(self) -> None:
        assert statuses(index_of(entry("esp32", "0.4.5")), "0.4.5") == ["current"]

    def test_older_is_behind(self) -> None:
        assert statuses(index_of(entry("esp32s3", "0.3.2")), "0.4.5") == ["behind"]

    def test_newer_is_ahead(self) -> None:
        assert statuses(index_of(entry("esp32", "0.5.0")), "0.4.5") == ["ahead"]

    def test_comparison_is_numeric_not_lexical(self) -> None:
        assert statuses(index_of(entry("esp32", "0.4.10")), "0.4.9") == ["ahead"]

    def test_empty_version_differs(self) -> None:
        assert statuses(index_of(entry("esp32", "")), "0.4.5") == ["differs"]

    def test_suffixed_version_differs(self) -> None:
        assert statuses(index_of(entry("esp32", "0.4.5-review")), "0.4.5") == ["differs"]

    def test_expected_whitespace_is_ignored(self) -> None:
        assert statuses(index_of(entry("esp32", "0.4.5")), "0.4.5\n") == ["current"]

    def test_listed_target_without_entry_is_missing(self) -> None:
        rows = compare_published_versions(index_of(entry("esp32", "0.4.5")), "0.4.5", ["esp32c3"])
        assert [(r.target, r.partition_layout, r.published, r.status) for r in rows] == [
            ("esp32c3", "", "", "missing")
        ]

    def test_two_layouts_give_two_rows(self) -> None:
        index = index_of(entry("esp32", "0.4.5"), entry("esp32", "0.3.0", "ab-4m-arduino-v1"))
        rows = compare_published_versions(index, "0.4.5", ["esp32"])
        assert [(r.partition_layout, r.status) for r in rows] == [
            ("ab-4m-arduino-v1", "behind"),
            ("ab-4m-v1", "current"),
        ]

    def test_no_targets_checks_every_entry_sorted(self) -> None:
        index = index_of(entry("esp32s3", "0.4.5"), entry("esp32", "0.2.0"))
        rows = compare_published_versions(index, "0.4.5")
        assert [(r.target, r.status) for r in rows] == [("esp32", "behind"), ("esp32s3", "current")]

    def test_unlisted_targets_are_not_checked(self) -> None:
        index = index_of(entry("esp32", "0.2.0"), entry("esp32s3", "0.4.5"))
        assert statuses(index, "0.4.5", ["esp32s3"]) == ["current"]


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> MemoryObjectStore:
    fake = MemoryObjectStore()
    monkeypatch.setattr(cli, "Settings", lambda: _Settings())
    monkeypatch.setattr(cli, "create_object_store", lambda settings: fake)
    return fake


class _Settings:
    agent_index_key = "agent/index.json"
    # Only so `_store` can name a backend; the store itself is the in-memory fake.
    object_store_backend = "s3"
    s3_endpoint_url = "http://minio.test"
    s3_bucket = "test"


async def seed(store: MemoryObjectStore, *entries: AgentIndexEntry) -> None:
    await write_index(store, index_of(*entries), "agent/index.json")


class TestCheckVersionCli:
    async def test_lagging_exits_1_with_stale_on_stderr(
        self, store: MemoryObjectStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await seed(store, entry("esp32", "0.2.0"))
        code = await _run(["check-version", "--expect", "0.4.5", "esp32"])
        out = capsys.readouterr()
        assert code == 1
        assert "BEHIND (repo 0.4.5)" in out.out
        assert "STALE: 1 bundle(s) lag agent 0.4.5" in out.err
        assert "CHECK-VERSION OK" not in out.out

    async def test_warn_only_exits_0_with_warning_and_no_ok(
        self, store: MemoryObjectStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await seed(store, entry("esp32", "0.2.0"))
        code = await _run(["check-version", "--expect", "0.4.5", "--warn-only", "esp32"])
        out = capsys.readouterr()
        assert code == 0
        assert "WARNING: STALE" in out.err
        assert "CHECK-VERSION OK" not in out.out

    async def test_current_exits_0_with_ok(
        self, store: MemoryObjectStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await seed(store, entry("esp32", "0.4.5"))
        code = await _run(["check-version", "--expect", "0.4.5", "esp32"])
        out = capsys.readouterr()
        assert code == 0
        assert "CURRENT" in out.out
        assert "CHECK-VERSION OK" in out.out

    async def test_empty_index_marks_every_listed_target_missing(
        self, store: MemoryObjectStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        code = await _run(["check-version", "--expect", "0.4.5", "esp32", "esp32s3"])
        out = capsys.readouterr().out
        assert code == 1
        assert out.count("MISSING") == 2

    async def test_list_still_prints_list_ok(
        self, store: MemoryObjectStore, capsys: pytest.CaptureFixture[str]
    ) -> None:
        await seed(store, entry("esp32", "0.4.5"))
        code = await _run(["list"])
        assert code == 0
        assert capsys.readouterr().out.rstrip().endswith("LIST OK")


async def _run(argv: list[str]) -> int:
    # `main` owns its event loop (`asyncio.run`), so it cannot be called from a running
    # one; hand it to a worker thread.
    import asyncio

    return await asyncio.to_thread(cli.main, argv)
