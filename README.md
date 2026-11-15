# Vindriktning ESP32 air sensor (MicroPython, BLE / BTHome v2)

Firmware for a modified IKEA VINDRIKTNING air-quality sensor. The original PCB is replaced by the
[LaskaKit ESP-VINDRIKTNING ESP-32 I2C](https://www.laskakit.cz/laskakit-esp-vindriktning-esp-32-i2c/)
board (ESP32-WROOM-32, PM1006 particle sensor) and a
[LaskaKit BME688](https://www.laskakit.cz/laskakit-bme688-senzor-tlaku--teploty--vlhkosti-a-kvalitu-vzduchu/)
module is attached over I2C. Measurements are broadcast over Bluetooth LE as non-connectable legacy
advertisements in the [BTHome v2](https://bthome.io/format/) format (unencrypted). Wi-Fi is not used.

Target: MicroPython 1.29 on ESP32.

## What it measures

| Sensor  | Quantity                                 | BTHome object | Default interval |
|---------|------------------------------------------|---------------|------------------|
| PM1006  | PM2.5 (µg/m³)                            | `0x0D`        | 300 s            |
| BME688  | temperature (0.01 °C)                    | `0x02`        | 30 s             |
| BME688  | relative humidity (0.01 %)               | `0x03`        | 30 s             |
| BME688  | pressure (0.01 hPa)                      | `0x04`        | 30 s             |

The two intervals are independent (`asyncio`): the ~23 s fan spin-up and sampling of the PM1006
does not delay the BME688 readings. Temperature, humidity and pressure are sent together in one
frame; PM2.5 is sent in its own frame. Gas resistance is not measured; the BME688 gas heater
stays off.

### PM2.5

The fan is switched on, the sensor spins up for 15 s, 5 samples are taken 2 s apart, and the
trimmed mean (lowest and highest sample dropped) is used. The fan is switched off afterwards.
Frames are validated (header, length, checksum); invalid frames are skipped.

### BME688

Forced mode, oversampling T x2 / P x16 / H x1, IIR filter off. Compensation follows the Bosch
BME688 datasheet (BST-BME688-DS000, floating point formulas). Calibration data is read on the
first measurement, so a sensor that is missing at boot only causes a log line and is picked up once
it responds.

## Advertisement

Two legacy advertisement frames are used, each well under the 31 byte limit.

Environment frame (20 B), sent after every BME688 measurement (30 s):

```
02 01 06            flags
10 16 D2 FC         service data, UUID 0xFCD2 (BTHome)
40                  device info: BTHome v2, not encrypted
00 NN               packet id (incremented for every frame sent)
02 TT TT            temperature, sint16, 0.01 °C
03 HH HH            humidity, uint16, 0.01 %
04 PP PP PP         pressure, uint24, 0.01 hPa
```

PM2.5 frame (13 B), sent after every PM1006 measurement (300 s):

```
02 01 06 09 16 D2 FC 40 00 NN 0D MM MM      (PM2.5, uint16, µg/m³)
```

All multi-byte values are little-endian. Each frame is sent `ADV_COUNT` (3) times: advertising is
started with an interval of `ADV_INTERVAL_MS` (500 ms) plus a random -`ADV_JITTER_MS` to
+`ADV_JITTER_MS` (20 ms), chosen anew for every send, and stopped half an interval after the last
event, so the three events go out at about 0, 1 and 2 intervals (about 1.25 s per send). The same
applies to both frames.

Nothing is remembered between measurements: a failed measurement sends no frame, and between
frames the device is silent. The radio can advertise one frame at a time, so sends are serialized
(a send that is due while another is running waits for it). Advertising is non-connectable and the
device has no BLE input, so nothing can connect to it. The frames are not encrypted: anyone in
range can read the values.

The device has no BLE name and is identified by its BLE address, which is printed at boot.
In Home Assistant it appears through the BTHome integration (Bluetooth adapter or ESPHome
Bluetooth proxy in range).

## Hardware

- Board: [LaskaKit ESP-VINDRIKTNING ESP-32 I2C](https://www.laskakit.cz/laskakit-esp-vindriktning-esp-32-i2c/)
- Sensor: [LaskaKit BME688](https://www.laskakit.cz/laskakit-bme688-senzor-tlaku--teploty--vlhkosti-a-kvalitu-vzduchu/)

| Function              | ESP32 pin       |
|-----------------------|-----------------|
| PM1006 UART2 RX / TX  | GPIO16 / GPIO17 (9600 baud) |
| PM1006 fan control    | GPIO12          |
| I2C SDA / SCL         | GPIO21 / GPIO22 (100 kHz) |

The BME688 address is set in `config.py` (`BME688_ADDR`, `0x76` or `0x77`). Check with
`i2c.scan()` if unsure. Supply the BME688 module with 3.3 V.

The LaskaKit board is listed as discontinued by the vendor; hardware revisions differ, so verify
the pins above against your revision.

## Files

| File               | Purpose |
|--------------------|---------|
| `main.py`          | Entry point, calls `app.main()`. Stays as plain `.py`. |
| `app.py`           | Hardware init, scheduling, BLE advertiser, watchdog. |
| `bthome.py`        | BTHome v2 frame encoder (no hardware dependencies). |
| `pm1006.py`        | PM1006 driver. |
| `bme688.py`        | BME688 driver (temperature, pressure, humidity). |
| `config.py`        | Per-device settings: measurement intervals, advertising count, interval and jitter, BME688 address. Stays as plain `.py`. |
| `Makefile`         | Compile, upload and reset targets. |

## Build and deploy

Everything except `main.py` and `config.py` is compiled to `.mpy` with `mpy-cross` from the same
MicroPython release as the firmware (1.29), and only the `.mpy` files are uploaded.

Requirements: `make`, `mpy-cross` and `mpremote` in `PATH`. The serial port defaults to
`/dev/ttyUSB0`; override it with `PORT`.

```sh
make deploy                    # compile, upload and reset the board
make deploy PORT=/dev/ttyACM0  # different serial port
```

| Target    | Action |
|-----------|--------|
| `build`   | Compile `app`, `pm1006`, `bme688` and `bthome` to `.mpy`. |
| `upload`  | Build, then copy the `.mpy` files, `main.py` and `config.py` to the board. |
| `reset`   | Reset the board. |
| `deploy`  | `upload` + `reset` (default target). |
| `repl`    | Open the serial console. |
| `clean`   | Remove the compiled `.mpy` files. |

Do not leave `.py` copies of the compiled modules on the device.

### Upgrading a board running the Wi-Fi version

Remove the files that are no longer used (they contain the stored Wi-Fi credentials):

```sh
mpremote connect /dev/ttyUSB0 fs rm :secrets.mpy :secrets.enc
```

## Runtime behaviour

- Both sensor drivers have the same interface: `await measure()` returns a dict of values or raises
  `OSError`. A sensor that fails (I2C error, no valid UART frame) is logged in one line, nothing is
  sent for that cycle, and the next cycle tries again.
- Any other error in a cycle is logged with a traceback and the task carries on at the next
  interval. A fatal error in `main()` prints the traceback, waits 10 s and resets the board.
- A hardware watchdog (180 s) is started first and fed by a dedicated task. If anything blocks the
  event loop, the board resets. The watchdog cannot be stopped: after Ctrl-C in the REPL the board
  resets 180 s later.
- The board never enters deep sleep.

## Limitations

- BME688 temperature is affected by heat from the board and fan; mount the module away from heat
  sources if absolute temperature matters.
- A PM1006 UART read blocks the event loop for up to 200 ms (`UART_TIMEOUT_MS`) when the sensor
  does not answer; normally it returns within a few tens of milliseconds.
- Frames are not encrypted (BTHome encryption is not implemented).
