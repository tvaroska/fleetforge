#!/usr/bin/env python3
"""Capacity snapshot: host + container memory, disk, swap and verdict.

Reads /proc and cgroup v2 directly — stdlib only, no project venv, no credentials.
Must run unmodified on prod over ssh (Python 3.11.2+). See docs/runbooks/capacity.md.

This output can be pasted into design/production.md — it reads and prints no secrets.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# Page size for vmstat counters
PAGE_SIZE = 4096

# Verdict thresholds
FAIL_PSWPIN_PAGES = 1000
FAIL_MEM_AVAILABLE_MIB = 256
TIGHT_MEM_AVAILABLE_MIB = 512
TIGHT_PEAK_PCT = 85.0


@dataclass
class HostSample:
    """One sample of host-level metrics."""

    timestamp: float
    mem_free: int
    mem_available: int
    cached: int
    swap_free: int
    pswpin: int
    pswpout: int
    pgmajfault: int
    pgscan_direct: int
    pgsteal_direct: int
    load_1: float
    load_5: float
    load_15: float


@dataclass
class ContainerSample:
    """One sample of per-container metrics."""

    timestamp: float
    current: int
    peak: int
    anon: int
    swap_current: int
    events_max: int
    events_oom: int
    events_oom_kill: int


@dataclass
class ContainerInfo:
    """Static container info plus samples."""

    id: str
    name: str
    image: str
    limit: int | None
    restart_count: int
    oom_killed: bool
    samples: list[ContainerSample] = field(default_factory=list)


def parse_meminfo(text: str) -> dict[str, int]:
    """Parse /proc/meminfo into {key: value_in_bytes}."""
    result = {}
    for line in text.strip().split("\n"):
        if ":" not in line:
            continue
        key, rest = line.split(":", 1)
        value_str = rest.strip().split()[0]
        # meminfo is in kB
        result[key] = int(value_str) * 1024
    return result


def parse_vmstat(text: str) -> dict[str, int]:
    """Parse /proc/vmstat into {key: value}."""
    result = {}
    for line in text.strip().split("\n"):
        if " " not in line:
            continue
        key, value = line.split(None, 1)
        result[key] = int(value)
    return result


def parse_memory_stat(text: str) -> dict[str, int]:
    """Parse cgroup memory.stat into {key: value}."""
    result = {}
    for line in text.strip().split("\n"):
        if " " not in line:
            continue
        key, value = line.split(None, 1)
        result[key] = int(value)
    return result


def parse_memory_events(text: str) -> dict[str, int]:
    """Parse cgroup memory.events into {key: value}."""
    result = {}
    for line in text.strip().split("\n"):
        if " " not in line:
            continue
        key, value = line.split(None, 1)
        result[key] = int(value)
    return result


def read_file_safe(path: Path) -> str | None:
    """Read a file, return None on any error (file may not exist)."""
    try:
        return path.read_text()
    except Exception:  # noqa: BLE001 - file may not exist, graceful fallback
        return None


def get_gce_metadata(key: str) -> str | None:
    """Fetch GCE metadata, return None if not on GCE or timeout."""
    url = f"http://metadata.google.internal/computeMetadata/v1/{key}"
    try:
        req = urllib.request.Request(url, headers={"Metadata-Flavor": "Google"})
        with urllib.request.urlopen(req, timeout=1) as response:  # noqa: S310 - http(s) URL from the adapter
            data: bytes = response.read()
            return data.decode("utf-8").strip()
    except Exception:  # noqa: BLE001 - not on GCE or timeout, graceful fallback
        return None


def get_host_info() -> dict[str, Any]:
    """Collect static host information."""
    uname = os.uname()
    meminfo_text = Path("/proc/meminfo").read_text()
    meminfo = parse_meminfo(meminfo_text)

    swaps_text = Path("/proc/swaps").read_text()
    swap_info = []
    for line in swaps_text.strip().split("\n")[1:]:  # skip header
        if line:
            swap_info.append(line)

    disk_usage = shutil.disk_usage("/")

    # Try to get vm.swappiness
    swappiness = None
    try:
        result = subprocess.run(
            ["sysctl", "-n", "vm.swappiness"],  # noqa: S603,S607 - fixed argv, no shell
            capture_output=True,
            text=True,
            timeout=2,
        )
        if result.returncode == 0:
            swappiness = int(result.stdout.strip())
    except Exception:  # noqa: BLE001,S110 - sysctl may not exist, graceful fallback
        pass

    # GCE metadata
    machine_type = get_gce_metadata("instance/machine-type")
    if machine_type and "/" in machine_type:
        # Extract just the type from projects/.../zones/.../machineTypes/e2-medium
        machine_type = machine_type.split("/")[-1]
    zone = get_gce_metadata("instance/zone")
    if zone and "/" in zone:
        zone = zone.split("/")[-1]
    instance_name = get_gce_metadata("instance/name")

    return {
        "hostname": uname.nodename,
        "kernel": uname.release,
        "nproc": os.cpu_count() or 0,
        "mem_total": meminfo.get("MemTotal", 0),
        "swap_total": meminfo.get("SwapTotal", 0),
        "swappiness": swappiness,
        "swap_devices": swap_info,
        "disk_total": disk_usage.total,
        "disk_free": disk_usage.free,
        "gce_machine_type": machine_type,
        "gce_zone": zone,
        "gce_instance": instance_name,
    }


def sample_host() -> HostSample:
    """Take one host sample."""
    meminfo = parse_meminfo(Path("/proc/meminfo").read_text())
    vmstat = parse_vmstat(Path("/proc/vmstat").read_text())
    load = os.getloadavg()

    return HostSample(
        timestamp=time.time(),
        mem_free=meminfo.get("MemFree", 0),
        mem_available=meminfo.get("MemAvailable", 0),
        cached=meminfo.get("Cached", 0),
        swap_free=meminfo.get("SwapFree", 0),
        pswpin=vmstat.get("pswpin", 0),
        pswpout=vmstat.get("pswpout", 0),
        pgmajfault=vmstat.get("pgmajfault", 0),
        pgscan_direct=vmstat.get("pgscan_direct", 0),
        pgsteal_direct=vmstat.get("pgsteal_direct", 0),
        load_1=load[0],
        load_5=load[1],
        load_15=load[2],
    )


def get_container_list(match_filter: str | None = None) -> list[ContainerInfo]:
    """Get list of running containers via docker."""
    # Get container IDs and names
    result = subprocess.run(
        ["docker", "ps", "--format", "{{.ID}} {{.Names}}"],  # noqa: S603,S607 - fixed argv, no shell
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    containers = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split(None, 1)
        if len(parts) != 2:
            continue
        cid, name = parts

        # Apply filter if given
        if match_filter and match_filter not in name:
            continue

        containers.append({"id": cid, "name": name})

    if not containers:
        return []

    # Get full container info via docker inspect
    all_ids = [c["id"] for c in containers]
    inspect_format = "{{.Id}}\t{{.Name}}\t{{.HostConfig.Memory}}\t{{.RestartCount}}\t{{.State.OOMKilled}}\t{{.Config.Image}}"
    result = subprocess.run(  # noqa: S603,S607 - fixed argv, no shell
        ["docker", "inspect", "--format", inspect_format] + all_ids,
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    )

    container_infos = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        parts = line.split("\t")
        if len(parts) != 6:
            continue

        cid_full, name, mem_str, restart_str, oom_str, image = parts
        # docker inspect returns name with leading /
        name = name.lstrip("/")

        # Memory limit: 0 means no limit
        mem_limit_int = int(mem_str) if mem_str else 0
        mem_limit: int | None = None if mem_limit_int == 0 else mem_limit_int

        container_infos.append(
            ContainerInfo(
                id=cid_full,
                name=name,
                image=image,
                limit=mem_limit,
                restart_count=int(restart_str) if restart_str else 0,
                oom_killed=(oom_str.lower() == "true"),
            )
        )

    return container_infos


def find_cgroup_path(container_id: str) -> Path | None:
    """Find cgroup v2 path for a container, with fallbacks."""
    # Try cgroup v2 system.slice first (verified on both hosts)
    path = Path(f"/sys/fs/cgroup/system.slice/docker-{container_id}.scope")
    if path.exists():
        return path

    # Fallback: try /sys/fs/cgroup/docker/<id>
    path = Path(f"/sys/fs/cgroup/docker/{container_id}")
    if path.exists():
        return path

    # Fallback: cgroup v1
    path = Path(f"/sys/fs/cgroup/memory/docker/{container_id}")
    if path.exists():
        return path

    return None


def sample_container(container: ContainerInfo) -> ContainerSample | None:
    """Take one container sample via cgroup."""
    cgroup_path = find_cgroup_path(container.id)
    if not cgroup_path:
        return None

    # Read cgroup files
    current_text = read_file_safe(cgroup_path / "memory.current")
    peak_text = read_file_safe(cgroup_path / "memory.peak")
    stat_text = read_file_safe(cgroup_path / "memory.stat")
    swap_current_text = read_file_safe(cgroup_path / "memory.swap.current")
    events_text = read_file_safe(cgroup_path / "memory.events")

    if current_text is None:
        return None

    current = int(current_text.strip())
    peak = int(peak_text.strip()) if peak_text else current

    stat = parse_memory_stat(stat_text) if stat_text else {}
    anon = stat.get("anon", 0)

    swap_current = int(swap_current_text.strip()) if swap_current_text else 0

    events = parse_memory_events(events_text) if events_text else {}

    return ContainerSample(
        timestamp=time.time(),
        current=current,
        peak=peak,
        anon=anon,
        swap_current=swap_current,
        events_max=events.get("max", 0),
        events_oom=events.get("oom", 0),
        events_oom_kill=events.get("oom_kill", 0),
    )


def bytes_to_mib(b: int) -> float:
    """Convert bytes to MiB."""
    return b / (1024 * 1024)


def format_mib(b: int) -> str:
    """Format bytes as MiB string."""
    return f"{bytes_to_mib(b):.1f}M"


def format_gib(b: int) -> str:
    """Format bytes as GiB string."""
    return f"{b / (1024**3):.1f}G"


def compute_verdict(
    containers: list[ContainerInfo],
    host_info: dict[str, Any],
    host_samples: list[HostSample],
) -> tuple[str, list[str]]:
    """Compute verdict: FAIL, TIGHT, or OK, with reasons."""
    reasons = []

    # Check for OOM kills
    oom_kills = [
        c.name for c in containers if c.oom_killed or any(s.events_oom_kill > 0 for s in c.samples)
    ]
    if oom_kills:
        reasons.append(f"OOM kill: {', '.join(oom_kills)}")
        return "FAIL", reasons

    # Check pswpin delta (thrashing)
    if len(host_samples) >= 2:
        pswpin_delta = host_samples[-1].pswpin - host_samples[0].pswpin
        if pswpin_delta > FAIL_PSWPIN_PAGES:
            duration = host_samples[-1].timestamp - host_samples[0].timestamp
            reasons.append(f"pswpin delta {pswpin_delta} pages over {duration:.0f}s")
            return "FAIL", reasons

    # Check MemAvailable floor
    mem_avail_floor = min(s.mem_available for s in host_samples)
    mem_avail_floor_mib = bytes_to_mib(mem_avail_floor)
    if mem_avail_floor_mib < FAIL_MEM_AVAILABLE_MIB:
        reasons.append(f"MemAvailable floor {mem_avail_floor_mib:.0f} MiB")
        return "FAIL", reasons

    # TIGHT conditions
    tight = False

    # Check peak% >= 85%
    for c in containers:
        if not c.limit or not c.samples:
            continue
        peak = max(s.peak for s in c.samples)
        peak_pct = (peak / c.limit) * 100
        if peak_pct >= TIGHT_PEAK_PCT:
            reasons.append(f"{c.name} peak {peak_pct:.0f}% of limit")
            tight = True

    # Check memory.events max
    containers_with_max = [c.name for c in containers if any(s.events_max > 0 for s in c.samples)]
    if containers_with_max:
        reasons.append(f"memory.events max > 0: {', '.join(containers_with_max)}")
        tight = True

    # Check MemAvailable floor < 512 MiB
    if mem_avail_floor_mib < TIGHT_MEM_AVAILABLE_MIB:
        reasons.append(f"MemAvailable floor {mem_avail_floor_mib:.0f} MiB")
        tight = True

    # Check declared overcommit
    total_limits = sum(c.limit for c in containers if c.limit)
    mem_total = host_info["mem_total"]
    if total_limits > mem_total:
        pct = (total_limits / mem_total) * 100
        reasons.append(f"declared limits {pct:.0f}% of MemTotal")
        tight = True

    if tight:
        return "TIGHT", reasons

    return "OK", []


def print_human_report(
    host_info: dict[str, Any],
    host_samples: list[HostSample],
    containers: list[ContainerInfo],
    label: str | None,
) -> None:
    """Print human-readable report."""
    # Host header
    hostname = host_info["hostname"]
    kernel = host_info["kernel"].split("-")[0]  # shortened
    nproc = host_info["nproc"]

    header_parts = [f"host  {hostname}"]
    if host_info["gce_machine_type"]:
        header_parts.append(f"({host_info['gce_machine_type']}, {nproc} vCPU)")
    header_parts.append(f"kernel {kernel}")
    if label:
        header_parts.append(f"— {label}")

    print(f"== {' '.join(header_parts)} ==")

    # Memory
    mem_total_mib = bytes_to_mib(host_info["mem_total"])
    if len(host_samples) > 1:
        mem_avail_floor = min(s.mem_available for s in host_samples)
        mem_avail_floor_mib = bytes_to_mib(mem_avail_floor)
        print(
            f"mem      {mem_total_mib:.0f} MiB total · available floor {mem_avail_floor_mib:.0f} MiB (min over {len(host_samples)} samples)"
        )
    else:
        mem_avail = host_samples[0].mem_available
        mem_avail_mib = bytes_to_mib(mem_avail)
        print(f"mem      {mem_total_mib:.0f} MiB total · {mem_avail_mib:.0f} MiB available")

    # Swap
    swap_total_mib = bytes_to_mib(host_info["swap_total"])
    swap_used = host_info["swap_total"] - host_samples[-1].swap_free
    swap_used_mib = bytes_to_mib(swap_used)

    # Total swap.current across containers
    total_container_swap = sum(c.samples[-1].swap_current for c in containers if c.samples)
    total_container_swap_mib = bytes_to_mib(total_container_swap)

    print(
        f"swap     {swap_total_mib:.0f} MiB total · {swap_used_mib:.0f} MiB used · swap.current across containers {total_container_swap_mib:.0f} MiB"
    )

    # Paging deltas
    if len(host_samples) > 1:
        first, last = host_samples[0], host_samples[-1]
        duration = last.timestamp - first.timestamp
        pswpin_delta = last.pswpin - first.pswpin
        pswpout_delta = last.pswpout - first.pswpout
        pgmajfault_delta = last.pgmajfault - first.pgmajfault
        print(
            f"paging   pswpin +{pswpin_delta}  pswpout +{pswpout_delta}  pgmajfault +{pgmajfault_delta}   (delta over {duration:.0f} s)"
        )

    # Disk
    disk_total_g = bytes_to_mib(host_info["disk_total"]) / 1024
    disk_free_g = bytes_to_mib(host_info["disk_free"]) / 1024
    disk_used_pct = (
        (host_info["disk_total"] - host_info["disk_free"]) / host_info["disk_total"]
    ) * 100
    print(
        f"disk     /  {disk_total_g:.1f} G · {disk_free_g:.1f} G free ({disk_used_pct:.0f}% used)"
    )

    # Load
    load = host_samples[-1]
    print(f"load     {load.load_1:.2f} {load.load_5:.2f} {load.load_15:.2f}")
    print()

    # Containers table
    print(f"== containers ({len(containers)}) ==")
    print(
        f"{'NAME':<25} {'CUR':>8} {'PEAK':>8} {'ANON':>8} {'SWAP':>8} {'LIMIT':>7} {'PEAK%':>6} {'EVT(max/oom)':>13}"
    )

    for c in containers:
        if not c.samples:
            print(f"{c.name:<25} cgroup: unavailable")
            continue

        sample = c.samples[-1]
        # Track max across samples for peak
        peak = max(s.peak for s in c.samples)

        cur_str = format_mib(sample.current)
        peak_str = format_mib(peak)
        anon_str = format_mib(sample.anon)
        swap_str = format_mib(sample.swap_current)

        if c.limit:
            limit_str = format_mib(c.limit)
            peak_pct = (peak / c.limit) * 100
            peak_pct_str = f"{peak_pct:5.1f}%"
        else:
            limit_str = "max"
            peak_pct_str = "-"

        evt_max = max(s.events_max for s in c.samples)
        evt_oom = max(s.events_oom_kill for s in c.samples)
        evt_str = f"{evt_max}/{evt_oom}"

        print(
            f"{c.name:<25} {cur_str:>8} {peak_str:>8} {anon_str:>8} {swap_str:>8} {limit_str:>7} {peak_pct_str:>6} {evt_str:>13}"
        )

    # Declared limits summary
    total_limits = sum(c.limit for c in containers if c.limit)
    total_limits_mib = bytes_to_mib(total_limits)
    mem_total_mib = bytes_to_mib(host_info["mem_total"])
    commit_pct = (total_limits / host_info["mem_total"]) * 100
    print(
        f"-- declared limits {total_limits_mib:.0f} MiB / {mem_total_mib:.0f} MiB MemTotal = {commit_pct:.0f}% committed"
    )
    print()

    # Verdict
    verdict, reasons = compute_verdict(containers, host_info, host_samples)
    print(f"VERDICT: {verdict}")

    mem_avail_floor = min(s.mem_available for s in host_samples)
    mem_avail_floor_mib = bytes_to_mib(mem_avail_floor)
    print(f"  headroom  MemAvailable floor {mem_avail_floor_mib:.0f} MiB")

    containers_with_max = [c.name for c in containers if any(s.events_max > 0 for s in c.samples)]
    print(f"  reclaim   {len(containers_with_max)} containers hit their limit (memory.events max)")

    if len(host_samples) > 1:
        pswpin_delta = host_samples[-1].pswpin - host_samples[0].pswpin
        duration = host_samples[-1].timestamp - host_samples[0].timestamp
        print(f"  thrash    pswpin delta {pswpin_delta} over {duration:.0f} s")

    if reasons:
        for reason in reasons:
            print(f"  - {reason}")


def print_json_report(
    host_info: dict[str, Any],
    host_samples: list[HostSample],
    containers: list[ContainerInfo],
) -> None:
    """Print JSON report."""
    verdict, reasons = compute_verdict(containers, host_info, host_samples)

    # Build container data
    container_data = []
    for c in containers:
        if not c.samples:
            continue

        sample = c.samples[-1]
        peak = max(s.peak for s in c.samples)
        evt_max = max(s.events_max for s in c.samples)
        evt_oom_kill = max(s.events_oom_kill for s in c.samples)

        container_data.append(
            {
                "name": c.name,
                "image": c.image,
                "current": sample.current,
                "peak": peak,
                "anon": sample.anon,
                "swap_current": sample.swap_current,
                "limit": c.limit,
                "events_max": evt_max,
                "events_oom_kill": evt_oom_kill,
            }
        )

    # Build host sample summary
    mem_avail_floor = min(s.mem_available for s in host_samples)

    if len(host_samples) > 1:
        first, last = host_samples[0], host_samples[-1]
        duration = last.timestamp - first.timestamp
        vmstat_deltas = {
            "pswpin": last.pswpin - first.pswpin,
            "pswpout": last.pswpout - first.pswpout,
            "pgmajfault": last.pgmajfault - first.pgmajfault,
            "duration_s": duration,
        }
    else:
        vmstat_deltas = None

    report = {
        "host": host_info,
        "samples": len(host_samples),
        "mem_available_floor": mem_avail_floor,
        "vmstat_deltas": vmstat_deltas,
        "containers": container_data,
        "verdict": verdict,
        "reasons": reasons,
    }

    print(json.dumps(report, indent=2))


def main() -> int:
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description="Capacity snapshot: host + container memory, disk, swap and verdict"
    )
    parser.add_argument(
        "--watch",
        type=int,
        default=0,
        metavar="SECONDS",
        help="Sample for this many seconds (default: 0 = one sample)",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=30,
        metavar="SECONDS",
        help="Sampling interval in seconds (default: 30)",
    )
    parser.add_argument(
        "--json", action="store_true", help="Output JSON instead of human-readable report"
    )
    parser.add_argument(
        "--label", type=str, help="Label for this run (appears in human output only)"
    )
    parser.add_argument(
        "--match",
        type=str,
        metavar="SUBSTRING",
        help="Only include containers whose names contain this substring",
    )

    args = parser.parse_args()

    # Collect static info
    try:
        host_info = get_host_info()
        containers = get_container_list(args.match)
    except Exception as e:  # noqa: BLE001 - top-level error handler, print and exit
        print(f"Error collecting host/container info: {e}", file=sys.stderr)
        return 1

    # Sampling loop
    host_samples = []
    start_time = time.time()

    try:
        while True:
            # Sample host
            host_sample = sample_host()
            host_samples.append(host_sample)

            # Sample containers
            for container in containers:
                sample = sample_container(container)
                if sample:
                    container.samples.append(sample)

            # Check if we're done
            if args.watch == 0:
                break

            elapsed = time.time() - start_time
            if elapsed >= args.watch:
                break

            # Sleep until next interval
            time.sleep(args.interval)

    except KeyboardInterrupt:
        # Allow Ctrl-C to end early
        pass

    # Print report
    if args.json:
        print_json_report(host_info, host_samples, containers)
    else:
        print_human_report(host_info, host_samples, containers, args.label)

    # Exit code based on verdict
    verdict, _ = compute_verdict(containers, host_info, host_samples)
    return 1 if verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
