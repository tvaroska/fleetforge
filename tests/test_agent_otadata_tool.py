"""`agent/tools/otadata.py` — the otadata decoder and the power-cut tear (R2-test-1, F4).

The tool is how a torn otadata sector gets into a QEMU flash image, because QEMU itself
never tears one (it completes every SPI flash command atomically). A tear that wrote one
byte outside its sector, or a decoder that disagreed with IDF about the crc, would make the
F4 result meaningless, so both are held here against synthetic images:

* `decode` prints exactly the runbook's `otadecode` lines (old transcripts compare);
* `tear` touches its 4096 bytes and nothing else, and writes only the two shapes a cut
  inside IDF's erase-then-program can leave;
* `newest` is the sector the bootloader would act on;
* the crc formula reproduces a value read off a REAL emulated board's flash — which is what
  proves it is IDF's crc and not merely self-consistent.
"""

import importlib.util
import json
import struct
import zlib
from pathlib import Path
from types import ModuleType

import pytest

OTADATA_PY = Path(__file__).resolve().parent.parent / "agent" / "tools" / "otadata.py"

OTADATA_OFFSET = 0xF000
FLASH = 4 * 1024 * 1024
SECTOR = 4096

# Read off a fresh QEMU esp32 board's flash during R2-test-1's T2 (agent 0.4.2, IDF v5.5.5):
# `xxd -s 0xF000 -l 32 .qemu/flash-esp32.bin` -> seq=1 label=FF… state=2 (VALID), and this
# crc word. The bootloader accepted that entry, so it is IDF's crc of seq=1.
REAL_FLASH_SEQ1_CRC = 0x4743989A


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("otadata_tool", OTADATA_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


otadata = _load_module()


def _entry(seq: int, state: int, *, crc: int | None = None) -> bytes:
    if crc is None:
        crc = zlib.crc32(struct.pack("<I", seq), 0xFFFFFFFF)
    return struct.pack("<I20sII", seq, b"\xff" * 20, state, crc)


def _bundle(tmp_path: Path, *, with_otadata: bool = True, flash_size: str = "4MB") -> Path:
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    parts = [{"name": "app", "path": "app.bin", "offset": 0x20000, "size": 1024}]
    if with_otadata:
        parts.insert(
            0,
            {
                "name": "ota-data",
                "path": "ota-data-initial.bin",
                "offset": OTADATA_OFFSET,
                "size": 2 * SECTOR,
            },
        )
    manifest = {"target": "esp32", "flash_size": flash_size, "parts": parts}
    (bundle / "manifest.json").write_text(json.dumps(manifest))
    return bundle


def _image(tmp_path: Path, sector0: bytes | None, sector1: bytes | None, size: int = FLASH) -> Path:
    data = bytearray(b"\xa5" * size)  # not 0xFF, so a stray erase anywhere is visible
    for index, entry in enumerate((sector0, sector1)):
        start = OTADATA_OFFSET + index * SECTOR
        data[start : start + SECTOR] = b"\xff" * SECTOR
        if entry is not None:
            data[start : start + len(entry)] = entry
    path = tmp_path / "flash.bin"
    path.write_bytes(bytes(data))
    path.chmod(0o600)
    return path


def _run(bundle: Path, image: Path, *args: str) -> int:
    return int(otadata.main(["--bundle", str(bundle), "--image", str(image), *args]))


def test_a_fresh_board_decodes_to_the_two_runbook_lines(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path)
    image = _image(tmp_path, _entry(1, 2), None)
    assert _run(bundle, image, "decode") == 0
    assert capsys.readouterr().out.splitlines() == [
        "sector0: seq=1 -> ota_0 state=VALID crc=ok",
        "sector1: empty",
    ]


def test_tear_newest_erased_blanks_that_sector_and_nothing_else(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path)
    image = _image(tmp_path, _entry(1, 2), _entry(2, 0))
    before = image.read_bytes()
    assert _run(bundle, image, "tear", "--sector", "newest", "--mode", "erased") == 0
    after = image.read_bytes()

    lo, hi = 0x10000, 0x11000
    assert after[:lo] == before[:lo]
    assert after[hi:] == before[hi:]
    assert after[lo:hi] == b"\xff" * SECTOR
    assert len(after) == FLASH
    assert image.stat().st_mode & 0o777 == 0o600

    capsys.readouterr()
    _run(bundle, image, "decode")
    assert capsys.readouterr().out.splitlines() == [
        "sector0: seq=1 -> ota_0 state=VALID crc=ok",
        "sector1: empty",
    ]


def test_tear_newest_partial_keeps_the_entry_but_not_its_crc(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path)
    image = _image(tmp_path, _entry(1, 2), _entry(2, 0))
    before = image.read_bytes()
    assert _run(bundle, image, "tear", "--sector", "newest", "--mode", "partial") == 0
    after = image.read_bytes()

    sector1 = after[0x10000:0x11000]
    assert sector1[:28] == _entry(2, 0)[:28]
    assert struct.unpack("<I", sector1[28:32])[0] == 0xFFFFFFFF
    assert sector1[32:] == b"\xff" * (SECTOR - 32)
    assert after[:0x10000] == before[:0x10000]
    assert after[0x11000:] == before[0x11000:]

    capsys.readouterr()
    _run(bundle, image, "decode")
    assert capsys.readouterr().out.splitlines()[1] == "sector1: seq=2 -> ota_1 state=NEW crc=BAD"


def test_newest_is_the_highest_valid_seq_and_skips_a_bad_crc(tmp_path: Path) -> None:
    good, bad = _entry(3, 2), _entry(4, 0, crc=0x12345678)
    assert otadata.newest_valid((good + b"\xff" * (SECTOR - 32), bad)) == 0
    assert otadata.newest_valid((_entry(5, 0), good)) == 0
    assert otadata.newest_valid((good, _entry(5, 0))) == 1
    with pytest.raises(otadata.OtadataError, match="neither"):
        otadata.newest_valid((bad, b"\xff" * SECTOR))


def test_tear_newest_refuses_when_neither_sector_is_valid(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    image = _image(tmp_path, None, _entry(2, 0, crc=0))
    before = image.read_bytes()
    assert _run(bundle, image, "tear", "--sector", "newest", "--mode", "erased") == 1
    assert image.read_bytes() == before


def test_a_wrong_size_image_is_refused(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    image = _image(tmp_path, _entry(1, 2), None, size=2 * 1024 * 1024)
    assert _run(bundle, image, "decode") == 1
    assert _run(bundle, image, "tear", "--sector", "0", "--mode", "erased") == 1


def test_a_manifest_without_otadata_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bundle = _bundle(tmp_path, with_otadata=False)
    image = _image(tmp_path, _entry(1, 2), None)
    assert _run(bundle, image, "decode") == 1
    assert "ota-data" in capsys.readouterr().err


def test_the_crc_is_idfs_as_read_off_real_flash() -> None:
    assert otadata.entry_crc(1) == REAL_FLASH_SEQ1_CRC
    assert otadata.is_valid(_entry(1, 2, crc=REAL_FLASH_SEQ1_CRC))
