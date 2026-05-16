from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass

from .protocol import (
    COMM_TEST_COMMAND,
    FRAME_LENGTH,
    FUNCTION_READ_POSITION,
    FUNCTION_REACHED_POSITION,
    RESPONSE_HEAD,
    Axis,
    AxisPosition,
    build_absolute_move_command,
    build_clear_position_command,
    build_disable_realtime_position_command,
    build_enable_realtime_position_command,
    build_multi_axis_relative_move_command,
    build_relative_move_command,
    build_read_position_command,
    build_stop_command,
    hex_bytes,
    parse_frame,
    parse_axis_position_response,
    validate_comm_test_response,
)


@dataclass
class CommunicationTestResult:
    ok: bool
    request_hex: str
    response_hex: str
    message: str


class ControllerSerialClient:
    def __init__(self, port: str, baudrate: int = 115200, timeout: float = 1.0) -> None:
        self.port = port
        self.baudrate = baudrate
        self.timeout = timeout
        self._serial = None
        self._lock = threading.RLock()

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("Missing dependency: install pyserial with `pip install -r requirements.txt`.") from exc

        with self._lock:
            if self.is_open:
                return

            self._serial = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=self.timeout,
                write_timeout=self.timeout,
            )

    def close(self) -> None:
        with self._lock:
            if self._serial:
                self._serial.close()
                self._serial = None

    def send_and_read_frame(self, command: bytes) -> bytes:
        with self._lock:
            if not self.is_open:
                self.open()
            assert self._serial is not None

            self._serial.reset_input_buffer()
            self._serial.write(command)
            self._serial.flush()
            return self._serial.read(FRAME_LENGTH)

    def write_command(self, command: bytes, reset_input: bool = False) -> None:
        with self._lock:
            if not self.is_open:
                self.open()
            assert self._serial is not None

            if reset_input:
                self._serial.reset_input_buffer()
            self._serial.write(command)
            self._serial.flush()

    def read_frame(self) -> bytes:
        with self._lock:
            if not self.is_open:
                self.open()
            assert self._serial is not None
            return self._serial.read(FRAME_LENGTH)

    def send_raw(self, payload: bytes, read_length: int = FRAME_LENGTH, reset_input: bool = True) -> bytes:
        with self._lock:
            if not self.is_open:
                self.open()
            assert self._serial is not None

            if reset_input:
                self._serial.reset_input_buffer()
            self._serial.write(payload)
            self._serial.flush()
            if read_length <= 0:
                return b""
            return self._serial.read(read_length)

    def communication_test(self) -> CommunicationTestResult:
        response = self.send_and_read_frame(COMM_TEST_COMMAND)
        request_hex = hex_bytes(COMM_TEST_COMMAND)
        response_hex = hex_bytes(response)

        if len(response) != FRAME_LENGTH:
            return CommunicationTestResult(
                ok=False,
                request_hex=request_hex,
                response_hex=response_hex,
                message=f"Timeout or incomplete response: received {len(response)} byte(s).",
            )

        try:
            ok = validate_comm_test_response(response)
        except ValueError as exc:
            return CommunicationTestResult(
                ok=False,
                request_hex=request_hex,
                response_hex=response_hex,
                message=str(exc),
            )

        return CommunicationTestResult(
            ok=ok,
            request_hex=request_hex,
            response_hex=response_hex,
            message="Communication test passed." if ok else "Unexpected controller response.",
        )

    def enable_realtime_position(self) -> bytes:
        command = build_enable_realtime_position_command()
        self.write_command(command, reset_input=True)
        return command

    def disable_realtime_position(self) -> bytes:
        command = build_disable_realtime_position_command()
        self.write_command(command)
        return command

    def read_axis_position(self, axis: Axis) -> tuple[bytes, bytes, AxisPosition]:
        command = build_read_position_command(axis)
        with self._lock:
            if not self.is_open:
                self.open()
            assert self._serial is not None

            self._serial.reset_input_buffer()
            self._serial.write(command)
            self._serial.flush()
            response = self._read_position_response(axis)
        return command, response, parse_axis_position_response(response)

    def _read_position_response(self, axis: Axis) -> bytes:
        assert self._serial is not None
        deadline = time.monotonic() + self.timeout
        last_seen = b""
        buffer = bytearray()

        while time.monotonic() < deadline:
            chunk = self._serial.read(1)
            if not chunk:
                continue
            buffer.extend(chunk)
            last_seen = bytes(buffer[-FRAME_LENGTH:])

            head_index = buffer.find(bytes((RESPONSE_HEAD,)))
            if head_index < 0:
                del buffer[:-1]
                continue
            if head_index > 0:
                del buffer[:head_index]

            while len(buffer) >= FRAME_LENGTH:
                frame = bytes(buffer[:FRAME_LENGTH])
                try:
                    parsed = parse_frame(frame, expected_head=RESPONSE_HEAD)
                except ValueError:
                    del buffer[0]
                    break
                del buffer[:FRAME_LENGTH]
                last_seen = frame
                if parsed.function_code == FUNCTION_READ_POSITION and parsed.axis == axis:
                    return frame

        detail = f"last bytes {hex_bytes(last_seen)}" if last_seen else "no frame"
        raise TimeoutError(f"Timeout waiting for {axis.name} position response ({detail}).")

    def wait_axis_reached(self, axis: Axis, timeout: float = 5.0) -> bytes:
        with self._lock:
            if not self.is_open:
                self.open()
            assert self._serial is not None
            return self._read_axis_reached_response(axis, timeout)

    def _read_axis_reached_response(self, axis: Axis, timeout: float) -> bytes:
        assert self._serial is not None
        deadline = time.monotonic() + timeout
        last_seen = b""
        buffer = bytearray()

        while time.monotonic() < deadline:
            chunk = self._serial.read(1)
            if not chunk:
                continue
            buffer.extend(chunk)
            last_seen = bytes(buffer[-FRAME_LENGTH:])

            head_index = buffer.find(bytes((RESPONSE_HEAD,)))
            if head_index < 0:
                del buffer[:-1]
                continue
            if head_index > 0:
                del buffer[:head_index]

            while len(buffer) >= FRAME_LENGTH:
                frame = bytes(buffer[:FRAME_LENGTH])
                try:
                    parsed = parse_frame(frame, expected_head=RESPONSE_HEAD)
                except ValueError:
                    del buffer[0]
                    break
                del buffer[:FRAME_LENGTH]
                last_seen = frame
                if parsed.function_code == FUNCTION_REACHED_POSITION and parsed.axis == axis:
                    return frame

        detail = f"last bytes {hex_bytes(last_seen)}" if last_seen else "no frame"
        raise TimeoutError(f"Timeout waiting for {axis.name} reached-position response ({detail}).")

    def read_xyz_positions(self) -> list[tuple[bytes, bytes, AxisPosition]]:
        return [self.read_axis_position(axis) for axis in (Axis.X, Axis.Y, Axis.Z)]

    def read_stable_xyz_positions(
        self,
        required_repeats: int = 3,
        max_attempts: int = 30,
        interval_seconds: float = 0.05,
    ) -> list[tuple[bytes, bytes, AxisPosition]]:
        last_positions: tuple[int, int, int] | None = None
        repeats = 0
        last_entries: list[tuple[bytes, bytes, AxisPosition]] = []

        for _ in range(max_attempts):
            entries = self.read_xyz_positions()
            positions = tuple(entry[2].position for entry in entries)
            last_entries = entries
            if positions == last_positions:
                repeats += 1
            else:
                last_positions = positions
                repeats = 1
            if repeats >= required_repeats:
                return entries
            time.sleep(interval_seconds)

        return last_entries

    def move_relative(self, axis: Axis, reverse: bool, pulses: int, speed_percent: int = 100) -> bytes:
        command = build_relative_move_command(axis=axis, reverse=reverse, pulses=pulses, speed_percent=speed_percent)
        self.write_command(command)
        return command

    def move_absolute(self, axis: Axis, target_position: int, speed_percent: int = 100) -> bytes:
        command = build_absolute_move_command(axis=axis, target_position=target_position, speed_percent=speed_percent)
        self.write_command(command)
        return command

    def move_multi_axis_relative(self, axis_params: dict[Axis, tuple[bool, int, int, int]]) -> bytes:
        command = build_multi_axis_relative_move_command(axis_params)
        self.write_command(command)
        return command

    def stop_axis(self, axis: Axis, emergency: bool = False) -> bytes:
        command = build_stop_command(axis=axis, emergency=emergency)
        self.write_command(command)
        return command

    def emergency_stop_all(self) -> bytes:
        command = build_stop_command(axis=Axis.ALL, emergency=True)
        self.write_command(command)
        return command

    def clear_position(self, axis: Axis) -> bytes:
        command = build_clear_position_command(axis)
        self.write_command(command, reset_input=True)
        return command


def list_serial_ports() -> list[str]:
    try:
        import serial
        from serial.tools import list_ports
    except ImportError:
        return []

    ports_by_device: dict[str, str] = {}
    for port in list_ports.comports():
        device = port.device.strip()
        if not device:
            continue
        key = device.upper()
        if not key.startswith("COM"):
            continue
        ports_by_device.setdefault(key, device)

    available_ports = [
        device
        for device in ports_by_device.values()
        if _serial_port_is_available(serial, device)
    ]
    return sorted(available_ports, key=_com_sort_key)


def _serial_port_is_available(serial_module, device: str) -> bool:
    try:
        probe = serial_module.Serial(port=device, baudrate=115200, timeout=0.1, write_timeout=0.1)
    except (OSError, serial_module.SerialException):
        return False

    probe.close()
    return True


def _com_sort_key(device: str) -> tuple[int, str]:
    match = re.fullmatch(r"COM(\d+)", device.upper())
    if not match:
        return (10_000, device.upper())
    return (int(match.group(1)), device.upper())
