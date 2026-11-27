#!/usr/bin/env python3
"""Deep verification runner: ASan/UBSan differential delta fuzzing and mutation sweeps.

Executes two isolated verification stages in temporary directories without modifying the repo:
- Differential `ffdelta-v1` fuzzing (`A-34`): compiles `agent/components/fleetforge/src/ff_delta.c`
  with GCC `-fsanitize=address,undefined`, verifies byte parity between `fleetforge.delta_ota`
  and C `ff_delta`, and asserts corrupt/truncated/OOB/integer-overflow patches fail cleanly.
- 1-line mutation sweeps (`T-15`, `T-25`): mutates isolated copies of `url_safety.py`,
  `deploy_policy_gates.py`, `rollout_state.py`, and `artifact_urls.py` (plus PostgreSQL-backed
  `api/deps.py` and `rollout_state.py` mutants when `--with-db` or `--full` + `TEST_DATABASE_URL`
  is active), exiting `0` only when `100%` of active mutants are killed (`returncode != 0`).
"""

from __future__ import annotations

import argparse
import hashlib
import os
import random
import shutil
import struct
import subprocess
import sys
import tempfile
import time
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from fleetforge.delta_ota import (
    DELTA_MAGIC,
    HEADER_SIZE,
    MAX_DELTA_TARGET_BYTES,
    OP_COPY,
    OP_END,
    OP_INSERT,
    DeltaPatchError,
    apply_delta_patch,
    create_delta_patch,
)

REPO_ROOT = Path(__file__).resolve().parents[1]

_C_DRIVER_SOURCE = r"""
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include "ff_delta.h"

static int io_read(FILE *fp, void *dst, size_t n) {
    return (n == 0U || fread(dst, 1U, n, fp) == n) ? 0 : -1;
}
static int io_write(FILE *fp, const void *src, size_t n) {
    return (n == 0U || fwrite(src, 1U, n, fp) == n) ? 0 : -1;
}
int main(int argc, char **argv) {
    if (argc != 3) return 2;
    ff_delta_header_t hdr;
    uint8_t zsha[FF_DELTA_SHA256_BYTES] = {0}, zbuf[8] = {0};
    size_t zwr = 0U;
    if (ff_delta_parse_header(NULL, FF_DELTA_HEADER_BYTES, &hdr) == FF_DELTA_OK) return 3;
    if (ff_delta_apply_in_memory(NULL, 1U, zsha, zbuf, 8U, zbuf, 1U, &zwr) == FF_DELTA_OK) return 4;
    FILE *fin = fopen(argv[1], "rb"), *fout = fopen(argv[2], "wb");
    uint32_t count = 0U;
    if (!fin || !fout || io_read(fin, &count, sizeof(count)) != 0) return 5;
    for (uint32_t i = 0U; i < count; ++i) {
        uint32_t meta[3] = {0U, 0U, 0U};
        uint8_t bsha[FF_DELTA_SHA256_BYTES];
        if (io_read(fin, meta, sizeof(meta)) != 0 || io_read(fin, bsha, sizeof(bsha)) != 0) return 6;
        uint8_t *bbuf = (uint8_t *)malloc(meta[0] ? (size_t)meta[0] : 1U);
        uint8_t *pbuf = (uint8_t *)malloc(meta[1] ? (size_t)meta[1] : 1U);
        uint8_t *obuf = (uint8_t *)malloc(meta[2] ? (size_t)meta[2] : 1U);
        if (!bbuf || !pbuf || !obuf) return 7;
        if (io_read(fin, bbuf, meta[0]) != 0 || io_read(fin, pbuf, meta[1]) != 0) return 8;
        (void)ff_delta_parse_header(pbuf, (size_t)meta[1], &hdr);
        size_t written = 0U;
        ff_delta_err_t rc = ff_delta_apply_in_memory(
            bbuf, meta[0], bsha, pbuf, meta[1], obuf, meta[2], &written);
        int32_t rc32 = (int32_t)rc;
        uint32_t wr32 = (rc == FF_DELTA_OK) ? (uint32_t)written : 0U;
        if (io_write(fout, &rc32, 4U) != 0 || io_write(fout, &wr32, 4U) != 0) return 9;
        if (rc == FF_DELTA_OK) {
            if (io_write(fout, hdr.to_sha256, FF_DELTA_SHA256_BYTES) != 0) return 10;
            if (wr32 > 0U && io_write(fout, obuf, wr32) != 0) return 11;
        }
        free(obuf);
        free(pbuf);
        free(bbuf);
    }
    fclose(fin);
    return fclose(fout) == 0 ? 0 : 11;
}
"""


@dataclass(frozen=True)
class _DeltaCase:
    label: str
    base: bytes
    patch: bytes
    out_cap: int
    expected_target: bytes | None
    allow_digest_reject: bool = False


@dataclass(frozen=True)
class _WireSpec:
    u32_fmt: str
    target_size_off: int
    from_sha_off: int
    to_sha_off: int
    copy_fmt: str
    insert_len_fmt: str


@dataclass(frozen=True)
class MutantSpec:
    name: str
    rel_path: str
    target: str
    replacement: str
    pytest_args: tuple[str, ...]
    requires_db: bool = False


@dataclass(frozen=True)
class MutantOutcome:
    name: str
    rel_path: str
    killed: bool
    returncode: int
    elapsed_s: float


_URL_T = ("tests/test_project_cd_and_secrets.py", "-k", "reject_ssrf_targets_offline")
_GATE_T = ("tests/test_deploy_policy.py", "-k", "detect_ring_halt_offline_conditions")
_AIR_T = ("tests/test_deploy_policy.py", "-k", "enforces_link_and_gateway_airtime_caps")
_SUP_T = ("tests/test_deploy_policy.py", "-k", "supersession_guard_blocks_downgrade")
_SIG_T = ("tests/test_cli_provenance.py", "-k", "enforces_hmac_signature_and_expiry")
_RBAC_T = ("tests/test_project_rollout.py", "-k", "canary_waves_halt_and_rbac_over_postgres")
_RECON_T = ("tests/test_project_rollout.py", "-k", "reconcile_does_not_restage_a_superseded")
_HOST_TGT = (
    "if host in _FORBIDDEN_HOSTNAMES or any(host.endswith(suf) for suf in _FORBIDDEN_SUFFIXES):"
)
_HMAC_TGT = "if not hmac.compare_digest(_signature(sha256, expires_at, secret), sig):"
_EXP_TGT = "if expires_at <= int((now or now_utc()).timestamp()):"

_IP_TGT = "    else:\n        _check_ip(ip, field_name)\n\n    return trimmed"
_IP_REP = "    else:\n        pass\n\n    return trimmed"
_ROLL_TGT = (
    '            row.status = "superseded"\n'
    '            row.close_reason = f"superseded by rollout of {version}"'
)
_ROLL_REP = (
    '            row.status = "open"\n'
    '            row.close_reason = f"superseded by rollout of {version}"'
)

MUTANT_SPECS: tuple[MutantSpec, ...] = (
    MutantSpec("url_ip_guard", "url_safety.py", _IP_TGT, _IP_REP, _URL_T),
    MutantSpec("url_host_guard", "url_safety.py", _HOST_TGT, "if False:", _URL_T),
    MutantSpec(
        "gate_rollback",
        "deploy_policy_gates.py",
        'if snap.state == "rolled_back":',
        "if False:",
        _GATE_T,
    ),
    MutantSpec(
        "gate_heap",
        "deploy_policy_gates.py",
        "and dev.free_heap < min_free_heap_bytes",
        "and False",
        _GATE_T,
    ),
    MutantSpec("lora_airtime", "rollout_state.py", '"lora": 1,', '"lora": 99,', _AIR_T),
    MutantSpec(
        "supersede",
        "rollout_state.py",
        'snap.state == "confirmed" or not snap.is_terminal',
        "False",
        _SUP_T,
    ),
    MutantSpec("url_hmac", "artifact_urls.py", _HMAC_TGT, "if False:", _SIG_T),
    MutantSpec("url_expiry", "artifact_urls.py", _EXP_TGT, "if False:", _SIG_T),
    MutantSpec(
        "deps_superadmin",
        "api/deps.py",
        "user_is_superadmin = admin.is_superadmin",
        "user_is_superadmin = True",
        _RBAC_T,
        True,
    ),
    MutantSpec(
        "rollout_status",
        "rollout_state.py",
        _ROLL_TGT,
        _ROLL_REP,
        _RECON_T,
        True,
    ),
)


def _introspect_wire_format() -> _WireSpec:
    base, target_ins = b"01234567" * 32, b"XYZ"
    patch_ins = create_delta_patch(b"A" * 16, target_ins)
    hdr = patch_ins[:HEADER_SIZE]
    from_off = hdr.index(hashlib.sha256(b"A" * 16).digest())
    to_off = hdr.index(hashlib.sha256(target_ins).digest())
    used = set(range(8)) | set(range(from_off, from_off + 32)) | set(range(to_off, to_off + 32))
    size_off = min(idx for idx in range(HEADER_SIZE) if idx not in used)
    u32_fmt = "<I" if struct.unpack("<I", hdr[size_off : size_off + 4])[0] == 3 else ">I"
    endian = u32_fmt[0]
    ins_w = len(patch_ins) - HEADER_SIZE - 1 - len(target_ins) - 1
    copy_w = (len(create_delta_patch(base, base)) - HEADER_SIZE - 2) // 2
    cu = "H" if copy_w == 2 else "I"
    return _WireSpec(
        u32_fmt,
        size_off,
        from_off,
        to_off,
        f"{endian}{cu}{cu}",
        f"{endian}{'H' if ins_w == 2 else 'I'}",
    )


def _deterministic_pairs(iterations: int, seed: int) -> list[tuple[bytes, bytes]]:
    rng = random.Random(seed)  # noqa: S311
    b64k = bytes((idx * 31 + 7) & 0xFF for idx in range(65537))
    t70k = b"BOOT-HDR" + b64k[128:32000] + (b"\x00" * 4096) + b64k[30000:] + b"END"
    pairs: list[tuple[bytes, bytes]] = [
        (b"", b"\x42"),
        (b"\x00", b"\x00"),
        (b"\x00", b"\xff"),
        (b"a", b"ab"),
        (b"abcd", b"x"),
        (b"\x00" * 8192, b"\x00" * 12288),
        (b"\xff" * 4096, (b"\xff" * 2048) + (b"\x00" * 1024) + (b"\xff" * 2048)),
        (b64k, t70k),
    ]
    while len(pairs) < iterations:
        base = rng.randbytes(rng.choice([1, 7, 63, 64, 65, 511, 2048, 8192, 19000]))
        parts, cur = [], 0
        while cur < len(base):
            step = rng.randint(1, max(1, len(base) // 3))
            mode = rng.randrange(4)
            chunk = (
                base[cur : cur + step]
                if mode < 2
                else (
                    rng.randbytes(rng.randint(1, 96))
                    if mode == 2
                    else bytes([rng.randrange(256)]) * rng.randint(1, 256)
                )
            )
            parts.append(chunk)
            cur += step
        pairs.append((base, b"".join(parts) or b"\x7e"))
    return pairs[:iterations]


def _make_header(spec: _WireSpec, base: bytes, target_bytes: bytes, target_size: int) -> bytes:
    buf = bytearray(HEADER_SIZE)
    buf[0:8] = DELTA_MAGIC
    buf[spec.from_sha_off : spec.from_sha_off + 32] = hashlib.sha256(base).digest()
    buf[spec.to_sha_off : spec.to_sha_off + 32] = hashlib.sha256(target_bytes).digest()
    buf[spec.target_size_off : spec.target_size_off + 4] = struct.pack(spec.u32_fmt, target_size)
    return bytes(buf)


def _corrupt_cases(seed: int, *, full: bool) -> list[_DeltaCase]:
    spec = _introspect_wire_format()
    base = random.Random(seed ^ 0x1357).randbytes(1024)  # noqa: S311
    target = b"HDR!" + base[16:700] + b"TAIL"
    valid = create_delta_patch(base, target)
    hdr, cap = valid[:HEADER_SIZE], len(target)

    def _flip(data: bytes, idx: int) -> bytes:
        arr = bytearray(data)
        arr[idx] ^= 0x01
        return bytes(arr)

    pcopy = lambda off, ln: bytes([OP_COPY]) + struct.pack(spec.copy_fmt, off, ln)  # noqa: E731
    pins = lambda ln: bytes([OP_INSERT]) + struct.pack(spec.insert_len_fmt, ln)  # noqa: E731
    raw_invalid: list[tuple[str, bytes]] = [
        ("empty_patch", b""),
        ("truncated_header", valid[: HEADER_SIZE - 1]),
        ("header_without_end", hdr),
        ("bad_magic", b"FFDLTA99" + valid[8:]),
        ("zero_target_size", _make_header(spec, base, b"", 0) + bytes([OP_END])),
        (
            "oversized_target",
            _make_header(spec, base, target, MAX_DELTA_TARGET_BYTES + 1) + bytes([OP_END]),
        ),
        ("wrong_base_sha256", _flip(valid, spec.from_sha_off)),
        ("wrong_target_sha256", _flip(valid, spec.to_sha_off)),
        ("unknown_opcode", hdr + b"\x7f" + bytes([OP_END])),
        ("missing_op_end", valid[:-1]),
        ("trailing_bytes_after_end", valid + b"\x00"),
        ("truncated_copy_operands", hdr + bytes([OP_COPY]) + b"\x00\x00"),
        ("zero_len_copy", hdr + pcopy(0, 0) + bytes([OP_END])),
        ("oob_copy_past_base", hdr + pcopy(len(base) - 4, 16) + bytes([OP_END])),
        ("u32_wrap_copy_overflow", hdr + pcopy(0xFFFFFFF0, 0x20) + bytes([OP_END])),
        ("copy_exceeds_target_size", hdr + pcopy(0, cap) + pcopy(0, 4) + bytes([OP_END])),
        ("truncated_insert_header", hdr + bytes([OP_INSERT]) + b"\x01"),
        ("zero_len_insert", hdr + pins(0) + bytes([OP_END])),
        ("truncated_insert_payload", hdr + pins(32) + b"short"),
        ("insert_exceeds_target", hdr + pins(cap + 1) + (b"X" * (cap + 1)) + bytes([OP_END])),
        ("short_output_at_end", hdr + pins(cap - 1) + (b"X" * (cap - 1)) + bytes([OP_END])),
    ]
    if full:
        rng = random.Random(seed ^ 0x5A5A)  # noqa: S311
        raw_invalid.extend(
            (f"bitflip_{idx}", _flip(valid, rng.randrange(len(valid)))) for idx in range(24)
        )

    cases: list[_DeltaCase] = [_DeltaCase("undersized_out_cap", base, valid, cap - 1, None)]
    for label, bad_patch in raw_invalid:
        digest_only = label == "wrong_target_sha256" or label.startswith("bitflip_")
        try:
            apply_delta_patch(base, bad_patch)
        except DeltaPatchError:
            cases.append(_DeltaCase(label, base, bad_patch, cap + 64, None, digest_only))
        else:
            raise RuntimeError(f"Python apply_delta_patch unexpectedly accepted {label}")
    return cases


def _compile_and_run_c_driver(tmp: Path, cases: Sequence[_DeltaCase]) -> bytes:
    c_dir = REPO_ROOT / "agent" / "components" / "fleetforge" / "src"
    drv_c, drv_bin = tmp / "ff_delta_fuzz.c", tmp / "ff_delta_fuzz"
    in_bin, out_bin = tmp / "cases.bin", tmp / "results.bin"
    drv_c.write_text(_C_DRIVER_SOURCE, encoding="utf-8")
    with in_bin.open("wb") as fp:
        fp.write(struct.pack("=I", len(cases)))
        for case in cases:
            fp.write(struct.pack("=III", len(case.base), len(case.patch), case.out_cap))
            fp.write(hashlib.sha256(case.base).digest() + case.base + case.patch)

    cflags = ["-std=c99", "-O1", "-g", "-fsanitize=address,undefined", "-fno-omit-frame-pointer"]
    wflags = ["-Wall", "-Wextra", "-Werror", f"-I{c_dir}"]
    cmd = ["gcc", *cflags, *wflags, str(c_dir / "ff_delta.c"), str(drv_c), "-o", str(drv_bin)]
    comp = subprocess.run(cmd, capture_output=True, text=True, check=False)  # noqa: S603,S607
    if comp.returncode != 0:
        raise RuntimeError(f"ASan/UBSan gcc build failed:\n{comp.stderr}")

    env = {
        **os.environ,
        "ASAN_OPTIONS": "detect_leaks=1:halt_on_error=1:strict_string_checks=1",
        "UBSAN_OPTIONS": "print_stacktrace=1:halt_on_error=1",
    }
    run = subprocess.run(  # noqa: S603
        [str(drv_bin), str(in_bin), str(out_bin)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    if run.returncode != 0 or run.stderr.strip():
        raise RuntimeError(f"ASan/UBSan C harness failed (rc={run.returncode}):\n{run.stderr}")
    return out_bin.read_bytes()


def _verify_c_outcomes(cases: Sequence[_DeltaCase], raw_results: bytes) -> None:
    offset = 0
    for case in cases:
        rc, written = struct.unpack_from("=iI", raw_results, offset)
        offset += 8
        to_sha = b""
        actual = b""
        if rc == 0:
            to_sha = raw_results[offset : offset + 32]
            offset += 32
            actual = raw_results[offset : offset + written]
            offset += written
        if case.expected_target is not None:
            digest_ok = hashlib.sha256(actual).digest() == to_sha
            if rc != 0 or actual != case.expected_target or not digest_ok:
                raise RuntimeError(f"Differential mismatch on {case.label}: rc={rc}")
        elif rc == 0:
            if not case.allow_digest_reject or hashlib.sha256(actual).digest() == to_sha:
                raise RuntimeError(f"Corrupt patch {case.label} was not rejected by C (rc={rc})")


def run_differential_delta_fuzz(
    *, iterations: int, seed: int, full: bool = False
) -> tuple[int, int]:
    """Run Python-vs-C ASan/UBSan differential fuzzing and corrupt-patch verification."""
    pos_cases: list[_DeltaCase] = []
    for idx, (base, target) in enumerate(_deterministic_pairs(iterations, seed)):
        patch = create_delta_patch(base, target)
        if (
            apply_delta_patch(base, patch, expected_to_sha256=hashlib.sha256(target).hexdigest())
            != target
        ):
            raise RuntimeError(f"Python round-trip mismatch at pair {idx}")
        pos_cases.append(_DeltaCase(f"pair_{idx}", base, patch, len(target), target))

    neg_cases = _corrupt_cases(seed, full=full)
    all_cases = [*pos_cases, *neg_cases]
    with tempfile.TemporaryDirectory(prefix="ff-delta-asan-") as tmp_str:
        raw_results = _compile_and_run_c_driver(Path(tmp_str), all_cases)
    _verify_c_outcomes(all_cases, raw_results)
    return len(pos_cases), len(neg_cases)


def validate_mutant_specs(specs: Sequence[MutantSpec], *, src_root: Path) -> None:
    """Verify every mutant target exists in current production source."""
    problems: list[str] = []
    for spec in specs:
        text = (src_root / spec.rel_path).read_text(encoding="utf-8")
        count = text.count(spec.target)
        if count != 1 or spec.target == spec.replacement:
            problems.append(f"{spec.name} ({spec.rel_path}): count={count}")
    if problems:
        raise RuntimeError("Mutant spec mismatch: " + "; ".join(problems))


def _run_single_mutant(spec: MutantSpec, *, tmp_pkg_parent: Path, repo_root: Path) -> MutantOutcome:
    target_file = tmp_pkg_parent / "fleetforge" / spec.rel_path
    original = target_file.read_text(encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": f"{tmp_pkg_parent}:{repo_root}",
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if not spec.requires_db:
        env["FF_ALLOW_DB_SKIP"] = "1"

    boot = (
        "import sys, pytest; "
        "sys.meta_path[:] = [f for f in sys.meta_path if 'editable' not in type(f).__name__.lower()]; "
        "raise SystemExit(pytest.main(sys.argv[1:]))"
    )
    started = time.monotonic()
    target_file.write_text(original.replace(spec.target, spec.replacement, 1), encoding="utf-8")
    try:
        py_opt = f"pythonpath={tmp_pkg_parent} {repo_root}"
        flags = ["-q", "-p", "no:cacheprovider", "--no-header", "-o", py_opt]
        cmd = [sys.executable, "-c", boot, *flags, *spec.pytest_args]
        proc = subprocess.run(  # noqa: S603
            cmd, cwd=repo_root, env=env, capture_output=True, text=True, check=False
        )
    finally:
        target_file.write_text(original, encoding="utf-8")
    return MutantOutcome(
        spec.name, spec.rel_path, proc.returncode != 0, proc.returncode, time.monotonic() - started
    )


def run_mutation_sweep(
    specs: Sequence[MutantSpec], *, repo_root: Path = REPO_ROOT
) -> list[MutantOutcome]:
    """Execute 1-line mutations inside an isolated temp copy of `src/fleetforge`."""
    src_pkg = repo_root / "src" / "fleetforge"
    validate_mutant_specs(specs, src_root=src_pkg)
    with tempfile.TemporaryDirectory(prefix="ff-mutants-") as tmp_str:
        tmp_src = Path(tmp_str) / "src"
        shutil.copytree(
            src_pkg, tmp_src / "fleetforge", ignore=shutil.ignore_patterns("__pycache__", "*.pyc")
        )
        return [
            _run_single_mutant(spec, tmp_pkg_parent=tmp_src, repo_root=repo_root) for spec in specs
        ]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="FleetForge sanitizer & mutation verifier")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--quick", action="store_true", help="Deterministic fast gate (~5-15s)")
    mode.add_argument("--full", action="store_true", help="Extended fuzzing + DB mutants if set")
    parser.add_argument("--with-db", action="store_true", help="Include PostgreSQL-backed mutants")
    parser.add_argument("--seed", type=int, default=20261126, help="Deterministic PRNG seed")
    args = parser.parse_args(list(argv) if argv is not None else None)

    is_full = bool(args.full)
    include_db = bool(args.with_db or (is_full and os.environ.get("TEST_DATABASE_URL")))
    validate_mutant_specs(MUTANT_SPECS, src_root=REPO_ROOT / "src" / "fleetforge")
    pos_n, neg_n = run_differential_delta_fuzz(
        iterations=160 if is_full else 24, seed=args.seed, full=is_full
    )
    print(f"[deep-verify] ffdelta-v1 ASan+UBSan passed: {pos_n} pairs, {neg_n} hostile patches")

    active_specs = [s for s in MUTANT_SPECS if include_db or not s.requires_db]
    outcomes = run_mutation_sweep(active_specs, repo_root=REPO_ROOT)
    for out in outcomes:
        tag = "KILLED" if out.killed else "SURVIVED"
        print(f"[deep-verify] mutant {out.name} ({out.rel_path}): {tag} ({out.elapsed_s:.2f}s)")
    survived = [o.name for o in outcomes if not o.killed]
    if survived:
        print(f"[deep-verify] FAILED: survived mutants: {', '.join(survived)}", file=sys.stderr)
        return 1
    print(f"[deep-verify] ALL {len(outcomes)}/{len(outcomes)} mutants killed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
