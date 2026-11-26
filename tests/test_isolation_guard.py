"""Ports, Adapters and Contracts (`Rule 0`) AST import isolation guard (`A-29`, `STD-ARCH §2.2`).

Enforces three architectural boundaries across `src/fleetforge/`:
1. Core modules outside `fleetforge.api` never import `fastapi` or `fleetforge.api`.
2. `fleetforge.api` never imports `fleetforge.ingestor` (shared ingest code lives in `fleetforge.ingest`).
3. `fleetforge.api.routers.*` never imports another sibling router under `fleetforge.api.routers.*`.

Includes a positive control asserting the AST checker flags synthetic violations for all 3 rules.
"""

from __future__ import annotations

import ast
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "fleetforge"


def _imported_modules(source: str, filename: str) -> list[tuple[int, str]]:
    """Return `(lineno, dotted_module)` pairs for every import in `source`."""
    tree = ast.parse(source, filename=filename)
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.append((node.lineno, alias.name))
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            found.append((node.lineno, mod))
            for alias in node.names:
                if mod:
                    found.append((node.lineno, f"{mod}.{alias.name}"))
    return found


def check_source_isolation(module_name: str, source: str) -> list[str]:
    """Check one module's AST against the 3 Ports, Adapters and Contracts isolation rules."""
    violations: list[str] = []
    imports = _imported_modules(source, module_name)

    is_api = module_name == "fleetforge.api" or module_name.startswith("fleetforge.api.")
    is_router = module_name.startswith("fleetforge.api.routers.") and not module_name.endswith(
        ".__init__"
    )

    for lineno, target in imports:
        if not is_api and (
            target == "fastapi"
            or target.startswith("fastapi.")
            or target == "fleetforge.api"
            or target.startswith("fleetforge.api.")
        ):
            violations.append(f"{module_name}:{lineno}: core module imports HTTP layer `{target}`")
        if is_api and (
            target == "fleetforge.ingestor" or target.startswith("fleetforge.ingestor.")
        ):
            violations.append(
                f"{module_name}:{lineno}: API module imports ingestor process `{target}`"
            )
        if is_router and (
            target == "fleetforge.api.routers" or target.startswith("fleetforge.api.routers.")
        ):
            violations.append(f"{module_name}:{lineno}: router imports sibling router `{target}`")
    return violations


def _module_name_for(path: Path) -> str:
    rel = path.relative_to(REPO_ROOT / "src").with_suffix("")
    return ".".join(rel.parts)


def test_source_tree_obeys_layer_isolation_boundaries() -> None:
    """Every module under `src/fleetforge/` respects core/API/ingestor/router isolation."""
    all_violations: list[str] = []
    py_files = sorted(SRC_ROOT.rglob("*.py"))
    assert py_files, "expected Python files under src/fleetforge"
    for path in py_files:
        mod = _module_name_for(path)
        all_violations.extend(check_source_isolation(mod, path.read_text(encoding="utf-8")))
    assert not all_violations, "Isolation boundary violations:\n" + "\n".join(all_violations)


def test_isolation_guard_positive_control() -> None:
    """STD-ARCH §2.2 positive control: synthetic violations in each layer MUST be caught."""
    core_fastapi = check_source_isolation(
        "fleetforge.rollout",
        "from fastapi import HTTPException\n",
    )
    assert any("core module imports HTTP layer" in v for v in core_fastapi)

    core_api = check_source_isolation(
        "fleetforge.deploy_policies",
        "from fleetforge.api.schemas import DeployAccepted\n",
    )
    assert any("core module imports HTTP layer" in v for v in core_api)

    api_ingestor = check_source_isolation(
        "fleetforge.api.routers.devices",
        "from fleetforge.ingestor.handlers import handle_up_message\n",
    )
    assert any("API module imports ingestor process" in v for v in api_ingestor)

    router_sibling = check_source_isolation(
        "fleetforge.api.routers.commands",
        "from fleetforge.api.routers.deploys import _sender\n",
    )
    assert any("router imports sibling router" in v for v in router_sibling)

    clean = check_source_isolation(
        "fleetforge.api.routers.commands",
        "from fleetforge.api.router_helpers import command_summary\n",
    )
    assert clean == []
