# Runbook — measuring what an Arduino build does to the partition table

How to reproduce the measurement behind `R3-fw-1`. Run this whenever the Arduino-ESP32
core is bumped: every finding below is a property of **that core's** `platform.txt` and
`boards.txt`, not of ESP-IDF, and a core release can change any of them without warning.
The conclusions it produced are in `spec/open-questions.md` → *ota-library* and
`design/decisions/arduino-gets-its-own-layout-id.md`.

|  |  |
|---|---|
| Measured against | `esp32:esp32@3.3.12`, `arduino-cli` 1.5.2 |
| Boards compiled | `esp32`, `esp32s3`, `esp32doit-devkit-v1` |
| Disk | ~7.8 GB installed, ~2.1 GB after pruning. **This box runs at 90%+.** |
| Runs where | Developer box only. Never on `prod`. |

**Disk is the failure mode.** The core pulls every target's libs plus both toolchains.
Install, prune what you are not compiling, and delete the scratch tree when done — check
`df -h /` before you start and after you finish.

## Setup — everything in a scratch tree, nothing in `$HOME`

`arduino-cli` defaults to `~/.arduino15`. Point it somewhere disposable instead so cleanup
is one `rm -rf` and the box is left exactly as found.

```bash
export SP=/tmp/ff-arduino-spike
mkdir -p $SP/bin $SP/data $SP/user $SP/dl && cd $SP
curl -fsSL https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Linux_64bit.tar.gz \
  | tar xz -C bin arduino-cli
cat > $SP/arduino-cli.yaml <<EOF
directories:
  data: $SP/data
  downloads: $SP/dl
  user: $SP/user
board_manager:
  additional_urls:
    - https://espressif.github.io/arduino-esp32/package_esp32_index.json
EOF
alias acli="$SP/bin/arduino-cli --config-file $SP/arduino-cli.yaml"
acli core update-index
acli core install esp32:esp32@3.3.12     # ~7.8 GB, several minutes
```

Then prune — for `esp32`/`esp32s3` (both Xtensa) the RISC-V toolchain and the other
targets' libs are dead weight, and `dl/` is 1.7 GB of already-unpacked archives:

```bash
rm -rf $SP/dl $SP/data/packages/esp32/tools/{esp-rv32,riscv32-esp-elf-gdb}
rm -rf $SP/data/packages/esp32/tools/esp32{c3,c5,c6,h2,p4,p4_es,s2}-libs
df -h /
```

`gen_esp32part.py` for decoding ships inside the core — no IDF container needed:

```bash
export GEN=$SP/data/packages/esp32/hardware/esp32/3.3.12/tools/gen_esp32part.py
export H=$SP/data/packages/esp32/hardware/esp32/3.3.12
```

## The measurements

A two-line sketch is enough; none of this depends on what the sketch does.

```bash
mkdir -p $SP/sk/Probe
printf 'void setup(){Serial.begin(115200);}\nvoid loop(){delay(1000);}\n' > $SP/sk/Probe/Probe.ino
```

**Always decode the built `partitions.bin`, never read back the csv you wrote.** The csv in
the build dir is an input; the `.bin` is what the bootloader will actually read, and the
gap between them is the whole point of the exercise.

```bash
acli compile -b esp32:esp32:esp32 --build-path $SP/b1 $SP/sk/Probe
python3 $GEN $SP/b1/*.partitions.bin     # the table the board gets
cat $SP/b1/flash_args                    # the offsets the upload writes
```

`flash_args` is the second half of every finding here. A table and an upload that disagree
produce a board that builds clean and never boots.

| # | What to establish | How |
|---|---|---|
| 1 | The stock table | compile as above for `esp32:esp32:esp32` and `:esp32s3`, decode |
| 2 | Whether any stock scheme matches our slot size | decode every `$H/tools/partitions/*.csv`; look for two **equal** `app`/`ota_*` rows of `0x1E0000` |
| 3 | Whether a sketch-local table wins | drop a `partitions.csv` in the *sketch* folder, recompile, decode. Repeat with `-b esp32:esp32:esp32:PartitionScheme=min_spiffs` to check it beats an explicit menu choice too |
| 4 | Whether it survives a board change | compile the **same sketch folder** under a second FQBN, decode both |
| 5 | Which boards have no menu | `grep '\.menu\.PartitionScheme\.' $H/boards.txt` vs `grep '\.name=' $H/boards.txt` |
| 6 | The bootloader's safety posture | `grep -E 'APP_ROLLBACK|ANTI_ROLLBACK|SECURE_BOOT|FLASH_ENC' $SP/data/packages/esp32/tools/esp32-libs/*/sdkconfig` |
| 7 | Cost of a config path | compile a sketch that reads config from NVS and one that reads it from a partition; compare the `Sketch uses N bytes` lines against the bare probe |

Precedence for #3 is stated in `platform.txt` itself — the `recipe.hooks.prebuild.1/2/3`
lines, `build.partitions` < variant < `{build.source.path}`, last write winning. Read
those three lines when a core bump surprises you; they are the mechanism.

## Two traps

- **The `Sketch uses … Maximum is N bytes` line comes from the board menu's
  `upload.maximum_size`, not from the table that was built.** A build with 1966080 B slots
  reported `Maximum is 1310720 bytes`. It is not a layout check and cannot be used as one.
- **A library cannot ship the table.** The hook reads `{build.source.path}` — the sketch
  folder. A `partitions.csv` inside `libraries/…` is never consulted, so the file has to
  arrive with the *example*, or by the user copying it. The override is also silent: the
  IDE says nothing when a sketch-local table replaces the board's.

## Teardown

```bash
rm -rf /tmp/ff-arduino-spike && df -h /
```
