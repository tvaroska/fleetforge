"""Canonical cross-language protocol contracts (`capabilities.json`, `wire_vectors.json`).

Pure-stdlib loaders shared by firmware/simulator/ingestor contract tests (`D-10`, `D-29`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CONTRACTS_DIR = Path(__file__).resolve().parent
CAPABILITIES_CONTRACT_PATH = CONTRACTS_DIR / "capabilities.json"
WIRE_VECTORS_CONTRACT_PATH = CONTRACTS_DIR / "wire_vectors.json"


def load_capabilities_contract() -> dict[str, Any]:
    """Load the canonical capability registry JSON (`capabilities.json`)."""
    data = json.loads(CAPABILITIES_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("capabilities.json root must be a JSON object")
    return data


def load_wire_vectors_contract() -> dict[str, Any]:
    """Load the canonical wire protocol test vectors (`wire_vectors.json`)."""
    data = json.loads(WIRE_VECTORS_CONTRACT_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("wire_vectors.json root must be a JSON object")
    return data


__all__ = [
    "CAPABILITIES_CONTRACT_PATH",
    "CONTRACTS_DIR",
    "WIRE_VECTORS_CONTRACT_PATH",
    "load_capabilities_contract",
    "load_wire_vectors_contract",
]
