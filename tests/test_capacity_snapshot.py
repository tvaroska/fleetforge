"""Tests for scripts/capacity_snapshot.py.

Pure function tests over fixture text; no docker, no host, no /proc.
Importing via importlib.util to load the script by path.
"""

import importlib.util
from pathlib import Path

# Load the script as a module
script_path = Path(__file__).parent.parent / "scripts" / "capacity_snapshot.py"
spec = importlib.util.spec_from_file_location("capacity_snapshot", script_path)
assert spec and spec.loader
cs = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cs)


def test_parse_meminfo():
    """Parse /proc/meminfo fixture."""
    fixture = """MemTotal:        4014080 kB
MemFree:         1692128 kB
MemAvailable:    2332640 kB
Buffers:           51200 kB
Cached:          1030144 kB
SwapTotal:       2097148 kB
SwapFree:        2085888 kB
"""
    result = cs.parse_meminfo(fixture)

    assert result["MemTotal"] == 4014080 * 1024
    assert result["MemFree"] == 1692128 * 1024
    assert result["MemAvailable"] == 2332640 * 1024
    assert result["SwapTotal"] == 2097148 * 1024
    assert result["SwapFree"] == 2085888 * 1024


def test_parse_vmstat():
    """Parse /proc/vmstat fixture and delta arithmetic."""
    fixture1 = """pswpin 100
pswpout 500
pgmajfault 1000
pgscan_direct 50
pgsteal_direct 40
"""
    fixture2 = """pswpin 120
pswpout 550
pgmajfault 1012
pgscan_direct 55
pgsteal_direct 45
"""
    result1 = cs.parse_vmstat(fixture1)
    result2 = cs.parse_vmstat(fixture2)

    assert result1["pswpin"] == 100
    assert result2["pswpin"] == 120
    # Delta
    assert result2["pswpin"] - result1["pswpin"] == 20
    assert result2["pswpout"] - result1["pswpout"] == 50
    assert result2["pgmajfault"] - result1["pgmajfault"] == 12


def test_parse_memory_events():
    """Parse cgroup memory.events fixture."""
    fixture = """low 0
high 0
max 3
oom 0
oom_kill 0
"""
    result = cs.parse_memory_events(fixture)
    assert result["max"] == 3
    assert result["oom"] == 0
    assert result["oom_kill"] == 0


def test_parse_memory_stat():
    """Parse cgroup memory.stat fixture."""
    fixture = """anon 188743680
file 5242880
"""
    result = cs.parse_memory_stat(fixture)
    assert result["anon"] == 188743680
    assert result["file"] == 5242880


def test_unlimited_container_no_division_by_zero():
    """Container with memory.max='max' does not cause ZeroDivisionError."""
    # Simulate a container with no limit
    container = cs.ContainerInfo(
        id="abc123",
        name="unlimited-container",
        image="test:latest",
        limit=None,  # No limit
        restart_count=0,
        oom_killed=False,
    )
    container.samples.append(
        cs.ContainerSample(
            timestamp=1000.0,
            current=100 * 1024 * 1024,
            peak=150 * 1024 * 1024,
            anon=90 * 1024 * 1024,
            swap_current=0,
            events_max=0,
            events_oom=0,
            events_oom_kill=0,
        )
    )

    # This should not raise
    host_info = {
        "hostname": "test",
        "kernel": "6.1.0",
        "nproc": 2,
        "mem_total": 4 * 1024**3,
        "swap_total": 2 * 1024**3,
        "swappiness": 10,
        "swap_devices": [],
        "disk_total": 25 * 1024**3,
        "disk_free": 7 * 1024**3,
        "gce_machine_type": None,
        "gce_zone": None,
        "gce_instance": None,
    }
    host_samples = [
        cs.HostSample(
            timestamp=1000.0,
            mem_free=2 * 1024**3,
            mem_available=2.5 * 1024**3,
            cached=1 * 1024**3,
            swap_free=2 * 1024**3,
            pswpin=0,
            pswpout=0,
            pgmajfault=0,
            pgscan_direct=0,
            pgsteal_direct=0,
            load_1=0.1,
            load_5=0.1,
            load_15=0.1,
        )
    ]

    verdict, reasons = cs.compute_verdict([container], host_info, host_samples)
    # Should complete without error; verdict is OK (no tight/fail conditions)
    assert verdict in ("OK", "TIGHT", "FAIL")


def test_verdict_oom_kill_is_fail():
    """Verdict is FAIL if any container has oom_kill > 0."""
    container = cs.ContainerInfo(
        id="abc123",
        name="oom-container",
        image="test:latest",
        limit=256 * 1024**2,
        restart_count=1,
        oom_killed=False,
    )
    container.samples.append(
        cs.ContainerSample(
            timestamp=1000.0,
            current=100 * 1024**2,
            peak=250 * 1024**2,
            anon=90 * 1024**2,
            swap_current=0,
            events_max=5,
            events_oom=2,
            events_oom_kill=1,  # OOM kill happened
        )
    )

    host_info = {
        "hostname": "test",
        "kernel": "6.1.0",
        "nproc": 2,
        "mem_total": 4 * 1024**3,
        "swap_total": 2 * 1024**3,
        "swappiness": 10,
        "swap_devices": [],
        "disk_total": 25 * 1024**3,
        "disk_free": 7 * 1024**3,
        "gce_machine_type": None,
        "gce_zone": None,
        "gce_instance": None,
    }
    host_samples = [
        cs.HostSample(
            timestamp=1000.0,
            mem_free=2 * 1024**3,
            mem_available=2.5 * 1024**3,
            cached=1 * 1024**3,
            swap_free=2 * 1024**3,
            pswpin=0,
            pswpout=0,
            pgmajfault=0,
            pgscan_direct=0,
            pgsteal_direct=0,
            load_1=0.1,
            load_5=0.1,
            load_15=0.1,
        )
    ]

    verdict, reasons = cs.compute_verdict([container], host_info, host_samples)
    assert verdict == "FAIL"
    assert any("oom" in r.lower() for r in reasons)


def test_verdict_peak_85_percent_is_tight():
    """Verdict is TIGHT if peak >= 85% of limit."""
    container = cs.ContainerInfo(
        id="abc123",
        name="tight-container",
        image="test:latest",
        limit=256 * 1024**2,
        restart_count=0,
        oom_killed=False,
    )
    # Peak at 220 MiB = 85.9% of 256 MiB limit
    container.samples.append(
        cs.ContainerSample(
            timestamp=1000.0,
            current=200 * 1024**2,
            peak=220 * 1024**2,
            anon=190 * 1024**2,
            swap_current=0,
            events_max=0,
            events_oom=0,
            events_oom_kill=0,
        )
    )

    host_info = {
        "hostname": "test",
        "kernel": "6.1.0",
        "nproc": 2,
        "mem_total": 4 * 1024**3,
        "swap_total": 2 * 1024**3,
        "swappiness": 10,
        "swap_devices": [],
        "disk_total": 25 * 1024**3,
        "disk_free": 7 * 1024**3,
        "gce_machine_type": None,
        "gce_zone": None,
        "gce_instance": None,
    }
    host_samples = [
        cs.HostSample(
            timestamp=1000.0,
            mem_free=2 * 1024**3,
            mem_available=2.5 * 1024**3,
            cached=1 * 1024**3,
            swap_free=2 * 1024**3,
            pswpin=0,
            pswpout=0,
            pgmajfault=0,
            pgscan_direct=0,
            pgsteal_direct=0,
            load_1=0.1,
            load_5=0.1,
            load_15=0.1,
        )
    ]

    verdict, reasons = cs.compute_verdict([container], host_info, host_samples)
    assert verdict == "TIGHT"
    assert any("peak" in r.lower() and "%" in r for r in reasons)


def test_verdict_all_clear_is_ok():
    """Verdict is OK when no tight/fail conditions are met."""
    container = cs.ContainerInfo(
        id="abc123",
        name="healthy-container",
        image="test:latest",
        limit=256 * 1024**2,
        restart_count=0,
        oom_killed=False,
    )
    # Peak at 100 MiB = 39% of 256 MiB limit
    container.samples.append(
        cs.ContainerSample(
            timestamp=1000.0,
            current=90 * 1024**2,
            peak=100 * 1024**2,
            anon=80 * 1024**2,
            swap_current=0,
            events_max=0,
            events_oom=0,
            events_oom_kill=0,
        )
    )

    host_info = {
        "hostname": "test",
        "kernel": "6.1.0",
        "nproc": 2,
        "mem_total": 4 * 1024**3,
        "swap_total": 2 * 1024**3,
        "swappiness": 10,
        "swap_devices": [],
        "disk_total": 25 * 1024**3,
        "disk_free": 7 * 1024**3,
        "gce_machine_type": None,
        "gce_zone": None,
        "gce_instance": None,
    }
    host_samples = [
        cs.HostSample(
            timestamp=1000.0,
            mem_free=2 * 1024**3,
            mem_available=2.5 * 1024**3,
            cached=1 * 1024**3,
            swap_free=2 * 1024**3,
            pswpin=0,
            pswpout=0,
            pgmajfault=0,
            pgscan_direct=0,
            pgsteal_direct=0,
            load_1=0.1,
            load_5=0.1,
            load_15=0.1,
        )
    ]

    verdict, reasons = cs.compute_verdict([container], host_info, host_samples)
    assert verdict == "OK"
    assert len(reasons) == 0


def test_bytes_to_mib_formatting():
    """Byte→MiB formatting is stable (no locale, no float drift)."""
    # 1 MiB exactly
    assert cs.format_mib(1024 * 1024) == "1.0M"
    # 100 MiB
    assert cs.format_mib(100 * 1024 * 1024) == "100.0M"
    # 256 MiB
    assert cs.format_mib(256 * 1024 * 1024) == "256.0M"
    # Fractional
    assert cs.format_mib(1536 * 1024) == "1.5M"
    # Large value
    assert cs.format_mib(1024 * 1024 * 1024) == "1024.0M"
