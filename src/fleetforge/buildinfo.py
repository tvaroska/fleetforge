"""What build is this, exactly — the answer to "is prod running what I think it is?".

`__version__` alone cannot answer that. Two images built a week apart from different
commits both say "0.4.0", which is precisely the confusion this module exists to end:
a board was reflashed from the dashboard and came back on the old agent because the
running API was serving a store nobody had published to, and nothing on the page could
have revealed it.

The commit and build time are baked into the image by `docker build --build-arg` (see
`Dockerfile` and the `_build-images` recipe) and read here from the environment. They
are deliberately NOT derived from `git` at runtime: the production image has no
checkout, so a runtime `git describe` would be empty exactly where it matters.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from . import __version__

# An unset value means "built without provenance" — a plain `docker build` with no
# `--build-arg`, which is a dev build. "unknown" is the honest answer; inventing a
# value here would recreate the stale-string problem one layer down.
UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class BuildInfo:
    """Identity of the running build."""

    version: str
    commit: str
    built_at: str

    @property
    def short_commit(self) -> str:
        """First 8 chars, or `unknown`. What a human compares at a glance."""
        return self.commit if self.commit == UNKNOWN else self.commit[:8]

    def as_dict(self) -> dict[str, str]:
        return {
            "version": self.version,
            "commit": self.commit,
            "built_at": self.built_at,
        }


def build_info() -> BuildInfo:
    """Read build provenance from the environment. No I/O — safe on a liveness path."""
    return BuildInfo(
        version=__version__,
        commit=os.environ.get("FF_SOURCE_COMMIT") or UNKNOWN,
        built_at=os.environ.get("FF_BUILT_AT") or UNKNOWN,
    )
