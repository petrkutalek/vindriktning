# The values are not validated at boot; keep them within the limits given here.

# Measurement intervals in whole seconds (integers). PM25_INTERVAL_S: at least 25, longer than
# one PM2.5 measurement (about 24 s). ENV_INTERVAL_S: at least 1.
# (MEASURE_ATTEMPTS + 1) times the longer interval must stay under 6 days (range of ticks_ms).
PM25_INTERVAL_S = 300
ENV_INTERVAL_S = 60

# A stored result is dropped, and no longer advertised, when this many measurements of its sensor
# in a row have failed. It is stored again by the next successful measurement.
# Integer, at least 1.
MEASURE_ATTEMPTS = 3

# The latest measurements are advertised once every ADV_PERIOD_S seconds in one BTHome frame.
# Integer, 1..10 (the advertising interval of the controller is 10.24 s at most).
ADV_PERIOD_S = 10

# Scan response data: complete local name and appearance (0x0542 = air quality sensor,
# Bluetooth SIG Assigned Numbers).
# https://www.bluetooth.com/wp-content/uploads/Files/Specification/HTML/Assigned_Numbers/out/en/index-en.html#UUID-8a3aae07-7d35-d567-d404-3864c89f86fe
# ADV_NAME: string, at most 25 bytes in UTF-8. ADV_APPEARANCE: integer, 0..0xFFFF.
ADV_NAME = "Vindriktning"
ADV_APPEARANCE = 0x0542

# BME688 I2C address: 0x76 (SDO to GND) or 0x77 (SDO to VDDIO)
BME688_ADDR = 0x77
