# Fleetforge Basic — a Morse blinker that updates itself (Arduino / PlatformIO)

`Basic.ino` blinks a message in Morse code on the board's LED and prints it on the serial
port. Two lines of the sketch say what it blinks and which version it is. Change them,
upload the new build to your fleet, and the board updates itself over the air. If the new
build cannot reach the fleet, the board rolls back to the old one on its own. Your sketch
keeps blinking the whole time, network or not.

> No board? `just lib-quickstart` (from the repository root) plays every step below in the
> QEMU emulator against a local Fleetforge server.

## What you need

- [PlatformIO Core](https://platformio.org/install/cli) 6.2.0 or newer (`pio --version`).
- An ESP32 or ESP32-S3 DevKit with 4 MB of flash, and a USB cable.
- A Fleetforge server: its dashboard URL, and you logged in to it.

`platformio.ini` pins the platform to pioarduino `55.03.312-1` (Arduino-ESP32 core 3.3.12 on
ESP-IDF 5.5.5). PlatformIO's official `espressif32` platform cannot build this library.

## 1. Build it

From the repository root:

```sh
# quickstart: arduino-build
cd agent/components/fleetforge/examples/Basic
pio run -e esp32
pio run -e esp32s3
```

The first build downloads the platform and takes a few minutes. Each build ends in
`SUCCESS`; the firmware is `.pio/build/<env>/firmware.bin`. Build only the env of your
board if you like (`esp32` is any ESP32 DevKit, `esp32s3` is the S3 DevKitC-1).

## 2. Get an enrollment token

In the dashboard, under **Enroll a board**, press **Generate enrollment token** and copy
it. A token enrolls exactly one board, once. In your shell, keep it in a variable,
`FFE`, rather than in a file.

## 3. Write the board's config blob

The board learns where its fleet is, how to reach the network and its token from a 4 KB
config blob, `ff_cfg`. Nothing of that is in the sketch, so one build serves every board.

```sh
python3 agent/tools/ff_cfg.py --out ~/ff_cfg.bin \
    --api-base https://your-fleetforge-server --mqtt-uri mqtts://your-fleetforge-server:8883 \
    --ssid "<your network>" --psk "<its passphrase>" --token "$FFE"
```

`~/ff_cfg.bin` now holds a live token: keep it out of git and delete it after flashing.

## 4. Flash the board, once, over USB

```sh
cd agent/components/fleetforge/examples/Basic
pio run -e esp32 -t upload
~/.platformio/penv/bin/esptool --chip esp32 write-flash 0x3D0000 ~/ff_cfg.bin
```

For an S3 use `-e esp32s3` and `--chip esp32s3`. `0x3D0000` is where this folder's
`partitions.csv` (layout `ab-4m-arduino-v1`) keeps `ff_cfg`. Two things to never do:

- **Never replace or edit `partitions.csv`.** The board carries its partition table for
  life, and the fleet deploys against the layout it announces.
- **Never "Erase All Flash".** It wipes `ff_cfg` and the credential the board enrolled
  with; the board then needs a new token.

## 5. Watch it join the fleet

The serial monitor (`pio device monitor -e esp32`) shows the board enroll, then the
blinker:

```
ff-enroll: enroll 200 https://your-fleetforge-server/v1/enroll
ff-mqtt: announce acknowledged by the broker
morse: SOS (firmware 1.0.0)
```

The dashboard's fleet list now shows the board, online, at version `1.0.0`, with layout
`ab-4m-arduino-v1`.

## 6. Make build B

Change the two `#define` lines at the top of `Basic.ino` in your editor, the message to
`"HELLO"` and the version to `"1.1.0"`, or run:

```sh
# quickstart: arduino-edit
cd agent/components/fleetforge/examples/Basic
sed -i 's/"SOS"/"HELLO"/; s/"1.0.0"/"1.1.0"/' Basic.ino
```

Then build it:

```sh
# quickstart: arduino-build-b
cd agent/components/fleetforge/examples/Basic
pio run -e esp32
```

Every build you upload needs a new version: a version names exactly one image, and the
server refuses different bytes under a version it already has.

## 7. Deploy it

In the dashboard, under **Upload a build**, upload `.pio/build/esp32/firmware.bin` as target
`esp32`, version `1.1.0`, layout `ab-4m-arduino-v1`. Then press **Deploy** on the board's
row and pick `1.1.0`.

The board downloads the build, reboots into it, reaches the fleet and confirms it. The
dashboard shows the deploy `confirmed` and the board at `1.1.0`; the serial port says
`morse: HELLO (firmware 1.1.0)`.

If build B could not reach the fleet (a wrong Wi-Fi password compiled into your own code,
a crash at boot), the board would not confirm it, roll back to `1.0.0` on its own, and the
dashboard would say `rolled back`. You would not need the USB cable for that either.
