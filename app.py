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
from pm1006 import BAUDRATE, MEASURE_S, PM1006

VERSION = "2.0.2"

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
ADV_NAME_MAX_LEN = 29  # 31 bytes of scan response data minus the 2 byte AD structure header

WDT_TIMEOUT_S = 180
WDT_FEED_PERIOD_S = 10
# The watchdog only judges jobs that run at least this often, so that a healthy job always
# finishes a run well within WDT_TIMEOUT_S. Slower jobs are not watched.
WATCHED_PERIOD_S = 120
FATAL_RESET_DELAY_S = 10


class Watchdog:
    """Hardware watchdog, fed only while every watched job keeps finishing its runs.

    A run counts when the job returns without raising. If a watched job has not finished a run
    for WDT_TIMEOUT_S, or the event loop is blocked, the watchdog is not fed and the board resets.
    """

    def __init__(self):
        self._wdt = machine.WDT(timeout=WDT_TIMEOUT_S * 1_000)
        self._last_run = {}  # job name -> ticks_ms of its last finished run

    def watch(self, name, period_s):
        if period_s <= WATCHED_PERIOD_S:
            self._last_run[name] = time.ticks_ms()

    def finished(self, name):
        if name in self._last_run:
            self._last_run[name] = time.ticks_ms()

    async def run(self):
        timeout_ms = WDT_TIMEOUT_S * 1_000
        while True:
            now = time.ticks_ms()
            if all(time.ticks_diff(now, t) < timeout_ms for t in self._last_run.values()):
                self._wdt.feed()
            await asyncio.sleep(WDT_FEED_PERIOD_S)


class Advertiser:
    """Broadcasts one BTHome v2 frame as a single advertising event.

    Advertising is started and stopped half an interval later, before the second event is due.
    The packet id changes only when the transmitted data change, so receivers can recognise a
    repeated frame as a duplicate. Advertising is non-connectable, so nothing can connect.
    """

    def __init__(self, ble, name):
        self._ble = ble
        name = name.encode()
        # Scan response: AD structure with type 0x09 (Complete Local Name).
        self._resp_data = bytes((len(name) + 1, 0x09)) + name
        self._payload = None
        # Random start, so that the first frame after a reboot is not taken for a duplicate.
        self._packet_id = random.getrandbits(8)

    async def send(self, **values):
        payload = encode(0, **values)  # compared without the packet id, at the encoded resolution
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


async def sample(name, sensor, readings):
    """Measure once and store the result."""
    try:
        values = await sensor.measure()
    except Exception as e:  # whatever the cause, a failed measurement must be counted
        print(name, "failed:", type(e).__name__, e)
        readings.fail(name)
    else:
        print(name, values)
        readings.store(name, values)
    gc.collect()  # the measurement allocates the most


async def broadcast(name, readings, advertiser):
    """Broadcast the stored values as one frame; nothing is sent while there are none.

    name is not used; it is only there because every job is called as job(name, *args).
    """
    values = readings.merged()
    if values:
        await advertiser.send(**values)


async def every(name, period_s, job, *args, watchdog, jitter_ms=0):
    """Run job(name, *args) every period_s seconds plus a random -jitter_ms..+jitter_ms.

    An error in the job is logged, not fatal; only a run without error counts for the watchdog.
    """
    watchdog.watch(name, period_s)
    while True:
        period_ms = period_s * 1_000 + random.randint(-jitter_ms, jitter_ms)
        next_run = time.ticks_add(time.ticks_ms(), period_ms)
        try:
            await job(name, *args)
        except Exception as e:
            print(name, "failed:")
            sys.print_exception(e)
        else:
            watchdog.finished(name)
        await asyncio.sleep_ms(max(0, time.ticks_diff(next_run, time.ticks_ms())))


def check_config():
    """Raise ValueError for a setting that cannot work."""
    for name, value, minimum in (
        ("ENV_INTERVAL_S", ENV_INTERVAL_S, 1),
        ("PM25_INTERVAL_S", PM25_INTERVAL_S, MEASURE_S + 1),  # longer than one measurement
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


def measure_intervals():
    """Return the (ENV, PM25) intervals; if both exceed WATCHED_PERIOD_S, lower the shorter one."""
    env_s, pm25_s = ENV_INTERVAL_S, PM25_INTERVAL_S
    if min(env_s, pm25_s) > WATCHED_PERIOD_S:
        if env_s <= pm25_s:
            print("boot: ENV_INTERVAL_S lowered to", WATCHED_PERIOD_S, "s")
            env_s = WATCHED_PERIOD_S
        else:
            print("boot: PM25_INTERVAL_S lowered to", WATCHED_PERIOD_S, "s")
            pm25_s = WATCHED_PERIOD_S
    return env_s, pm25_s


def init(watchdog):
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
    env_s, pm25_s = measure_intervals()

    readings = Readings(MEASURE_ATTEMPTS)  # shared by the tasks below
    advertiser = Advertiser(ble, ADV_NAME)
    return [
        every("PM25", pm25_s, sample, PM1006(uart, fan), readings, watchdog=watchdog),
        every("ENV", env_s, sample, BME688(i2c, BME688_ADDR), readings, watchdog=watchdog),
        every(
            "BROADCAST",
            ADV_PERIOD_S,
            broadcast,
            readings,
            advertiser,
            watchdog=watchdog,
            jitter_ms=ADV_JITTER_MS,
        ),
    ]


def main():
    async def run():
        # Started first, so that a hang during init() is covered as well.
        watchdog = Watchdog()
        await asyncio.gather(watchdog.run(), *init(watchdog))

    try:
        asyncio.run(run())
    except Exception as e:
        sys.print_exception(e)
        time.sleep(FATAL_RESET_DELAY_S)
        machine.reset()
