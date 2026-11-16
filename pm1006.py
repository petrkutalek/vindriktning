import asyncio

BAUDRATE = 9_600
REQUEST = b"\x11\x02\x0b\x01\xe1"
RESPONSE_HEADER = b"\x16\x11\x0b"
FRAME_LEN = 20
PM25_OFFSET = 5

SPINUP_S = 15
SAMPLE_COUNT = 5
SAMPLE_GAP_S = 2
MEASURE_S = SPINUP_S + (SAMPLE_COUNT - 1) * SAMPLE_GAP_S  # without UART timeouts


def trimmed_mean(values):
    """Rounded mean without the lowest and highest value (when there are more than two)."""
    ordered = sorted(values)
    if len(ordered) > 2:
        ordered = ordered[1:-1]
    count = len(ordered)
    return (sum(ordered) + count // 2) // count


class PM1006:
    """PM1006 particle sensor on a UART, with a switchable fan.

    measure() returns {"pm25": ug/m3} or raises OSError if no sample was valid.
    """

    def __init__(self, uart, fan):
        self._uart = uart
        self._fan = fan

    async def measure(self):
        samples = []
        self._fan.on()
        try:
            await asyncio.sleep(SPINUP_S)
            for i in range(SAMPLE_COUNT):
                if i:
                    await asyncio.sleep(SAMPLE_GAP_S)
                value = self._read_pm25()
                if value is not None:
                    samples.append(value)
        finally:
            self._fan.off()
        if not samples:
            raise OSError("no valid response")
        return {"pm25": trimmed_mean(samples)}

    def _read_pm25(self):
        # Returns None for an invalid frame; measure() skips such a sample.
        pending = self._uart.any()
        if pending:
            self._uart.read(pending)  # drop stale bytes
        self._uart.write(REQUEST)
        frame = self._uart.read(FRAME_LEN)
        if (
            not frame
            or len(frame) != FRAME_LEN
            or not frame.startswith(RESPONSE_HEADER)
            or sum(frame) & 0xFF
        ):
            return None
        return frame[PM25_OFFSET] << 8 | frame[PM25_OFFSET + 1]
