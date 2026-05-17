from __future__ import annotations

import os
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


PUBLISHER_HOST_ENV = "SEMI_AUTO_PROBE_PUBLISHER_HOST"
PUBLISHER_PORT_ENV = "SEMI_AUTO_PROBE_PUBLISHER_PORT"
PUBLISHER_FPS_ENV = "SEMI_AUTO_PROBE_PUBLISHER_FPS"
WEB_HOST_ENV = "SEMI_AUTO_PROBE_WEB_HOST"
WEB_PORT_ENV = "SEMI_AUTO_PROBE_WEB_PORT"
DEFAULT_PUBLISHER_HOST = "127.0.0.1"
DEFAULT_PUBLISHER_PORT = 8765
DEFAULT_PUBLISHER_FPS = 1.0
DEFAULT_WEB_PORT = 8000

_latest_frame_lock = threading.RLock()
_latest_frame_bytes: bytes | None = None
_latest_frame_updated_at = 0.0
_publisher_started = False


def publish_camera_frame(image_bgr: object, jpeg_quality: int = 82) -> None:
    global _latest_frame_bytes, _latest_frame_updated_at

    now = time.time()
    min_interval = 1.0 / max(_publisher_fps(), 1.0)
    with _latest_frame_lock:
        if now - _latest_frame_updated_at < min_interval:
            return

    try:
        import cv2
    except ImportError:
        return

    ok, jpeg = cv2.imencode(".jpg", image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), jpeg_quality])
    if not ok:
        return

    with _latest_frame_lock:
        _latest_frame_bytes = jpeg.tobytes()
        _latest_frame_updated_at = now


def read_latest_camera_frame(max_age_seconds: float = 3.0) -> bytes | None:
    with _latest_frame_lock:
        if _latest_frame_bytes is None:
            return None
        if time.time() - _latest_frame_updated_at > max_age_seconds:
            return None
        return _latest_frame_bytes


def publisher_url() -> str:
    host = os.environ.get(PUBLISHER_HOST_ENV, DEFAULT_PUBLISHER_HOST)
    port = int(os.environ.get(PUBLISHER_PORT_ENV, str(DEFAULT_PUBLISHER_PORT)))
    return f"http://{host}:{port}"


def request_web_fallback_camera_release(timeout_seconds: float = 0.5) -> bool:
    port = int(os.environ.get(WEB_PORT_ENV, str(DEFAULT_WEB_PORT)))
    request = Request(f"http://127.0.0.1:{port}/internal/release-camera", method="POST")
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            return 200 <= response.status < 300
    except (HTTPError, URLError, TimeoutError, OSError):
        return False


def _publisher_fps() -> float:
    try:
        return float(os.environ.get(PUBLISHER_FPS_ENV, str(DEFAULT_PUBLISHER_FPS)))
    except ValueError:
        return DEFAULT_PUBLISHER_FPS


def start_frame_publisher() -> None:
    global _publisher_started

    if _publisher_started:
        return

    host = os.environ.get(PUBLISHER_HOST_ENV, DEFAULT_PUBLISHER_HOST)
    port = int(os.environ.get(PUBLISHER_PORT_ENV, str(DEFAULT_PUBLISHER_PORT)))
    try:
        server = ThreadingHTTPServer((host, port), _FramePublisherHandler)
    except OSError:
        return

    _publisher_started = True
    thread = threading.Thread(target=server.serve_forever, name="probe-frame-publisher", daemon=True)
    thread.start()


class _FramePublisherHandler(BaseHTTPRequestHandler):
    server_version = "SemiAutoProbeFramePublisher/0.1"

    def do_GET(self) -> None:
        if self.path == "/health":
            self._write_health()
            return
        if self.path != "/frame.jpg":
            self.send_error(404)
            return

        frame = read_latest_camera_frame(max_age_seconds=5.0)
        if frame is None:
            self.send_response(204)
            self.end_headers()
            return

        self.send_response(200)
        self.send_header("Content-Type", "image/jpeg")
        self.send_header("Content-Length", str(len(frame)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(frame)

    def _write_health(self) -> None:
        now = time.time()
        with _latest_frame_lock:
            frame_age = None if _latest_frame_bytes is None else max(0.0, now - _latest_frame_updated_at)
            has_recent_frame = frame_age is not None and frame_age <= 5.0
        body = (
            "{"
            "\"desktop_app_running\":true,"
            f"\"camera_available\":{str(has_recent_frame).lower()},"
            f"\"frame_age_seconds\":{frame_age if frame_age is not None else 'null'},"
            f"\"publisher_fps\":{_publisher_fps()}"
            "}"
        ).encode("ascii")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format: str, *_args: object) -> None:
        return
