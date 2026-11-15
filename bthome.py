import struct

# BTHome v2 (https://bthome.io/format/), unencrypted, legacy advertisement.
FLAGS = b"\x02\x01\x06"  # LE General Discoverable, BR/EDR not supported
AD_SERVICE_DATA_16 = 0x16
UUID = b"\xd2\xfc"  # 0xFCD2, little-endian
DEVICE_INFO = 0x40  # version 2, no encryption, not trigger based

ID_PACKET = 0x00
ID_TEMPERATURE = 0x02  # sint16, 0.01 degC
ID_HUMIDITY = 0x03  # uint16, 0.01 %
ID_PRESSURE = 0x04  # uint24, 0.01 hPa
ID_PM25 = 0x0D  # uint16, ug/m3


def encode(packet_id, temperature=None, humidity=None, pressure=None, pm25=None):
    """Return the advertising payload for the given measurements.

    temperature in degC, humidity in %, pressure in hPa, pm25 in ug/m3 (int).
    A value of None is left out. Objects are written in ascending object ID
    order, as the BTHome format requires. The payload is at most 23 bytes,
    well under the 31 byte legacy advertising limit.
    """
    data = bytearray((ID_PACKET, packet_id & 0xFF))
    if temperature is not None:
        data += struct.pack("<Bh", ID_TEMPERATURE, round(temperature * 100))
    if humidity is not None:
        data += struct.pack("<BH", ID_HUMIDITY, round(humidity * 100))
    if pressure is not None:
        data += struct.pack("<BI", ID_PRESSURE, round(pressure * 100))[:4]  # uint24
    if pm25 is not None:
        data += struct.pack("<BH", ID_PM25, pm25)
    service = UUID + bytes((DEVICE_INFO,)) + data
    return FLAGS + bytes((len(service) + 1, AD_SERVICE_DATA_16)) + service
