PM25_INTERVAL_S = 300
ENV_INTERVAL_S = 20

# Each measurement is broadcast ADV_COUNT times. The advertising interval is ADV_INTERVAL_MS plus
# a random -ADV_JITTER_MS..+ADV_JITTER_MS (chosen anew for every send), 20 - 10240 ms in total.
ADV_COUNT = 3
ADV_INTERVAL_MS = 500
ADV_JITTER_MS = 30

# BME688 I2C address: 0x76 (SDO to GND) or 0x77 (SDO to VDDIO)
BME688_ADDR = 0x77
