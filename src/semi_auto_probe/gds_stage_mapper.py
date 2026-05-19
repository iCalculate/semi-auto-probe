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
DEFAULT_MAX_GDS_SHAPES = 50000


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
    def load(cls, path: str | Path, top_cell_name: str | None = None, *, max_shapes: int = DEFAULT_MAX_GDS_SHAPES) -> "GDSLayoutModel":
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
            if len(shapes) >= max_shapes:
                warning = f"GDS contains more than {max_shapes} polygons/paths; rendering is limited to the first {max_shapes}."
                break

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
    ) -> None:
        self.colors = colors
        self.on_cursor_gds = on_cursor_gds
        self.on_select_gds = on_select_gds
        self.model: GDSLayoutModel | None = None
        self.transform = CanvasTransform()
        self.layer_visibility: dict[tuple[int, int], bool] = {}
        self.layer_order: list[tuple[int, int]] = []
        self.selected_gds: tuple[float, float] | None = None
        self.stage_center_gds: tuple[float, float] | None = None
        self.fov_polygon_gds: list[tuple[float, float]] | None = None
        self.drag_start: tuple[int, int, float, float] | None = None
        self.dragging = False
        self.configure_job: str | None = None

        self.canvas = tk.Canvas(parent, bg="#05070a", highlightthickness=1, highlightbackground=colors["border"], bd=0, cursor="crosshair")
        self.canvas.grid(row=0, column=0, sticky="nsew")
        self.canvas.bind("<Configure>", self._on_configure)
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Leave>", lambda _event: self.on_cursor_gds(None))
        self.canvas.bind("<MouseWheel>", self._on_mouse_wheel)
        self.canvas.bind("<Button-4>", self._on_mouse_wheel)
        self.canvas.bind("<Button-5>", self._on_mouse_wheel)
        self.canvas.bind("<ButtonPress-1>", self._on_button_press)
        self.canvas.bind("<B1-Motion>", self._on_drag)
        self.canvas.bind("<ButtonRelease-1>", self._on_button_release)
        self.draw_message("Load a GDS file to begin.")

    def set_model(self, model: GDSLayoutModel) -> None:
        self.model = model
        self.selected_gds = None
        self.stage_center_gds = None
        self.fov_polygon_gds = None
        self.layer_order = list(model.layers)
        self.layer_visibility = {layer: True for layer in self.layer_order}
        self.fit_to_view()

    def set_layer_visibility(self, layer: tuple[int, int], visible: bool) -> None:
        self.layer_visibility[layer] = bool(visible)
        self.redraw()

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
        if self.model is None:
            self.draw_message("Load a GDS file to begin.")
            return
        if not self.model.shapes:
            self.draw_message("No polygons or paths were found in the selected top cell.")
            return

        width = max(self.canvas.winfo_width(), 1)
        height = max(self.canvas.winfo_height(), 1)
        for shape in self.model.shapes:
            if not self.layer_visibility.get(shape.layer_key, True):
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

        if self.transform.scale > 0.02:
            for label in self.model.labels[:300]:
                x, y = self.transform.gds_to_canvas(*label.origin)
                if -40 <= x <= width + 40 and -20 <= y <= height + 20:
                    self.canvas.create_text(x, y, text=label.text, fill="#e5e7eb", anchor="center", font=("Segoe UI", 8), tags="gds_labels")

        self._draw_overlay_items()

    def _draw_overlay_items(self) -> None:
        try:
            self.canvas.delete("gds_overlay")
            self.canvas.delete("gds_selection")
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
                self._draw_cross(self.selected_gds, "#fbbf24", "gds_selection", radius=8)
        except tk.TclError:
            return

    def _draw_cross(self, point: tuple[float, float], color: str, tag: str, radius: int) -> None:
        x, y = self.transform.gds_to_canvas(*point)
        self.canvas.create_line(x - radius, y, x + radius, y, fill=color, width=2, tags=tag)
        self.canvas.create_line(x, y - radius, x, y + radius, fill=color, width=2, tags=tag)
        self.canvas.create_oval(x - 3, y - 3, x + 3, y + 3, outline=color, width=1, tags=tag)

    def _shape_visible(self, shape: GDSShape, width: int, height: int) -> bool:
        min_u, min_v, max_u, max_v = shape.bbox
        x1, y1 = self.transform.gds_to_canvas(min_u, max_v)
        x2, y2 = self.transform.gds_to_canvas(max_u, min_v)
        left, right = sorted((x1, x2))
        top, bottom = sorted((y1, y2))
        return right >= -40 and left <= width + 40 and bottom >= -40 and top <= height + 40

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
            self.on_cursor_gds(None)
            return
        self.on_cursor_gds(self.transform.canvas_to_gds(event.x, event.y))

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
            self.redraw()
        return "break"

    def _on_button_release(self, event: tk.Event) -> str:
        if self.model is not None and not self.dragging:
            point = self.transform.canvas_to_gds(event.x, event.y)
            self.selected_gds = point
            self.on_select_gds(point[0], point[1])
            self._draw_overlay_items()
        self.drag_start = None
        self.dragging = False
        return "break"


class GDSStageMapperPanel:
    POINT_NAMES = ("P1", "P2", "P3", "P4")

    def __init__(
        self,
        parent: tk.Widget,
        colors: dict[str, str],
        *,
        get_stage_position_um: Callable[[], tuple[float, float]],
        move_to_stage_um: Callable[[float, float], None],
        set_status: Callable[[str], None] | None = None,
    ) -> None:
        self.colors = colors
        self.get_stage_position_um = get_stage_position_um
        self.move_to_stage_um = move_to_stage_um
        self.set_app_status = set_status
        self.model: GDSLayoutModel | None = None
        self.gds_path: Path | None = None
        self.mapper: AffineCoordinateMapper | None = None
        self.pending_gds_point: str | None = None
        self.selected_target_gds: tuple[float, float] | None = None
        self.selected_target_stage_um: tuple[float, float] | None = None
        self.loader_queue: queue.Queue[tuple[str, object]] = queue.Queue()
        self.loader_poll_job: str | None = None

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
        self.fov_width_var = tk.StringVar(value="200")
        self.fov_height_var = tk.StringVar(value="150")
        self.residual_threshold_var = tk.StringVar(value="5")
        self.current_stage_var = tk.StringVar(value="Current stage: -")
        self.current_gds_var = tk.StringVar(value="Current GDS: -")
        self.motion_status_var = tk.StringVar(value="Idle")
        self.layer_vars: dict[tuple[int, int], tk.BooleanVar] = {}
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
        if importlib.util.find_spec("gdstk") is None:
            self.load_status_var.set(GDS_MISSING_MESSAGE)
        self._schedule_overlay_poll()

    def _build_ui(self) -> None:
        pane = ttk.PanedWindow(self.frame, orient=tk.HORIZONTAL)
        pane.grid(row=0, column=0, sticky="nsew")

        viewer_panel = ttk.Frame(pane, style="Panel.TFrame", padding=10)
        viewer_panel.columnconfigure(0, weight=1)
        viewer_panel.rowconfigure(0, weight=1)
        self.viewer = GDSCanvasViewer(
            viewer_panel,
            self.colors,
            on_cursor_gds=self._set_cursor_gds,
            on_select_gds=self._handle_gds_click,
        )
        pane.add(viewer_panel, weight=1)

        controls_outer = ttk.Frame(pane, style="Panel.TFrame")
        controls_outer.columnconfigure(0, weight=1)
        controls_outer.rowconfigure(0, weight=1)
        controls_canvas = tk.Canvas(controls_outer, bg=self.colors["surface"], highlightthickness=0, width=430)
        scrollbar = ttk.Scrollbar(controls_outer, orient=tk.VERTICAL, command=controls_canvas.yview)
        controls_canvas.configure(yscrollcommand=scrollbar.set)
        controls_canvas.grid(row=0, column=0, sticky="nsew")
        scrollbar.grid(row=0, column=1, sticky="ns")
        controls = ttk.Frame(controls_canvas, style="Panel.TFrame", padding=12)
        controls_window = controls_canvas.create_window((0, 0), window=controls, anchor="nw")
        controls.bind("<Configure>", lambda event: controls_canvas.configure(scrollregion=controls_canvas.bbox("all")))
        controls_canvas.bind("<Configure>", lambda event: controls_canvas.itemconfigure(controls_window, width=event.width))
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

    def _build_gds_file_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "GDS File", row)
        toolbar = ttk.Frame(section, style="Panel.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew")
        toolbar.columnconfigure(2, weight=1)
        ttk.Button(toolbar, text="Load GDS", style="Accent.TButton", command=self.load_gds_dialog).grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Button(toolbar, text="Fit to View", command=self.viewer.fit_to_view).grid(row=0, column=1, sticky="w")
        ttk.Label(section, textvariable=self.load_status_var, style="Value.TLabel", wraplength=390, padding=8).grid(row=1, column=0, sticky="ew", pady=(8, 0))

        top_row = ttk.Frame(section, style="Panel.TFrame")
        top_row.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        top_row.columnconfigure(1, weight=1)
        ttk.Label(top_row, text="Top cell", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.top_cell_combo = ttk.Combobox(top_row, textvariable=self.top_cell_var, state="readonly", values=(), width=24)
        self.top_cell_combo.grid(row=0, column=1, sticky="ew")
        self.top_cell_combo.bind("<<ComboboxSelected>>", self._on_top_cell_selected)

        self.layer_frame = ttk.Frame(section, style="Panel.TFrame")
        self.layer_frame.grid(row=3, column=0, sticky="ew", pady=(10, 0))
        return row + 1

    def _build_cursor_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Cursor and Selection", row)
        for index, variable in enumerate((self.cursor_var, self.selection_var, self.target_stage_var, self.move_distance_var)):
            ttk.Label(section, textvariable=variable, style="Value.TLabel", wraplength=390, padding=7).grid(row=index, column=0, sticky="ew", pady=(0 if index == 0 else 5, 0))
        return row + 1

    def _build_calibration_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Calibration Points", row)
        headings = ("Pt", "GDS u", "GDS v", "Stage x um", "Stage y um")
        for col, heading in enumerate(headings):
            ttk.Label(section, text=heading, style="Muted.TLabel").grid(row=0, column=col, sticky="w", padx=(0, 5))
        for col in range(1, 5):
            section.columnconfigure(col, weight=1)

        for row_index, name in enumerate(self.POINT_NAMES, start=1):
            ttk.Label(section, text=name, style="Panel.TLabel").grid(row=row_index, column=0, sticky="w", padx=(0, 5), pady=(5, 0))
            for col, key in enumerate(("u", "v", "x_um", "y_um"), start=1):
                ttk.Entry(section, textvariable=self.point_vars[name][key], width=9).grid(row=row_index, column=col, sticky="ew", padx=(0, 5), pady=(5, 0))
            button_row = ttk.Frame(section, style="Panel.TFrame")
            button_row.grid(row=row_index, column=5, sticky="ew", pady=(5, 0))
            ttk.Button(button_row, text="Set GDS", command=lambda point=name: self._arm_gds_point_capture(point)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
            ttk.Button(button_row, text="Set Stage", command=lambda point=name: self._set_point_stage_from_current(point)).grid(row=0, column=1, sticky="ew")
        return row + 1

    def _build_mapping_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Mapping", row)
        top = ttk.Frame(section, style="Panel.TFrame")
        top.grid(row=0, column=0, sticky="ew")
        top.columnconfigure(0, weight=1)
        ttk.Button(top, text="Fit / Update Mapping", style="Accent.TButton", command=self.fit_mapping_from_entries).grid(row=0, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(top, text="Warn um", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(0, 4))
        ttk.Entry(top, textvariable=self.residual_threshold_var, width=7).grid(row=0, column=2, sticky="e")
        ttk.Label(section, textvariable=self.mapping_status_var, style="Status.TLabel", wraplength=390, padding=8).grid(row=1, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(section, textvariable=self.mapping_matrix_var, style="Value.TLabel", wraplength=390, padding=8).grid(row=2, column=0, sticky="ew", pady=(6, 0))
        ttk.Label(section, textvariable=self.residuals_var, style="Value.TLabel", wraplength=390, padding=8).grid(row=3, column=0, sticky="ew", pady=(6, 0))
        files = ttk.Frame(section, style="Panel.TFrame")
        files.grid(row=4, column=0, sticky="ew", pady=(8, 0))
        files.columnconfigure((0, 1), weight=1, uniform="cal_files")
        ttk.Button(files, text="Save Calibration", command=self.save_calibration_dialog).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(files, text="Load Calibration", command=self.load_calibration_dialog).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        return row + 1

    def _build_overlay_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Current Position Overlay", row)
        ttk.Checkbutton(section, text="Enable overlay", variable=self.overlay_enabled_var, command=self._update_stage_overlay).grid(row=0, column=0, sticky="w")
        fov = ttk.Frame(section, style="Panel.TFrame")
        fov.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        fov.columnconfigure((1, 3), weight=1, uniform="fov")
        ttk.Label(fov, text="FOV W um", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 4))
        ttk.Entry(fov, textvariable=self.fov_width_var, width=9).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(fov, text="FOV H um", style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 4))
        ttk.Entry(fov, textvariable=self.fov_height_var, width=9).grid(row=0, column=3, sticky="ew")
        ttk.Label(section, textvariable=self.current_stage_var, style="Value.TLabel", wraplength=390, padding=7).grid(row=2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(section, textvariable=self.current_gds_var, style="Value.TLabel", wraplength=390, padding=7).grid(row=3, column=0, sticky="ew", pady=(5, 0))
        return row + 1

    def _build_motion_section(self, parent: ttk.Frame, row: int) -> None:
        section = self._section(parent, "Motion", row)
        ttk.Button(section, text="Move to Selected Target", style="Accent.TButton", command=self.move_selected_target).grid(row=0, column=0, sticky="ew")
        ttk.Label(section, textvariable=self.motion_status_var, style="Status.TLabel", wraplength=390, padding=8).grid(row=1, column=0, sticky="ew", pady=(8, 0))

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
        visible_layers = self.model.layers[:48]
        for index, layer in enumerate(visible_layers, start=1):
            variable = tk.BooleanVar(value=True)
            self.layer_vars[layer] = variable
            text = f"L{layer[0]} / D{layer[1]}"
            ttk.Checkbutton(
                self.layer_frame,
                text=text,
                variable=variable,
                command=lambda key=layer, var=variable: self.viewer.set_layer_visibility(key, var.get()),
            ).grid(row=index, column=0, sticky="w")
        if len(self.model.layers) > len(visible_layers):
            ttk.Label(self.layer_frame, text=f"{len(self.model.layers) - len(visible_layers)} more layers are visible by default.", style="Muted.TLabel", wraplength=360).grid(row=len(visible_layers) + 1, column=0, sticky="w", pady=(4, 0))

    def _set_cursor_gds(self, point: tuple[float, float] | None) -> None:
        if point is None:
            self.cursor_var.set("Cursor u, v: -")
        else:
            self.cursor_var.set(f"Cursor u, v: {point[0]:.6g}, {point[1]:.6g}")

    def _handle_gds_click(self, u: float, v: float) -> None:
        if self.pending_gds_point:
            point_name = self.pending_gds_point
            self.pending_gds_point = None
            self.point_vars[point_name]["u"].set(f"{u:.12g}")
            self.point_vars[point_name]["v"].set(f"{v:.12g}")
            self.motion_status_var.set(f"{point_name} GDS coordinate set from click.")
            self._set_status(f"{point_name} GDS coordinate set.")
            return

        self.selected_target_gds = (u, v)
        self.viewer.set_selected_gds((u, v))
        self._update_target_preview()

    def _arm_gds_point_capture(self, point_name: str) -> None:
        self.pending_gds_point = point_name
        self.motion_status_var.set(f"Click a GDS location for {point_name}.")
        self._set_status(f"GDS Stage Mapper: click a GDS location for {point_name}.")

    def _set_point_stage_from_current(self, point_name: str) -> None:
        try:
            x_um, y_um = self.get_stage_position_um()
        except Exception as exc:
            self.motion_status_var.set(f"Current stage position unavailable: {exc}")
            return
        self.point_vars[point_name]["x_um"].set(f"{x_um:.12g}")
        self.point_vars[point_name]["y_um"].set(f"{y_um:.12g}")
        self.motion_status_var.set(f"{point_name} stage coordinate set from current position.")

    def fit_mapping_from_entries(self) -> bool:
        try:
            points = self._points_from_entries()
            mapper = AffineCoordinateMapper.fit(points)
        except Exception as exc:
            self.mapper = None
            self.mapping_status_var.set(f"Mapping: invalid ({exc})")
            self.mapping_matrix_var.set("No affine transform.")
            self.residuals_var.set("Residuals: -")
            self.viewer.set_stage_overlay(None, None)
            self._set_status(f"GDS mapping invalid: {exc}")
            return False

        self.mapper = mapper
        self._update_mapping_display()
        self._update_target_preview()
        self._update_stage_overlay()
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
        self.mapping_matrix_var.set(
            f"x = {a0:.6g} + {a1:.6g}*u + {a2:.6g}*v\n"
            f"y = {b0:.6g} + {b1:.6g}*u + {b2:.6g}*v"
        )
        residual_text = " | ".join(f"{name}: {value:.4g} um" for name, value in sorted(self.mapper.residuals_um.items()))
        self.residuals_var.set(f"Residuals: {residual_text}")
        self._set_status("GDS affine mapping updated.")

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
            current_x, current_y = self.get_stage_position_um()
            distance = math.hypot(x_um - current_x, y_um - current_y)
            self.move_distance_var.set(f"Move distance: {distance:.6g} um")
        except Exception:
            self.move_distance_var.set("Move distance: current position unavailable")

    def move_selected_target(self) -> None:
        if self.selected_target_stage_um is None:
            self.motion_status_var.set("Select a GDS target after fitting a valid mapping.")
            return
        x_um, y_um = self.selected_target_stage_um
        self.motion_status_var.set(f"Move requested: X {x_um:.6g} um, Y {y_um:.6g} um.")
        self.move_to_stage_um(x_um, y_um)

    def set_motion_status(self, message: str) -> None:
        self.motion_status_var.set(message)

    def _schedule_overlay_poll(self) -> None:
        try:
            self._update_stage_overlay()
            self.frame.after(300, self._schedule_overlay_poll)
        except tk.TclError:
            return

    def _update_stage_overlay(self) -> None:
        self._update_target_preview()
        if self.mapper is None or not self.overlay_enabled_var.get():
            self.viewer.set_stage_overlay(None, None)
            return
        try:
            x_um, y_um = self.get_stage_position_um()
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

        self.fit_mapping_from_entries()
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


def _shapes_bbox(shapes: list[GDSShape]) -> tuple[float, float, float, float] | None:
    if not shapes:
        return None
    min_u = min(shape.bbox[0] for shape in shapes)
    min_v = min(shape.bbox[1] for shape in shapes)
    max_u = max(shape.bbox[2] for shape in shapes)
    max_v = max(shape.bbox[3] for shape in shapes)
    return min_u, min_v, max_u, max_v
