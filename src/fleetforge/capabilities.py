"""Shared pure-stdlib capability registry (`D-29`).

Loads `fleetforge/contracts/capabilities.json` so server, test, and tooling code share one
canonical definition of the capability tokens announced by `ff_identity.c` and
`fleetforge.simulator`.
"""

from __future__ import annotations

import re
from typing import Any

from fleetforge.contracts import load_capabilities_contract


def load_capabilities_registry() -> dict[str, Any]:
    """Return the parsed `capabilities.json` contract dictionary."""
    return load_capabilities_contract()


_REGISTRY = load_capabilities_registry()

BASE_CAPABILITIES: tuple[str, ...] = tuple(str(x) for x in _REGISTRY["base"])
OPTIONAL_BUILTIN_CAPABILITIES: tuple[str, ...] = tuple(
    str(x) for x in _REGISTRY["optional_builtin"]
)
DEFAULT_AGENT_CAPABILITIES: tuple[str, ...] = tuple(str(x) for x in _REGISTRY["default_agent"])
ALL_BUILTIN_CAPABILITIES: tuple[str, ...] = tuple(str(x) for x in _REGISTRY["all_builtin"])
DYNAMIC_CARGO_CAPABILITY_RE: re.Pattern[str] = re.compile(str(_REGISTRY["dynamic_cargo_pattern"]))


def is_known_capability(capability: str, *, allow_cargo: bool = True) -> bool:
    """Return True if `capability` is built-in (or a valid cargo type when `allow_cargo`)."""
    if capability in ALL_BUILTIN_CAPABILITIES:
        return True
    if allow_cargo and capability != "app":
        return DYNAMIC_CARGO_CAPABILITY_RE.fullmatch(capability) is not None
    return False


__all__ = [
    "ALL_BUILTIN_CAPABILITIES",
    "BASE_CAPABILITIES",
    "DEFAULT_AGENT_CAPABILITIES",
    "DYNAMIC_CARGO_CAPABILITY_RE",
    "OPTIONAL_BUILTIN_CAPABILITIES",
    "is_known_capability",
    "load_capabilities_registry",
]
