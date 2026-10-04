"""Tests for scripts/bench_judge.py (R2b-test-2).

Pure function tests over psql-style row text (`epoch|cmd|state|t/f|from|artifact|detail`),
transcribed from the QEMU/metal transcripts in docs/features/ota-deploy.md. No database.
Importing via importlib.util to load the script by path.
"""

import importlib.util
import io
import sys
from pathlib import Path

import pytest

script_path = Path(__file__).parent.parent / "scripts" / "bench_judge.py"
spec = importlib.util.spec_from_file_location("bench_judge", script_path)
assert spec and spec.loader
bj = importlib.util.module_from_spec(spec)
sys.modules["bench_judge"] = bj  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(bj)

CMD = "761416d5" + "0" * 24
ROLLBACK = "returned to ota_0; ota_1 did not confirm"


def txn(
    states: list[str], frm: str, art: str, *, terminal_detail: str = "", times: list[float] = ()
) -> str:
    """psql text for one transaction; the last state is terminal unless it is open-ended."""
    open_states = {
        "requested",
        "staging",
        "downloading",
        "verifying",
        "staged",
        "applying",
        "rebooting",
        "confirming",
        "rolling_back",
    }
    lines = []
    for i, state in enumerate(states):
        at = times[i] if times else 1_759_600_000.0 + 5 * i
        last = i == len(states) - 1
        term = "t" if last and state not in open_states else "f"
        detail = terminal_detail if last else ""
        lines.append(f"{at}|{CMD}|{state}|{term}|{frm}|{art}|{detail}")
    return "\n".join(lines) + "\n"


def grade(name: str, text: str) -> tuple[str, list]:
    return bj.judge(bj.SCENARIOS[name], bj.parse_rows(text))


def failed(checks: list) -> list[str]:
    return [c.name for c in checks if c.result == "FAIL"]


# ota-deploy.md B1: requested staging downloading verifying staged confirming confirmed|t
CONFIRMED = txn(
    ["requested", "staging", "downloading", "verifying", "staged", "confirming", "confirmed"],
    "0.4.0",
    "0.4.3",
)
# ota-deploy.md F1 / T2-1: on_command bootloop, no confirming
BOOTLOOP = txn(
    ["requested", "staging", "downloading", "verifying", "staged", "rolled_back"],
    "0.4.4",
    "0.4.5-bltest",
    terminal_detail=ROLLBACK,
)


def _hang(art: str, gap: float, with_rebooting: bool = False) -> str:
    states = ["requested", "staging", "downloading", "verifying", "staged"]
    times = [0.0, 1.0, 2.0, 20.0, 22.0]
    if with_rebooting:
        states += ["applying", "rebooting"]
        times += [23.0, 24.0]
    states.append("rolled_back")
    times.append(times[-1] + gap)
    return txn(states, "0.4.4", art, terminal_detail=ROLLBACK, times=times)


# --- scenarios: one PASS each, plus the negatives that matter --------------------------


def test_confirmed_passes():
    verdict, checks = grade("confirmed", CONFIRMED)
    assert verdict == "PASS", checks


def test_confirmed_open_is_incomplete():
    verdict, _ = grade("confirmed", txn(["requested", "staging", "downloading"], "0.4.4", "0.4.5"))
    assert verdict == "INCOMPLETE"


def test_confirmed_judged_as_bootloop_fails():
    verdict, checks = grade("bootloop", CONFIRMED)
    assert verdict == "FAIL"
    assert "image" in failed(checks)
    assert "forbidden-rows" in failed(checks)


def test_rbtest_with_rolling_back_passes():
    # ota-deploy.md B3: staged confirming rolling_back rolled_back|t
    text = txn(
        ["requested", "staging", "downloading", "verifying", "staged", "confirming"]
        + ["rolling_back", "rolled_back"],
        "0.4.3",
        "0.4.4-rbtest",
        terminal_detail="returned to ota_1; ota_0 did not confirm",
    )
    verdict, checks = grade("rbtest", text)
    assert verdict == "PASS", checks
    assert not [c for c in checks if c.result == "WARN"]


def test_rbtest_without_rolling_back_passes_with_warn():
    text = txn(
        ["requested", "staging", "downloading", "verifying", "staged", "confirming", "rolled_back"],
        "0.4.3",
        "0.4.5-rbtest",
        terminal_detail=ROLLBACK,
    )
    verdict, checks = grade("rbtest", text)
    assert verdict == "PASS"
    assert [c.why for c in checks if c.result == "WARN"] == [
        "best-effort row missing (rolling_back)"
    ]


def test_rbtest_window_applies_with_rebooting():
    states = ["requested", "staging", "downloading", "verifying", "staged", "applying"]
    states += ["rebooting", "confirming", "rolling_back", "rolled_back"]
    times = [0, 1, 2, 10, 11, 12, 13, 25, 75, 30.0]
    times[-1] = 13 + 90.0
    verdict, checks = grade(
        "rbtest", txn(states, "0.4.5", "0.4.5-rbtest", terminal_detail=ROLLBACK, times=times)
    )
    assert verdict == "PASS", checks
    times[-1] = 13 + 30.0
    times[-2] = 13 + 29.0
    verdict, checks = grade(
        "rbtest", txn(states, "0.4.5", "0.4.5-rbtest", terminal_detail=ROLLBACK, times=times)
    )
    assert verdict == "FAIL"
    assert failed(checks) == ["window"]


def test_simulator_rollback_detail_fails_only_the_detail_check():
    text = txn(
        ["requested", "staging", "downloading", "verifying", "staged", "confirming"]
        + ["rolling_back", "rolled_back"],
        "9.9.8",
        "9.9.9-rbtest",
        terminal_detail="returned to 9.9.8; the new image did not confirm",
    )
    verdict, checks = grade("rbtest", text)
    assert verdict == "FAIL"
    assert failed(checks) == ["rollback-detail"]


def test_bootloop_passes_and_says_the_bound_is_skipped():
    verdict, checks = grade("bootloop", BOOTLOOP)
    assert verdict == "PASS", checks
    window = next(c for c in checks if c.name == "window")
    assert window.result == "SKIP"
    assert "bound skipped" in window.why


def test_bootloop_with_confirming_fails():
    text = txn(
        ["requested", "staging", "downloading", "verifying", "staged", "confirming", "rolled_back"],
        "0.4.4",
        "0.4.5-bltest",
        terminal_detail=ROLLBACK,
    )
    verdict, checks = grade("bootloop", text)
    assert verdict == "FAIL"
    assert failed(checks) == ["forbidden-rows"]


def test_bootloop_auto_with_rebooting_is_bounded():
    # ota-deploy.md B2: ... applying rebooting rolled_back|t, apply auto
    states = ["requested", "staging", "downloading", "verifying", "staged", "applying"]
    states += ["rebooting", "rolled_back"]
    times = [0, 1, 2, 10, 11, 12, 13, 13 + 40.0]
    verdict, _ = grade(
        "bootloop", txn(states, "0.4.4", "0.4.5-bltest", terminal_detail=ROLLBACK, times=times)
    )
    assert verdict == "PASS"
    times[-1] = 13 + 200.0
    verdict, checks = grade(
        "bootloop", txn(states, "0.4.4", "0.4.5-bltest", terminal_detail=ROLLBACK, times=times)
    )
    assert verdict == "FAIL"
    assert failed(checks) == ["window"]


def test_hang_passes_with_the_gap_printed():
    verdict, checks = grade("hang", _hang("0.4.5-hangtest", 330.0))
    assert verdict == "PASS", checks
    window = next(c for c in checks if c.name == "window")
    assert "staged -> rolled_back = 330.0 s" in window.why


def test_hang_pre_043_is_the_negative_control():
    verdict, checks = grade("hang", _hang("0.4.2-hangtest", 330.0))
    assert verdict == "FAIL"
    assert failed(checks) == ["artifact-version"]
    assert "negative control" in next(c.why for c in checks if c.name == "artifact-version")


def test_hang_too_fast_fails():
    verdict, checks = grade("hang", _hang("0.4.5-hangtest", 120.0, with_rebooting=True))
    assert verdict == "FAIL"
    assert failed(checks) == ["window"]


def test_hang_too_slow_warns():
    verdict, checks = grade("hang", _hang("0.4.5-hangtest", 1000.0))
    assert verdict == "PASS"
    assert next(c for c in checks if c.name == "window").result == "WARN"


def test_power_cut_open_download_passes():
    # ota-deploy.md F2: requested staging downloading (non-terminal by design)
    verdict, checks = grade(
        "power-cut", txn(["requested", "staging", "downloading"], "0.4.2", "0.4.20")
    )
    assert verdict == "PASS", checks
    # F3: ends at verifying
    verdict, _ = grade(
        "power-cut", txn(["requested", "staging", "downloading", "verifying"], "0.4.20", "0.4.23")
    )
    assert verdict == "PASS"


def test_power_cut_with_staged_fails_and_never_incomplete():
    verdict, checks = grade(
        "power-cut",
        txn(["requested", "staging", "downloading", "verifying", "staged"], "0.4.4", "0.4.5"),
    )
    assert verdict == "FAIL"
    assert set(failed(checks)) == {"forbidden-rows", "open-end"}
    verdict, _ = grade("power-cut", txn(["requested", "staging"], "0.4.4", "0.4.5"))
    assert verdict == "FAIL"


def test_power_cut_then_reuse_confirmed():
    # F2 retry: same cmd, rows extend to confirmed
    text = txn(
        ["requested", "staging", "downloading", "staging", "downloading", "verifying", "staged"]
        + ["confirming", "confirmed"],
        "0.4.2",
        "0.4.20",
    )
    verdict, _ = grade("confirmed", text)
    assert verdict == "PASS"


@pytest.mark.parametrize("detail", ["download stalled", "download failed"])
def test_stall_passes_on_either_failure(detail):
    text = txn(
        ["requested", "staging", "downloading", "failed"],
        "0.4.5",
        "0.4.5-bench",
        terminal_detail=detail,
    )
    verdict, checks = grade("stall", text)
    assert verdict == "PASS", checks


def test_stall_other_failure_fails():
    text = txn(
        ["requested", "staging", "downloading", "verifying", "failed"],
        "0.4.5",
        "0.4.5-bench",
        terminal_detail="sha256 mismatch",
    )
    verdict, checks = grade("stall", text)
    assert verdict == "FAIL"
    assert failed(checks) == ["terminal"]
    assert "record it" in next(c.why for c in checks if c.name == "terminal")


def test_stall_still_downloading_is_incomplete():
    verdict, _ = grade(
        "stall", txn(["requested", "staging", "downloading"], "0.4.5", "0.4.5-bench")
    )
    assert verdict == "INCOMPLETE"


def test_stall_from_old_agent_fails():
    text = txn(
        ["requested", "staging", "downloading", "failed"],
        "0.4.3",
        "0.4.5",
        terminal_detail="download failed",
    )
    verdict, checks = grade("stall", text)
    assert verdict == "FAIL"
    assert failed(checks) == ["from-version"]


def test_outage_short_confirmed_or_download_failure():
    verdict, _ = grade(
        "outage-short",
        txn(
            [
                "requested",
                "staging",
                "downloading",
                "verifying",
                "staged",
                "confirming",
                "confirmed",
            ],
            "0.4.5",
            "0.4.5-bench",
        ),
    )
    assert verdict == "PASS"
    verdict, _ = grade(
        "outage-short",
        txn(
            ["requested", "staging", "downloading", "failed"],
            "0.4.5",
            "0.4.5-bench",
            terminal_detail="download failed",
        ),
    )
    assert verdict == "PASS"
    verdict, checks = grade(
        "outage-short",
        txn(
            ["requested", "staging", "downloading", "failed"],
            "0.4.5",
            "0.4.5-bench",
            terminal_detail="no space",
        ),
    )
    assert verdict == "FAIL"
    assert "record it" in next(c.why for c in checks if c.name == "terminal")
    verdict, _ = grade(
        "outage-short",
        txn(
            ["requested", "staging", "downloading", "verifying", "staged", "applying", "rebooting"],
            "0.4.5",
            "0.4.5-bench",
        ),
    )
    assert verdict == "INCOMPLETE"


def test_reboot_outage_long():
    states = ["requested", "staging", "downloading", "verifying", "staged", "applying"]
    states += ["rebooting", "rolled_back"]
    times = [0, 1, 2, 10, 11, 12, 13, 13 + 400.0]
    verdict, checks = grade(
        "reboot-outage-long",
        txn(states, "0.4.5", "0.4.5-bench", terminal_detail=ROLLBACK, times=times),
    )
    assert verdict == "PASS", checks
    times[-1] = 13 + 100.0
    verdict, checks = grade(
        "reboot-outage-long",
        txn(states, "0.4.5", "0.4.5-bench", terminal_detail=ROLLBACK, times=times),
    )
    assert verdict == "FAIL"
    assert failed(checks) == ["window"]


def test_missing_requested_row_fails():
    text = "\n".join(CONFIRMED.splitlines()[1:]) + "\n"
    verdict, checks = grade("confirmed", text)
    assert verdict == "FAIL"
    assert "requested-first" in failed(checks)


def test_from_version_with_suffix_warns():
    text = txn(
        ["requested", "staging", "downloading", "verifying", "staged", "confirming", "confirmed"],
        "0.4.5-rbtest",
        "0.4.5",
    )
    verdict, checks = grade("confirmed", text)
    assert verdict == "PASS"
    assert any(c.result == "WARN" and "not on a normal image" in c.why for c in checks)


def test_unparseable_version_warns_and_skips():
    verdict, checks = grade(
        "stall",
        txn(
            ["requested", "staging", "downloading", "failed"],
            "dev",
            "0.4.5",
            terminal_detail="download failed",
        ),
    )
    assert verdict == "PASS"
    assert next(c for c in checks if c.name == "from-version").result == "WARN"


# --- REF validation and SQL ------------------------------------------------------------


def test_sql_for_cmd_and_device():
    sql = bj.build_sql(CMD.upper())
    assert sql.count(CMD) == 1
    assert '"' not in sql
    assert sql.rstrip().endswith("ORDER BY at, id;")
    dev = bj.build_sql("94A990DD09A4")
    assert "device_id = '94a990dd09a4'" in dev
    assert "state = 'requested'" in dev
    assert '"' not in dev


@pytest.mark.parametrize(
    "ref",
    [
        "x'; drop table",
        "",
        "a" * 31,
        "0123456789ab" + "0",
        "761416d5-0000-4000-8000-000000000000",
        "x';--",
    ],
)
def test_bad_refs_are_refused_with_no_sql(ref, capsys):
    assert bj.main(["--sql", ref]) == 2
    out = capsys.readouterr()
    assert out.out == ""
    assert "refused" in out.err


# --- parsing and main() ----------------------------------------------------------------


def test_detail_with_a_pipe_is_kept_whole():
    rows = bj.parse_rows(f"1.0|{CMD}|failed|t|0.4.5|0.4.6|a | b | c\n")
    assert rows[0].detail == "a | b | c"


def run_main(monkeypatch, capsys, scenario: str, text: str) -> tuple[int, str]:
    monkeypatch.setattr(sys, "stdin", io.StringIO(text))
    code = bj.main([scenario])
    return code, capsys.readouterr().out


def test_main_exit_codes(monkeypatch, capsys):
    code, out = run_main(monkeypatch, capsys, "confirmed", CONFIRMED)
    assert code == 0
    assert out.splitlines()[-1] == "JUDGE PASS confirmed"
    assert out.splitlines()[0].startswith("cmd 761416d5…  0.4.0 -> 0.4.3")
    code, out = run_main(monkeypatch, capsys, "bootloop", CONFIRMED)
    assert code == 1
    assert out.splitlines()[-1] == "JUDGE FAIL bootloop"
    code, out = run_main(
        monkeypatch,
        capsys,
        "stall",
        txn(["requested", "staging", "downloading"], "0.4.5", "0.4.5-bench"),
    )
    assert code == 3
    assert out.splitlines()[-1] == "JUDGE INCOMPLETE stall"
    code, out = run_main(monkeypatch, capsys, "confirmed", "")
    assert code == 2
    assert "no rows" in out
    assert "http" not in out


def test_main_unknown_scenario_and_list(capsys):
    assert bj.main(["nope"]) == 2
    assert bj.main(["--list"]) == 0
    out = capsys.readouterr().out
    for name in (
        "confirmed",
        "rbtest",
        "bootloop",
        "hang",
        "power-cut",
        "outage-short",
        "stall",
        "reboot-outage-long",
    ):
        assert name in out
