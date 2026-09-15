"""The agent index — the one mutable object that says which bundles are flashable.

`ObjectStore` has four verbs and deliberately no `list` (`storage/objectstore.py`), so
something has to say *what exists*. That something is a single small JSON object at
`Settings.agent_index_key` (`agent/index.json`, store-relative), written by
`firmware/publish.py` and read by `firmware/catalog.py`. Everything it points at is
content-addressed and immutable; **this object is the only pointer in the scheme**, which
is why it is written with `cache_control="no-store"` and never through `put_blob`
(`BLOB_CACHE_CONTROL` is `immutable` for a year — on a mutable object that is a bug an
intermediary keeps for a year).

An entry names a bundle by the sha256 of its `manifest.json` *bytes as built*. That gives
every published bundle a stable identity, and makes rollback one object write: point the
entry back at an older manifest digest (`superseded` is the menu of what that can be).
Blobs are never deleted, so a bundle published once stays flashable forever.

**Read-modify-write is racy and knowingly so.** Two concurrent publishers can lose one
another's entry: neither backend exposes a precondition (generation / If-Match) through
our four-verb Protocol, and this is a single-developer estate. `agent-publish` prints the
whole entry list after every write, so a lost update is visible in the transcript rather
than silent. If that ever stops being enough, the fix is a precondition on `put`, not a
lock here.

No SDK and no I/O in this module (the same rule as `storage/blobs.py`), so the schema and
the `upsert` rule are testable on their own.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field

from fleetforge.firmware.manifest import SafeSegment, Sha256Hex

# Bumped only when the on-disk shape changes incompatibly. An unknown schema is
# **refused**, not guessed at — the same rule `MANIFEST_SCHEMA` follows.
INDEX_SCHEMA: Final[Literal[1]] = 1

# Store-relative, never absolute: the `fleetforge/` half is the store's prefix and is
# joined on by `resolve_key`. See `storage/blobs.py`'s docstring for the trap.
DEFAULT_INDEX_KEY = "agent/index.json"

# The index is fetched on every catalog refresh, so the rollback menu is capped. Twenty
# is ~2 KB and about two years of releases at this project's pace; the blobs behind a
# forgotten digest still exist, they just stop being one command away.
MAX_SUPERSEDED = 20


def utc_now() -> str:
    """`2026-09-15T10:11:12Z` — the timestamp spelling the manifests already use."""
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class SupersededEntry(BaseModel):
    """A bundle this (target, layout) used to serve, and can be rolled back to."""

    model_config = ConfigDict(extra="ignore")

    manifest_sha256: Sha256Hex
    agent_version: str = ""
    published_at: str = ""


class AgentIndexEntry(BaseModel):
    """The current bundle for one (target, partition_layout), plus its history."""

    model_config = ConfigDict(extra="ignore")

    target: SafeSegment
    partition_layout: SafeSegment
    # Informational only. The manifest blob is the authority on everything about a
    # build; this is here so `agent-list` reads as something other than hex.
    agent_version: str = ""
    manifest_sha256: Sha256Hex
    published_at: str = ""
    superseded: list[SupersededEntry] = Field(default_factory=list)

    @property
    def key(self) -> tuple[str, str]:
        """(target, partition_layout) — the catalog key `AgentBundle.key` also uses."""
        return (self.target, self.partition_layout)


class AgentIndex(BaseModel):
    """`agent/index.json`. At most one entry per (target, partition_layout)."""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    schema_version: Literal[1] = Field(default=INDEX_SCHEMA, alias="schema")
    updated_at: str = ""
    bundles: list[AgentIndexEntry] = Field(default_factory=list)

    def entry_for(self, target: str, layout: str | None = None) -> AgentIndexEntry | None:
        """The entry for `target` (and `layout`, when given), or `None`.

        With no `layout` and more than one entry for the target, the first in index order
        wins — the caller that must not guess is the *catalog*, which raises
        `AmbiguousBundleError`; here this is only ever used to resolve an operator's
        `rollback esp32` when one layout exists.
        """
        for entry in self.bundles:
            if entry.target == target and (layout is None or entry.partition_layout == layout):
                return entry
        return None

    def upsert(self, entry: AgentIndexEntry) -> AgentIndex:
        """A new index with `entry` current for its key. Pure — nothing is mutated.

        The displaced entry is pushed onto the new one's `superseded`, newest first, and
        the list is truncated to `MAX_SUPERSEDED`. Re-publishing the *same* manifest
        digest is idempotent: it does not push the entry onto its own history.
        """
        previous = next((e for e in self.bundles if e.key == entry.key), None)
        history = list(entry.superseded)
        if previous is not None and previous.manifest_sha256 != entry.manifest_sha256:
            history = [
                SupersededEntry(
                    manifest_sha256=previous.manifest_sha256,
                    agent_version=previous.agent_version,
                    published_at=previous.published_at,
                ),
                *[e for e in previous.superseded if e.manifest_sha256 != entry.manifest_sha256],
                *history,
            ]
        elif previous is not None:
            history = [*previous.superseded, *history]

        # De-duplicate, keeping the first (newest) spelling of each digest.
        seen: set[str] = set()
        deduped: list[SupersededEntry] = []
        for item in history:
            if item.manifest_sha256 in seen:
                continue
            seen.add(item.manifest_sha256)
            deduped.append(item)

        replacement = entry.model_copy(update={"superseded": deduped[:MAX_SUPERSEDED]})
        others = [e for e in self.bundles if e.key != entry.key]
        return AgentIndex(
            schema=INDEX_SCHEMA,
            updated_at=utc_now(),
            bundles=sorted([*others, replacement], key=lambda e: e.key),
        )
