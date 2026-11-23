import asyncio
import gc
import random
import sys
import time

import bluetooth
import machine

from bme688 import BME688
from bthome import encode
from config import (
    ADV_APPEARANCE,
    ADV_NAME,
    ADV_PERIOD_S,
    BME688_ADDR,
    ENV_INTERVAL_S,
    MEASURE_ATTEMPTS,
    PM25_INTERVAL_S,
)
from pm1006 import BAUDRATE, PM1006

VERSION = "2.2.0"

# Board wiring (LaskaKit ESP-VINDRIKTNING ESP-32 I2C).
PIN_FAN = 12
PIN_PM_RX = 16
PIN_PM_TX = 17
PIN_I2C_SDA = 21
PIN_I2C_SCL = 22
I2C_FREQ_HZ = 100_000

# Lower than the default to reduce the heat the BME688 picks up from the ESP32.
CPU_FREQ_HZ = 80_000_000

WDT_TIMEOUT_S = 180
WDT_FEED_PERIOD_S = 10
FATAL_RESET_DELAY_S = 10


class Advertiser:
    """Advertises one BTHome v2 frame continuously, once every period_s seconds.

    The controller repeats the frame by itself; advertising is restarted only when the data
    change. The packet id changes with the data, so receivers can recognise a repeated frame as
    a duplicate. Advertising is non-connectable, so nothing can connect; it is scannable, so an
    active scanner can get the name and appearance.
    """

    def __init__(self, ble, period_s, name, appearance):
        self._ble = ble
        self._interval_us = period_s * 1_000_000
        name = name.encode()
        # Scan response: AD structures Complete Local Name (0x09) and Appearance (0x19).
        self._resp_data = (
            bytes((len(name) + 1, 0x09))
            + name
            + bytes((3, 0x19))
            + appearance.to_bytes(2, "little")
        )
        self._payload = None
        # The start counts as a success.
        self._last_ok = time.ticks_ms()
        # Random start, so that the first frame after a reboot is not taken for a duplicate.
        self._packet_id = random.getrandbits(8)

    def update(self, **values):
        """Advertise the given values; advertising is stopped while there are none."""
        # Compared without the packet id, at the encoded resolution.
        payload = encode(0, **values) if values else None
        if payload != self._payload:
            self._packet_id = (self._packet_id + 1) & 0xFF
            if values:
                self._ble.gap_advertise(
                    self._interval_us,
                    adv_data=encode(self._packet_id, **values),
                    resp_data=self._resp_data,
                    connectable=False,
                )
            else:
                self._ble.gap_advertise(None)
            # Stored only now, so that a failed call is repeated by the next update.
            self._payload = payload
        self._last_ok = time.ticks_ms()

    def since_ok_ms(self):
        """Time since the last update without an error."""
        return time.ticks_diff(time.ticks_ms(), self._last_ok)


class Readings:
    """Latest result of each sensor.

    A result is kept through failed measurements and dropped once max_failures
    measurements in a row have failed, so it is no longer advertised.
    """

    def __init__(self, max_failures):
        self._max_failures = max_failures
        self._values = {}
        self._failures = {}
        # The start counts as a success.
        self._last_success = time.ticks_ms()

    def store(self, name, values):
        self._values[name] = values
        self._failures[name] = 0
        self._last_success = time.ticks_ms()

    def fail(self, name):
        self._failures[name] = self._failures.get(name, 0) + 1
        if self._failures[name] >= self._max_failures and name in self._values:
            del self._values[name]
            print(name, "dropped after", self._failures[name], "failed measurements")

    def since_success_ms(self):
        """Time since the last successful measurement of any sensor."""
        return time.ticks_diff(time.ticks_ms(), self._last_success)

    def merged(self):
        merged = {}
        for values in self._values.values():
            merged.update(values)
        return merged


async def sample(name, sensor, readings, advertiser):
    """Measure once, store the result and advertise the stored values."""
    try:
        values = await sensor.measure()
    # Whatever the cause, a failed measurement must be counted.
    except Exception as e:
        print(name, "failed:", type(e).__name__, e)
        readings.fail(name)
    else:
        print(name, values)
        readings.store(name, values)
    # Free memory after the measurement, which allocates the most.
    gc.collect()
    advertiser.update(**readings.merged())


async def supervise(name, wdt, readings, advertiser, limit_ms):
    """Feed the hardware watchdog; reset the board unless, within limit_ms, a measurement of any
    sensor has succeeded and the advertiser has been updated without an error.

    The watchdog is not fed when the event loop is blocked or this job keeps failing.
    """
    for what, idle_ms in (
        ("measurement", readings.since_success_ms()),
        ("advertising", advertiser.since_ok_ms()),
    ):
        if idle_ms > limit_ms:
            print(name, "no successful", what, "for", limit_ms // 1_000, "s, resetting")
            machine.reset()
    wdt.feed()


async def every(name, period_s, job, *args):
    """Run job(name, *args) every period_s seconds.

    An error in the job is logged, not fatal.
    """
    while True:
        next_run = time.ticks_add(time.ticks_ms(), period_s * 1_000)
        try:
            await job(name, *args)
        except Exception as e:
            print(name, "failed:")
            sys.print_exception(e)
        await asyncio.sleep_ms(max(0, time.ticks_diff(next_run, time.ticks_ms())))


def init(wdt):
    """Set up the hardware and return the periodic tasks."""
    fan = machine.Pin(PIN_FAN, machine.Pin.OUT, value=0)
    # timeout=0: reads do not block.
    uart = machine.UART(
        2,
        baudrate=BAUDRATE,
        bits=8,
        parity=None,
        stop=1,
        rx=PIN_PM_RX,
        tx=PIN_PM_TX,
        timeout=0,
    )
    i2c = machine.I2C(
        0,
        scl=machine.Pin(PIN_I2C_SCL),
        sda=machine.Pin(PIN_I2C_SDA),
        freq=I2C_FREQ_HZ,
    )

    ble = bluetooth.BLE()
    ble.active(True)
    mac = ":".join(f"{b:02X}" for b in ble.config("mac")[1])
    print("boot: version", VERSION, "CPU", machine.freq() // 1_000_000, "MHz, BLE address", mac)

    # Shared by the tasks below.
    readings = Readings(MEASURE_ATTEMPTS)
    advertiser = Advertiser(ble, ADV_PERIOD_S, ADV_NAME, ADV_APPEARANCE)
    # Held while the PM1006 fan runs and during a BME688 measurement, so they never overlap.
    air_lock = asyncio.Lock()
    # One interval more than a stored result is kept for, so that the slower sensor alone is not
    # reset for fewer failed measurements than MEASURE_ATTEMPTS allows.
    limit_ms = (MEASURE_ATTEMPTS + 1) * max(ENV_INTERVAL_S, PM25_INTERVAL_S) * 1_000
    pm1006 = PM1006(uart, fan, air_lock)
    bme688 = BME688(i2c, BME688_ADDR, air_lock)
    return [
        every("PM25", PM25_INTERVAL_S, sample, pm1006, readings, advertiser),
        every("ENV", ENV_INTERVAL_S, sample, bme688, readings, advertiser),
        every("WATCHDOG", WDT_FEED_PERIOD_S, supervise, wdt, readings, advertiser, limit_ms),
    ]


def main():
    async def run():
        await asyncio.gather(*init(wdt))

    try:
        # Started first, so that a hang during init() is covered as well.
        wdt = machine.WDT(timeout=WDT_TIMEOUT_S * 1_000)
        # Before the peripherals are set up.
        machine.freq(CPU_FREQ_HZ)
        asyncio.run(run())
    except Exception as e:
        sys.print_exception(e)
        time.sleep(FATAL_RESET_DELAY_S)
        machine.reset()
