from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


FRAME_HEAD = 0x3A
RESPONSE_HEAD = 0xA3
FRAME_TAIL = bytes((0x0D, 0x0A))
FRAME_LENGTH = 12

COMM_TEST_COMMAND = bytes.fromhex("3A 55 00 00 00 00 00 00 00 8F 0D 0A")
COMM_TEST_RESPONSE = bytes.fromhex("A3 AA 00 00 00 00 00 00 00 4D 0D 0A")


class Axis(IntEnum):
    X = 0x01
    Y = 0x02
    Z = 0x04
    AXIS_4 = 0x08
    ALL = 0xFF


def checksum(payload_without_checksum_and_tail: bytes) -> int:
    """Return the controller checksum: low 8 bits of the byte sum."""
    return sum(payload_without_checksum_and_tail) & 0xFF


def hex_bytes(data: bytes) -> str:
    return " ".join(f"{byte:02X}" for byte in data)


def build_frame(function_code: int, axis: int = 0, data: bytes | None = None) -> bytes:
    """Build a normal 12-byte command frame.

    The normal controller command layout is:
    head, function, axis, six data bytes, checksum, CR, LF.
    """
    body_data = data or b""
    if len(body_data) > 6:
        raise ValueError("Normal command data must be at most 6 bytes.")

    first_nine = bytes((FRAME_HEAD, function_code & 0xFF, axis & 0xFF)) + body_data.ljust(6, b"\x00")
    return first_nine + bytes((checksum(first_nine),)) + FRAME_TAIL


@dataclass(frozen=True)
class ControllerFrame:
    raw: bytes
    head: int
    function_code: int
    axis: int
    data: bytes
    checksum_value: int

    @property
    def is_response(self) -> bool:
        return self.head == RESPONSE_HEAD


def parse_frame(raw: bytes, expected_head: int | None = None) -> ControllerFrame:
    if len(raw) != FRAME_LENGTH:
        raise ValueError(f"Expected {FRAME_LENGTH} bytes, got {len(raw)}.")
    if raw[-2:] != FRAME_TAIL:
        raise ValueError(f"Invalid frame tail: {hex_bytes(raw[-2:])}.")
    if expected_head is not None and raw[0] != expected_head:
        raise ValueError(f"Expected frame head {expected_head:02X}, got {raw[0]:02X}.")

    actual = raw[9]
    expected = checksum(raw[:9])
    if actual != expected:
        raise ValueError(f"Checksum mismatch: expected {expected:02X}, got {actual:02X}.")

    return ControllerFrame(
        raw=raw,
        head=raw[0],
        function_code=raw[1],
        axis=raw[2],
        data=raw[3:9],
        checksum_value=actual,
    )


def validate_comm_test_response(raw: bytes) -> bool:
    parse_frame(raw, expected_head=RESPONSE_HEAD)
    return raw == COMM_TEST_RESPONSE
