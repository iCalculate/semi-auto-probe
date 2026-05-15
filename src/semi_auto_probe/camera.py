from __future__ import annotations

import time
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
        self._last_frame_time: float | None = None
        self._fps = 0.0

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
        self._capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)

    def read(self) -> CameraFrame | None:
        if not self.is_open:
            self.open()
        assert self._capture is not None
        assert self._cv2 is not None

        ok, frame = self._capture.read()
        if not ok:
            return None

        frame = self._cv2.flip(frame, 0)
        self._draw_overlay(frame)
        rgb = self._cv2.cvtColor(frame, self._cv2.COLOR_BGR2RGB)
        height, width = rgb.shape[:2]
        header = f"P6 {width} {height} 255\n".encode("ascii")
        return CameraFrame(width=width, height=height, ppm_bytes=header + rgb.tobytes())

    def _draw_overlay(self, frame) -> None:
        assert self._cv2 is not None
        now = time.perf_counter()
        if self._last_frame_time is not None:
            instant_fps = 1.0 / max(now - self._last_frame_time, 1e-6)
            self._fps = instant_fps if self._fps == 0.0 else self._fps * 0.85 + instant_fps * 0.15
        self._last_frame_time = now

        cv2 = self._cv2
        height, width = frame.shape[:2]
        cv2.putText(
            frame,
            f"FPS {self._fps:4.1f}",
            (14, 28),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.72,
            (40, 255, 170),
            2,
            cv2.LINE_AA,
        )

        hist_width = min(180, max(120, width // 4))
        hist_height = min(90, max(64, height // 5))
        x0 = width - hist_width - 14
        y0 = height - hist_height - 14
        roi = frame[y0 : y0 + hist_height, x0 : x0 + hist_width]
        dark = roi.copy()
        dark[:] = (6, 10, 15)
        cv2.addWeighted(dark, 0.58, roi, 0.42, 0, roi)
        cv2.rectangle(frame, (x0, y0), (x0 + hist_width, y0 + hist_height), (90, 110, 130), 1)

        sample = cv2.resize(frame, (160, 90), interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(sample, cv2.COLOR_BGR2GRAY)
        hist = cv2.calcHist([gray], [0], None, [32], [0, 256]).flatten()
        max_value = float(hist.max()) or 1.0
        bin_width = hist_width / len(hist)
        for index, value in enumerate(hist):
            bar_height = int((value / max_value) * (hist_height - 18))
            x1 = int(x0 + index * bin_width)
            x2 = int(x0 + (index + 1) * bin_width) - 1
            y1 = y0 + hist_height - 6
            y2 = y1 - bar_height
            cv2.rectangle(frame, (x1, y2), (x2, y1), (80, 170, 255), -1)

        cv2.putText(frame, "LUMA", (x0 + 6, y0 + 15), cv2.FONT_HERSHEY_SIMPLEX, 0.42, (220, 230, 240), 1, cv2.LINE_AA)

    def close(self) -> None:
        if self._capture:
            self._capture.release()
            self._capture = None
