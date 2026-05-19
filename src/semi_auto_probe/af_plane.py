from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass, field
from typing import Iterable

import numpy as np


@dataclass(frozen=True)
class AFMeshPoint:
    index: int
    row: int
    col: int
    x: int
    y: int

    def to_dict(self) -> dict[str, int]:
        return {
            "index": self.index,
            "row": self.row,
            "col": self.col,
            "x": self.x,
            "y": self.y,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AFMeshPoint":
        return cls(
            index=int(data["index"]),
            row=int(data["row"]),
            col=int(data["col"]),
            x=int(data["x"]),
            y=int(data["y"]),
        )


@dataclass
class SamplePlaneModel:
    enabled: bool
    type: str
    a: float
    b: float
    c: float
    rms_residual: float
    pv_residual: float
    max_abs_residual: float
    tilt_x_deg: float
    tilt_y_deg: float
    valid_points: int
    failed_points: int
    timestamp: float = field(default_factory=time.time)
    mesh_points: list[dict[str, object]] = field(default_factory=list)
    measured_points: list[dict[str, object]] = field(default_factory=list)

    def z_at(self, x: float, y: float) -> float:
        return self.a * x + self.b * y + self.c

    def implicit_coefficients(self) -> tuple[float, float, float, float]:
        return self.a, self.b, -1.0, self.c

    def to_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "type": self.type,
            "a": self.a,
            "b": self.b,
            "c": self.c,
            "rms_residual": self.rms_residual,
            "pv_residual": self.pv_residual,
            "max_abs_residual": self.max_abs_residual,
            "tilt_x_deg": self.tilt_x_deg,
            "tilt_y_deg": self.tilt_y_deg,
            "valid_points": self.valid_points,
            "failed_points": self.failed_points,
            "timestamp": self.timestamp,
            "mesh_points": list(self.mesh_points),
            "measured_points": list(self.measured_points),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "SamplePlaneModel":
        return cls(
            enabled=bool(data.get("enabled", True)),
            type=str(data.get("type", "plane")),
            a=float(data["a"]),
            b=float(data["b"]),
            c=float(data["c"]),
            rms_residual=float(data.get("rms_residual", 0.0)),
            pv_residual=float(data.get("pv_residual", 0.0)),
            max_abs_residual=float(data.get("max_abs_residual", 0.0)),
            tilt_x_deg=float(data.get("tilt_x_deg", math.degrees(math.atan(float(data["a"]))))),
            tilt_y_deg=float(data.get("tilt_y_deg", math.degrees(math.atan(float(data["b"]))))),
            valid_points=int(data.get("valid_points", 0)),
            failed_points=int(data.get("failed_points", 0)),
            timestamp=float(data.get("timestamp", time.time())),
            mesh_points=list(data.get("mesh_points", [])),
            measured_points=list(data.get("measured_points", [])),
        )


class SamplePlaneState:
    # Shared in-process plane model. Other modules can query this without
    # depending on Tk widgets or the mapping panel instance.
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._model: SamplePlaneModel | None = None

    def set_model(self, model: SamplePlaneModel) -> None:
        with self._lock:
            self._model = model

    def clear(self) -> None:
        with self._lock:
            self._model = None

    def get_model(self) -> SamplePlaneModel | None:
        with self._lock:
            return self._model

    def get_focus_z_at_xy(self, x: float, y: float) -> float | None:
        with self._lock:
            if self._model is None or not self._model.enabled:
                return None
            return self._model.z_at(x, y)


sample_plane_state = SamplePlaneState()


def set_sample_plane_model(model: SamplePlaneModel) -> None:
    sample_plane_state.set_model(model)


def clear_sample_plane_model() -> None:
    sample_plane_state.clear()


def get_sample_plane_model() -> SamplePlaneModel | None:
    return sample_plane_state.get_model()


def get_focus_z_at_xy(x: float, y: float) -> float | None:
    return sample_plane_state.get_focus_z_at_xy(x, y)


def generate_af_mesh(
    mesh_type: str,
    center_x: int,
    center_y: int,
    x_range: int,
    y_range: int,
    rows: int,
    cols: int,
    x_step: int | None = None,
    y_step: int | None = None,
    use_step_spacing: bool = False,
) -> list[AFMeshPoint]:
    if rows <= 0 or cols <= 0:
        raise ValueError("Rows and columns must be positive.")
    if x_range < 0 or y_range < 0:
        raise ValueError("X/Y range must be zero or positive.")

    normalized_type = mesh_type.strip().lower()
    if normalized_type not in {"rectangular", "square grid", "hexagonal", "hex"}:
        raise ValueError(f"Unsupported mesh type: {mesh_type}")

    if use_step_spacing:
        if x_step is None or x_step <= 0:
            raise ValueError("X step must be positive in step-spacing mode.")
        if y_step is None or y_step <= 0:
            raise ValueError("Y step must be positive in step-spacing mode.")
        x_values = _axis_values_by_step(center_x, x_range, x_step)
        y_values = _axis_values_by_step(center_y, y_range, y_step)
    else:
        x_values = _axis_values_by_count(center_x, x_range, cols)
        y_values = _axis_values_by_count(center_y, y_range, rows)

    # Mesh points are generated row-by-row in serpentine order to reduce XY travel.
    points: list[AFMeshPoint] = []
    x_min = center_x - x_range / 2.0
    x_max = center_x + x_range / 2.0
    spacing_x = _nominal_spacing(x_values)
    for row, y_value in enumerate(y_values):
        row_values = list(x_values)
        if normalized_type in {"hexagonal", "hex"} and row % 2 == 1:
            row_values = [int(round(x_value + spacing_x / 2.0)) for x_value in row_values]
            row_values = [x_value for x_value in row_values if x_min - 0.5 <= x_value <= x_max + 0.5]
        if row % 2 == 1:
            row_values.reverse()
        for col, x_value in enumerate(row_values):
            points.append(AFMeshPoint(index=len(points) + 1, row=row, col=col, x=int(x_value), y=int(y_value)))
    return points


def fit_sample_plane(
    samples: Iterable[tuple[float, float, float]],
    failed_points: int = 0,
    mesh_points: list[dict[str, object]] | None = None,
    measured_points: list[dict[str, object]] | None = None,
) -> SamplePlaneModel:
    # First implementation is a conservative least-squares plane:
    # z = a*x + b*y + c. Higher-order models can be added beside this later.
    points = list(samples)
    if len(points) < 3:
        raise ValueError("At least three valid AF points are required to fit a plane.")

    matrix = np.array([[x, y, 1.0] for x, y, _z in points], dtype=np.float64)
    values = np.array([z for _x, _y, z in points], dtype=np.float64)
    coefficients, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    a, b, c = (float(coefficients[0]), float(coefficients[1]), float(coefficients[2]))
    fitted = matrix @ coefficients
    residuals = values - fitted
    rms = float(math.sqrt(float(np.mean(residuals * residuals))))
    pv = float(np.max(residuals) - np.min(residuals))
    max_abs = float(np.max(np.abs(residuals)))
    return SamplePlaneModel(
        enabled=True,
        type="plane",
        a=a,
        b=b,
        c=c,
        rms_residual=rms,
        pv_residual=pv,
        max_abs_residual=max_abs,
        tilt_x_deg=math.degrees(math.atan(a)),
        tilt_y_deg=math.degrees(math.atan(b)),
        valid_points=len(points),
        failed_points=failed_points,
        mesh_points=list(mesh_points or []),
        measured_points=list(measured_points or []),
    )


def _axis_values_by_count(center: int, span: int, count: int) -> list[int]:
    if count <= 0:
        raise ValueError("Axis point count must be positive.")
    if count == 1:
        return [int(center)]
    start = center - span / 2.0
    step = span / (count - 1)
    return [int(round(start + index * step)) for index in range(count)]


def _axis_values_by_step(center: int, span: int, step: int) -> list[int]:
    if step <= 0:
        raise ValueError("Axis step must be positive.")
    start = center - span / 2.0
    end = center + span / 2.0
    values: list[int] = []
    position = start
    while position <= end + 1e-9:
        values.append(int(round(position)))
        position += step
    end_value = int(round(end))
    if values[-1] != end_value:
        values.append(end_value)
    return values


def _nominal_spacing(values: list[int]) -> float:
    if len(values) < 2:
        return 0.0
    return float(values[1] - values[0])
