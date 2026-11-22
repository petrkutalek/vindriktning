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
| BME688  | temperature (0.01 °C)                    | `0x02`        | 60 s             |
| BME688  | relative humidity (0.01 %)               | `0x03`        | 60 s             |
| BME688  | pressure (0.01 hPa)                      | `0x04`        | 60 s             |

The two measurements run as independent `asyncio` tasks at their own intervals, so the ~24 s fan
spin-up and sampling of the PM1006 does not delay the BME688 readings. A BME688 measurement
waits until the fan run is over, because the airflow would disturb it, and the fan cannot start
during a BME688 measurement either. Each task stores its latest result and hands the stored values to
the BLE controller, which advertises them every `ADV_PERIOD_S` seconds (10 s by default) in one
frame, independently of the measurement intervals. A third task feeds the watchdog (see Runtime
behaviour). Gas resistance is not measured; the BME688 gas heater stays off.

### PM2.5

The fan is switched on, the sensor spins up for 15 s, 5 samples are taken 2 s apart, and the
trimmed mean (lowest and highest sample dropped) is used. The fan is switched off afterwards.
The UART is read without blocking: the response is collected 200 ms after each request.
Frames are validated (header, length, checksum); invalid frames are skipped.

### BME688

Forced mode, oversampling T x2 / P x16 / H x1, IIR filter off. Compensation follows the Bosch
BME688 datasheet (BST-BME688-DS000, floating point formulas). Calibration data is read on the
first measurement and again after a failed one, so a sensor that is missing at boot only causes a
log line and is picked up once it responds. A temperature outside -40...85 °C or a pressure
outside 300...1100 hPa (datasheet range) is a failed measurement; humidity is limited to
0...100 %RH.

## Advertisement

All stored values go out in one legacy advertisement (23 of 31 bytes). A value that does not exist
(its sensor has not answered yet, or its measurement failed `MEASURE_ATTEMPTS` times in a row) is
left out of the frame.

```
02 01 06            flags
13 16 D2 FC         service data, UUID 0xFCD2 (BTHome)
40                  device info: BTHome v2, not encrypted
00 NN               packet id
02 TT TT            temperature, sint16, 0.01 °C
03 HH HH            humidity, uint16, 0.01 %
04 PP PP PP         pressure, uint24, 0.01 hPa
0D MM MM            PM2.5, uint16, µg/m³
```

All multi-byte values are little-endian. The scan response data carries the complete local name
from `ADV_NAME` (`Vindriktning`, at most 25 bytes) and the appearance from `ADV_APPEARANCE`
(`0x0542`, air quality sensor, AD type `0x19`):

```
0D 09 "Vindriktning"   complete local name
03 19 42 05            appearance 0x0542
```

Advertising runs continuously with an advertising interval of `ADV_PERIOD_S` (10 s; the
controller accepts at most 10.24 s and adds its own random delay of up to 10 ms to every event).
It is restarted with new data only when the stored values change (compared at the encoded
resolution, 0.01 °C, 0.01 % and 0.01 hPa) and stopped while there are no values.

The packet id changes with the data. A repeated frame keeps its id, so BTHome receivers can
recognise it as a duplicate and ignore it. The id starts at a random value after every boot.

Advertising is non-connectable and the device has no BLE input, so nothing can connect to it. The
frames are not encrypted: anyone in range can read the values.

The name and appearance are sent as scan response data. With `connectable=False` the NimBLE stack
advertises as `ADV_SCAN_IND` (scannable, non-connectable), so a receiver gets them only when it
scans actively. Otherwise it identifies the device by its BLE address, which is printed at boot.

In Home Assistant the device appears through the BTHome integration (Bluetooth adapter or ESPHome
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
| `config.py`        | Per-device settings: measurement intervals, advertising period, advertised name and appearance, BME688 address. |
| `Makefile`         | Compile, upload, package and reset targets. |

## Build and deploy

Everything except `main.py` is compiled to `.mpy` with `mpy-cross` from the same
MicroPython release as the firmware (1.29), and only the `.mpy` files are uploaded. The files for
the board (the `.mpy` files and a copy of `main.py`) are collected in the `build/` directory.

Requirements: `make` and `mpremote` in `PATH`, and the `mpy-cross` 1.29.0 binary as
`./mpy-cross-1.29.0` in the project directory (override with `MPY_CROSS`). It is run with
`-O2 -march=xtensawin` (override with `MPY_FLAGS`; `-O3` would drop the line numbers from
tracebacks). The serial port defaults to `/dev/ttyUSB0`;
override it with `PORT`.

```sh
make deploy                    # compile, upload and reset the board
make deploy PORT=/dev/ttyACM0  # different serial port
```

| Target    | Action |
|-----------|--------|
| `build`   | Compile `app`, `pm1006`, `bme688`, `bthome` and `config` to `.mpy` and copy `main.py`, all into `build/`. |
| `upload`  | Build, then copy the files from `build/` to the board. |
| `pkg`     | Build, then pack the files from `build/` into `app.tgz`. |
| `reset`   | Reset the board. |
| `deploy`  | `upload` + `reset` (default target). |
| `repl`    | Open the serial console. |
| `clean`   | Remove `build/` and `app.tgz`. |

Do not leave `.py` copies of the compiled modules on the device.

## Runtime behaviour

- The settings in `config.py` are not validated; the limits are given in its comments. A value
  that cannot work shows up as a logged error of the job that uses it, or as a fatal error at
  boot (see below).
- Both sensor drivers have the same interface: `await measure()` returns a dict of values or raises
  `OSError`. Any failed measurement (I2C error, no valid UART frame, value out of range, or any
  other exception) is logged in one line. The last result stays in the frame until
  `MEASURE_ATTEMPTS` (3) measurements in a row have failed; then it is dropped and no longer
  advertised until the next successful measurement.
- The advertised data are updated after every measurement, successful or not. An error there is
  logged with a traceback and the update is repeated after the next measurement.
- A fatal error in `main()` prints the traceback, waits 10 s and resets the board.
- A hardware watchdog (180 s) is started first and fed every 10 s by a dedicated job, which also
  supervises the application: if, within `MEASURE_ATTEMPTS` + 1 times the longer of
  `ENV_INTERVAL_S` and `PM25_INTERVAL_S` (20 min by default), no measurement of either sensor has
  succeeded or the advertised data have not been updated without an error, it resets the board.
  If the event loop is blocked, the job does not run and the hardware watchdog resets the board.
  The watchdog cannot be stopped: after Ctrl-C in the REPL the board resets 180 s later.
- The board never enters deep sleep.

## Limitations

- BME688 temperature is affected by heat from the board and fan; mount the module away from heat
  sources if absolute temperature matters.
- A receiver that is not scanning at the moment of an advertising event misses it; the next one
  follows after `ADV_PERIOD_S`.
- Frames are not encrypted (BTHome encryption is not implemented).
