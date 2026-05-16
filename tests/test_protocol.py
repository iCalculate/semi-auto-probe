import unittest

from semi_auto_probe.protocol import (
    COMM_TEST_COMMAND,
    COMM_TEST_RESPONSE,
    Axis,
    build_absolute_move_command,
    build_clear_position_command,
    build_disable_realtime_position_command,
    build_enable_realtime_position_command,
    build_multi_axis_relative_move_command,
    build_read_io_status_command,
    build_relative_move_command,
    build_frame,
    build_read_position_command,
    build_stop_command,
    checksum,
    parse_axis_position_response,
    parse_frame,
    parse_io_status_response,
    validate_comm_test_response,
)


class ProtocolTest(unittest.TestCase):
    def test_checksum_matches_reference_command(self) -> None:
        self.assertEqual(checksum(COMM_TEST_COMMAND[:9]), 0x8F)

    def test_build_comm_test_command(self) -> None:
        self.assertEqual(build_frame(0x55), COMM_TEST_COMMAND)

    def test_validate_comm_test_response(self) -> None:
        self.assertTrue(validate_comm_test_response(COMM_TEST_RESPONSE))

    def test_parse_rejects_bad_checksum(self) -> None:
        bad = bytearray(COMM_TEST_RESPONSE)
        bad[9] ^= 0x01
        with self.assertRaises(ValueError):
            parse_frame(bytes(bad))

    def test_build_position_commands(self) -> None:
        self.assertEqual(build_enable_realtime_position_command(), bytes.fromhex("3A D1 00 00 00 00 00 00 00 0B 0D 0A"))
        self.assertEqual(build_disable_realtime_position_command(), bytes.fromhex("3A D4 00 00 00 00 00 00 00 0E 0D 0A"))
        self.assertEqual(build_read_position_command(Axis.X), bytes.fromhex("3A CB 01 00 00 00 00 00 00 06 0D 0A"))
        self.assertEqual(build_read_io_status_command(), bytes.fromhex("3A D7 00 00 00 00 00 00 00 11 0D 0A"))
        self.assertEqual(build_clear_position_command(Axis.Z), bytes.fromhex("3A D3 04 00 00 00 00 00 00 11 0D 0A"))
        self.assertEqual(build_clear_position_command(Axis.ALL), bytes.fromhex("3A D3 FF 00 00 00 00 00 00 0C 0D 0A"))

    def test_build_motor_commands(self) -> None:
        self.assertEqual(build_relative_move_command(Axis.Z, reverse=True, pulses=0x00002710, speed_percent=0x20), bytes.fromhex("3A FA 04 01 00 00 27 10 20 90 0D 0A"))
        self.assertEqual(build_absolute_move_command(Axis.AXIS_4, target_position=0x00003511, speed_percent=0x32), bytes.fromhex("3A FB 08 00 00 35 11 32 00 B5 0D 0A"))
        self.assertEqual(build_stop_command(Axis.X), bytes.fromhex("3A FC 01 4A 00 00 00 00 00 81 0D 0A"))
        self.assertEqual(build_stop_command(Axis.ALL, emergency=True), bytes.fromhex("3A FC FF 49 00 00 00 00 00 7E 0D 0A"))

    def test_build_multi_axis_relative_move_command(self) -> None:
        self.assertEqual(
            build_multi_axis_relative_move_command(
                {
                    Axis.X: (True, 0x00002710, 0x32, 0x00),
                    Axis.Y: (True, 0x00002710, 0x20, 0x00),
                    Axis.Z: (False, 0x00000010, 0x20, 0x0A),
                    Axis.AXIS_4: (True, 0x00002710, 0x32, 0x00),
                }
            ),
            bytes.fromhex(
                "3A CC 01 00 00 27 10 32 00 01 00 00 27 10 20 00 "
                "00 00 00 00 10 20 0A 01 00 00 27 10 32 00 6C A5 A5"
            ),
        )

    def test_parse_axis_position_response(self) -> None:
        position = parse_axis_position_response(bytes.fromhex("A3 CB 01 01 00 00 00 D9 94 DD 0D 0A"))
        self.assertEqual(position.axis, Axis.X)
        self.assertTrue(position.is_running)
        self.assertEqual(position.position, 55700)

    def test_parse_io_status_response(self) -> None:
        status = parse_io_status_response(bytes.fromhex("A3 D7 02 00 10 81 04 00 00 11 0D 0A"))
        self.assertFalse(status.home_triggered(Axis.X))
        self.assertTrue(status.home_triggered(Axis.Y))
        self.assertFalse(status.home_triggered(Axis.Z))
        self.assertEqual(status.limit_mask, 0x0010)
        self.assertEqual(status.input_mask, 0x81)
        self.assertEqual(status.output_mask, 0x04)


if __name__ == "__main__":
    unittest.main()
