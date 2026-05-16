import unittest

from semi_auto_probe.protocol import FRAME_TAIL, RESPONSE_HEAD, Axis, checksum
from semi_auto_probe.serial_client import ControllerSerialClient


class FakeSerial:
    is_open = True

    def __init__(self, payload: bytes) -> None:
        self.payload = bytearray(payload)

    def read(self, size: int = 1) -> bytes:
        if not self.payload:
            return b""
        chunk = bytes(self.payload[:size])
        del self.payload[:size]
        return chunk


def position_response(axis: Axis, position: int) -> bytes:
    data = bytes((0x00, 0x00, 0x00, 0x00)) + position.to_bytes(2, "big")
    first_nine = bytes((RESPONSE_HEAD, 0xCB, axis)) + data
    return first_nine + bytes((checksum(first_nine),)) + FRAME_TAIL


def reached_response(axis: Axis) -> bytes:
    first_nine = bytes((RESPONSE_HEAD, 0xB5, axis)) + bytes(6)
    return first_nine + bytes((checksum(first_nine),)) + FRAME_TAIL


class SerialClientTest(unittest.TestCase):
    def test_position_reader_resynchronizes_after_fragment(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        expected = position_response(Axis.Y, 20)
        client._serial = FakeSerial(bytes.fromhex("0A 7A 0D 0A") + expected)

        self.assertEqual(client._read_position_response(Axis.Y), expected)

    def test_reached_reader_waits_for_axis_b5(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        expected = reached_response(Axis.Z)
        client._serial = FakeSerial(position_response(Axis.X, 10) + expected)

        self.assertEqual(client._read_axis_reached_response(Axis.Z, timeout=0.05), expected)


if __name__ == "__main__":
    unittest.main()
