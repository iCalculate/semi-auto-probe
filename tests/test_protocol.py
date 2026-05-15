import unittest

from semi_auto_probe.protocol import (
    COMM_TEST_COMMAND,
    COMM_TEST_RESPONSE,
    build_frame,
    checksum,
    parse_frame,
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


if __name__ == "__main__":
    unittest.main()
