from __future__ import annotations

import importlib.util
import json
import math
import queue
import threading
import time
import tkinter as tk
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import numpy as np


GDS_MISSING_MESSAGE = "gdstk is required for GDS loading. Please install it with: pip install gdstk"
DEFAULT_MAX_GDS_SHAPES: int | None = None
LARGE_GDS_WARNING_SHAPES = 50000
LAYER_TOGGLE_COLUMNS = 5
GDS_VIEW_MARGIN_PX = 40
LAYOUTBOND_AUTOSAVE_FILENAME = "last_layoutbond_mapping.json"
SHIFT_EVENT_MASK = 0x0001


class ToggleSwitch(tk.Canvas):
    def __init__(
        self,
        parent: tk.Widget,
        variable: tk.BooleanVar,
        colors: dict[str, str],
        *,
        command: Callable[[], None] | None = None,
        background: str | None = None,
        width: int = 44,
        height: int = 24,
    ) -> None:
        super().__init__(
            parent,
            width=width,
            height=height,
            bg=background or colors["surface"],
            bd=0,
            highlightthickness=0,
            cursor="hand2",
        )
        self.variable = variable
        self.colors = colors
        self.command = command
        self.switch_width = width
        self.switch_height = height
        self.variable.trace_add("write", lambda *_args: self._draw())
        self.bind("<Button-1>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Return>", self._toggle)
        self._draw()

    def _toggle(self, _event: tk.Event | None = None) -> str:
        self.variable.set(not bool(self.variable.get()))
        if self.command is not None:
            self.command()
        return "break"

    def _draw(self) -> None:
        self.delete("all")
        enabled = bool(self.variable.get())
        width = self.switch_width
        height = self.switch_height
        radius = height / 2
        track_fill = "#0f3b2d" if enabled else self.colors["surface_3"]
        track_outline = "#1f7a5a" if enabled else self.colors["border"]
        knob_fill = "#d1fae5" if enabled else self.colors["muted"]
        self.create_oval(1, 1, 1 + height - 2, height - 1, fill=track_fill, outline=track_outline, width=1)
        self.create_oval(width - height + 1, 1, width - 1, height - 1, fill=track_fill, outline=track_outline, width=1)
        self.create_rectangle(radius, 1, width - radius, height - 1, fill=track_fill, outline=track_fill)
        self.create_line(radius, 1, width - radius, 1, fill=track_outline)
        self.create_line(radius, height - 1, width - radius, height - 1, fill=track_outline)
        knob_radius = radius - 4
        knob_center = width - radius if enabled else radius
        self.create_oval(
            knob_center - knob_radius,
            radius - knob_radius,
            knob_center + knob_radius,
            radius + knob_radius,
            fill=knob_fill,
            outline=knob_fill,
        )


@dataclass(frozen=True)
class CalibrationPoint:
    name: str
    u: float | None = None
    v: float | None = None
    x_um: float | None = None
    y_um: float | None = None

    @property
    def is_complete(self) -> bool:
        return all(value is not None and math.isfinite(float(value)) for value in (self.u, self.v, self.x_um, self.y_um))

    def to_dict(self) -> dict[str, float | str | None]:
        return {
            "name": self.name,
            "u": self.u,
            "v": self.v,
            "x_um": self.x_um,
            "y_um": self.y_um,
        }

    @classmethod
    def from_dict(cls, name: str, data: dict[str, object]) -> "CalibrationPoint":
        return cls(
            name=name,
            u=_optional_float(data.get("u")),
            v=_optional_float(data.get("v")),
            x_um=_optional_float(data.get("x_um", data.get("x"))),
            y_um=_optional_float(data.get("y_um", data.get("y"))),
        )


@dataclass(frozen=True)
class StageMovePlan:
    target_pulses: dict[str, int]
    deltas: dict[str, int]

    @property
    def has_motion(self) -> bool:
        return any(delta != 0 for delta in self.deltas.values())


def stage_move_plan_from_um(
    current_pulses: dict[str, int],
    target_x_um: float,
    target_y_um: float,
    um_per_pulse_x: float,
    um_per_pulse_y: float,
) -> StageMovePlan:
    if um_per_pulse_x <= 0 or um_per_pulse_y <= 0:
        raise ValueError("Stage um-per-pulse values must be positive.")
    if not math.isfinite(target_x_um) or not math.isfinite(target_y_um):
        raise ValueError("Target stage coordinates must be finite.")

    target_pulses = {
        "X": int(round(target_x_um / um_per_pulse_x)),
        "Y": int(round(target_y_um / um_per_pulse_y)),
    }
    deltas = {
        axis: target_pulses[axis] - int(current_pulses.get(axis, 0))
        for axis in ("X", "Y")
    }
    return StageMovePlan(target_pulses=target_pulses, deltas=deltas)


def stage_xyz_move_plan_from_um(
    current_pulses: dict[str, int],
    target_um: dict[str, float],
    um_per_pulse: dict[str, float],
) -> StageMovePlan:
    target_pulses: dict[str, int] = {}
    for axis, value_um in target_um.items():
        if axis not in {"X", "Y", "Z"}:
            raise ValueError(f"Unsupported stage axis: {axis}")
        scale = float(um_per_pulse.get(axis, 0.0))
        if scale <= 0:
            raise ValueError(f"{axis} um-per-pulse value must be positive.")
        if not math.isfinite(float(value_um)):
            raise ValueError(f"{axis} target stage coordinate must be finite.")
        target_pulses[axis] = int(round(float(value_um) / scale))
    deltas = {axis: target_pulses[axis] - int(current_pulses.get(axis, 0)) for axis in target_pulses}
    return StageMovePlan(target_pulses=target_pulses, deltas=deltas)


def snap_gds_point(point: tuple[float, float], grid_um: float) -> tuple[float, float]:
    if grid_um <= 0:
        return point
    u, v = point
    return round(u / grid_um) * grid_um, round(v / grid_um) * grid_um


def layer_grid_position(index: int, columns: int = LAYER_TOGGLE_COLUMNS) -> tuple[int, int]:
    if columns <= 0:
        raise ValueError("Layer toggle column count must be positive.")
    if index < 0:
        raise ValueError("Layer toggle index must be non-negative.")
    return index // columns, index % columns


def shape_visible_in_view(shape: "GDSShape", transform: "CanvasTransform", width: int, height: int, *, margin: int = GDS_VIEW_MARGIN_PX) -> bool:
    min_u, min_v, max_u, max_v = shape.bbox
    x1, y1 = transform.gds_to_canvas(min_u, max_v)
    x2, y2 = transform.gds_to_canvas(max_u, min_v)
    left, right = sorted((x1, x2))
    top, bottom = sorted((y1, y2))
    return right >= -margin and left <= width + margin and bottom >= -margin and top <= height + margin


def render_gds_preview_ppm(
    model: "GDSLayoutModel",
    transform: "CanvasTransform",
    width: int,
    height: int,
    layer_visibility: dict[tuple[int, int], bool],
    layer_colors: dict[tuple[int, int], str],
) -> tuple[bytes, int] | None:
    """Rasterize visible GDS geometry into a PPM image for fast Tk display."""
    try:
        import cv2  # type: ignore[import-not-found]
    except Exception:
        return None

    width = max(int(width), 1)
    height = max(int(height), 1)
    image = np.zeros((height, width, 3), dtype=np.uint8)
    image[:, :] = _hex_to_rgb("#05070a")
    rendered = 0
    scale = float(transform.scale)
    offset_x = float(transform.offset_x)
    offset_y = float(transform.offset_y)

    for shape in model.shapes:
        if not layer_visibility.get(shape.layer_key, False):
            continue
        if not shape_visible_in_view(shape, transform, width, height):
            continue
        points = np.asarray(shape.points, dtype=float)
        if points.shape[0] < 3:
            continue
        coords = np.empty((points.shape[0], 2), dtype=np.int32)
        coords[:, 0] = np.rint(offset_x + points[:, 0] * scale).astype(np.int32)
        coords[:, 1] = np.rint(offset_y - points[:, 1] * scale).astype(np.int32)
        color = _hex_to_rgb(layer_colors.get(shape.layer_key, "#60a5fa"))
        try:
            cv2.fillPoly(image, [coords], color=color, lineType=cv2.LINE_8)
            cv2.polylines(image, [coords], isClosed=True, color=color, thickness=1, lineType=cv2.LINE_8)
        except Exception:
            continue
        rendered += 1

    header = f"P6 {width} {height} 255\n".encode("ascii")
    return header + image.tobytes(), rendered


def apply_center_magnifier_ppm(payload: bytes, magnification: float, radius_fraction: float = 0.26) -> bytes:
    if magnification <= 1.0:
        return payload
    try:
        header, body = payload.split(b"\n", 1)
        parts = header.split()
        if len(parts) != 4 or parts[0] != b"P6" or parts[3] != b"255":
            return payload
        width = int(parts[1])
        height = int(parts[2])
        image = np.frombuffer(body, dtype=np.uint8).reshape((height, width, 3)).copy()
        import cv2  # type: ignore[import-not-found]

        center_x = (width - 1) / 2.0
        center_y = (height - 1) / 2.0
        radius = max(min(width, height) * max(min(radius_fraction, 0.48), 0.08), 8.0)
        y_grid, x_grid = np.indices((height, width), dtype=np.float32)
        distance = np.sqrt((x_grid - center_x) ** 2 + (y_grid - center_y) ** 2)
        mask = distance <= radius
        if not np.any(mask):
            return payload

        map_x = x_grid.copy()
        map_y = y_grid.copy()
        map_x[mask] = center_x + (x_grid[mask] - center_x) / magnification
        map_y[mask] = center_y + (y_grid[mask] - center_y) / magnification
        magnified = cv2.remap(image, map_x, map_y, interpolation=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
        result = image.copy()
        result[mask] = magnified[mask]
        cv2.circle(result, (int(round(center_x)), int(round(center_y))), int(round(radius)), (45, 212, 191), 2, cv2.LINE_AA)
        return f"P6 {width} {height} 255\n".encode("ascii") + result.tobytes()
    except Exception:
        return payload


class AffineCoordinateMapper:
    """Affine transform between GDS layout coordinates and stage micrometers."""

    def __init__(self, matrix: np.ndarray, residuals_um: dict[str, float] | None = None, rms_error_um: float = 0.0) -> None:
        matrix_array = np.asarray(matrix, dtype=float)
        if matrix_array.shape != (2, 3):
            raise ValueError("Affine matrix must have shape 2x3.")
        self.matrix = matrix_array
        self.residuals_um = dict(residuals_um or {})
        self.rms_error_um = float(rms_error_um)
        self._inverse_linear = self._invert_linear_part(matrix_array[:, 1:3])

    @classmethod
    def fit(cls, points: Iterable[CalibrationPoint], *, singular_tolerance: float = 1e-12) -> "AffineCoordinateMapper":
        complete_points = list(points)
        if len(complete_points) != 4 or any(not point.is_complete for point in complete_points):
            raise ValueError("Four complete calibration points are required.")

        gds_pairs = [(float(point.u), float(point.v)) for point in complete_points]
        if _has_duplicate_pairs(gds_pairs):
            raise ValueError("Calibration GDS points must be distinct.")

        stage_pairs = [(float(point.x_um), float(point.y_um)) for point in complete_points]
        if _has_duplicate_pairs(stage_pairs):
            raise ValueError("Calibration stage points must be distinct.")

        design = np.array([[1.0, u, v] for u, v in gds_pairs], dtype=float)
        if np.linalg.matrix_rank(design, tol=singular_tolerance) < 3:
            raise ValueError("Calibration GDS points must not be collinear.")

        x_values = np.array([point.x_um for point in complete_points], dtype=float)
        y_values = np.array([point.y_um for point in complete_points], dtype=float)
        x_coefficients, *_ = np.linalg.lstsq(design, x_values, rcond=None)
        y_coefficients, *_ = np.linalg.lstsq(design, y_values, rcond=None)
        matrix = np.vstack([x_coefficients, y_coefficients])

        linear = matrix[:, 1:3]
        determinant = float(np.linalg.det(linear))
        if abs(determinant) <= singular_tolerance:
            raise ValueError("Fitted affine transform is singular.")

        mapper = cls(matrix)
        residuals: dict[str, float] = {}
        squared_errors = []
        for point in complete_points:
            predicted_x, predicted_y = mapper.gds_to_stage(float(point.u), float(point.v))
            residual = math.hypot(predicted_x - float(point.x_um), predicted_y - float(point.y_um))
            residuals[point.name] = residual
            squared_errors.append(residual * residual)

        rms_error = math.sqrt(sum(squared_errors) / len(squared_errors)) if squared_errors else 0.0
        return cls(matrix, residuals_um=residuals, rms_error_um=rms_error)

    @staticmethod
    def _invert_linear_part(linear: np.ndarray) -> np.ndarray:
        determinant = float(np.linalg.det(linear))
        if abs(determinant) <= 1e-12:
            raise ValueError("Affine transform is singular.")
        return np.linalg.inv(linear)

    def gds_to_stage(self, u: float, v: float) -> tuple[float, float]:
        values = self.matrix @ np.array([1.0, float(u), float(v)], dtype=float)
        return float(values[0]), float(values[1])

    def stage_to_gds(self, x_um: float, y_um: float) -> tuple[float, float]:
        offset = self.matrix[:, 0]
        layout = self._inverse_linear @ (np.array([float(x_um), float(y_um)], dtype=float) - offset)
        return float(layout[0]), float(layout[1])

    def to_dict(self) -> dict[str, object]:
        return {
            "matrix": self.matrix.tolist(),
            "rms_error_um": self.rms_error_um,
            "residuals_um": dict(self.residuals_um),
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "AffineCoordinateMapper":
        return cls(
            np.asarray(data["matrix"], dtype=float),
            residuals_um={str(key): float(value) for key, value in dict(data.get("residuals_um", {})).items()},
            rms_error_um=float(data.get("rms_error_um", 0.0)),
        )


@dataclass
class CanvasTransform:
    scale: float = 1.0
    offset_x: float = 0.0
    offset_y: float = 0.0

    def gds_to_canvas(self, u: float, v: float) -> tuple[float, float]:
        return self.offset_x + float(u) * self.scale, self.offset_y - float(v) * self.scale

    def canvas_to_gds(self, screen_x: float, screen_y: float) -> tuple[float, float]:
        if self.scale == 0:
            raise ValueError("Canvas transform scale must be non-zero.")
        return (float(screen_x) - self.offset_x) / self.scale, (self.offset_y - float(screen_y)) / self.scale

    def fit_to_bounds(self, bounds: tuple[float, float, float, float], width: int, height: int, padding: float = 32.0) -> None:
        min_u, min_v, max_u, max_v = bounds
        span_u = max(max_u - min_u, 1e-9)
        span_v = max(max_v - min_v, 1e-9)
        usable_width = max(float(width) - 2.0 * padding, 1.0)
        usable_height = max(float(height) - 2.0 * padding, 1.0)
        self.scale = max(min(usable_width / span_u, usable_height / span_v), 1e-12)
        center_u = (min_u + max_u) / 2.0
        center_v = (min_v + max_v) / 2.0
        self.offset_x = float(width) / 2.0 - center_u * self.scale
        self.offset_y = float(height) / 2.0 + center_v * self.scale

    def pan(self, dx: float, dy: float) -> None:
        self.offset_x += float(dx)
        self.offset_y += float(dy)

    def zoom_at(self, canvas_x: float, canvas_y: float, factor: float, *, min_scale: float = 1e-6, max_scale: float = 1e6) -> None:
        if factor <= 0:
            return
        u, v = self.canvas_to_gds(canvas_x, canvas_y)
        self.scale = max(min(self.scale * factor, max_scale), min_scale)
        self.offset_x = float(canvas_x) - u * self.scale
        self.offset_y = float(canvas_y) + v * self.scale


@dataclass(frozen=True)
class GDSShape:
    points: tuple[tuple[float, float], ...]
    layer: int
    datatype: int
    bbox: tuple[float, float, float, float]

    @property
    def layer_key(self) -> tuple[int, int]:
        return self.layer, self.datatype


@dataclass(frozen=True)
class GDSLabel:
    text: str
    origin: tuple[float, float]
    layer: int


@dataclass
class GDSLayoutModel:
    path: Path
    top_cell_name: str
    top_cell_names: tuple[str, ...]
    shapes: list[GDSShape]
    labels: list[GDSLabel]
    bounds: tuple[float, float, float, float] | None
    warning: str | None = None

    @property
    def layers(self) -> tuple[tuple[int, int], ...]:
        return tuple(sorted({shape.layer_key for shape in self.shapes}))

    @classmethod
    def load(cls, path: str | Path, top_cell_name: str | None = None, *, max_shapes: int | None = DEFAULT_MAX_GDS_SHAPES) -> "GDSLayoutModel":
        if importlib.util.find_spec("gdstk") is None:
            raise RuntimeError(GDS_MISSING_MESSAGE)
        import gdstk  # type: ignore[import-not-found]

        gds_path = Path(path)
        library = gdstk.read_gds(str(gds_path))
        top_cells = list(library.top_level())
        if not top_cells:
            raise ValueError("GDS file contains no top-level cells.")

        top_cell_names = tuple(cell.name for cell in top_cells)
        if top_cell_name:
            candidates = [cell for cell in top_cells if cell.name == top_cell_name]
            if not candidates:
                raise ValueError(f"Top cell not found: {top_cell_name}")
            top_cell = candidates[0]
        else:
            top_cell = top_cells[0]

        working_cell = cls._copy_cell(top_cell)
        try:
            working_cell.flatten()
        except TypeError:
            working_cell.flatten(True)

        shapes: list[GDSShape] = []
        warning = None
        for polygon in cls._iter_polygons_and_paths(working_cell):
            points = _shape_points(polygon)
            if len(points) < 3:
                continue
            shape = GDSShape(
                points=points,
                layer=int(getattr(polygon, "layer", 0)),
                datatype=int(getattr(polygon, "datatype", 0)),
                bbox=_points_bbox(points),
            )
            shapes.append(shape)
            if max_shapes is not None and len(shapes) >= max_shapes:
                warning = f"GDS contains more than {max_shapes} polygons/paths; rendering is limited to the first {max_shapes}."
                break

        if warning is None and len(shapes) > LARGE_GDS_WARNING_SHAPES:
            warning = f"Large GDS: {len(shapes)} polygons/paths loaded. Raster preview is enabled for faster display."

        labels = []
        for label in list(getattr(working_cell, "labels", []))[:2000]:
            origin = getattr(label, "origin", None)
            text = getattr(label, "text", "")
            if origin is None or not text:
                continue
            labels.append(GDSLabel(text=str(text), origin=(float(origin[0]), float(origin[1])), layer=int(getattr(label, "layer", 0))))

        bounds = _shapes_bbox(shapes)
        if bounds is None:
            cell_bbox = working_cell.bounding_box()
            if cell_bbox:
                (min_u, min_v), (max_u, max_v) = cell_bbox
                bounds = (float(min_u), float(min_v), float(max_u), float(max_v))

        return cls(
            path=gds_path,
            top_cell_name=top_cell.name,
            top_cell_names=top_cell_names,
            shapes=shapes,
            labels=labels,
            bounds=bounds,
            warning=warning,
        )

    @staticmethod
    def _copy_cell(cell):
        try:
            return cell.copy(f"{cell.name}__mapper_view")
        except TypeError:
            return cell.copy()

    @staticmethod
    def _iter_polygons_and_paths(cell) -> Iterable[object]:
        yield from list(getattr(cell, "polygons", []))
        for path in list(getattr(cell, "paths", [])):
            to_polygons = getattr(path, "to_polygons", None)
            if to_polygons is None:
                continue
            try:
                yield from list(to_polygons())
            except TypeError:
                yield from list(to_polygons(False))


class GDSCanvasViewer:
    def __init__(
        self,
        parent: tk.Widget,
        colors: dict[str, str],
        *,
        on_cursor_gds: Callable[[tuple[float, float] | None], None],
        on_select_gds: Callable[[float, float], None],
        on_shift_double_click_gds: Callable[[float, float], None] | None = None,
    ) -> None:
        self.colors = colors
        self.on_cursor_gds = on_cursor_gds
        self.on_select_gds = on_select_gds
        self.on_shift_double_click_gds = on_shift_double_click_gds
        self.model: GDSLayoutModel | None = None
        self.transform = CanvasTransform()
        self.layer_visibility: dict[tuple[int, int], bool] = {}
        self.layer_order: list[tuple[int, int]] = []
        self.selected_gds: tuple[float, float] | None = None
        self.cursor_gds: tuple[float, float] | None = None
        self.stage_center_gds: tuple[float, float] | None = None
        self.fov_polygon_gds: list[tuple[float, float]] | None = None
        self.matrix_fov_polygons_gds: list[tuple[list[tuple[float, float]], str]] = []
        self.snap_grid_um = 1.0
        self.require_double_click_pick = False
        self.ignore_next_release = False
        self.drag_start: tuple[int, int, float, float] | None = None
        self.drag_last: tuple[int, int] | None = None
        self.dragging = False
        self.configure_job: str | None = None
        self.geometry_photo: tk.PhotoImage | None = None
        self.last_rendered_shape_count = 0

        self.canvas = tk.Canvas(parent, bg="#05070a", highlightthickness=1, highlightbackground=colors["border"], bd=0, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", self._on_leave)
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_button_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_button_release)
        self.canvas.bind("<Double-Button-1>", self._on_double_click)
        self.draw_message("Load a GDS file to begin.")

    def set_model(self, model: GDSLayoutModel) -> None:
        self.model = model
        self.selected_gds = None
        self.stage_center_gds = None
        self.fov_polygon_gds = None
        self.layer_order = list(model.layers)
        self.layer_visibility = {layer: False for layer in self.layer_order}
        self.fit_to_view()

    def set_layer_visibility(self, layer: tuple[int, int], visible: bool) -> None:
        self.layer_visibility[layer] = bool(visible)
        self.redraw()

    def set_snap_grid_um(self, grid_um: float) -> None:
        self.snap_grid_um = max(float(grid_um), 0.0)

    def set_pick_mode(self, active: bool) -> None:
        self.require_double_click_pick = bool(active)
        self.canvas.configure(cursor="tcross" if active else "crosshair")

    def fit_to_view(self) -> None:
        if self.model is None or self.model.bounds is None:
            self.redraw()
            return
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        self.transform.fit_to_bounds(self.model.bounds, width, height)
        self.redraw()

    def set_selected_gds(self, point: tuple[float, float] | None) -> None:
        self.selected_gds = point
        self._draw_overlay_items()

    def set_stage_overlay(self, center_gds: tuple[float, float] | None, fov_polygon_gds: list[tuple[float, float]] | None) -> None:
        self.stage_center_gds = center_gds
        self.fov_polygon_gds = fov_polygon_gds
        self._draw_overlay_items()

    def set_matrix_overlay(self, polygons_gds: list[tuple[list[tuple[float, float]], str]]) -> None:
        self.matrix_fov_polygons_gds = polygons_gds
        self._draw_overlay_items()

    def draw_message(self, message: str) -> None:
        self.canvas.delete("all")
        self.canvas.create_text(
            max(self.canvas.winfo_width(), 1) / 2,
            max(self.canvas.winfo_height(), 1) / 2,
            text=message,
            fill=self.colors.get("muted", "#94a3b8"),
            font=("Segoe UI Semibold", 14),
            width=max(self.canvas.winfo_width() - 40, 200),
            justify="center",
        )

    def redraw(self) -> None:
        self.canvas.delete("all")
        self.geometry_photo = None
        self.last_rendered_shape_count = 0
        if self.model is None:
            self.draw_message("Load a GDS file to begin.")
            return
        if not self.model.shapes:
            self.draw_message("No polygons or paths were found in the selected top cell.")
            return

        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        if self._draw_geometry_raster(width, height):
            self._draw_labels(width, height)
            self._draw_overlay_items()
            return

        for shape in self.model.shapes:
            if not self.layer_visibility.get(shape.layer_key, False):
                continue
            if not self._shape_visible(shape, width, height):
                continue
            coords: list[float] = []
            for u, v in shape.points:
                x, y = self.transform.gds_to_canvas(u, v)
                coords.extend((x, y))
            if len(coords) >= 6:
                color = self._layer_color(shape.layer_key)
                try:
                    self.canvas.create_polygon(coords, fill=color, outline=color, width=1, tags="gds_geometry")
                except tk.TclError:
                    continue

        self._draw_labels(width, height)
        self._draw_overlay_items()

    def _draw_geometry_raster(self, width: int, height: int) -> bool:
        if self.model is None:
            return False
        layer_colors = {layer: self._layer_color(layer) for layer in self.layer_order}
        rendered = render_gds_preview_ppm(self.model, self.transform, width, height, self.layer_visibility, layer_colors)
        if rendered is None:
            return False
        ppm_bytes, rendered_count = rendered
        try:
            self.geometry_photo = tk.PhotoImage(data=ppm_bytes, format="PPM")
            self.canvas.create_image(0, 0, image=self.geometry_photo, anchor="nw", tags="gds_geometry")
            self.last_rendered_shape_count = rendered_count
        except tk.TclError:
            self.geometry_photo = None
            return False
        return True

    def _draw_labels(self, width: int, height: int) -> None:
        if self.model is None or self.transform.scale <= 0.02:
            return
        for label in self.model.labels[:300]:
            x, y = self.transform.gds_to_canvas(*label.origin)
            if -40 <= x <= width + 40 and -20 <= y <= height + 20:
                self.canvas.create_text(x, y, text=label.text, fill="#e5e7eb", anchor="center", font=("Segoe UI", 8), tags="gds_labels")

    def _draw_overlay_items(self) -> None:
        try:
            self.canvas.delete("gds_cursor")
            self.canvas.delete("gds_overlay")
            self.canvas.delete("gds_matrix_overlay")
            self.canvas.delete("gds_selection")
            for polygon_gds, label in self.matrix_fov_polygons_gds:
                if len(polygon_gds) < 3:
                    continue
                coords: list[float] = []
                canvas_points = []
                for u, v in polygon_gds:
                    x, y = self.transform.gds_to_canvas(u, v)
                    coords.extend((x, y))
                    canvas_points.append((x, y))
                self.canvas.create_polygon(
                    coords,
                    fill="#9ca3af",
                    outline="#e5e7eb",
                    width=2,
                    dash=(7, 5),
                    stipple="gray25",
                    tags="gds_matrix_overlay",
                )
                if label:
                    cx = sum(point[0] for point in canvas_points) / len(canvas_points)
                    cy = sum(point[1] for point in canvas_points) / len(canvas_points)
                    self.canvas.create_text(
                        cx,
                        cy,
                        text=label,
                        fill="#f8fafc",
                        font=("Segoe UI Semibold", 8),
                        tags="gds_matrix_overlay",
                    )
            if self.fov_polygon_gds and len(self.fov_polygon_gds) >= 3:
                coords: list[float] = []
                for u, v in self.fov_polygon_gds:
                    x, y = self.transform.gds_to_canvas(u, v)
                    coords.extend((x, y))
                self.canvas.create_polygon(
                    coords,
                    fill="#22c55e",
                    outline="#bbf7d0",
                    width=2,
                    stipple="gray25",
                    tags="gds_overlay",
                )
            if self.stage_center_gds is not None:
                self._draw_cross(self.stage_center_gds, "#86efac", "gds_overlay", radius=7)
            if self.selected_gds is not None:
                self._draw_cross(self.selected_gds, "#ef4444", "gds_selection", radius=8)
            if self.cursor_gds is not None:
                self._draw_cursor_crosshair(self.cursor_gds)
        except tk.TclError:
            return

    def _draw_cross(self, point: tuple[float, float], color: str, tag: str, radius: int) -> None:
        x, y = self.transform.gds_to_canvas(*point)
        self.canvas.create_line(x - radius, y, x + radius, y, fill=color, width=2, tags=tag)
        self.canvas.create_line(x, y - radius, x, y + radius, fill=color, width=2, tags=tag)
        self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, outline=color, width=1, tags=tag)

    def _draw_cursor_crosshair(self, point: tuple[float, float]) -> None:
        x, y = self.transform.gds_to_canvas(*point)
        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        color = "#e0f2fe"
        self.canvas.create_line(0, y, width, y, fill=color, width=1, dash=(4, 5), tags="gds_cursor")
        self.canvas.create_line(x, 0, x, height, fill=color, width=1, dash=(4, 5), tags="gds_cursor")
        self.canvas.create_oval(x - 4, y - 4, x + 4, y + 4, outline=color, width=1, tags="gds_cursor")

    def _shape_visible(self, shape: GDSShape, width: int, height: int) -> bool:
        return shape_visible_in_view(shape, self.transform, width, height)

    def _layer_color(self, layer: tuple[int, int]) -> str:
        palette = (
            "#60a5fa",
            "#34d399",
            "#fbbf24",
            "#f472b6",
            "#a78bfa",
            "#fb7185",
            "#2dd4bf",
            "#c084fc",
            "#f97316",
            "#93c5fd",
        )
        try:
            index = self.layer_order.index(layer)
        except ValueError:
            index = 0
        return palette[index % len(palette)]

    def _on_configure(self, _event: tk.Event) -> None:
        if self.configure_job is not None:
            try:
                self.canvas.after_cancel(self.configure_job)
            except tk.TclError:
                pass
        self.configure_job = self.canvas.after(40, self._redraw_after_configure)

    def _redraw_after_configure(self) -> None:
        self.configure_job = None
        if self.model is not None and self.model.bounds is not None:
            self.fit_to_view()
        else:
            self.redraw()

    def _on_motion(self, event: tk.Event) -> None:
        if self.model is None:
            self.cursor_gds = None
            self._draw_overlay_items()
            self.on_cursor_gds(None)
            return
        self.cursor_gds = snap_gds_point(self.transform.canvas_to_gds(event.x, event.y), self.snap_grid_um)
        self.on_cursor_gds(self.cursor_gds)
        self._draw_overlay_items()

    def _on_leave(self, _event: tk.Event) -> None:
        self.cursor_gds = None
        self.on_cursor_gds(None)
        self._draw_overlay_items()

    def _on_mouse_wheel(self, event: tk.Event) -> str:
        if self.model is None:
            return "break"
        delta = getattr(event, "delta", 0)
        num = getattr(event, "num", None)
        zoom_in = delta > 0 or num == 4
        factor = 1.15 if zoom_in else 1.0 / 1.15
        self.transform.zoom_at(event.x, event.y, factor)
        self.redraw()
        return "break"

    def _on_button_press(self, event: tk.Event) -> str:
        self.drag_start = (event.x, event.y, self.transform.offset_x, self.transform.offset_y)
        self.drag_last = (event.x, event.y)
        self.dragging = False
        return "break"

    def _on_drag(self, event: tk.Event) -> str:
        if self.drag_start is None:
            return "break"
        start_x, start_y, offset_x, offset_y = self.drag_start
        dx = event.x - start_x
        dy = event.y - start_y
        if abs(dx) > 2 or abs(dy) > 2:
            self.dragging = True
            self.transform.offset_x = offset_x + dx
            self.transform.offset_y = offset_y + dy
            if self.drag_last is not None:
                last_x, last_y = self.drag_last
                self.canvas.move("all", event.x - last_x, event.y - last_y)
            self.drag_last = (event.x, event.y)
        return "break"

    def _on_button_release(self, event: tk.Event) -> str:
        if self.ignore_next_release:
            self.ignore_next_release = False
            self.drag_start = None
            self.drag_last = None
            self.dragging = False
            return "break"
        was_dragging = self.dragging
        if self.model is not None and not self.dragging and not self.require_double_click_pick:
            point = snap_gds_point(self.transform.canvas_to_gds(event.x, event.y), self.snap_grid_um)
            self.selected_gds = point
            self.on_select_gds(point[0], point[1])
            self._draw_overlay_items()
            self.ignore_next_release = True
        self.drag_start = None
        self.drag_last = None
        self.dragging = False
        if was_dragging:
            self.redraw()
        return "break"

    def _on_double_click(self, event: tk.Event) -> str:
        if self.model is not None and not self.dragging:
            point = snap_gds_point(self.transform.canvas_to_gds(event.x, event.y), self.snap_grid_um)
            self.selected_gds = point
            self.on_select_gds(point[0], point[1])
            self._draw_overlay_items()
            if (
                self.on_shift_double_click_gds is not None
                and not self.require_double_click_pick
                and bool(getattr(event, "state", 0) & SHIFT_EVENT_MASK)
            ):
                self.on_shift_double_click_gds(point[0], point[1])
        self.drag_start = None
        self.drag_last = None
        self.dragging = False
        return "break"


class GDSStageMapperPanel:
    POINT_NAMES = ("P1", "P2", "P3", "P4")

    def __init__(
        self,
        parent: tk.Widget,
        colors: dict[str, str],
        *,
        get_stage_position_um: Callable[[], tuple[float, float] | tuple[float, float, float]],
        move_to_stage_um: Callable[[float, float], None],
        move_to_stage_xyz_um: Callable[[float, float, float | None], None] | None = None,
        get_focus_z_um: Callable[[float, float], float | None] | None = None,
        get_microscope_preview: Callable[[], bytes | None] | None = None,
        fov_width_var: tk.StringVar | None = None,
        fov_height_var: tk.StringVar | None = None,
        use_focus_z_var: tk.BooleanVar | None = None,
        on_focus_z_toggle: Callable[[], None] | None = None,
        on_layout_changed: Callable[[], None] | None = None,
        set_status: Callable[[str], None] | None = None,
    ) -> None:
        self.colors = colors
        self.get_stage_position_um = get_stage_position_um
        self.move_to_stage_um = move_to_stage_um
        self.move_to_stage_xyz_um = move_to_stage_xyz_um
        self.get_focus_z_um = get_focus_z_um
        self.get_microscope_preview = get_microscope_preview
        self.on_focus_z_toggle = on_focus_z_toggle
        self.on_layout_changed = on_layout_changed
        self.set_app_status = set_status
        self.model: GDSLayoutModel | None = None
        self.gds_path: Path | None = None
        self.mapper: AffineCoordinateMapper | None = None
        self.pending_gds_point: str | None = None
        self.selected_target_gds: tuple[float, float] | None = None
        self.selected_target_stage_um: tuple[float, float] | None = None
        self.loader_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.loader_poll_job: str | None = None
        self.microscope_poll_job: str | None = None
        self.microscope_photo: tk.PhotoImage | None = None

        self.snap_grid_options = {
            "100 nm": 0.1,
            "1 um": 1.0,
            "5 um": 5.0,
            "10 um": 10.0,
        }
        self.snap_grid_var = tk.StringVar(value="1 um")
        self.cursor_var = tk.StringVar(value="Cursor u, v: -")
        self.selection_var = tk.StringVar(value="Selected target: -")
        self.target_stage_var = tk.StringVar(value="Target stage: -")
        self.move_distance_var = tk.StringVar(value="Move distance: -")
        self.top_cell_var = tk.StringVar(value="-")
        self.load_status_var = tk.StringVar(value="No GDS loaded.")
        self.mapping_status_var = tk.StringVar(value="Mapping: invalid")
        self.mapping_matrix_var = tk.StringVar(value="No affine transform.")
        self.residuals_var = tk.StringVar(value="Residuals: -")
        self.overlay_enabled_var = tk.BooleanVar(value=True)
        self.fov_width_var = fov_width_var or tk.StringVar(value="200")
        self.fov_height_var = fov_height_var or tk.StringVar(value="150")
        self.residual_threshold_var = tk.StringVar(value="5")
        self.current_stage_var = tk.StringVar(value="Current stage: -")
        self.current_gds_var = tk.StringVar(value="Current GDS: -")
        self.motion_status_var = tk.StringVar(value="Idle")
        self.stage_jog_step_um_var = tk.StringVar(value="10")
        self.layout_jog_step_uv_var = tk.StringVar(value="1")
        self.magnifier_enabled_var = tk.BooleanVar(value=False)
        self.magnifier_scale_var = tk.StringVar(value="2")
        self.magnifier_radius_var = tk.StringVar(value="26")
        self.layout_jog_buttons: list[ttk.Button] = []
        self.responsive_labels: list[tuple[ttk.Label, float, int]] = []
        self.coord_vars: dict[str, tk.StringVar] = {axis: tk.StringVar(value="-") for axis in ("X", "Y", "Z", "U", "V")}
        self.coord_inputs: dict[str, tk.Entry] = {}
        self.coord_edit_modes: dict[str, str | None] = {axis: None for axis in ("X", "Y", "Z", "U", "V")}
        self.modified_coord_axes: set[str] = set()
        self.current_coord_edit_mode: str | None = None
        self.coord_click_job: str | None = None
        self.use_focus_z_var = use_focus_z_var or tk.BooleanVar(value=False)
        self.stage_nav_status_var = tk.StringVar(value="Stage XY: -")
        self.microscope_status_var = tk.StringVar(value="Microscope: waiting for camera frame.")
        self.layer_vars: dict[tuple[int, int], tk.BooleanVar] = {}
        self.gds_point_buttons: dict[str, tk.Button] = {}
        self.point_vars: dict[str, dict[str, tk.StringVar]] = {
            name: {
                "u": tk.StringVar(value=""),
                "v": tk.StringVar(value=""),
                "x_um": tk.StringVar(value=""),
                "y_um": tk.StringVar(value=""),
            }
            for name in self.POINT_NAMES
        }

        self.frame = ttk.Frame(parent, style="App.TFrame")
        self.frame.grid(row=0, column=0, sticky="nsew")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self._build_ui()
        self._update_snap_grid()
        if importlib.util.find_spec("gdstk") is None:
            self.load_status_var.set(GDS_MISSING_MESSAGE)
        self._schedule_overlay_poll()
        self._schedule_microscope_preview_poll()

    def _build_ui(self) -> None:
        pane = ttk.PanedWindow(self.frame, orient=tk.HORIZONTAL)
        pane.grid(row=0, column=0, sticky="nsew")

        viewer_panel = ttk.Frame(pane, style="Panel.TFrame", padding=10)
        viewer_panel.columnconfigure(0, weight=0, minsize=180)
        viewer_panel.columnconfigure(1, weight=1)
        viewer_panel.rowconfigure(0, weight=1)

        left_panel = ttk.Frame(viewer_panel, style="Panel.TFrame")
        left_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        left_panel.columnconfigure(0, weight=1)
        left_panel.rowconfigure(2, weight=1)
        self._build_microscope_preview(left_panel)
        self._build_stage_jog_panel(left_panel)
        self._build_stage_navigation_panel(left_panel)

        gds_canvas_panel = ttk.Frame(viewer_panel, style="Panel.TFrame")
        gds_canvas_panel.grid(row=0, column=1, sticky="nsew")
        gds_canvas_panel.columnconfigure(0, weight=1)
        gds_canvas_panel.rowconfigure(0, weight=1)
        self.viewer = GDSCanvasViewer(
            gds_canvas_panel,
            self.colors,
            on_cursor_gds=self._set_cursor_gds,
            on_select_gds=self._handle_gds_click,
            on_shift_double_click_gds=self._handle_shift_double_click_move,
        )
        pane.add(viewer_panel, weight=1)

        controls_outer = ttk.Frame(pane, style="Panel.TFrame")
        controls_outer.columnconfigure(0, weight=1)
        controls_outer.rowconfigure(0, weight=1)
        controls_canvas = tk.Canvas(controls_outer, bg=self.colors["surface"], highlightthickness=0, width=480)
        scrollbar = ttk.Scrollbar(controls_outer, orient=tk.VERTICAL, command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=scrollbar.set)
        controls_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        controls = ttk.Frame(controls_canvas, style="Panel.TFrame", padding=12)
        controls_window = controls_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda event: controls_canvas.configure(scrollregion=controls_canvas.bbox("all")))
        controls_canvas.bind("<Configure>", lambda event: self._on_controls_canvas_configure(controls_canvas, controls_window, event.width))
        pane.add(controls_outer, weight=0)

        row = 0
        row = self._build_gds_file_section(controls, row)
        row = self._build_cursor_section(controls, row)
        row = self._build_calibration_section(controls, row)
        row = self._build_mapping_section(controls, row)
        row = self._build_overlay_section(controls, row)
        self._build_motion_section(controls, row)

    def _section(self, parent: ttk.Frame, title: str, row: int) -> ttk.LabelFrame:
        section = ttk.LabelFrame(parent, text=title, padding=10)
        section.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        section.columnconfigure(0, weight=1)
        return section

    def _responsive_label(self, parent: tk.Widget, *, textvariable: tk.StringVar, style: str, fraction: float = 1.0, min_width: int = 120, padding=6) -> ttk.Label:
        label = ttk.Label(parent, textvariable=textvariable, style=style, padding=padding)
        self.responsive_labels.append((label, fraction, min_width))
        return label

    def _on_controls_canvas_configure(self, canvas: tk.Canvas, window: int, width: int) -> None:
        canvas.itemconfigure(window, width=width)
        content_width = max(width - 34, 160)
        for label, fraction, min_width in self.responsive_labels:
            try:
                label.configure(wraplength=max(int(content_width * fraction), min_width))
            except tk.TclError:
                pass

    def _build_microscope_preview(self, parent: ttk.Frame) -> None:
        preview = ttk.LabelFrame(parent, text="Microscope Live", padding=8)
        preview.grid(row=0, column=0, sticky="ew")
        preview.columnconfigure(0, weight=1)
        self.microscope_label = ttk.Label(
            preview,
            text="No microscope frame",
            anchor="center",
            style="Value.TLabel",
            padding=8,
        )
        self.microscope_label.grid(row=0, column=0, sticky="ew")
        controls = ttk.Frame(preview, style="Panel.TFrame")
        controls.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        controls.columnconfigure(1, weight=1)
        ttk.Label(controls, text="Mag", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ToggleSwitch(
            controls,
            self.magnifier_enabled_var,
            self.colors,
            command=lambda: self._update_microscope_preview(),
        ).grid(row=0, column=1, sticky="w")
        ttk.Label(controls, text="Scale", style="Muted.TLabel").grid(row=0, column=2, sticky="e", padx=(6, 4))
        ttk.Spinbox(
            controls,
            from_=1.1,
            to=6.0,
            increment=0.1,
            textvariable=self.magnifier_scale_var,
            width=4,
            command=self._update_microscope_preview,
        ).grid(row=0, column=3, sticky="e", padx=(0, 6))
        ttk.Label(controls, text="Size", style="Muted.TLabel").grid(row=0, column=4, sticky="e", padx=(0, 4))
        ttk.Spinbox(
            controls,
            from_=8,
            to=48,
            increment=1,
            textvariable=self.magnifier_radius_var,
            width=4,
            command=self._update_microscope_preview,
        ).grid(row=0, column=5, sticky="e")
        ttk.Label(preview, textvariable=self.microscope_status_var, style="Muted.TLabel").grid(row=2, column=0, sticky="ew", pady=(6, 0))

    def _build_stage_jog_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="Stage Jog", padding=8)
        panel.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        panel.columnconfigure((0, 1), weight=1, uniform="stage_jog_groups")
        xy = self._build_jog_group(panel, "XY", "um", self.stage_jog_step_um_var, self._move_stage_jog)
        xy.grid(row=0, column=0, sticky="nsew", padx=(0, 4))
        uv = self._build_jog_group(panel, "UV", "UV", self.layout_jog_step_uv_var, self._move_layout_uv_jog, gated=True)
        uv.grid(row=0, column=1, sticky="nsew", padx=(4, 0))
        self._update_layout_jog_state()

    def _build_jog_group(
        self,
        parent: ttk.Frame,
        title: str,
        step_label: str,
        step_variable: tk.StringVar,
        command: Callable[[float, float], None],
        *,
        gated: bool = False,
    ) -> ttk.LabelFrame:
        group = ttk.LabelFrame(parent, text=title, padding=6)
        group.columnconfigure((0, 1, 2), weight=1, uniform=f"{title}_cols")
        buttons = (
            ("\u2196", -1.0, 1.0, 0, 0, (0, 1), (0, 1)),
            ("\u2191", 0.0, 1.0, 0, 1, (0, 1), (0, 1)),
            ("\u2197", 1.0, 1.0, 0, 2, (0, 1), (0, 0)),
            ("\u2190", -1.0, 0.0, 1, 0, (0, 1), (0, 1)),
            ("\u2192", 1.0, 0.0, 1, 2, (0, 1), (0, 0)),
            ("\u2199", -1.0, -1.0, 2, 0, (0, 0), (0, 1)),
            ("\u2193", 0.0, -1.0, 2, 1, (0, 0), (0, 1)),
            ("\u2198", 1.0, -1.0, 2, 2, (0, 0), (0, 0)),
        )
        created_buttons: list[ttk.Button] = []
        for text, dx, dy, row, column, pady, padx in buttons:
            button = ttk.Button(group, text=text, width=2, command=lambda x=dx, y=dy: command(x, y))
            button.grid(row=row, column=column, sticky="ew", padx=padx, pady=pady)
            created_buttons.append(button)

        center = ttk.Frame(group, style="Panel.TFrame")
        center.grid(row=1, column=1, sticky="ew")
        center.columnconfigure(1, weight=1)
        ttk.Label(center, text=step_label, style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 2))
        ttk.Entry(center, textvariable=step_variable, width=3, justify="center").grid(row=0, column=1, sticky="ew")
        if gated:
            self.layout_jog_buttons.extend(created_buttons)
        return group

    def _build_stage_navigation_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.LabelFrame(parent, text="Stage XY", padding=8)
        panel.grid(row=2, column=0, sticky="new", pady=(10, 0))
        panel.columnconfigure(0, weight=1)

        grid = ttk.Frame(panel, style="Panel.TFrame")
        grid.grid(row=0, column=0, sticky="ew")
        for column in range(3):
            grid.columnconfigure(column, weight=1, uniform="layoutbond_xyz")
        for column, axis in enumerate(("X", "Y", "Z")):
            self._build_coordinate_cell(grid, axis, row=0, column=column)

        uv_grid = ttk.Frame(panel, style="Panel.TFrame")
        uv_grid.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        uv_grid.columnconfigure((0, 1), weight=1, uniform="layoutbond_uv")
        self._build_coordinate_cell(uv_grid, "U", row=0, column=0)
        self._build_coordinate_cell(uv_grid, "V", row=0, column=1)

        actions = ttk.Frame(panel, style="Panel.TFrame")
        actions.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        actions.columnconfigure(2, weight=1)
        ttk.Button(actions, text="Move", style="Accent.TButton", command=self.move_coordinate_target).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        ttk.Button(actions, text="Clear", command=self.clear_coordinate_edits).grid(row=0, column=1, sticky="ew", padx=(0, 5))
        ttk.Button(actions, text="Use Target", command=self.copy_selected_target_to_coordinates).grid(row=0, column=2, sticky="ew", padx=(0, 8))
        ttk.Label(actions, text="FocusZ", style="Panel.TLabel").grid(row=0, column=3, sticky="e", padx=(0, 5))
        ToggleSwitch(actions, self.use_focus_z_var, self.colors, command=self._on_focus_z_toggle).grid(row=0, column=4, sticky="e")

    def _build_coordinate_cell(self, parent: ttk.Frame, axis: str, row: int, column: int) -> None:
        cell = ttk.Frame(parent, style="Panel.TFrame")
        cell.grid(row=row, column=column, sticky="ew", padx=(0 if column == 0 else 4, 0 if axis in {"Z", "V"} else 4))
        cell.columnconfigure(0, weight=1)
        unit = "um" if axis in {"X", "Y", "Z"} else "layout"
        ttk.Label(cell, text=f"{axis} {unit}", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        entry = tk.Entry(
            cell,
            textvariable=self.coord_vars[axis],
            justify="center",
            bg=self.colors.get("input", self.colors["surface_2"]),
            fg=self.colors["accent"],
            insertbackground=self.colors["accent"],
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            highlightthickness=2,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors.get("border_focus", "#38bdf8"),
            relief="flat",
            readonlybackground=self.colors.get("input", self.colors["surface_2"]),
            font=("Cascadia Mono", 11, "bold"),
            width=4,
        )
        entry.configure(state="readonly")
        entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), ipady=6)
        entry.bind("<Button-1>", lambda _event, a=axis: self.schedule_coordinate_edit(a, "Relative"))
        entry.bind("<Double-Button-1>", lambda _event, a=axis: self.begin_coordinate_edit(a, "Absolute"))
        entry.bind("<KeyRelease>", lambda _event: self._update_coordinate_counterparts_from_edits())
        entry.bind("<Return>", lambda _event: self.move_coordinate_target())
        entry.bind("<Escape>", lambda _event: self.clear_coordinate_edits())
        self.coord_inputs[axis] = entry

    def _build_gds_file_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "GDS File", row)
        toolbar = ttk.Frame(section, style="Panel.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(2, weight=1)
        ttk.Button(toolbar, text="Load GDS", style="Accent.TButton", command=self.load_gds_dialog).grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Button(toolbar, text="Fit to View", command=self.viewer.fit_to_view).grid(row=0, column=1, sticky="w")
        self._responsive_label(section, textvariable=self.load_status_var, style="Value.TLabel", padding=8).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        top_row = ttk.Frame(section, style="Panel.TFrame")
        top_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        top_row.columnconfigure(1, weight=1)
        ttk.Label(top_row, text="Top cell", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.top_cell_combo = ttk.Combobox(top_row, textvariable=self.top_cell_var, state="readonly", values=(), width=18)
        self.top_cell_combo.grid(row=0, column=1, sticky="ew")
        self.top_cell_combo.bind("<<ComboboxSelected>>", self._on_top_cell_selected)

        self.layer_frame = ttk.Frame(section, style="Panel.TFrame")
        self.layer_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        return row + 1

    def _build_cursor_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Cursor and Selection", row)
        snap_row = ttk.Frame(section, style="Panel.TFrame")
        snap_row.grid(row=0, column=0, sticky="ew")
        snap_row.columnconfigure(1, weight=1)
        ttk.Label(snap_row, text="Grid snap", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        snap_combo = ttk.Combobox(
            snap_row,
            textvariable=self.snap_grid_var,
            values=tuple(self.snap_grid_options),
            state="readonly",
            width=12,
        )
        snap_combo.grid(row=0, column=1, sticky="ew")
        snap_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_snap_grid())
        readouts = ttk.Frame(section, style="Panel.TFrame")
        readouts.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        readouts.columnconfigure((0, 1), weight=1, uniform="layoutbond_cursor")
        self._responsive_label(readouts, textvariable=self.cursor_var, style="Value.TLabel", fraction=0.5, min_width=120, padding=6).grid(row=0, column=0, sticky="ew", padx=(0, 3))
        self._responsive_label(readouts, textvariable=self.selection_var, style="Value.TLabel", fraction=0.5, min_width=120, padding=6).grid(row=0, column=1, sticky="ew", padx=(3, 0))
        self._responsive_label(readouts, textvariable=self.target_stage_var, style="Value.TLabel", fraction=0.5, min_width=120, padding=6).grid(row=1, column=0, sticky="ew", padx=(0, 3), pady=(5, 0))
        self._responsive_label(readouts, textvariable=self.move_distance_var, style="Value.TLabel", fraction=0.5, min_width=120, padding=6).grid(row=1, column=1, sticky="ew", padx=(3, 0), pady=(5, 0))
        return row + 1

    def _build_calibration_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Calibration Points", row)
        headings = ("Pt", "GDSu", "GDSv", "Set GDS", "x um", "y um", "Set Stage")
        for col, heading in enumerate(headings):
            ttk.Label(section, text=heading, style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 5))
        for col in (1, 2, 4, 5):
            section.columnconfigure(col, weight=1)

        for row_index, name in enumerate(self.POINT_NAMES, start=1):
            ttk.Label(section, text=name, style="Panel.TLabel").grid(row=row_index, column=0, sticky="w", padx=(0, 5), pady=(5, 0))
            for col, key in ((1, "u"), (2, "v")):
                ttk.Entry(section, textvariable=self.point_vars[name][key], width=6).grid(row=row_index, column=col, sticky="ew", padx=(0, 4), pady=(5, 0))
            set_gds_button = tk.Button(
                section,
                text="Set GDS",
                command=lambda point=name: self._arm_gds_point_capture(point),
                bg=self.colors["surface_3"],
                fg=self.colors["text"],
                activebackground="#223144",
                activeforeground=self.colors["text"],
                relief="flat",
                bd=0,
                padx=5,
                pady=5,
                font=("Segoe UI", 9),
                cursor="hand2",
            )
            set_gds_button.grid(row=row_index, column=3, sticky="ew", padx=(0, 4), pady=(5, 0))
            self.gds_point_buttons[name] = set_gds_button
            for col, key in ((4, "x_um"), (5, "y_um")):
                ttk.Entry(section, textvariable=self.point_vars[name][key], width=6).grid(row=row_index, column=col, sticky="ew", padx=(0, 4), pady=(5, 0))
            ttk.Button(section, text="Set Stage", command=lambda point=name: self._set_point_stage_from_current(point)).grid(row=row_index, column=6, sticky="ew", pady=(5, 0))
        return row + 1

    def _build_mapping_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Mapping", row)
        top = ttk.Frame(section, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Button(top, text="Fit / Update Mapping", style="Accent.TButton", command=self.fit_mapping_from_entries).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(top, text="Warn um", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(0, 4))
        ttk.Entry(top, textvariable=self.residual_threshold_var, width=7).grid(row=0, column=2, sticky="e")
        status_row = ttk.Frame(section, style="Panel.TFrame")
        status_row.grid(row=1, column=0, sticky="ew", pady=(6, 0))
        status_row.columnconfigure(0, weight=1)
        status_row.columnconfigure(1, weight=2)
        self._responsive_label(status_row, textvariable=self.mapping_status_var, style="Status.TLabel", fraction=0.32, min_width=110, padding=(6, 4)).grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self._responsive_label(status_row, textvariable=self.mapping_matrix_var, style="Value.TLabel", fraction=0.62, min_width=160, padding=(6, 4)).grid(row=0, column=1, sticky="ew")
        files = ttk.Frame(section, style="Panel.TFrame")
        files.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        files.columnconfigure((0, 1), weight=1, uniform="cal_files")
        ttk.Button(files, text="Save Calibration", command=self.save_calibration_dialog).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(files, text="Load Calibration", command=self.load_calibration_dialog).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        return row + 1

    def _build_overlay_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Current Position Overlay", row)
        toggle_row = ttk.Frame(section, style="Panel.TFrame")
        toggle_row.grid(row=0, column=0, sticky="ew")
        toggle_row.columnconfigure(0, weight=1)
        ttk.Label(toggle_row, text="Enable overlay", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ToggleSwitch(toggle_row, self.overlay_enabled_var, self.colors, command=self._update_stage_overlay).grid(row=0, column=1, sticky="e")
        status_row = ttk.Frame(section, style="Panel.TFrame")
        status_row.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        status_row.columnconfigure((0, 1), weight=1, uniform="overlay_status")
        self._responsive_label(status_row, textvariable=self.current_stage_var, style="Value.TLabel", fraction=0.5, min_width=120, padding=7).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self._responsive_label(status_row, textvariable=self.current_gds_var, style="Value.TLabel", fraction=0.5, min_width=120, padding=7).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        return row + 1

    def _build_motion_section(self, parent: ttk.Frame, row: int) -> None:
        section = self._section(parent, "Motion", row)
        ttk.Button(section, text="Move to Selected Target", style="Accent.TButton", command=self.move_selected_target).grid(row=0, column=0, sticky="ew")
        self._responsive_label(section, textvariable=self.motion_status_var, style="Status.TLabel", padding=8).grid(row=1, column=0, sticky="ew", pady=(8, 0))

    def load_gds_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Load GDS",
            filetypes=(("GDS files", "*.gds *.GDS"), ("All files", "*.*")),
        )
        if path:
            self.start_gds_load(Path(path))

    def start_gds_load(self, path: Path, top_cell_name: str | None = None) -> None:
        self.gds_path = path
        self.load_status_var.set(f"Loading {path.name}...")
        self._set_status(f"Loading GDS: {path}")
        threading.Thread(target=self._gds_loader_worker, args=(path, top_cell_name), daemon=True).start()
        self._poll_loader_queue()

    def _gds_loader_worker(self, path: Path, top_cell_name: str | None) -> None:
        try:
            model = GDSLayoutModel.load(path, top_cell_name=top_cell_name)
            self.loader_queue.put(("loaded", model))
        except Exception as exc:
            self.loader_queue.put(("error", exc))

    def _poll_loader_queue(self) -> None:
        self.loader_poll_job = None
        try:
            kind, payload = self.loader_queue.get_nowait()
        except queue.Empty:
            self.loader_poll_job = self.frame.after(80, self._poll_loader_queue)
            return

        if kind == "loaded":
            self._apply_loaded_model(payload)  # type: ignore[arg-type]
        else:
            message = str(payload)
            self.load_status_var.set(message)
            self._set_status(message)
            self.viewer.draw_message(message)

    def _apply_loaded_model(self, model: GDSLayoutModel) -> None:
        self.model = model
        self.gds_path = model.path
        self.viewer.set_model(model)
        self.top_cell_combo.configure(values=model.top_cell_names)
        self.top_cell_var.set(model.top_cell_name)
        status = f"Loaded {model.path.name}: {len(model.shapes)} shapes, {len(model.layers)} layers."
        if model.warning:
            status = f"{status} {model.warning}"
        self.load_status_var.set(status)
        self._set_status(status)
        self._rebuild_layer_controls()
        self._update_stage_overlay()
        if self.on_layout_changed is not None:
            self.on_layout_changed()

    def _on_top_cell_selected(self, _event: tk.Event) -> None:
        if self.gds_path is None:
            return
        selected = self.top_cell_var.get().strip()
        if selected:
            self.start_gds_load(self.gds_path, selected)

    def _rebuild_layer_controls(self) -> None:
        for child in self.layer_frame.winfo_children():
            child.destroy()
        self.layer_vars.clear()
        if self.model is None:
            return
        ttk.Label(self.layer_frame, text="Layers", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 4))
        grid = ttk.Frame(self.layer_frame, style="Panel.TFrame")
        grid.grid(row=1, column=0, sticky="ew")
        for column in range(LAYER_TOGGLE_COLUMNS):
            grid.columnconfigure(column, weight=1, uniform="layoutbond_layers")
        visible_layers = self.model.layers[:48]
        for index, layer in enumerate(visible_layers):
            variable = tk.BooleanVar(value=False)
            self.layer_vars[layer] = variable
            text = f"L{layer[0]} / D{layer[1]}"
            grid_row, grid_column = layer_grid_position(index)
            ttk.Checkbutton(
                grid,
                text=text,
                variable=variable,
                command=lambda key=layer, var=variable: self._set_layer_visibility(key, var.get()),
            ).grid(row=grid_row, column=grid_column, sticky="w", padx=(0, 10), pady=(0, 3))
        if len(self.model.layers) > len(visible_layers):
            label = ttk.Label(self.layer_frame, text=f"{len(self.model.layers) - len(visible_layers)} more layers are visible by default.", style="Muted.TLabel")
            self.responsive_labels.append((label, 1.0, 160))
            label.grid(row=2, column=0, sticky="w", pady=(4, 0))

    def _set_layer_visibility(self, layer: tuple[int, int], visible: bool) -> None:
        self.viewer.set_layer_visibility(layer, visible)
        if self.on_layout_changed is not None:
            self.on_layout_changed()

    def _update_snap_grid(self) -> None:
        grid_um = self.snap_grid_options.get(self.snap_grid_var.get(), 1.0)
        self.viewer.set_snap_grid_um(grid_um)
        self.motion_status_var.set(f"Cursor grid snap: {self.snap_grid_var.get()}.")

    def _set_cursor_gds(self, point: tuple[float, float] | None) -> None:
        if point is None:
            self.cursor_var.set("Cursor u, v: -")
        else:
            self.cursor_var.set(f"Cursor u, v: {point[0]:.6g}, {point[1]:.6g}")

    def _handle_gds_click(self, u: float, v: float) -> None:
        if self.pending_gds_point:
            point_name = self.pending_gds_point
            self.pending_gds_point = None
            self.viewer.set_pick_mode(False)
            self._reset_gds_point_buttons()
            self.point_vars[point_name]["u"].set(f"{u:.12g}")
            self.point_vars[point_name]["v"].set(f"{v:.12g}")
            self.motion_status_var.set(f"{point_name} GDS coordinate set from double-click pick.")
            self._set_status(f"{point_name} GDS coordinate set.")
            return

        self.selected_target_gds = (u, v)
        self.viewer.set_selected_gds((u, v))
        self._update_target_preview()

    def _handle_shift_double_click_move(self, _u: float, _v: float) -> None:
        self.move_selected_target()

    def _arm_gds_point_capture(self, point_name: str) -> None:
        self.pending_gds_point = point_name
        self.viewer.set_pick_mode(True)
        self._highlight_gds_point_button(point_name)
        self.motion_status_var.set(f"Double-click a snapped GDS location for {point_name}.")
        self._set_status(f"LayoutBond: double-click a snapped GDS location for {point_name}.")

    def _highlight_gds_point_button(self, point_name: str) -> None:
        self._reset_gds_point_buttons()
        button = self.gds_point_buttons.get(point_name)
        if button is not None:
            button.configure(bg="#f59e0b", fg="#111827", activebackground="#fbbf24", activeforeground="#111827")

    def _reset_gds_point_buttons(self) -> None:
        for button in self.gds_point_buttons.values():
            button.configure(
                bg=self.colors["surface_3"],
                fg=self.colors["text"],
                activebackground="#223144",
                activeforeground=self.colors["text"],
            )

    def _set_point_stage_from_current(self, point_name: str) -> None:
        try:
            x_um, y_um, _z_um = self._stage_position_xyz_um()
        except Exception as exc:
            self.motion_status_var.set(f"Current stage position unavailable: {exc}")
            return
        self.point_vars[point_name]["x_um"].set(f"{x_um:.12g}")
        self.point_vars[point_name]["y_um"].set(f"{y_um:.12g}")
        self.motion_status_var.set(f"{point_name} stage coordinate set from current position.")

    def fit_mapping_from_entries(self, *, autosave: bool = True) -> bool:
        try:
            points = self._points_from_entries()
            mapper = AffineCoordinateMapper.fit(points)
        except Exception as exc:
            self.mapper = None
            self.mapping_status_var.set(f"Mapping: invalid ({exc})")
            self.mapping_matrix_var.set("No affine transform.")
            self.residuals_var.set("Residuals: -")
            self.viewer.set_stage_overlay(None, None)
            self._update_layout_jog_state()
            self._set_status(f"LayoutBond mapping invalid: {exc}")
            return False

        self.mapper = mapper
        self._update_layout_jog_state()
        self._update_mapping_display()
        self._update_target_preview()
        self._update_stage_overlay()
        if self.on_layout_changed is not None:
            self.on_layout_changed()
        if autosave:
            try:
                path = self._autosave_calibration_result()
            except Exception as exc:
                self.motion_status_var.set(f"LayoutBond autosave failed: {exc}")
            else:
                self.motion_status_var.set(f"LayoutBond mapping updated and autosaved to {path.name}.")
        return True

    def _points_from_entries(self) -> list[CalibrationPoint]:
        points = []
        for name in self.POINT_NAMES:
            values = self.point_vars[name]
            points.append(
                CalibrationPoint(
                    name=name,
                    u=_entry_float(values["u"].get(), f"{name} GDS u"),
                    v=_entry_float(values["v"].get(), f"{name} GDS v"),
                    x_um=_entry_float(values["x_um"].get(), f"{name} stage x"),
                    y_um=_entry_float(values["y_um"].get(), f"{name} stage y"),
                )
            )
        return points

    def _update_mapping_display(self) -> None:
        if self.mapper is None:
            return
        a0, a1, a2 = self.mapper.matrix[0]
        b0, b1, b2 = self.mapper.matrix[1]
        warning = ""
        threshold = self._residual_threshold_um()
        if self.mapper.rms_error_um > threshold:
            warning = f" Warning: RMS exceeds {threshold:g} um."
        self.mapping_status_var.set(f"Mapping: valid. RMS {self.mapper.rms_error_um:.4g} um.{warning}")
        residual_text = " | ".join(f"{name}: {value:.4g} um" for name, value in sorted(self.mapper.residuals_um.items()))
        self.mapping_matrix_var.set(
            f"x={a0:.5g}+{a1:.5g}u+{a2:.5g}v | "
            f"y={b0:.5g}+{b1:.5g}u+{b2:.5g}v | "
            f"Residuals: {residual_text}"
        )
        self.residuals_var.set(f"Residuals: {residual_text}")
        self._set_status("LayoutBond affine mapping updated.")

    def _residual_threshold_um(self) -> float:
        try:
            value = float(self.residual_threshold_var.get())
        except ValueError:
            return 5.0
        return value if value > 0 else 5.0

    def _update_target_preview(self) -> None:
        if self.selected_target_gds is None:
            self.selection_var.set("Selected target: -")
            self.target_stage_var.set("Target stage: -")
            self.move_distance_var.set("Move distance: -")
            self.selected_target_stage_um = None
            return

        u, v = self.selected_target_gds
        self.selection_var.set(f"Selected target u, v: {u:.6g}, {v:.6g}")
        if self.mapper is None:
            self.target_stage_var.set("Target stage: fit mapping first")
            self.move_distance_var.set("Move distance: -")
            self.selected_target_stage_um = None
            return

        x_um, y_um = self.mapper.gds_to_stage(u, v)
        self.selected_target_stage_um = (x_um, y_um)
        self.target_stage_var.set(f"Target stage x, y: {x_um:.6g} um, {y_um:.6g} um")
        try:
            current_x, current_y, _current_z = self._stage_position_xyz_um()
            distance = math.hypot(x_um - current_x, y_um - current_y)
            self.move_distance_var.set(f"Move distance: {distance:.6g} um")
        except Exception:
            self.move_distance_var.set("Move distance: current position unavailable")

    def move_selected_target(self) -> None:
        if self.selected_target_stage_um is None:
            self.motion_status_var.set("Select a GDS target after fitting a valid mapping.")
            return
        x_um, y_um = self.selected_target_stage_um
        target_z_um: float | None = None
        if self.use_focus_z_var.get():
            if self.get_focus_z_um is None:
                self.motion_status_var.set("FocusZ source is unavailable.")
                return
            target_z_um = self.get_focus_z_um(x_um, y_um)
            if target_z_um is None:
                self.motion_status_var.set("Use FocusZ is enabled, but no FocusMap plane is stored.")
                return
        z_text = "" if target_z_um is None else f", Z {target_z_um:.6g} um"
        self.motion_status_var.set(f"Move requested: X {x_um:.6g} um, Y {y_um:.6g} um{z_text}.")
        if target_z_um is not None and self.move_to_stage_xyz_um is not None:
            self.move_to_stage_xyz_um(x_um, y_um, target_z_um)
        elif target_z_um is None:
            self.move_to_stage_um(x_um, y_um)
        else:
            self.motion_status_var.set("FocusZ move requires XYZ stage callback.")

    def copy_selected_target_to_coordinates(self) -> None:
        if self.selected_target_stage_um is None:
            self.motion_status_var.set("No selected stage target is available.")
            return
        x_um, y_um = self.selected_target_stage_um
        _current_x, _current_y, current_z = self._stage_position_xyz_um()
        self._set_coordinate_values_from_target(x_um, y_um, current_z)
        self.motion_status_var.set("Selected stage target copied to manual fields.")

    def schedule_coordinate_edit(self, axis: str, mode: str) -> str | None:
        entry = self.coord_inputs.get(axis)
        if entry is not None and str(entry.cget("state")) == "normal" and axis in self.modified_coord_axes:
            return None
        if self.coord_click_job is not None:
            try:
                self.frame.after_cancel(self.coord_click_job)
            except tk.TclError:
                pass
        self.coord_click_job = self.frame.after(180, lambda a=axis, m=mode: self.begin_coordinate_edit(a, m))
        return "break"

    def begin_coordinate_edit(self, axis: str, mode: str) -> str:
        if axis in {"U", "V"} and self.mapper is None:
            self.motion_status_var.set("Bind GDS mapping before editing U/V.")
            return "break"
        if axis == "Z" and self.use_focus_z_var.get():
            self.motion_status_var.set("Use FocusZ is enabled; Z follows the mapped plane.")
            self._update_focus_z_preview()
            return "break"
        if self.coord_click_job is not None:
            try:
                self.frame.after_cancel(self.coord_click_job)
            except tk.TclError:
                pass
            self.coord_click_job = None

        starting_new_mode = self.current_coord_edit_mode != mode
        self.current_coord_edit_mode = mode
        if starting_new_mode:
            self.modified_coord_axes.clear()
            for target_axis in ("X", "Y", "Z", "U", "V"):
                self.coord_edit_modes[target_axis] = None
                self._set_coord_input_readonly(target_axis)
            self._refresh_coordinate_display(force=True)

        first_axis_edit = axis not in self.modified_coord_axes
        self.modified_coord_axes.add(axis)
        self.coord_edit_modes[axis] = mode
        entry = self.coord_inputs[axis]
        entry.configure(state="normal", fg=self.colors["warning"] if mode == "Relative" else self.colors["blue"])
        entry.focus_set()
        if mode == "Relative" and first_axis_edit:
            self.coord_vars[axis].set("")
        self.motion_status_var.set(
            "Relative coordinate input. Empty fields default to 0."
            if mode == "Relative"
            else "Absolute coordinate input. Empty fields default to current position."
        )
        entry.after_idle(lambda a=axis: self.coord_inputs[a].icursor("end"))
        return "break"

    def clear_coordinate_edits(self) -> None:
        self.modified_coord_axes.clear()
        self.current_coord_edit_mode = None
        for axis in ("X", "Y", "Z", "U", "V"):
            self.coord_edit_modes[axis] = None
            self._set_coord_input_readonly(axis)
        self._refresh_coordinate_display(force=True)

    def move_coordinate_target(self) -> None:
        try:
            target_x_um, target_y_um, target_z_um = self._coordinate_target_from_edits()
        except ValueError as exc:
            self.motion_status_var.set(str(exc))
            return
        self.motion_status_var.set(f"Coordinate move requested: X {target_x_um:.6g} um, Y {target_y_um:.6g} um, Z {target_z_um if target_z_um is not None else '-'} um.")
        if self.move_to_stage_xyz_um is not None:
            self.move_to_stage_xyz_um(target_x_um, target_y_um, target_z_um)
        else:
            self.move_to_stage_um(target_x_um, target_y_um)
        self.clear_coordinate_edits()

    def _coordinate_target_from_edits(self) -> tuple[float, float, float | None]:
        if not self.modified_coord_axes:
            raise ValueError("No coordinate input has been modified.")
        current_x, current_y, current_z = self._stage_position_xyz_um()
        target_x, target_y, target_z = current_x, current_y, current_z
        if self.modified_coord_axes & {"U", "V"}:
            if self.mapper is None:
                raise ValueError("Bind GDS mapping before moving by U/V.")
            current_u, current_v = self.mapper.stage_to_gds(current_x, current_y)
            target_u = self._coordinate_axis_target("U", current_u)
            target_v = self._coordinate_axis_target("V", current_v)
            target_x, target_y = self.mapper.gds_to_stage(target_u, target_v)
        if self.modified_coord_axes & {"X", "Y"}:
            target_x = self._coordinate_axis_target("X", current_x)
            target_y = self._coordinate_axis_target("Y", current_y)
        if self.use_focus_z_var.get():
            if self.get_focus_z_um is None:
                raise ValueError("FocusZ source is unavailable.")
            focus_z = self.get_focus_z_um(target_x, target_y)
            if focus_z is None:
                raise ValueError("Use FocusZ is enabled, but no FocusMap plane is stored.")
            target_z = focus_z
        elif "Z" in self.modified_coord_axes:
            target_z = self._coordinate_axis_target("Z", current_z)
        else:
            target_z = None
        return target_x, target_y, target_z

    def _coordinate_axis_target(self, axis: str, current_value: float) -> float:
        text = self.coord_vars[axis].get().strip()
        mode = self.coord_edit_modes[axis] or self.current_coord_edit_mode or "Relative"
        if not text:
            value = 0.0 if mode == "Relative" else current_value
        else:
            try:
                value = float(text)
            except ValueError as exc:
                raise ValueError(f"{axis} coordinate must be numeric.") from exc
        if not math.isfinite(value):
            raise ValueError(f"{axis} coordinate must be finite.")
        return current_value + value if mode == "Relative" else value

    def _update_coordinate_counterparts_from_edits(self) -> None:
        try:
            target_x, target_y, target_z = self._coordinate_target_from_edits()
        except ValueError:
            return
        self._set_coordinate_values_from_target(target_x, target_y, target_z)

    def _set_coordinate_values_from_target(self, x_um: float, y_um: float, z_um: float | None) -> None:
        for axis, value in (("X", x_um), ("Y", y_um)):
            if axis not in self.modified_coord_axes:
                self.coord_vars[axis].set(f"{value:.6g}")
        if z_um is not None and "Z" not in self.modified_coord_axes:
            self.coord_vars["Z"].set(f"{z_um:.6g}")
        if self.mapper is not None:
            try:
                u, v = self.mapper.stage_to_gds(x_um, y_um)
                if "U" not in self.modified_coord_axes:
                    self.coord_vars["U"].set(f"{u:.6g}")
                if "V" not in self.modified_coord_axes:
                    self.coord_vars["V"].set(f"{v:.6g}")
            except Exception:
                return

    def _stage_position_xyz_um(self) -> tuple[float, float, float]:
        values = self.get_stage_position_um()
        if len(values) == 2:
            x_um, y_um = values
            return float(x_um), float(y_um), 0.0
        x_um, y_um, z_um = values
        return float(x_um), float(y_um), float(z_um)

    def _set_coord_input_readonly(self, axis: str) -> None:
        entry = self.coord_inputs.get(axis)
        if entry is None:
            return
        is_uv_disabled = axis in {"U", "V"} and self.mapper is None
        is_z_locked = axis == "Z" and self.use_focus_z_var.get()
        entry.configure(
            fg=self.colors["muted"] if is_uv_disabled or is_z_locked else self.colors["accent"],
            state="readonly",
            readonlybackground=self.colors["surface_2"],
        )

    def _refresh_coordinate_display(self, *, force: bool = False) -> None:
        if self.modified_coord_axes and not force:
            return
        try:
            x_um, y_um, z_um = self._stage_position_xyz_um()
        except Exception as exc:
            self.stage_nav_status_var.set(f"Current XYZ: unavailable ({exc})")
            return
        self.coord_vars["X"].set(f"{x_um:.6g}")
        self.coord_vars["Y"].set(f"{y_um:.6g}")
        self.coord_vars["Z"].set(f"{z_um:.6g}")
        if self.use_focus_z_var.get() and self.get_focus_z_um is not None:
            try:
                focus_z = self.get_focus_z_um(x_um, y_um)
            except Exception:
                focus_z = None
            if focus_z is not None:
                self.coord_vars["Z"].set(f"{focus_z:.6g}")
        if self.mapper is None:
            self.coord_vars["U"].set("-")
            self.coord_vars["V"].set("-")
        else:
            try:
                u, v = self.mapper.stage_to_gds(x_um, y_um)
                self.coord_vars["U"].set(f"{u:.6g}")
                self.coord_vars["V"].set(f"{v:.6g}")
            except Exception:
                self.coord_vars["U"].set("-")
                self.coord_vars["V"].set("-")
        for axis in ("X", "Y", "Z", "U", "V"):
            self._set_coord_input_readonly(axis)

    def _on_focus_z_toggle(self) -> None:
        if self.on_focus_z_toggle is not None:
            self.on_focus_z_toggle()
        if self.use_focus_z_var.get():
            self.modified_coord_axes.discard("Z")
            self.coord_edit_modes["Z"] = None
            self._update_focus_z_preview()
        self._set_coord_input_readonly("Z")

    def _update_layout_jog_state(self) -> None:
        state = "normal" if self.mapper is not None else "disabled"
        for button in self.layout_jog_buttons:
            try:
                button.configure(state=state)
            except tk.TclError:
                pass

    def _move_stage_jog(self, direction_x: float, direction_y: float) -> None:
        try:
            step_um = float(self.stage_jog_step_um_var.get())
            if step_um <= 0 or not math.isfinite(step_um):
                raise ValueError
        except ValueError:
            self.motion_status_var.set("Stage jog step must be a positive number.")
            return
        try:
            current_x, current_y, _current_z = self._stage_position_xyz_um()
        except Exception as exc:
            self.motion_status_var.set(f"Current stage position unavailable: {exc}")
            return
        target_x = current_x + direction_x * step_um
        target_y = current_y + direction_y * step_um
        target_z: float | None = None
        if self.use_focus_z_var.get():
            if self.get_focus_z_um is None:
                self.motion_status_var.set("FocusZ source is unavailable.")
                return
            target_z = self.get_focus_z_um(target_x, target_y)
            if target_z is None:
                self.motion_status_var.set("Use FocusZ is enabled, but no FocusMap plane is stored.")
                return
        if self.move_to_stage_xyz_um is not None:
            self.move_to_stage_xyz_um(target_x, target_y, target_z)
        else:
            self.move_to_stage_um(target_x, target_y)
        z_text = "" if target_z is None else f", Z {target_z:.6g} um"
        self.motion_status_var.set(f"Jog requested: X {target_x:.6g} um, Y {target_y:.6g} um{z_text}.")

    def _move_layout_uv_jog(self, direction_u: float, direction_v: float) -> None:
        if self.mapper is None:
            self.motion_status_var.set("Bind GDS mapping before Layout UV jog.")
            self._update_layout_jog_state()
            return
        try:
            step_uv = float(self.layout_jog_step_uv_var.get())
            if step_uv <= 0 or not math.isfinite(step_uv):
                raise ValueError
        except ValueError:
            self.motion_status_var.set("Layout UV jog step must be a positive number.")
            return
        try:
            current_x, current_y, _current_z = self._stage_position_xyz_um()
            current_u, current_v = self.mapper.stage_to_gds(current_x, current_y)
            target_u = current_u + direction_u * step_uv
            target_v = current_v + direction_v * step_uv
            target_x, target_y = self.mapper.gds_to_stage(target_u, target_v)
        except Exception as exc:
            self.motion_status_var.set(f"Layout UV jog unavailable: {exc}")
            return

        target_z: float | None = None
        if self.use_focus_z_var.get():
            if self.get_focus_z_um is None:
                self.motion_status_var.set("FocusZ source is unavailable.")
                return
            target_z = self.get_focus_z_um(target_x, target_y)
            if target_z is None:
                self.motion_status_var.set("Use FocusZ is enabled, but no FocusMap plane is stored.")
                return
        if self.move_to_stage_xyz_um is not None:
            self.move_to_stage_xyz_um(target_x, target_y, target_z)
        else:
            self.move_to_stage_um(target_x, target_y)
        z_text = "" if target_z is None else f", Z {target_z:.6g} um"
        self.motion_status_var.set(
            f"Layout UV jog requested: U {target_u:.6g}, V {target_v:.6g}; "
            f"X {target_x:.6g} um, Y {target_y:.6g} um{z_text}."
        )

    def _update_focus_z_preview(self) -> None:
        if not self.use_focus_z_var.get() or self.get_focus_z_um is None:
            return
        try:
            x_um, y_um, _z_um = self._coordinate_target_from_edits() if self.modified_coord_axes else self._stage_position_xyz_um()
            focus_z = self.get_focus_z_um(x_um, y_um)
        except Exception:
            return
        if focus_z is not None:
            self.coord_vars["Z"].set(f"{focus_z:.6g}")

    def set_motion_status(self, message: str) -> None:
        self.motion_status_var.set(message)

    def _schedule_overlay_poll(self) -> None:
        try:
            self._update_stage_overlay()
            self.frame.after(300, self._schedule_overlay_poll)
        except tk.TclError:
            return

    def _schedule_microscope_preview_poll(self) -> None:
        try:
            self._update_microscope_preview()
            self.microscope_poll_job = self.frame.after(120, self._schedule_microscope_preview_poll)
        except tk.TclError:
            return

    def _update_microscope_preview(self) -> None:
        if self.get_microscope_preview is None:
            self.microscope_status_var.set("Microscope: preview source unavailable.")
            return
        try:
            payload = self.get_microscope_preview()
        except Exception as exc:
            self.microscope_status_var.set(f"Microscope: unavailable ({exc})")
            return
        if not payload:
            self.microscope_status_var.set("Microscope: waiting for camera frame.")
            return
        try:
            magnifier_text = ""
            if self.magnifier_enabled_var.get():
                try:
                    magnification = float(self.magnifier_scale_var.get())
                except ValueError:
                    magnification = 2.0
                    self.magnifier_scale_var.set("2")
                try:
                    radius_fraction = float(self.magnifier_radius_var.get()) / 100.0
                except ValueError:
                    radius_fraction = 0.26
                    self.magnifier_radius_var.set("26")
                payload = apply_center_magnifier_ppm(payload, magnification, radius_fraction)
                magnifier_text = f" Magnifier {magnification:.2g}x, size {radius_fraction * 100:.0f}%."
            self.microscope_photo = tk.PhotoImage(data=payload, format="PPM")
            self.microscope_label.configure(image=self.microscope_photo, text="")
            self.microscope_status_var.set(f"Microscope: live frame.{magnifier_text}")
        except tk.TclError as exc:
            self.microscope_status_var.set(f"Microscope: preview error ({exc})")

    def _update_stage_overlay(self) -> None:
        self._update_target_preview()
        self._refresh_coordinate_display()
        try:
            nav_x_um, nav_y_um, nav_z_um = self._stage_position_xyz_um()
            self.stage_nav_status_var.set(f"Current XYZ: {nav_x_um:.6g}, {nav_y_um:.6g}, {nav_z_um:.6g} um")
        except Exception as exc:
            self.stage_nav_status_var.set(f"Current XYZ: unavailable ({exc})")
        if self.mapper is None or not self.overlay_enabled_var.get():
            self.viewer.set_stage_overlay(None, None)
            return
        try:
            x_um, y_um, _z_um = self._stage_position_xyz_um()
            width_um = float(self.fov_width_var.get())
            height_um = float(self.fov_height_var.get())
            if width_um <= 0 or height_um <= 0:
                raise ValueError("FOV dimensions must be positive.")
            center_gds = self.mapper.stage_to_gds(x_um, y_um)
            corners_stage = [
                (x_um - width_um / 2.0, y_um - height_um / 2.0),
                (x_um + width_um / 2.0, y_um - height_um / 2.0),
                (x_um + width_um / 2.0, y_um + height_um / 2.0),
                (x_um - width_um / 2.0, y_um + height_um / 2.0),
            ]
            corners_gds = [self.mapper.stage_to_gds(x, y) for x, y in corners_stage]
        except Exception as exc:
            self.current_stage_var.set(f"Current stage: unavailable ({exc})")
            self.current_gds_var.set("Current GDS: -")
            self.viewer.set_stage_overlay(None, None)
            return

        self.current_stage_var.set(f"Current stage x, y: {x_um:.6g} um, {y_um:.6g} um")
        self.current_gds_var.set(f"Current mapped GDS u, v: {center_gds[0]:.6g}, {center_gds[1]:.6g}")
        self.viewer.set_stage_overlay(center_gds, corners_gds)

    def save_calibration_dialog(self) -> None:
        path = filedialog.asksaveasfilename(
            title="Save GDS Calibration",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            data = self._calibration_payload()
            Path(path).write_text(json.dumps(data, indent=2), encoding="utf-8")
        except Exception as exc:
            messagebox.showerror("GDS Calibration", f"Save failed: {exc}", parent=self.frame)
            return
        self.motion_status_var.set(f"Calibration saved: {Path(path).name}")

    def _autosave_calibration_result(self) -> Path:
        output_path = Path.cwd() / LAYOUTBOND_AUTOSAVE_FILENAME
        output_path.write_text(json.dumps(self._calibration_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return output_path

    def _calibration_payload(self) -> dict[str, object]:
        points_payload: dict[str, object] = {}
        for name in self.POINT_NAMES:
            values = self.point_vars[name]
            points_payload[name] = {
                "u": _optional_float(values["u"].get()),
                "v": _optional_float(values["v"].get()),
                "x_um": _optional_float(values["x_um"].get()),
                "y_um": _optional_float(values["y_um"].get()),
            }
        return {
            "version": 1,
            "units": "um",
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "gds_file_path": str(self.gds_path) if self.gds_path else None,
            "top_cell_name": self.model.top_cell_name if self.model else self.top_cell_var.get(),
            "calibration_points": points_payload,
            "affine_mapping": self.mapper.to_dict() if self.mapper else None,
            "fov_width_um": _optional_float(self.fov_width_var.get()),
            "fov_height_um": _optional_float(self.fov_height_var.get()),
            "residual_warning_threshold_um": _optional_float(self.residual_threshold_var.get()),
        }

    def load_calibration_dialog(self) -> None:
        path = filedialog.askopenfilename(
            title="Load GDS Calibration",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            self._apply_calibration_payload(data)
        except Exception as exc:
            messagebox.showerror("GDS Calibration", f"Load failed: {exc}", parent=self.frame)
            return
        self.motion_status_var.set(f"Calibration loaded: {Path(path).name}")

    def _apply_calibration_payload(self, data: dict[str, object]) -> None:
        points = dict(data.get("calibration_points", {}))
        for name in self.POINT_NAMES:
            point_data = dict(points.get(name, {}))
            for key in ("u", "v", "x_um", "y_um"):
                value = _optional_float(point_data.get(key))
                self.point_vars[name][key].set("" if value is None else f"{value:.12g}")

        for key, variable in (
            ("fov_width_um", self.fov_width_var),
            ("fov_height_um", self.fov_height_var),
            ("residual_warning_threshold_um", self.residual_threshold_var),
        ):
            value = _optional_float(data.get(key))
            if value is not None:
                variable.set(f"{value:.12g}")

        self.fit_mapping_from_entries(autosave=False)
        gds_file_path = data.get("gds_file_path")
        top_cell_name = data.get("top_cell_name")
        if gds_file_path:
            path = Path(str(gds_file_path))
            self.gds_path = path
            if path.exists():
                self.start_gds_load(path, str(top_cell_name) if top_cell_name else None)
            else:
                messagebox.showwarning("GDS Calibration", f"Saved GDS file was not found. Reload it manually:\n{path}", parent=self.frame)

    def _set_status(self, message: str) -> None:
        if self.set_app_status is not None:
            self.set_app_status(message)


def _optional_float(value: object) -> float | None:
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _entry_float(text: str, label: str) -> float:
    value = _optional_float(text)
    if value is None:
        raise ValueError(f"{label} is required.")
    return value


def _has_duplicate_pairs(pairs: list[tuple[float, float]], *, tolerance: float = 1e-9) -> bool:
    for index, first in enumerate(pairs):
        for second in pairs[index + 1 :]:
            if math.hypot(first[0] - second[0], first[1] - second[1]) <= tolerance:
                return True
    return False


def _shape_points(polygon: object) -> tuple[tuple[float, float], ...]:
    points = getattr(polygon, "points", ())
    return tuple((float(point[0]), float(point[1])) for point in points)


def _points_bbox(points: tuple[tuple[float, float], ...]) -> tuple[float, float, float, float]:
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    return min(xs), min(ys), max(xs), max(ys)


def _hex_to_rgb(color: str) -> tuple[int, int, int]:
    value = color.strip().lstrip("#")
    if len(value) != 6:
        return (96, 165, 250)
    return int(value[0:2], 16), int(value[2:4], 16), int(value[4:6], 16)


def _shapes_bbox(shapes: list[GDSShape]) -> tuple[float, float, float, float] | None:
    if not shapes:
        return None
    min_u = min(shape.bbox[0] for shape in shapes)
    min_v = min(shape.bbox[1] for shape in shapes)
    max_u = max(shape.bbox[2] for shape in shapes)
    max_v = max(shape.bbox[3] for shape in shapes)
    return min_u, min_v, max_u, max_v
