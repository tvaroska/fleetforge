"""The version string exists in three files; this is what keeps them one value.

`fleetforge.__version__` drifted to "0.1.0" while `pyproject.toml` said "0.4.0", and
`GET /v1/healthz` — the endpoint an operator curls to ask "what is deployed?" — served
the stale number. Nothing caught it because nothing compared them. This does.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

from fleetforge import __version__
from fleetforge.buildinfo import UNKNOWN, build_info

REPO_ROOT = Path(__file__).resolve().parents[1]


def _pyproject_version() -> str:
    with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
        return tomllib.load(handle)["project"]["version"]


def _frontend_version() -> str:
    return json.loads((REPO_ROOT / "frontend" / "package.json").read_text())["version"]


def test_package_version_matches_pyproject() -> None:
    assert __version__ == _pyproject_version(), (
        "fleetforge.__version__ and pyproject.toml disagree — bump both, or "
        "/v1/healthz will report a version nobody shipped"
    )


def test_frontend_version_matches_backend() -> None:
    """One image pair is deployed together, so one version describes both."""
    assert _frontend_version() == _pyproject_version(), (
        "frontend/package.json and pyproject.toml disagree — the dashboard footer "
        "renders both, and two numbers there means two deploys nobody made"
    )


def test_build_info_reports_unknown_without_build_args(monkeypatch) -> None:
    """A plain `docker build` says so, rather than inventing provenance."""
    monkeypatch.delenv("FF_SOURCE_COMMIT", raising=False)
    monkeypatch.delenv("FF_BUILT_AT", raising=False)

    info = build_info()

    assert info.version == __version__
    assert info.commit == UNKNOWN
    assert info.built_at == UNKNOWN
    assert info.short_commit == UNKNOWN


def test_build_info_reads_baked_provenance(monkeypatch) -> None:
    monkeypatch.setenv("FF_SOURCE_COMMIT", "815596b2c0ffee1234567890abcdef0123456789")
    monkeypatch.setenv("FF_BUILT_AT", "2026-09-23T19:26:56Z")

    info = build_info()

    assert info.short_commit == "815596b2"
    assert info.as_dict() == {
        "version": __version__,
        "commit": "815596b2c0ffee1234567890abcdef0123456789",
        "built_at": "2026-09-23T19:26:56Z",
    }
