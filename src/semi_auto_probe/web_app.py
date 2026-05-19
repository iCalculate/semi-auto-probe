from __future__ import annotations

import os
import json
import threading
import time
import atexit
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterator
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi import Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from .camera import UsbCamera
from .monitor_feed import publisher_url
from .protocol import hex_bytes
from .serial_client import ControllerSerialClient, list_serial_ports


WEB_DIR = Path(__file__).parent / "web"
STATIC_DIR = WEB_DIR / "static"
ACCESS_TOKEN_ENV = "SEMI_AUTO_PROBE_WEB_TOKEN"
PID_FILE_ENV = "SEMI_AUTO_PROBE_WEB_PID_FILE"
DIRECT_CAMERA_INDEX_ENV = "SEMI_AUTO_PROBE_WEB_CAMERA_INDEX"
DIRECT_CAMERA_WIDTH_ENV = "SEMI_AUTO_PROBE_WEB_CAMERA_WIDTH"
DIRECT_CAMERA_HEIGHT_ENV = "SEMI_AUTO_PROBE_WEB_CAMERA_HEIGHT"
DIRECT_CAMERA_FPS_ENV = "SEMI_AUTO_PROBE_WEB_DIRECT_CAMERA_FPS"
DEFAULT_DIRECT_CAMERA_FPS = 10.0
DEFAULT_PID_FILE = Path.cwd() / ".runtime" / "semi-auto-probe-web.pid"
DIRECT_CAMERA_LABELS = {
    0: "ProbeOM",
    1: "EmbeddedCam",
    2: "MonitorCam",
}


@dataclass
class WebStatus:
    desktop_app_running: bool
    serial_connected: bool
    serial_port: str | None
    camera_running: bool
    camera_source: str | None
    camera_source_label: str | None
    selected_camera_source: str
    frame_age_seconds: float | None
    publisher_fps: float | None
    active_camera_streams: int
    active_http_requests: int
    total_http_requests: int
    last_error: str | None


class WebProbeService:
    def __init__(self) -> None:
        self._serial_lock = threading.RLock()
        self._camera_lock = threading.RLock()
        self._serial: ControllerSerialClient | None = None
        self._direct_camera: UsbCamera | None = None
        self._direct_camera_index: int | None = None
        self._selected_camera_source = "auto"
        self._active_camera_streams = 0
        self._metrics_lock = threading.RLock()
        self._active_http_requests = 0
        self._total_http_requests = 0
        self._clients: dict[str, dict[str, object]] = {}
        self._last_error: str | None = None

    def start_from_environment(self) -> None:
        serial_port = os.environ.get("SEMI_AUTO_PROBE_WEB_SERIAL_PORT")
        if serial_port:
            try:
                self.connect_serial(serial_port=serial_port)
            except Exception as exc:
                self._last_error = f"Serial startup failed: {exc}"

    def status(self) -> WebStatus:
        with self._serial_lock, self._camera_lock:
            publisher_status = self._read_publisher_status()
            desktop_app_running = publisher_status is not None
            if desktop_app_running and self._direct_camera and self._selected_camera_source in {"auto", "desktop"}:
                self._close_direct_camera()
            camera_available = bool(publisher_status and publisher_status.get("camera_available"))
            direct_camera_running = bool(self._direct_camera and self._direct_camera.is_open)
            active_source = "desktop" if camera_available and self._selected_camera_source in {"auto", "desktop"} else ("direct" if direct_camera_running else None)
            return WebStatus(
                desktop_app_running=desktop_app_running,
                serial_connected=bool(self._serial and self._serial.is_open),
                serial_port=self._serial.port if self._serial else None,
                camera_running=(camera_available and self._selected_camera_source in {"auto", "desktop"}) or direct_camera_running,
                camera_source=active_source,
                camera_source_label=self._camera_source_label(active_source, self._direct_camera_index),
                selected_camera_source=self._selected_camera_source,
                frame_age_seconds=publisher_status.get("frame_age_seconds") if publisher_status else None,
                publisher_fps=publisher_status.get("publisher_fps") if active_source == "desktop" and publisher_status else (self._direct_camera_fps() if active_source == "direct" else None),
                active_camera_streams=self._active_camera_streams,
                active_http_requests=self._active_http_requests,
                total_http_requests=self._total_http_requests,
                last_error=self._last_error,
            )

    def begin_request(self, request: Request) -> str:
        client_id = self._client_id_from_request(request)
        with self._metrics_lock:
            self._active_http_requests += 1
            self._total_http_requests += 1
            entry = self._clients.setdefault(
                client_id,
                {
                    "ip": client_id,
                    "user_agent": request.headers.get("user-agent", "-"),
                    "active_requests": 0,
                    "active_camera_streams": 0,
                    "total_requests": 0,
                    "last_path": "",
                    "last_seen": 0.0,
                },
            )
            entry["active_requests"] = int(entry["active_requests"]) + 1
            entry["total_requests"] = int(entry["total_requests"]) + 1
            entry["last_path"] = request.url.path
            entry["last_seen"] = time.time()
            entry["user_agent"] = request.headers.get("user-agent", "-")
            return client_id

    def end_request(self, client_id: str) -> None:
        with self._metrics_lock:
            self._active_http_requests = max(0, self._active_http_requests - 1)
            if client_id in self._clients:
                self._clients[client_id]["active_requests"] = max(0, int(self._clients[client_id]["active_requests"]) - 1)

    def begin_camera_stream(self, request: Request) -> str:
        client_id = self._client_id_from_request(request)
        with self._metrics_lock:
            entry = self._clients.setdefault(
                client_id,
                {
                    "ip": client_id,
                    "user_agent": request.headers.get("user-agent", "-"),
                    "active_requests": 0,
                    "active_camera_streams": 0,
                    "total_requests": 0,
                    "last_path": "",
                    "last_seen": 0.0,
                },
            )
            entry["active_camera_streams"] = int(entry["active_camera_streams"]) + 1
            entry["last_path"] = request.url.path
            entry["last_seen"] = time.time()
            entry["user_agent"] = request.headers.get("user-agent", "-")
            return client_id

    def end_camera_stream(self, client_id: str) -> None:
        with self._metrics_lock:
            if client_id in self._clients:
                self._clients[client_id]["active_camera_streams"] = max(0, int(self._clients[client_id]["active_camera_streams"]) - 1)

    def connections(self) -> dict[str, object]:
        now = time.time()
        with self._metrics_lock:
            clients = []
            for entry in self._clients.values():
                clients.append(
                    {
                        "ip": entry["ip"],
                        "user_agent": entry["user_agent"],
                        "active_requests": entry["active_requests"],
                        "active_camera_streams": entry["active_camera_streams"],
                        "total_requests": entry["total_requests"],
                        "last_path": entry["last_path"],
                        "last_seen_seconds_ago": round(now - float(entry["last_seen"]), 1) if entry["last_seen"] else None,
                    }
                )
            clients.sort(key=lambda item: (int(item["active_camera_streams"]), int(item["active_requests"]), int(item["total_requests"])), reverse=True)
            return {
                "active_http_requests": self._active_http_requests,
                "active_camera_streams": self._active_camera_streams,
                "total_http_requests": self._total_http_requests,
                "client_count": len(clients),
                "clients": clients,
            }

    @staticmethod
    def _client_id_from_request(request: Request) -> str:
        cf_ip = request.headers.get("cf-connecting-ip")
        if cf_ip:
            return cf_ip.strip()
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            return forwarded_for.split(",", 1)[0].strip()
        return request.client.host if request.client else "unknown"

    def connect_serial(self, serial_port: str, timeout: float = 1.0) -> dict[str, object]:
        with self._serial_lock:
            if self._serial:
                self._serial.close()
            self._serial = ControllerSerialClient(port=serial_port, timeout=timeout)
            self._serial.open()
            return {"connected": True, "port": serial_port}

    def disconnect_serial(self) -> dict[str, object]:
        with self._serial_lock:
            if self._serial:
                self._serial.close()
            self._serial = None
            return {"connected": False}

    def read_positions(self) -> dict[str, object]:
        with self._serial_lock:
            client = self._require_serial()
            entries = client.read_xyz_positions()
            return {
                "positions": {
                    position.axis_name: {
                        "position": position.position,
                        "is_running": position.is_running,
                        "rx": hex_bytes(response),
                        "tx": hex_bytes(command),
                    }
                    for command, response, position in entries
                }
            }

    def set_camera_source(self, source: str) -> dict[str, object]:
        parsed_source, index = self._parse_camera_source(source)
        with self._camera_lock:
            self._selected_camera_source = parsed_source if index is None else f"direct:{index}"
            if parsed_source != "direct" or index != self._direct_camera_index:
                self._close_direct_camera()
            return {"selected_camera_source": self._selected_camera_source}

    def camera_sources(self) -> dict[str, object]:
        publisher_status = self._read_publisher_status()
        desktop_available = bool(publisher_status and publisher_status.get("camera_available"))
        max_index = int(os.environ.get("SEMI_AUTO_PROBE_WEB_CAMERA_MAX_INDEX", "4"))
        sources = [
            {"id": "auto", "label": "Auto", "fps": "1/10", "available": True},
            {"id": "desktop", "label": "Microscope feed", "fps": 1, "available": desktop_available},
        ]
        for index in range(max_index + 1):
            sources.append({"id": f"direct:{index}", "label": self._direct_camera_label(index), "fps": self._direct_camera_fps(), "available": True})
        return {"selected": self._selected_camera_source, "sources": sources}

    def mjpeg_frames(self, request: Request, source: str | None = None) -> Iterator[bytes]:
        if source:
            self.set_camera_source(source)
        client_id = self.begin_camera_stream(request)
        with self._camera_lock:
            self._active_camera_streams += 1
        try:
            while True:
                selected = self._selected_camera_source
                if selected in {"auto", "desktop"}:
                    shared_frame = self._read_published_frame()
                    if shared_frame is not None:
                        self.release_direct_camera()
                        yield self._mjpeg_part(shared_frame)
                        time.sleep(1.0)
                        continue
                    if selected == "desktop":
                        time.sleep(1.0)
                        continue

                direct_index = self._direct_index_for_selected_source(selected)
                direct_frame = self._read_direct_camera_frame(direct_index)
                if direct_frame is not None:
                    yield self._mjpeg_part(direct_frame)
                    time.sleep(1.0 / self._direct_camera_fps())
                    continue
                time.sleep(1.0)
        finally:
            with self._camera_lock:
                self._active_camera_streams = max(0, self._active_camera_streams - 1)
                if self._active_camera_streams == 0:
                    self._close_direct_camera()
            self.end_camera_stream(client_id)

    def release_direct_camera(self) -> dict[str, object]:
        with self._camera_lock:
            was_running = bool(self._direct_camera)
            self._close_direct_camera()
            return {"released": was_running}

    def _require_serial(self) -> ControllerSerialClient:
        if not self._serial:
            raise HTTPException(status_code=409, detail="Serial port is not connected.")
        return self._serial

    def _read_direct_camera_frame(self, index: int) -> bytes | None:
        with self._camera_lock:
            try:
                if self._direct_camera and self._direct_camera_index != index:
                    self._close_direct_camera()
                if not self._direct_camera:
                    self._direct_camera = UsbCamera(
                        index=index,
                        width=int(os.environ.get(DIRECT_CAMERA_WIDTH_ENV, "960")),
                        height=int(os.environ.get(DIRECT_CAMERA_HEIGHT_ENV, "540")),
                    )
                    self._direct_camera.open()
                    self._direct_camera_index = self._direct_camera.index
                frame = self._direct_camera.read()
                if frame is None:
                    return None
                import cv2

                ok, jpeg = cv2.imencode(".jpg", frame.image_bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 82])
                if not ok:
                    return None
                return jpeg.tobytes()
            except Exception as exc:
                self._last_error = f"Direct camera fallback failed: {exc}"
                self._close_direct_camera()
                return None

    def _close_direct_camera(self) -> None:
        if self._direct_camera:
            self._direct_camera.close()
        self._direct_camera = None
        self._direct_camera_index = None

    @staticmethod
    def _parse_camera_source(source: str) -> tuple[str, int | None]:
        if source in {"auto", "desktop"}:
            return source, None
        if source.startswith("direct:"):
            try:
                index = int(source.split(":", 1)[1])
            except ValueError as exc:
                raise HTTPException(status_code=400, detail="Invalid direct camera source.") from exc
            if index < 0 or index > 16:
                raise HTTPException(status_code=400, detail="Camera index out of range.")
            return "direct", index
        raise HTTPException(status_code=400, detail="Unsupported camera source.")

    @staticmethod
    def _direct_index_for_selected_source(source: str) -> int:
        if source.startswith("direct:"):
            return int(source.split(":", 1)[1])
        return int(os.environ.get(DIRECT_CAMERA_INDEX_ENV, "0"))

    @staticmethod
    def _camera_source_label(source: str | None, direct_index: int | None) -> str | None:
        if source == "desktop":
            return "Microscope feed"
        if source == "direct":
            return WebProbeService._direct_camera_label(direct_index) if direct_index is not None else "Direct camera"
        return None

    @staticmethod
    def _direct_camera_label(index: int) -> str:
        return DIRECT_CAMERA_LABELS.get(index, f"Camera {index}")

    @staticmethod
    def _direct_camera_fps() -> float:
        try:
            return max(1.0, float(os.environ.get(DIRECT_CAMERA_FPS_ENV, str(DEFAULT_DIRECT_CAMERA_FPS))))
        except ValueError:
            return DEFAULT_DIRECT_CAMERA_FPS

    @staticmethod
    def _read_published_frame(timeout_seconds: float = 1.0) -> bytes | None:
        try:
            with urlopen(f"{publisher_url()}/frame.jpg", timeout=timeout_seconds) as response:
                if response.status != 200:
                    return None
                return response.read()
        except (HTTPError, URLError, TimeoutError, OSError):
            return None

    @staticmethod
    def _read_publisher_status(timeout_seconds: float = 0.2) -> dict[str, object] | None:
        try:
            with urlopen(f"{publisher_url()}/health", timeout=timeout_seconds) as response:
                if response.status != 200:
                    return None
                return json.loads(response.read().decode("ascii"))
        except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None

    @staticmethod
    def _mjpeg_part(payload: bytes) -> bytes:
        return (
            b"--frame\r\n"
            b"Content-Type: image/jpeg\r\n"
            + f"Content-Length: {len(payload)}\r\n\r\n".encode("ascii")
            + payload
            + b"\r\n"
        )


service = WebProbeService()
app = FastAPI(
    title="Semi Auto Probe Web",
    version="0.1.0",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.middleware("http")
async def collect_request_metrics(request: Request, call_next):
    client_id = ""
    if not request.url.path.startswith("/internal/"):
        client_id = service.begin_request(request)
    try:
        return await call_next(request)
    finally:
        if not request.url.path.startswith("/internal/"):
            service.end_request(client_id)


def pid_file_path() -> Path:
    return Path(os.environ.get(PID_FILE_ENV, str(DEFAULT_PID_FILE)))


def write_pid_file() -> None:
    path = pid_file_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(os.getpid()), encoding="ascii")
    atexit.register(remove_pid_file)


def remove_pid_file() -> None:
    path = pid_file_path()
    try:
        if path.read_text(encoding="ascii").strip() == str(os.getpid()):
            path.unlink()
    except OSError:
        return


def require_access_token(
    x_access_token: str | None = Header(default=None),
    token: str | None = Query(default=None),
) -> None:
    expected = os.environ.get(ACCESS_TOKEN_ENV)
    if not expected:
        return
    if x_access_token == expected or token == expected:
        return
    raise HTTPException(status_code=401, detail=f"Missing or invalid {ACCESS_TOKEN_ENV}.")


@app.on_event("startup")
def startup() -> None:
    write_pid_file()
    service.start_from_environment()


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/status", dependencies=[Depends(require_access_token)])
def api_status() -> dict[str, object]:
    status = service.status()
    return asdict(status) | {"auth_required": bool(os.environ.get(ACCESS_TOKEN_ENV))}


@app.get("/api/ports", dependencies=[Depends(require_access_token)])
def api_ports() -> dict[str, object]:
    return {"ports": list_serial_ports()}


@app.get("/api/positions", dependencies=[Depends(require_access_token)])
def api_positions() -> dict[str, object]:
    return service.read_positions()


@app.get("/api/connections", dependencies=[Depends(require_access_token)])
def api_connections() -> dict[str, object]:
    return service.connections()


@app.get("/api/camera-sources", dependencies=[Depends(require_access_token)])
def api_camera_sources() -> dict[str, object]:
    return service.camera_sources()


@app.post("/api/camera-source", dependencies=[Depends(require_access_token)])
def api_camera_source(source: str = Query(...)) -> dict[str, object]:
    return service.set_camera_source(source)


@app.post("/internal/release-camera")
def internal_release_camera(request: Request) -> dict[str, object]:
    client_host = request.client.host if request.client else ""
    if client_host not in {"127.0.0.1", "::1"}:
        raise HTTPException(status_code=403, detail="Localhost only.")
    return service.release_direct_camera()


@app.get("/camera.mjpg", dependencies=[Depends(require_access_token)])
def camera_stream(request: Request, source: str | None = Query(default=None)) -> StreamingResponse:
    return StreamingResponse(
        service.mjpeg_frames(request=request, source=source),
        media_type="multipart/x-mixed-replace; boundary=frame",
    )


def main() -> None:
    import uvicorn

    host = os.environ.get("SEMI_AUTO_PROBE_WEB_HOST", "127.0.0.1")
    port = int(os.environ.get("SEMI_AUTO_PROBE_WEB_PORT", "8000"))
    uvicorn.run("semi_auto_probe.web_app:app", host=host, port=port, reload=False)


if __name__ == "__main__":
    main()
