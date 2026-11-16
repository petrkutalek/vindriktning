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
    ADV_JITTER_MS,
    ADV_NAME,
    ADV_PERIOD_S,
    BME688_ADDR,
    ENV_INTERVAL_S,
    MEASURE_ATTEMPTS,
    PM25_INTERVAL_S,
)
from pm1006 import BAUDRATE, PM1006

VERSION = "2.0.1"

# Board wiring (LaskaKit ESP-VINDRIKTNING ESP-32 I2C).
PIN_FAN = 12
PIN_PM_RX = 16
PIN_PM_TX = 17
PIN_I2C_SDA = 21
PIN_I2C_SCL = 22
I2C_FREQ_HZ = 100_000
UART_TIMEOUT_MS = 200
UART_TIMEOUT_CHAR_MS = 50

ADV_INTERVAL_MS = 100

WDT_TIMEOUT_S = 180
WDT_FEED_PERIOD_S = 10
FATAL_RESET_DELAY_S = 10


class Advertiser:
    """Broadcasts one BTHome v2 frame as a single advertising event.

    Advertising is started and stopped half an interval later, before the second event is due.
    The packet id changes only when the values change, so receivers can recognise a repeated
    frame as a duplicate. Advertising is non-connectable, so nothing can connect to the device.
    """

    def __init__(self, ble, name):
        self._ble = ble
        name = name.encode()
        if len(name) > 29:
            raise ValueError("name longer than 29 bytes")
        # Scan response: AD structure with type 0x09 (Complete Local Name).
        self._resp_data = bytes((len(name) + 1, 0x09)) + name
        self._values = None
        self._packet_id = 0

    async def send(self, **values):
        if values != self._values:
            self._values = values
            self._packet_id = (self._packet_id + 1) & 0xFF
        try:
            self._ble.gap_advertise(
                ADV_INTERVAL_MS * 1_000,
                adv_data=encode(self._packet_id, **values),
                resp_data=self._resp_data,
                connectable=False,
            )
            await asyncio.sleep_ms(ADV_INTERVAL_MS // 2)
        finally:
            self._ble.gap_advertise(None)


class Readings:
    """Latest result of each sensor.

    A result is kept through failed measurements and dropped once max_failures
    measurements in a row have failed, so it is no longer broadcast.
    """

    def __init__(self, max_failures):
        self._max_failures = max_failures
        self._values = {}
        self._failures = {}

    def store(self, name, values):
        self._values[name] = values
        self._failures[name] = 0

    def fail(self, name):
        self._failures[name] = self._failures.get(name, 0) + 1
        if self._failures[name] >= self._max_failures and name in self._values:
            del self._values[name]
            print(name, "dropped after", self._failures[name], "failed measurements")

    def merged(self):
        merged = {}
        for values in self._values.values():
            merged.update(values)
        return merged


async def sample(sensor, readings):
    """Measure once and store the result."""
    name = type(sensor).__name__
    try:
        values = await sensor.measure()
    except OSError as e:
        print(name, "failed:", e)
        readings.fail(name)
        return
    print(name, values)
    readings.store(name, values)


async def broadcast(readings, advertiser):
    """Broadcast the stored values as one frame; nothing is sent while there are none."""
    values = readings.merged()
    if values:
        await advertiser.send(**values)


async def every(period_s, job, *args, jitter_ms=0):
    """Run job(*args) every period_s seconds plus a random -jitter_ms..+jitter_ms.

    An unexpected error in the job is logged, not fatal.
    """
    while True:
        period_ms = period_s * 1_000 + random.randint(-jitter_ms, jitter_ms)
        next_run = time.ticks_add(time.ticks_ms(), period_ms)
        try:
            await job(*args)
        except Exception as e:
            sys.print_exception(e)
        gc.collect()
        await asyncio.sleep_ms(max(0, time.ticks_diff(next_run, time.ticks_ms())))


async def feed_watchdog(wdt):
    # A blocking hang stalls the event loop, which starves this task and resets the board.
    while True:
        wdt.feed()
        await asyncio.sleep(WDT_FEED_PERIOD_S)


def init():
    """Set up the hardware and return the periodic tasks."""
    fan = machine.Pin(PIN_FAN, machine.Pin.OUT, value=0)
    uart = machine.UART(
        2,
        baudrate=BAUDRATE,
        bits=8,
        parity=None,
        stop=1,
        rx=PIN_PM_RX,
        tx=PIN_PM_TX,
        timeout=UART_TIMEOUT_MS,
        timeout_char=UART_TIMEOUT_CHAR_MS,
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
    print("boot: version", VERSION, "BLE address", mac)

    readings = Readings(MEASURE_ATTEMPTS)  # shared by the tasks below
    advertiser = Advertiser(ble, ADV_NAME)
    return [
        every(PM25_INTERVAL_S, sample, PM1006(uart, fan), readings),
        every(ENV_INTERVAL_S, sample, BME688(i2c, BME688_ADDR), readings),
        every(ADV_PERIOD_S, broadcast, readings, advertiser, jitter_ms=ADV_JITTER_MS),
    ]


def main():
    async def run():
        # Started first, so that a hang during init() is covered as well.
        wdt = machine.WDT(timeout=WDT_TIMEOUT_S * 1_000)
        await asyncio.gather(feed_watchdog(wdt), *init())

    try:
        asyncio.run(run())
    except Exception as e:
        sys.print_exception(e)
        time.sleep(FATAL_RESET_DELAY_S)
        machine.reset()
