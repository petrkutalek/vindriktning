# Measurement intervals in seconds. If both are longer than 120 s, the shorter one is lowered
# to 120 s at boot, so that the watchdog always has a job to watch.
PM25_INTERVAL_S = 300
ENV_INTERVAL_S = 30

# A stored result is dropped, and no longer broadcast, when this many measurements of its sensor
# in a row have failed. It is stored again by the next successful measurement.
MEASURE_ATTEMPTS = 3

# The latest measurements are broadcast once every ADV_PERIOD_S seconds in one BTHome frame,
# plus a random -ADV_JITTER_MS..+ADV_JITTER_MS (chosen anew for every period).
ADV_PERIOD_S = 5
ADV_JITTER_MS = 100

# Complete local name sent as scan response data (at most 29 bytes).
ADV_NAME = "Vindriktning"

# BME688 I2C address: 0x76 (SDO to GND) or 0x77 (SDO to VDDIO)
BME688_ADDR = 0x77
