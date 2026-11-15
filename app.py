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
    ADV_COUNT,
    ADV_INTERVAL_MS,
    ADV_JITTER_MS,
    BME688_ADDR,
    ENV_INTERVAL_S,
    PM25_INTERVAL_S,
)
from pm1006 import BAUDRATE, PM1006

VERSION = "2.0.0"

# Board wiring (LaskaKit ESP-VINDRIKTNING ESP-32 I2C).
PIN_FAN = 12
PIN_PM_RX = 16
PIN_PM_TX = 17
PIN_I2C_SDA = 21
PIN_I2C_SCL = 22
I2C_FREQ_HZ = 100_000
UART_TIMEOUT_MS = 200
UART_TIMEOUT_CHAR_MS = 50

WDT_TIMEOUT_S = 180
WDT_FEED_PERIOD_S = 10
FATAL_RESET_DELAY_S = 10


class Advertiser:
    """Broadcasts one BTHome v2 frame `count` times, then stops advertising.

    Only freshly measured values are sent; nothing is remembered between calls.
    The advertising interval is interval_ms plus a random -jitter_ms..+jitter_ms,
    chosen anew for every send.
    Advertising is non-connectable, so nothing can connect to the device.
    The lock serializes callers because the radio can advertise only one frame at a time.
    """

    def __init__(self, ble, count, interval_ms, jitter_ms):
        self._ble = ble
        self._count = count
        self._interval_ms = interval_ms
        self._jitter_ms = jitter_ms
        self._lock = asyncio.Lock()
        self._packet_id = 0

    async def send(self, **values):
        async with self._lock:
            self._packet_id = (self._packet_id + 1) & 0xFF
            interval_ms = self._interval_ms + random.randint(-self._jitter_ms, self._jitter_ms)
            try:
                self._ble.gap_advertise(
                    interval_ms * 1_000,
                    adv_data=encode(self._packet_id, **values),
                    connectable=False,
                )
                # Events go out at 0..count-1 intervals; stop half an interval after the last one.
                await asyncio.sleep_ms(interval_ms * (2 * self._count - 1) // 2)
            finally:
                self._ble.gap_advertise(None)


async def sample(sensor, advertiser):
    """Measure once and broadcast the result; a failed measurement sends nothing."""
    name = type(sensor).__name__
    try:
        values = await sensor.measure()
    except OSError as e:
        print(name, "failed:", e)
        return
    print(name, values)
    await advertiser.send(**values)


async def every(period_s, sensor, advertiser):
    """Run sample() every period_s seconds; an unexpected error is logged, not fatal."""
    while True:
        next_run = time.ticks_add(time.ticks_ms(), period_s * 1_000)
        try:
            await sample(sensor, advertiser)
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

    advertiser = Advertiser(ble, ADV_COUNT, ADV_INTERVAL_MS, ADV_JITTER_MS)
    return [
        every(PM25_INTERVAL_S, PM1006(uart, fan), advertiser),
        every(ENV_INTERVAL_S, BME688(i2c, BME688_ADDR), advertiser),
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
