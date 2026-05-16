import unittest

from semi_auto_probe.protocol import FRAME_TAIL, RESPONSE_HEAD, Axis, checksum
from semi_auto_probe.serial_client import ControllerSerialClient


class FakeSerial:
    is_open = True

    def __init__(self, payload: bytes) -> None:
        self.payload = bytearray(payload)
        self.written = bytearray()
        self.reset_count = 0

    def read(self, size: int = 1) -> bytes:
        if not self.payload:
            return b""
        chunk = bytes(self.payload[:size])
        del self.payload[:size]
        return chunk

    def reset_input_buffer(self) -> None:
        self.reset_count += 1

    def write(self, data: bytes) -> int:
        self.written.extend(data)
        return len(data)

    def flush(self) -> None:
        return None


def position_response(axis: Axis, position: int) -> bytes:
    data = bytes((0x00, 0x00, 0x00, 0x00)) + position.to_bytes(2, "big")
    first_nine = bytes((RESPONSE_HEAD, 0xCB, axis)) + data
    return first_nine + bytes((checksum(first_nine),)) + FRAME_TAIL


def reached_response(axis: Axis) -> bytes:
    first_nine = bytes((RESPONSE_HEAD, 0xB5, axis)) + bytes(6)
    return first_nine + bytes((checksum(first_nine),)) + FRAME_TAIL


def multi_axis_completed_response() -> bytes:
    first_nine = bytes((RESPONSE_HEAD, 0xBE, 0x00)) + bytes(6)
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

    def test_multi_axis_reader_waits_for_a5_completion(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        client._serial = FakeSerial(bytes.fromhex("00 FF A5"))

        self.assertEqual(client._read_multi_axis_completed_response(timeout=0.05), b"\xA5")

    def test_multi_axis_reader_accepts_b5_for_all_moving_axes(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        expected = reached_response(Axis.X) + reached_response(Axis.Y)
        client._serial = FakeSerial(expected)

        completed = client._read_multi_axis_completed_response(timeout=0.05, moving_axes={Axis.X, Axis.Y})

        self.assertEqual(completed, expected)

    def test_multi_axis_reader_accepts_b5_axis_mask(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        first_nine = bytes((RESPONSE_HEAD, 0xB5, int(Axis.X) | int(Axis.Y))) + bytes(6)
        expected = first_nine + bytes((checksum(first_nine),)) + FRAME_TAIL
        client._serial = FakeSerial(expected)

        completed = client._read_multi_axis_completed_response(timeout=0.05, moving_axes={Axis.X, Axis.Y})

        self.assertEqual(completed, expected)

    def test_multi_axis_reader_accepts_be_completion_frame(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        expected = multi_axis_completed_response()
        client._serial = FakeSerial(expected)

        completed = client._read_multi_axis_completed_response(timeout=0.05, moving_axes={Axis.X, Axis.Y})

        self.assertEqual(completed, expected)

    def test_multi_axis_move_resets_input_writes_and_waits(self) -> None:
        client = ControllerSerialClient("COM_TEST", timeout=0.05)
        fake = FakeSerial(multi_axis_completed_response())
        client._serial = fake

        command, completed = client.move_multi_axis_relative_and_wait({Axis.X: (False, 10, 100, 0)}, timeout=0.05)

        self.assertEqual(completed, multi_axis_completed_response())
        self.assertEqual(bytes(fake.written), command)
        self.assertEqual(fake.reset_count, 1)


if __name__ == "__main__":
    unittest.main()
