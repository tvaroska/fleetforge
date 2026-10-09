# Fleetforge basic — a Morse blinker that updates itself (ESP-IDF)

The ESP-IDF flavour of the worked example; the Arduino one is [`../Basic`](../Basic/README.md).
`main/main.c` blinks a message in Morse code on GPIO 2 and prints it on the console. Change
the message and the version, upload the new build to your fleet, and the board updates
itself over the air. If the new build cannot reach the fleet, the board rolls back to the
old one on its own. Your firmware keeps blinking the whole time, network or not.

| File | What it is |
|---|---|
| `main/main.c` | your firmware: `fleetforge_start()` first, then the blinker |
| `main/fleetforge_start.c`, `.h` | the library's boot sequence: copy both unchanged |
| `CMakeLists.txt` | `PROJECT_VER`, the version the dashboard shows |
| `partitions.csv` | layout `ab-4m-v1`: ship it unchanged |
| `sdkconfig.defaults` | automatic rollback on, no eFuse burns: ship it unchanged |

## What you need

- ESP-IDF **v5.5** (`idf.py --version`; the component declares `idf: ">=5.5"`), its
  environment exported (`. $IDF_PATH/export.sh`).
- An ESP32 or ESP32-S3 DevKit with 4 MB of flash, and a USB cable.
- A Fleetforge server: its dashboard URL, and you logged in to it.

## 1. Build it

From the repository root:

```sh
# quickstart: idf-build
cd agent/components/fleetforge/examples/basic_idf
idf.py set-target esp32
idf.py build
```

For an ESP32-S3:

```sh
# quickstart: idf-build-s3
cd agent/components/fleetforge/examples/basic_idf
idf.py set-target esp32s3
idf.py build
```

`set-target` starts over: it deletes `build/` and `sdkconfig`. The build prints
`App "fleetforge_basic" version: 1.0.0`, and the firmware is `build/fleetforge_basic.bin`.

## 2. Get an enrollment token

In the dashboard, under **Enroll a board**, press **Generate enrollment token** and copy
it. A token enrolls exactly one board, once. In your shell, keep it in a variable,
`FFE`, rather than in a file.

## 3. Write the board's config blob

The board learns where its fleet is, how to reach the network and its token from a 4 KB
config blob, `ff_cfg`. Nothing of that is in the firmware, so one build serves every board.

```sh
python3 agent/tools/ff_cfg.py --out ~/ff_cfg.bin \
    --api-base https://your-fleetforge-server --mqtt-uri mqtts://your-fleetforge-server:8883 \
    --ssid "<your network>" --psk "<its passphrase>" --token "$FFE"
```

`~/ff_cfg.bin` now holds a live token: keep it out of git and delete it after flashing.

## 4. Flash the board, once, over USB

```sh
cd agent/components/fleetforge/examples/basic_idf
idf.py -p PORT flash
esptool.py -p PORT write_flash 0x12000 ~/ff_cfg.bin
```

`0x12000` is where `partitions.csv` (layout `ab-4m-v1`) keeps `ff_cfg`. Two things to never
do:

- **Never edit `partitions.csv` or `sdkconfig.defaults`.** The board carries its partition
  table and bootloader options for life, and the fleet deploys against the layout it
  announces. `fleetforge_start.c` does not compile without
  `CONFIG_BOOTLOADER_APP_ROLLBACK_ENABLE=y`.
- **Never `idf.py erase-flash`.** It wipes `ff_cfg` and the credential the board enrolled
  with; the board then needs a new token.

## 5. Watch it join the fleet

`idf.py -p PORT monitor` shows the board enroll, then the blinker:

```
ff-enroll: enroll 200 https://your-fleetforge-server/v1/enroll
ff-mqtt: announce acknowledged by the broker
morse: SOS (firmware 1.0.0)
```

The dashboard's fleet list now shows the board, online, at version `1.0.0`, with layout
`ab-4m-v1`.

## 6. Make build B and deploy it

Edit two lines: `MORSE_MESSAGE` in `main/main.c` to `"HELLO"`, and `PROJECT_VER` in
`CMakeLists.txt` to `"1.1.0"`. Run `idf.py build`. Every build you upload needs a new
version: a version names exactly one image.

In the dashboard, under **Upload a build**, upload `build/fleetforge_basic.bin` as target
`esp32` (or `esp32s3`), version `1.1.0`, layout `ab-4m-v1`. Then press **Deploy** on the
board's row and pick `1.1.0`. The board downloads the build, reboots into it, reaches the
fleet and confirms it: the dashboard shows the deploy `confirmed` and the board at `1.1.0`,
and the console says `morse: HELLO (firmware 1.1.0)`. A build B that cannot reach the
fleet is not confirmed, and the board rolls back to `1.0.0` on its own.

## Known gaps

- In an ESP-IDF build the announce's `agent_version` is your `PROJECT_VER`, not the
  library's version (the Arduino library reports its own).
- The S3 DevKitC-1's LED is an RGB LED that a plain GPIO cannot light; on that board the
  `morse:` console line is the indicator.
