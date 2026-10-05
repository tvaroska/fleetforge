"""Tests for scripts/bench_net.py (R2b-test-4).

Pure tests over psql-style row text, with an injected fetch function and clock. No database,
no subprocess, no sleeping. The script is loaded by path with importlib.util.
"""

import importlib.util
import io
import sys
from collections.abc import Callable
from pathlib import Path

import pytest

script_path = Path(__file__).parent.parent / "scripts" / "bench_net.py"
spec = importlib.util.spec_from_file_location("bench_net", script_path)
assert spec and spec.loader
bn = importlib.util.module_from_spec(spec)
sys.modules["bench_net"] = bn  # dataclasses resolve annotations through sys.modules
spec.loader.exec_module(bn)

DEV = "94a990dd09a4"


def row(
    *,
    presence: str = "true",
    version: str = "0.4.7",
    link: str = "wifi",
    known: str = "2",
    age: str = "3",
    ssid: str = "home",
    power: str = "always_on",
    device: str = DEV,
) -> str:
    """psql text, columns in the SELECT's order: ssid last."""
    return f"{device}|{power}|{presence}|{version}|{link}|{known}|{age}|{ssid}\n"


def ok(text: str) -> "bn.FetchResult":
    return bn.FetchResult(0, text, "")


def verdict(text: str, ssid: str = "home", known: int = 2) -> "bn.Verdict":
    reading = bn.parse_reading(text)
    assert reading is not None
    return bn.grade(reading, ssid, known)


class Clock:
    """A fake monotonic clock that only `sleep` advances."""

    def __init__(self) -> None:
        self.now = 1000.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


def scripted(*texts: str) -> tuple[Callable[[str], "bn.FetchResult"], list[str]]:
    """A fetch that returns each text in turn (the last one repeats) and records the SQL."""
    calls: list[str] = []

    def fetch(sql: str) -> "bn.FetchResult":
        calls.append(sql)
        return ok(texts[min(len(calls) - 1, len(texts) - 1)])

    return fetch, calls


def run_watch(
    fetch: Callable[[str], "bn.FetchResult"],
    clock: Clock,
    *,
    timeout: int = 60,
    ssid: str = "home",
    known: int = 2,
) -> int:
    return bn.watch(
        fetch,
        DEV,
        ssid,
        known,
        timeout=timeout,
        interval=5,
        sleep=clock.sleep,
        monotonic=clock.monotonic,
        stamp=lambda: "12:00:00",
    )


class TestDevice:
    def test_hex_is_accepted_and_lowercased(self) -> None:
        assert bn.validate_device("94A990DD09A4") == DEV

    @pytest.mark.parametrize(
        "bad",
        [
            "x';--",
            "",
            "94a990dd09a",
            "94a990dd09a4f",
            "94:a9:90:dd:09:a4",
            "94a990dd09a4 ",
            "9" * 12 + "\n",
        ],
    )
    def test_anything_else_is_refused(self, bad: str) -> None:
        with pytest.raises(bn.BenchError, match="refused device"):
            bn.validate_device(bad)

    def test_refused_device_never_reaches_sql_or_fetch(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def boom(_sql: str) -> "bn.FetchResult":
            raise AssertionError("fetch must not run")

        monkeypatch.setattr(bn, "fetch_prod", boom)
        monkeypatch.setattr(bn, "fetch_dev", boom)
        assert bn.main(["--sql", "x';--"]) == bn.EXIT_USAGE
        assert bn.main(["watch", "x';--", "--ssid", "a", "--known", "2", "--where", "dev"]) == 2
        assert bn.main(["watch", "", "--ssid", "a", "--known", "2"]) == 2
        out = capsys.readouterr()
        assert out.out == ""  # no SQL on stdout
        assert "refused device" in out.err

    def test_sql_has_the_device_once_no_double_quote_and_ssid_last(self) -> None:
        sql = bn.build_sql(DEV.upper())
        assert sql.count(DEV) == 1
        assert '"' not in sql
        select = sql.split(" FROM devices")[0]
        assert select.rstrip().endswith("coalesce(ssid, '')")
        assert sql.rstrip().endswith("decommissioned_at IS NULL;")

    def test_sql_option_prints_and_exits_zero(self, capsys: pytest.CaptureFixture[str]) -> None:
        assert bn.main(["--sql", DEV]) == bn.EXIT_PASS
        assert f"'{DEV}'" in capsys.readouterr().out


class TestParsing:
    def test_pipe_inside_ssid_is_kept_whole(self) -> None:
        r = bn.parse_reading(row(ssid="a|b|c"))
        assert r is not None and r.ssid == "a|b|c"

    def test_first_non_empty_line_only(self) -> None:
        r = bn.parse_reading("\n" + row(ssid="first") + row(ssid="second"))
        assert r is not None and r.ssid == "first"

    def test_empty_is_none_and_blank_ssid_is_none(self) -> None:
        assert bn.parse_reading("") is None
        assert bn.parse_reading("\n\n") is None
        r = bn.parse_reading(row(ssid="", known="", age="", presence=""))
        assert r is not None
        assert r.ssid is None and r.known is None and r.age_s is None and not r.online

    def test_short_row_is_an_error(self) -> None:
        with pytest.raises(bn.BenchError, match="unreadable row"):
            bn.parse_reading("a|b|c\n")

    def test_non_numeric_known_is_an_error(self) -> None:
        with pytest.raises(bn.BenchError, match="unreadable row"):
            bn.parse_reading(row(known="two"))

    def test_check_with_no_row_exits_2(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        monkeypatch.setattr(sys, "stdin", io.StringIO(""))
        assert bn.main(["check", "--ssid", "home", "--known", "2"]) == bn.EXIT_USAGE
        assert "no live device" in capsys.readouterr().err


class TestVerdicts:
    def test_pass(self) -> None:
        v = verdict(row())
        assert v.code == bn.EXIT_PASS
        assert v.final == 'NET PASS on "home" (known network count 2)'
        assert v.status == (
            f'{DEV}  online, on: "home"  knows 2 networks  agent 0.4.7  last seen 3 s ago'
        )

    def test_wait_offline_with_the_right_ssid_uses_the_row_copy(self) -> None:
        v = verdict(row(presence="false"))
        assert v.code == bn.EXIT_WAIT
        assert 'offline, last on: "home"' in v.status
        assert v.final.startswith("NET WAIT")

    def test_wait_null_presence_is_offline(self) -> None:
        assert verdict(row(presence="")).code == bn.EXIT_WAIT

    def test_wait_online_on_the_other_ssid(self) -> None:
        v = verdict(row(ssid="hotspot"))
        assert v.code == bn.EXIT_WAIT
        assert 'online, on: "hotspot"' in v.status
        assert 'expected "home"' in v.final

    def test_wait_null_ssid(self) -> None:
        v = verdict(row(ssid=""))
        assert v.code == bn.EXIT_WAIT
        assert "no network reported" in v.status
        assert "no network reported" in v.final

    @pytest.mark.parametrize("version", ["0.3.2", "0.4.5", "0.1.0-sim", "0.0.9"])
    def test_fail_old_agent(self, version: str) -> None:
        v = verdict(row(version=version))
        assert v.code == bn.EXIT_FAIL
        assert "joins only network 1" in v.final and "0.4.6 or later" in v.final

    @pytest.mark.parametrize("version", ["0.4.6", "0.4.7-bench", "0.4.7", "1.0.0", "0.10.0"])
    def test_new_agent_passes_the_gate(self, version: str) -> None:
        assert verdict(row(version=version)).code == bn.EXIT_PASS

    def test_unparseable_version_warns_and_skips_the_gate(self) -> None:
        v = verdict(row(version="dev-build"))
        assert v.code == bn.EXIT_PASS
        assert any(w.startswith("WARN") and "unparseable" in w for w in v.warnings)

    def test_fail_ethernet(self) -> None:
        v = verdict(row(link="ethernet"))
        assert v.code == bn.EXIT_FAIL and "not wifi" in v.final

    def test_fail_wrong_known_count(self) -> None:
        v = verdict(row(known="1"))
        assert v.code == bn.EXIT_FAIL
        assert "reports 1 known networks, expected 2: re-flash with 2" in v.final

    def test_wrong_known_count_fails_even_while_offline(self) -> None:
        assert verdict(row(known="3", presence="false")).code == bn.EXIT_FAIL

    def test_fail_null_known_when_everything_else_passes(self) -> None:
        v = verdict(row(known=""))
        assert v.code == bn.EXIT_FAIL and "does not report known_networks" in v.final

    def test_null_known_while_waiting_is_a_wait(self) -> None:
        assert verdict(row(known="", presence="false")).code == bn.EXIT_WAIT

    def test_ssid_comparison_is_case_sensitive(self) -> None:
        assert verdict(row(ssid="Home"), ssid="home").code == bn.EXIT_WAIT
        assert verdict(row(ssid="home"), ssid="Home").code == bn.EXIT_WAIT
        assert verdict(row(ssid="Home"), ssid="Home").code == bn.EXIT_PASS

    def test_ssid_with_spaces_and_pipe_is_exact(self) -> None:
        assert verdict(row(ssid="my net|2"), ssid="my net|2").code == bn.EXIT_PASS
        assert verdict(row(ssid="my net|2"), ssid="my net|2 ").code == bn.EXIT_WAIT

    def test_sleepy_warns_and_still_uses_presence(self) -> None:
        v = verdict(row(power="sleepy"))
        assert v.code == bn.EXIT_PASS
        assert any("sleepy board" in w for w in v.warnings)

    def test_singular_network_count(self) -> None:
        v = verdict(row(known="1"), known=1)
        assert "knows 1 network " in v.status

    def test_unknown_age_prints_never(self) -> None:
        assert "last seen never" in verdict(row(age="")).status


class TestFetchErrors:
    def test_missing_ssid_column_names_migration_0005(self) -> None:
        err = 'ERROR:  column "ssid" does not exist\nLINE 1: ...\n'

        with pytest.raises(bn.BenchError, match="migration 0005"):
            bn.read_device(lambda _s: bn.FetchResult(1, "", err), DEV)

    def test_missing_known_networks_column_is_the_same_message(self) -> None:
        err = 'ERROR:  column "known_networks" does not exist'
        with pytest.raises(bn.BenchError, match="no ssid column"):
            bn.read_device(lambda _s: bn.FetchResult(1, "", err), DEV)

    def test_other_failure_prints_only_the_first_stderr_line(self) -> None:
        err = "connection refused\nsecret second line\n"
        with pytest.raises(bn.BenchError) as info:
            bn.read_device(lambda _s: bn.FetchResult(255, "", err), DEV)
        assert "connection refused" in str(info.value)
        assert "secret" not in str(info.value)

    def test_no_row_names_the_device(self) -> None:
        with pytest.raises(bn.BenchError, match=f"no live device {DEV}"):
            bn.read_device(lambda _s: ok(""), DEV)


class TestWatch:
    def test_offline_then_other_ssid_then_pass_prints_three_status_lines(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fetch, calls = scripted(
            row(presence="false", ssid="hotspot"),
            row(presence="true", ssid="hotspot"),
            row(presence="true", ssid="home"),
        )
        assert run_watch(fetch, Clock()) == bn.EXIT_PASS
        lines = capsys.readouterr().out.splitlines()
        status = [ln for ln in lines if ln.startswith("12:00:00  ")]
        assert len(status) == 3
        assert 'offline, last on: "hotspot"' in status[0]
        assert 'online, on: "hotspot"' in status[1]
        assert 'online, on: "home"' in status[2]
        assert lines[-1] == 'NET PASS on "home" (known network count 2)'
        assert len(calls) == 3

    def test_a_repeated_status_is_printed_once_even_if_the_age_moves(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fetch, _ = scripted(
            row(presence="false", age="10"),
            row(presence="false", age="15"),
            row(presence="false", age="20"),
            row(presence="true", age="1"),
        )
        assert run_watch(fetch, Clock()) == bn.EXIT_PASS
        out = capsys.readouterr().out.splitlines()
        assert sum(1 for ln in out if ln.startswith("12:00:00  ")) == 2

    def test_never_passing_times_out_with_exit_3(self, capsys: pytest.CaptureFixture[str]) -> None:
        fetch, calls = scripted(row(presence="false"))
        clock = Clock()
        assert run_watch(fetch, clock, timeout=20) == bn.EXIT_WAIT
        out = capsys.readouterr().out.splitlines()
        assert out[-1].startswith("NET TIMEOUT after 20 s: the board is offline")
        assert sum(1 for ln in out if ln.startswith("12:00:00  ")) == 1
        assert len(calls) == 5  # at 0, 5, 10, 15 and 20 s
        assert clock.now == 1020.0

    def test_the_last_sleep_is_clipped_to_the_deadline(self) -> None:
        fetch, _ = scripted(row(presence="false"))
        clock = Clock()
        run_watch(fetch, clock, timeout=7)
        assert clock.sleeps == [5, 2]

    def test_fail_mid_watch_returns_1_at_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        fetch, calls = scripted(row(presence="false"), row(known="1"), row())
        clock = Clock()
        assert run_watch(fetch, clock) == bn.EXIT_FAIL
        assert len(calls) == 2
        assert capsys.readouterr().out.splitlines()[-1].startswith("NET FAIL")

    def test_timeout_zero_makes_exactly_one_fetch(self, capsys: pytest.CaptureFixture[str]) -> None:
        fetch, calls = scripted(row(presence="false"))
        clock = Clock()
        assert run_watch(fetch, clock, timeout=0) == bn.EXIT_WAIT
        assert len(calls) == 1 and clock.sleeps == []
        out = capsys.readouterr().out
        assert "NET WAIT" in out and "NET TIMEOUT" not in out

    def test_fetch_error_mid_watch_exits_2(self, capsys: pytest.CaptureFixture[str]) -> None:
        results = [ok(row(presence="false")), bn.FetchResult(255, "", "ssh: no route\n")]

        def fetch(_sql: str) -> "bn.FetchResult":
            return results.pop(0)

        assert run_watch(fetch, Clock()) == bn.EXIT_USAGE
        assert "no route" in capsys.readouterr().err

    def test_a_warning_is_printed_once(self, capsys: pytest.CaptureFixture[str]) -> None:
        fetch, _ = scripted(
            row(power="sleepy", presence="false"), row(power="sleepy", presence="true")
        )
        assert run_watch(fetch, Clock()) == bn.EXIT_PASS
        assert capsys.readouterr().out.count("WARN sleepy board") == 1


class TestMain:
    def test_check_exit_codes(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        cases = [
            (row(), "NET PASS", 0),
            (row(known="1"), "NET FAIL", 1),
            (row(presence="false"), "NET WAIT", 3),
        ]
        for text, last, code in cases:
            monkeypatch.setattr(sys, "stdin", io.StringIO(text))
            assert bn.main(["check", "--ssid", "home", "--known", "2"]) == code
            assert capsys.readouterr().out.splitlines()[-1].startswith(last)

    def test_watch_through_main_uses_the_chosen_fetch(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        fetch, calls = scripted(row())
        monkeypatch.setattr(bn, "fetch_dev", fetch)
        monkeypatch.setattr(bn, "fetch_prod", lambda _s: pytest.fail("prod must not be used"))
        argv = ["watch", DEV.upper(), "--ssid", "home", "--known", "2", "--where", "dev"]
        assert bn.main([*argv, "--timeout", "0"]) == bn.EXIT_PASS
        assert len(calls) == 1 and f"'{DEV}'" in calls[0]
        assert capsys.readouterr().out.splitlines()[-1].startswith("NET PASS")

    def test_default_where_is_prod(self, monkeypatch: pytest.MonkeyPatch) -> None:
        fetch, calls = scripted(row())
        monkeypatch.setattr(bn, "fetch_prod", fetch)
        assert bn.main(["watch", DEV, "--ssid", "home", "--known", "2", "--timeout", "0"]) == 0
        assert len(calls) == 1

    def test_no_such_device_through_main_exits_2(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(bn, "fetch_dev", lambda _s: ok(""))
        argv = ["watch", DEV, "--ssid", "a", "--known", "2", "--where", "dev", "--timeout", "0"]
        assert bn.main(argv) == bn.EXIT_USAGE

    def test_no_command_is_a_usage_error(self) -> None:
        assert bn.main([]) == bn.EXIT_USAGE

    @pytest.mark.parametrize(
        "argv",
        [
            ["watch", DEV, "--ssid", "a", "--known", "2", "--where", "elsewhere"],
            ["watch", DEV, "--ssid", "a", "--known", "0"],
            ["watch", DEV, "--ssid", "a", "--known", "5"],
            ["watch", DEV, "--ssid", "a", "--known", "x"],
            ["watch", DEV, "--ssid", "", "--known", "2"],
            ["watch", DEV, "--ssid", "a", "--known", "2", "--timeout", "-1"],
            ["watch", DEV, "--ssid", "a", "--known", "2", "--interval", "0"],
        ],
    )
    def test_argparse_refusals_exit_2(self, argv: list[str]) -> None:
        with pytest.raises(SystemExit) as info:
            bn.main(argv)
        assert info.value.code == 2

    def test_a_leading_dash_ssid_works_with_the_equals_form(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        fetch, _ = scripted(row(ssid="-cafe"))
        monkeypatch.setattr(bn, "fetch_dev", fetch)
        argv = ["watch", DEV, "--ssid=-cafe", "--known=2", "--where=dev", "--timeout=0"]
        assert bn.main(argv) == bn.EXIT_PASS
