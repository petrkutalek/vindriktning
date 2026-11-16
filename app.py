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
    ADV_JITTER_MS,
    ADV_NAME,
    ADV_PERIOD_S,
    BME688_ADDR,
    ENV_INTERVAL_S,
    MEASURE_ATTEMPTS,
    PM25_INTERVAL_S,
)
from pm1006 import BAUDRATE, MEASURE_S, PM1006, UART_TIMEOUT_CHAR_MS, UART_TIMEOUT_MS

VERSION = "2.0.4"

# Board wiring (LaskaKit ESP-VINDRIKTNING ESP-32 I2C).
PIN_FAN = 12
PIN_PM_RX = 16
PIN_PM_TX = 17
PIN_I2C_SDA = 21
PIN_I2C_SCL = 22
I2C_FREQ_HZ = 100_000

ADV_INTERVAL_MS = 100
# The scan response data is 31 bytes at most: the name needs a 2 byte AD structure header and the
# appearance structure takes 4 bytes.
ADV_NAME_MAX_LEN = 31 - 2 - 4

WDT_TIMEOUT_S = 180
WDT_FEED_PERIOD_S = 10
FATAL_RESET_DELAY_S = 10


class Advertiser:
    """Broadcasts one BTHome v2 frame as a single advertising event.

    Advertising is started and stopped half an interval later, before the second event is due.
    The packet id changes only when the transmitted data change, so receivers can recognise a
    repeated frame as a duplicate. Advertising is non-connectable, so nothing can connect.
    """

    def __init__(self, ble, name, appearance):
        self._ble = ble
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
        self._last_sent = time.ticks_ms()
        # Random start, so that the first frame after a reboot is not taken for a duplicate.
        self._packet_id = random.getrandbits(8)

    async def send(self, **values):
        # Compared without the packet id, at the encoded resolution.
        payload = encode(0, **values)
        if payload != self._payload:
            self._payload = payload
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
        self._last_sent = time.ticks_ms()

    def since_sent_ms(self):
        """Time since the last frame was broadcast without an error."""
        return time.ticks_diff(time.ticks_ms(), self._last_sent)


class Readings:
    """Latest result of each sensor.

    A result is kept through failed measurements and dropped once max_failures
    measurements in a row have failed, so it is no longer broadcast.
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


async def sample(name, sensor, readings):
    """Measure once and store the result."""
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


async def broadcast(name, readings, advertiser):
    """Broadcast the stored values as one frame; nothing is sent while there are none.

    name is not used; it is only there because every job is called as job(name, *args).
    """
    values = readings.merged()
    if values:
        await advertiser.send(**values)


async def supervise(name, wdt, readings, advertiser, limit_ms):
    """Feed the hardware watchdog; reset the board unless, within limit_ms, a measurement of any
    sensor has succeeded and a frame has been broadcast.

    The watchdog is not fed when the event loop is blocked or this job keeps failing.
    """
    for what, idle_ms in (
        ("measurement", readings.since_success_ms()),
        ("broadcast", advertiser.since_sent_ms()),
    ):
        if idle_ms > limit_ms:
            print(name, "no successful", what, "for", limit_ms // 1_000, "s, resetting")
            machine.reset()
    wdt.feed()


async def every(name, period_s, job, *args, jitter_ms=0):
    """Run job(name, *args) every period_s seconds plus a random -jitter_ms..+jitter_ms.

    An error in the job is logged, not fatal.
    """
    while True:
        period_ms = period_s * 1_000 + random.randint(-jitter_ms, jitter_ms)
        next_run = time.ticks_add(time.ticks_ms(), period_ms)
        try:
            await job(name, *args)
        except Exception as e:
            print(name, "failed:")
            sys.print_exception(e)
        await asyncio.sleep_ms(max(0, time.ticks_diff(next_run, time.ticks_ms())))


def check_config():
    """Raise ValueError for a setting that cannot work."""
    for name, value, minimum in (
        ("ENV_INTERVAL_S", ENV_INTERVAL_S, 1),
        # Must be longer than one measurement.
        ("PM25_INTERVAL_S", PM25_INTERVAL_S, MEASURE_S + 1),
        ("MEASURE_ATTEMPTS", MEASURE_ATTEMPTS, 1),
        ("ADV_PERIOD_S", ADV_PERIOD_S, 1),
        ("ADV_JITTER_MS", ADV_JITTER_MS, 0),
    ):
        if not isinstance(value, int) or value < minimum:
            raise ValueError(f"{name} must be an integer of at least {minimum}")
    if ADV_PERIOD_S * 1_000 - ADV_JITTER_MS <= ADV_INTERVAL_MS:
        raise ValueError("ADV_JITTER_MS too large for ADV_PERIOD_S")
    if BME688_ADDR not in (0x76, 0x77):
        raise ValueError("BME688_ADDR must be 0x76 or 0x77")
    if not isinstance(ADV_NAME, str) or len(ADV_NAME.encode()) > ADV_NAME_MAX_LEN:
        raise ValueError(f"ADV_NAME must be a string of at most {ADV_NAME_MAX_LEN} bytes")
    if not isinstance(ADV_APPEARANCE, int) or not 0 <= ADV_APPEARANCE <= 0xFFFF:
        raise ValueError("ADV_APPEARANCE must be an integer of 16 bits")


def init(wdt):
    """Set up the hardware and return the periodic tasks."""
    check_config()
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

    # Shared by the tasks below.
    readings = Readings(MEASURE_ATTEMPTS)
    advertiser = Advertiser(ble, ADV_NAME, ADV_APPEARANCE)
    limit_ms = 2 * max(ENV_INTERVAL_S, PM25_INTERVAL_S) * 1_000
    return [
        every("PM25", PM25_INTERVAL_S, sample, PM1006(uart, fan), readings),
        every("ENV", ENV_INTERVAL_S, sample, BME688(i2c, BME688_ADDR), readings),
        every("BROADCAST", ADV_PERIOD_S, broadcast, readings, advertiser, jitter_ms=ADV_JITTER_MS),
        every("WATCHDOG", WDT_FEED_PERIOD_S, supervise, wdt, readings, advertiser, limit_ms),
    ]


def main():
    async def run():
        # Started first, so that a hang during init() is covered as well.
        wdt = machine.WDT(timeout=WDT_TIMEOUT_S * 1_000)
        await asyncio.gather(*init(wdt))

    try:
        asyncio.run(run())
    except Exception as e:
        sys.print_exception(e)
        time.sleep(FATAL_RESET_DELAY_S)
        machine.reset()
