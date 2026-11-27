"""Continuous verification tests for `scripts/run_deep_verification.py` (`A-34`, `T-15`, `T-25`)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "run_deep_verification.py"


def _load_deep_verification_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("run_deep_verification", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["run_deep_verification"] = module
    spec.loader.exec_module(module)
    return module


def test_deep_verification_quick_gate_passes() -> None:
    """`run_deep_verification.py --quick` passes ASan/UBSan delta fuzzing and kills 100% of mutants."""
    mod = _load_deep_verification_module()
    assert mod.main(["--quick"]) == 0


def test_full_differential_delta_fuzz_passes() -> None:
    """`--full` ASan/UBSan differential `ffdelta-v1` fuzzing (160 pairs + 46 hostile/bitflip cases) passes."""
    mod = _load_deep_verification_module()
    pos_n, neg_n = mod.run_differential_delta_fuzz(iterations=160, seed=20261126, full=True)
    assert pos_n == 160
    assert neg_n == 46


def test_mutation_sweep_fails_closed_when_mutant_survives() -> None:
    """A benign docstring mutation against an unrelated test is reported as surviving (`killed=False`)."""
    mod = _load_deep_verification_module()
    surviving_spec = mod.MutantSpec(
        name="synthetic_benign_docstring_mutation",
        rel_path="config.py",
        target='"""Application settings.',
        replacement='"""Application configuration.',
        pytest_args=("tests/test_isolation_guard.py",),
    )
    outcomes = mod.run_mutation_sweep([surviving_spec], repo_root=REPO_ROOT)
    assert len(outcomes) == 1
    assert outcomes[0].killed is False
    assert outcomes[0].returncode == 0
