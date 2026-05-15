from __future__ import annotations

from dataclasses import dataclass

from .protocol import COMM_TEST_COMMAND, FRAME_LENGTH, hex_bytes, validate_comm_test_response


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

    @property
    def is_open(self) -> bool:
        return bool(self._serial and self._serial.is_open)

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:
            raise RuntimeError("Missing dependency: install pyserial with `pip install -r requirements.txt`.") from exc

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
        if self._serial:
            self._serial.close()
            self._serial = None

    def send_and_read_frame(self, command: bytes) -> bytes:
        if not self.is_open:
            self.open()
        assert self._serial is not None

        self._serial.reset_input_buffer()
        self._serial.write(command)
        self._serial.flush()
        return self._serial.read(FRAME_LENGTH)

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


def list_serial_ports() -> list[str]:
    try:
        from serial.tools import list_ports
    except ImportError:
        return []

    return [port.device for port in list_ports.comports()]
