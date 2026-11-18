import asyncio

BAUDRATE = 9_600
# A response frame takes about 21 ms at 9600 baud; this is a generous wait for it.
RESPONSE_WAIT_MS = 200
REQUEST = b"\x11\x02\x0b\x01\xe1"
RESPONSE_HEADER = b"\x16\x11\x0b"
FRAME_LEN = 20
PM25_OFFSET = 5

SPINUP_S = 15
SAMPLE_COUNT = 5
SAMPLE_GAP_S = 2


def trimmed_mean(values):
    """Rounded mean without the lowest and highest value (when there are more than two)."""
    ordered = sorted(values)
    if len(ordered) > 2:
        ordered = ordered[1:-1]
    count = len(ordered)
    return (sum(ordered) + count // 2) // count


class PM1006:
    """PM1006 particle sensor on a UART with timeout=0 (non-blocking), with a switchable fan.

    measure() returns {"pm25": ug/m3} or raises OSError if no sample was valid. The asyncio lock
    air_lock is held while the fan runs, so that the BME688 does not measure meanwhile.
    """

    def __init__(self, uart, fan, air_lock):
        self._uart = uart
        self._fan = fan
        self._air_lock = air_lock

    async def measure(self):
        samples = []
        async with self._air_lock:
            self._fan.on()
            try:
                await asyncio.sleep(SPINUP_S)
                for i in range(SAMPLE_COUNT):
                    if i:
                        await asyncio.sleep(SAMPLE_GAP_S)
                    value = await self._read_pm25()
                    if value is not None:
                        samples.append(value)
            finally:
                self._fan.off()
        if not samples:
            raise OSError("no valid response")
        return {"pm25": trimmed_mean(samples)}

    async def _read_pm25(self):
        # Returns None for an invalid frame; measure() skips such a sample.
        # Drop stale bytes.
        self._uart.read()
        self._uart.write(REQUEST)
        await asyncio.sleep_ms(RESPONSE_WAIT_MS)
        frame = self._uart.read()
        if (
            not frame
            or len(frame) != FRAME_LEN
            or not frame.startswith(RESPONSE_HEADER)
            or sum(frame) & 0xFF
        ):
            return None
        return frame[PM25_OFFSET] << 8 | frame[PM25_OFFSET + 1]
