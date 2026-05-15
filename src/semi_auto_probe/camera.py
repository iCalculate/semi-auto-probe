from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CameraFrame:
    width: int
    height: int
    ppm_bytes: bytes


class UsbCamera:
    def __init__(self, index: int = 0, width: int = 960, height: int = 540) -> None:
        self.index = index
        self.width = width
        self.height = height
        self._cv2 = None
        self._capture = None

    @property
    def is_open(self) -> bool:
        return bool(self._capture and self._capture.isOpened())

    def open(self) -> None:
        try:
            import cv2
        except ImportError as exc:
            raise RuntimeError("Missing dependency: install opencv-python with `pip install -r requirements.txt`.") from exc

        if self.is_open:
            return

        self._cv2 = cv2
        self._capture = cv2.VideoCapture(self.index, cv2.CAP_DSHOW)
        if not self._capture.isOpened():
            self._capture.release()
            self._capture = cv2.VideoCapture(self.index)
        if not self._capture.isOpened():
            raise RuntimeError(f"Could not open camera index {self.index}.")

        self._capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
        self._capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)

    def read(self) -> CameraFrame | None:
        if not self.is_open:
            self.open()
        assert self._capture is not None
        assert self._cv2 is not None

        ok, frame = self._capture.read()
        if not ok:
            return None

        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        header = f"P6 {width} {height} 255\n".encode("ascii")
        return CameraFrame(width=width, height=height, ppm_bytes=header + rgb.tobytes())

    def close(self) -> None:
        if self._capture:
            self._capture.release()
            self._capture = None
