"""Fleetforge — OTA firmware management for embedded fleets.

One package, one image, two entrypoints: `fleetforge.api.main` (the HTTP control
plane) and `fleetforge.ingestor.main` (the single MQTT subscriber).
"""

# Kept in lockstep with `pyproject.toml` and `frontend/package.json` by
# `tests/test_version.py`, which fails the build if any of the three drift. This is a
# literal rather than an `importlib.metadata` lookup on purpose: the production image
# runs `uv sync --no-install-project` and then `COPY src`, so fleetforge is importable
# but NOT an installed distribution — a metadata lookup resolves to nothing in exactly
# the place the value matters. The test is what makes one source of truth out of three
# literals; before it existed this string sat at "0.1.0" while the project shipped
# 0.4.0, and `/v1/healthz` reported the stale value to anyone asking what was deployed.
__version__ = "0.4.1"
