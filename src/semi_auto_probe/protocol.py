from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum


FRAME_HEAD = 0x3A
RESPONSE_HEAD = 0xA3
FRAME_TAIL = bytes((0x0D, 0x0A))
FRAME_LENGTH = 12
MULTI_AXIS_FRAME_TAIL = bytes((0xA5, 0xA5))
MULTI_AXIS_FRAME_LENGTH = 33

COMM_TEST_COMMAND = bytes.fromhex("3A 55 00 00 00 00 00 00 00 8F 0D 0A")
COMM_TEST_RESPONSE = bytes.fromhex("A3 AA 00 00 00 00 00 00 00 4D 0D 0A")

FUNCTION_READ_POSITION = 0xCB
FUNCTION_MULTI_AXIS_RELATIVE_MOVE = 0xCC
FUNCTION_ENABLE_REALTIME_POSITION = 0xD1
FUNCTION_DISABLE_REALTIME_POSITION = 0xD4
FUNCTION_CLEAR_POSITION = 0xD3
FUNCTION_REACHED_POSITION = 0xB5
FUNCTION_MULTI_AXIS_COMPLETED = 0xBE
FUNCTION_READ_IO_STATUS = 0xD7
FUNCTION_RELATIVE_MOVE = 0xFA
FUNCTION_ABSOLUTE_MOVE = 0xFB
FUNCTION_STOP = 0xFC
STOP_MODE_DECELERATE = 0x4A
STOP_MODE_EMERGENCY = 0x49


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


@dataclass(frozen=True)
class AxisPosition:
    axis: Axis
    is_running: bool
    position: int
    raw: bytes

    @property
    def axis_name(self) -> str:
        return {
            Axis.X: "X",
            Axis.Y: "Y",
            Axis.Z: "Z",
            Axis.AXIS_4: "AXIS_4",
        }[self.axis]


@dataclass(frozen=True)
class IoStatus:
    home_mask: int
    limit_mask: int
    input_mask: int
    output_mask: int
    raw: bytes

    def home_triggered(self, axis: Axis) -> bool:
        return bool(self.home_mask & int(axis))


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


def build_read_position_command(axis: Axis) -> bytes:
    return build_frame(FUNCTION_READ_POSITION, axis)


def build_enable_realtime_position_command() -> bytes:
    return build_frame(FUNCTION_ENABLE_REALTIME_POSITION)


def build_disable_realtime_position_command() -> bytes:
    return build_frame(FUNCTION_DISABLE_REALTIME_POSITION)


def build_clear_position_command(axis: Axis) -> bytes:
    return build_frame(FUNCTION_CLEAR_POSITION, axis)


def build_read_io_status_command() -> bytes:
    return build_frame(FUNCTION_READ_IO_STATUS)


def build_relative_move_command(axis: Axis, reverse: bool, pulses: int, speed_percent: int = 100) -> bytes:
    if pulses < 0 or pulses > 0xFFFFFFFF:
        raise ValueError("Pulse count must be in range 0..4294967295.")
    if speed_percent < 0 or speed_percent > 100:
        raise ValueError("Speed percent must be in range 0..100.")

    direction = 0x01 if reverse else 0x00
    data = bytes((direction,)) + pulses.to_bytes(4, byteorder="big", signed=False) + bytes((speed_percent,))
    return build_frame(FUNCTION_RELATIVE_MOVE, axis, data)


def build_absolute_move_command(axis: Axis, target_position: int, speed_percent: int = 100) -> bytes:
    if target_position < 0 or target_position > 0xFFFFFFFF:
        raise ValueError("Absolute target position must be in range 0..4294967295.")
    if speed_percent < 0 or speed_percent > 100:
        raise ValueError("Speed percent must be in range 0..100.")

    data = target_position.to_bytes(4, byteorder="big", signed=False) + bytes((speed_percent, 0x00))
    return build_frame(FUNCTION_ABSOLUTE_MOVE, axis, data)


def build_multi_axis_relative_move_command(
    axis_params: dict[Axis, tuple[bool, int, int, int]],
) -> bytes:
    """Build protocol item 21: 4-axis relative positioning command.

    Each axis parameter is:
    reverse, pulses, speed_percent, acceleration_10ms_units.
    Missing axes are encoded as no movement.
    """
    payload = bytearray((FRAME_HEAD, FUNCTION_MULTI_AXIS_RELATIVE_MOVE))
    for axis in (Axis.X, Axis.Y, Axis.Z, Axis.AXIS_4):
        reverse, pulses, speed_percent, acceleration = axis_params.get(axis, (False, 0, 0, 0))
        if pulses < 0 or pulses > 0xFFFFFFFF:
            raise ValueError("Pulse count must be in range 0..4294967295.")
        if speed_percent < 0 or speed_percent > 100:
            raise ValueError("Speed percent must be in range 0..100.")
        if acceleration < 0 or acceleration > 0xFF:
            raise ValueError("Acceleration must be in range 0..255.")

        payload.extend(
            bytes((0x01 if reverse else 0x00,))
            + pulses.to_bytes(4, byteorder="big", signed=False)
            + bytes((speed_percent, acceleration))
        )

    payload.append(checksum(bytes(payload)))
    payload.extend(MULTI_AXIS_FRAME_TAIL)
    return bytes(payload)


def build_stop_command(axis: Axis, emergency: bool = False) -> bytes:
    stop_mode = STOP_MODE_EMERGENCY if emergency else STOP_MODE_DECELERATE
    return build_frame(FUNCTION_STOP, axis, bytes((stop_mode,)))


def parse_axis_position_response(raw: bytes) -> AxisPosition:
    frame = parse_frame(raw, expected_head=RESPONSE_HEAD)
    if frame.function_code != FUNCTION_READ_POSITION:
        raise ValueError(f"Expected position response CB, got {frame.function_code:02X}.")

    try:
        axis = Axis(frame.axis)
    except ValueError as exc:
        raise ValueError(f"Unexpected axis in position response: {frame.axis:02X}.") from exc

    is_running = raw[3] != 0
    sign = -1 if raw[4] else 1
    pulse_count = int.from_bytes(raw[5:9], byteorder="big", signed=False)
    return AxisPosition(axis=axis, is_running=is_running, position=sign * pulse_count, raw=raw)


def parse_io_status_response(raw: bytes) -> IoStatus:
    frame = parse_frame(raw, expected_head=RESPONSE_HEAD)
    if frame.function_code != FUNCTION_READ_IO_STATUS:
        raise ValueError(f"Expected I/O status response D7, got {frame.function_code:02X}.")
    return IoStatus(
        home_mask=raw[2],
        limit_mask=int.from_bytes(raw[3:5], byteorder="big", signed=False),
        input_mask=raw[5],
        output_mask=raw[6],
        raw=raw,
    )
