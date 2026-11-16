import asyncio
import struct

# 23 B: T2 T3 P1..P10
REG_COEFF1 = 0x8A
# 14 B: H1..H7 T1 (gas coefficients are not used)
REG_COEFF2 = 0xE1
# status, gas index, press, temp, hum
REG_FIELD0 = 0x1D
REG_CTRL_GAS_1 = 0x71
REG_CTRL_HUM = 0x72
REG_CTRL_MEAS = 0x74
REG_CONFIG = 0x75

FIELD_LEN = 10
NEW_DATA = 0x80

# IIR filter off
CONFIG = 0x00
# osrs_h = 1x
CTRL_HUM = 0x01
# run_gas = 0: gas measurement and heater off
CTRL_GAS_1 = 0x00
# osrs_t = 2x, osrs_p = 16x, forced mode
CTRL_MEAS = 2 << 5 | 5 << 2 | 1

# ~42 ms for T x2 / P x16 / H x1
MEASURE_WAIT_MS = 50
POLL_MS = 10
POLL_TRIES = 20

# Operating range from the datasheet; a result outside it (or NaN) counts as a failed measurement.
# Humidity is limited to 0..100 %RH by the compensation itself.
RANGES = {"temperature": (-40, 85), "pressure": (300, 1_100)}


class BME688:
    """BME688 on I2C, forced mode, gas measurement off.

    measure() returns {"temperature": degC, "humidity": %RH, "pressure": hPa}
    or raises OSError (I2C error, timeout, result out of range). Calibration is read on the
    first measure() and again after any failed one, so a sensor that is missing at boot only
    fails there and is picked up once present. The compensation is the floating point variant of
    the Bosch formulas, so the results are not identical to the integer variant.
    """

    def __init__(self, i2c, addr):
        self._i2c = i2c
        self._addr = addr
        self._cal = None

    async def measure(self):
        try:
            if self._cal is None:
                self._cal = self._read_calibration()
            self._trigger()
            values = self._compensate(await self._read_field())
            for name, (low, high) in RANGES.items():
                if not low <= values[name] <= high:
                    raise OSError("out of range: " + name)
            return values
        except Exception:
            # The calibration may be what is wrong, so it is read again at the next attempt.
            self._cal = None
            raise

    def _read(self, reg, length):
        return self._i2c.readfrom_mem(self._addr, reg, length)

    def _write(self, reg, value):
        self._i2c.writeto_mem(self._addr, reg, bytes((value,)))

    def _read_calibration(self):
        t2, t3, p1, p2, p3, p4, p5, p7, p6, p8, p9, p10 = struct.unpack(
            "<hbxHhbxhhbbxxhhB", self._read(REG_COEFF1, 23)
        )
        h2_msb, h12, h1_msb, h3, h4, h5, h6, h7, t1, _, _, _ = struct.unpack(
            "<BBBbbbBbHhbb", self._read(REG_COEFF2, 14)
        )
        return {
            "t": (t1, t2, t3),
            "p": (p1, p2, p3, p4, p5, p6, p7, p8, p9, p10),
            "h": (h1_msb << 4 | h12 & 0x0F, h2_msb << 4 | h12 >> 4, h3, h4, h5, h6, h7),
        }

    def _trigger(self):
        # Order matters: the filter and osrs_h first, osrs_t/osrs_p and forced mode last.
        self._write(REG_CONFIG, CONFIG)
        self._write(REG_CTRL_HUM, CTRL_HUM)
        self._write(REG_CTRL_GAS_1, CTRL_GAS_1)
        self._write(REG_CTRL_MEAS, CTRL_MEAS)

    async def _read_field(self):
        await asyncio.sleep_ms(MEASURE_WAIT_MS)
        for _ in range(POLL_TRIES):
            field = self._read(REG_FIELD0, FIELD_LEN)
            if field[0] & NEW_DATA:
                return field
            await asyncio.sleep_ms(POLL_MS)
        raise OSError("measurement timeout")

    def _compensate(self, f):
        press_adc = f[2] << 12 | f[3] << 4 | f[4] >> 4
        temp_adc = f[5] << 12 | f[6] << 4 | f[7] >> 4
        hum_adc = f[8] << 8 | f[9]

        temp, t_fine = self._temperature(temp_adc)
        return {
            "temperature": temp,
            "humidity": self._humidity(hum_adc, temp),
            # hPa
            "pressure": self._pressure(press_adc, t_fine) / 100,
        }

    def _temperature(self, adc):
        t1, t2, t3 = self._cal["t"]
        d = adc / 0x20000 - t1 / 0x2000
        t_fine = (adc / 0x4000 - t1 / 0x400) * t2 + d * d * t3 * 16
        return t_fine / 5_120, t_fine

    def _humidity(self, adc, temp):
        h1, h2, h3, h4, h5, h6, h7 = self._cal["h"]
        var1 = adc - (h1 * 16 + h3 / 2 * temp)
        var2 = var1 * (h2 / 0x40000 * (1 + h4 / 0x4000 * temp + h5 / 0x100000 * temp * temp))
        hum = var2 + (h6 / 0x4000 + h7 / 0x200000 * temp) * var2 * var2
        return max(0.0, min(100.0, hum))

    def _pressure(self, adc, t_fine):
        p1, p2, p3, p4, p5, p6, p7, p8, p9, p10 = self._cal["p"]
        var1 = t_fine / 2 - 64_000
        var2 = var1 * var1 * p6 / 0x20000
        var2 += var1 * p5 * 2
        var2 = var2 / 4 + p4 * 0x10000
        var1 = (p3 * var1 * var1 / 0x4000 + p2 * var1) / 0x80000
        var1 = (1 + var1 / 0x8000) * p1
        press = (0x100000 - adc - var2 / 0x1000) * 6_250 / var1
        var1 = p9 * press * press / 0x80000000
        var2 = press * p8 / 0x8000
        var3 = press / 0x100 * press / 0x100 * press / 0x100 * p10 / 0x20000
        return press + (var1 + var2 + var3 + p7 * 128) / 16
