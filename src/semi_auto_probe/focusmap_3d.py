from __future__ import annotations

import math
import tkinter as tk
from typing import Protocol

import numpy as np

from .af_plane import SamplePlaneModel


class FocusMap3DView(Protocol):
    widget: tk.Widget

    def render(self, records: list[dict[str, object]], model: SamplePlaneModel | None) -> None:
        ...


def create_focusmap_3d_view(parent: tk.Widget, colors: dict[str, str]) -> FocusMap3DView:
    try:
        return MatplotlibFocusMap3DView(parent, colors)
    except Exception:
        return CanvasFocusMap3DView(parent, colors)


class MatplotlibFocusMap3DView:
    def __init__(self, parent: tk.Widget, colors: dict[str, str]) -> None:
        from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
        from matplotlib.figure import Figure

        self.colors = colors
        self.widget = tk.Frame(parent, bg="#05070a", highlightthickness=1, highlightbackground="#334155")
        self.widget.columnconfigure(0, weight=1)
        self.widget.rowconfigure(0, weight=1)
        self.figure = Figure(figsize=(4.2, 3.0), dpi=100, facecolor="#05070a")
        self.axes = self.figure.add_subplot(111, projection="3d")
        self.canvas = FigureCanvasTkAgg(self.figure, master=self.widget)
        self.canvas_widget = self.canvas.get_tk_widget()
        self.canvas_widget.grid(row=0, column=0, sticky="nsew")
        self.records: list[dict[str, object]] = []
        self.model: SamplePlaneModel | None = None
        self.view_elev = 28.0
        self.view_azim = -45.0
        self.zoom = 1.0
        self.drag_start: tuple[float, float, float, float] | None = None
        self.base_limits: tuple[float, float, float, float, float, float] | None = None
        self.canvas.mpl_connect("button_press_event", self._on_press)
        self.canvas.mpl_connect("button_release_event", self._on_release)
        self.canvas.mpl_connect("motion_notify_event", self._on_motion)
        self.canvas.mpl_connect("scroll_event", self._on_scroll)
        try:
            self.axes.disable_mouse_rotation()
        except AttributeError:
            pass
        self._style_axes()

    def render(self, records: list[dict[str, object]], model: SamplePlaneModel | None) -> None:
        self.records = records
        self.model = model
        self.axes.clear()
        self._style_axes()
        measured = [record for record in records if record.get("measured_z") is not None]
        if not measured:
            self.axes.text2D(0.5, 0.5, "Waiting for measured points", transform=self.axes.transAxes, ha="center", color="#8fa0b3")
            self._apply_view()
            self.canvas.draw_idle()
            return

        xs = np.array([float(record["x"]) for record in measured], dtype=float)
        ys = np.array([float(record["y"]) for record in measured], dtype=float)
        zs = np.array([float(record["measured_z"]) for record in measured], dtype=float)
        residuals = np.array([float(record.get("residual", 0.0) or 0.0) for record in measured], dtype=float)
        max_abs = max(float(np.max(np.abs(residuals))) if residuals.size else 0.0, 1.0)
        point_colors = [_residual_color(float(residual), max_abs) for residual in residuals]

        if model is not None:
            min_x, max_x = float(np.min(xs)), float(np.max(xs))
            min_y, max_y = float(np.min(ys)), float(np.max(ys))
            if abs(max_x - min_x) < 1e-9:
                min_x -= 1.0
                max_x += 1.0
            if abs(max_y - min_y) < 1e-9:
                min_y -= 1.0
                max_y += 1.0
            grid_x, grid_y = np.meshgrid(np.linspace(min_x, max_x, 13), np.linspace(min_y, max_y, 13))
            grid_z = model.a * grid_x + model.b * grid_y + model.c
            self.axes.plot_surface(grid_x, grid_y, grid_z, color="#0ea5e9", alpha=0.22, linewidth=0, shade=True, antialiased=False)
            self.axes.plot_wireframe(grid_x, grid_y, grid_z, color="#7dd3fc", alpha=0.5, linewidth=0.35, rstride=2, cstride=2)
            for x_value, y_value, z_value in zip(xs, ys, zs):
                fit_z = model.z_at(float(x_value), float(y_value))
                self.axes.plot([x_value, x_value], [y_value, y_value], [fit_z, z_value], color="#e5edf5", alpha=0.55, linewidth=0.8)

        self.axes.scatter(xs, ys, zs, c=point_colors, s=36, depthshade=True, edgecolors="#e5edf5", linewidths=0.5)
        for record, x_value, y_value, z_value in zip(measured, xs, ys, zs):
            if len(measured) <= 25:
                self.axes.text(x_value, y_value, z_value, str(record["index"]), color="#e5edf5", fontsize=7)

        self.axes.set_xlabel("X", color="#cbd5e1", labelpad=2)
        self.axes.set_ylabel("Y", color="#cbd5e1", labelpad=2)
        self.axes.set_zlabel("Z", color="#cbd5e1", labelpad=2)
        self._update_limits(xs, ys, zs, model)
        self._apply_view()
        self.figure.subplots_adjust(left=0.0, right=0.98, bottom=0.0, top=0.98)
        self.canvas.draw_idle()

    def _style_axes(self) -> None:
        self.axes.set_facecolor("#05070a")
        self.axes.tick_params(colors="#94a3b8", labelsize=7, pad=0)
        for axis in (self.axes.xaxis, self.axes.yaxis, self.axes.zaxis):
            axis.pane.set_facecolor((0.02, 0.04, 0.07, 0.92))
            axis.pane.set_edgecolor("#334155")
        self.axes.grid(True, color="#334155")

    def _update_limits(self, xs: np.ndarray, ys: np.ndarray, zs: np.ndarray, model: SamplePlaneModel | None) -> None:
        z_values = list(zs)
        if model is not None and xs.size and ys.size:
            for x_value in (float(np.min(xs)), float(np.max(xs))):
                for y_value in (float(np.min(ys)), float(np.max(ys))):
                    z_values.append(model.z_at(x_value, y_value))
        min_x, max_x = float(np.min(xs)), float(np.max(xs))
        min_y, max_y = float(np.min(ys)), float(np.max(ys))
        min_z, max_z = float(min(z_values)), float(max(z_values))
        span_x = max(max_x - min_x, 1.0)
        span_y = max(max_y - min_y, 1.0)
        span_z = max(max_z - min_z, 1.0)
        pad_x = span_x * 0.12
        pad_y = span_y * 0.12
        pad_z = span_z * 0.22
        self.base_limits = (
            (min_x + max_x) / 2.0,
            span_x + pad_x * 2.0,
            (min_y + max_y) / 2.0,
            span_y + pad_y * 2.0,
            (min_z + max_z) / 2.0,
            span_z + pad_z * 2.0,
        )
        xy_span = max(span_x, span_y, 1.0)
        z_aspect = max(0.28, min(0.72, span_z / xy_span * 4.0))
        try:
            self.axes.set_box_aspect((max(span_x / xy_span, 0.45), max(span_y / xy_span, 0.45), z_aspect))
        except AttributeError:
            pass
        self._apply_limits()

    def _apply_limits(self) -> None:
        if self.base_limits is None:
            return
        center_x, span_x, center_y, span_y, center_z, span_z = self.base_limits
        scale = max(0.25, min(4.0, self.zoom))
        self.axes.set_xlim(center_x - span_x * scale / 2.0, center_x + span_x * scale / 2.0)
        self.axes.set_ylim(center_y - span_y * scale / 2.0, center_y + span_y * scale / 2.0)
        self.axes.set_zlim(center_z - span_z * scale / 2.0, center_z + span_z * scale / 2.0)

    def _apply_view(self) -> None:
        try:
            self.axes.view_init(elev=self.view_elev, azim=self.view_azim, roll=0.0)
        except TypeError:
            self.axes.view_init(elev=self.view_elev, azim=self.view_azim)

    def _on_press(self, event: object) -> None:
        if getattr(event, "dblclick", False):
            self.view_elev = 28.0
            self.view_azim = -45.0
            self.zoom = 1.0
            self._apply_view()
            self._apply_limits()
            self.canvas.draw_idle()
            return
        if getattr(event, "button", None) != 1 or getattr(event, "x", None) is None or getattr(event, "y", None) is None:
            return
        self.drag_start = (float(event.x), float(event.y), self.view_elev, self.view_azim)

    def _on_release(self, _event: object) -> None:
        self.drag_start = None

    def _on_motion(self, event: object) -> None:
        if self.drag_start is None or getattr(event, "x", None) is None or getattr(event, "y", None) is None:
            return
        start_x, start_y, start_elev, start_azim = self.drag_start
        self.view_azim = start_azim + (float(event.x) - start_x) * 0.35
        self.view_elev = max(-88.0, min(88.0, start_elev - (float(event.y) - start_y) * 0.28))
        self._apply_view()
        self.canvas.draw_idle()

    def _on_scroll(self, event: object) -> None:
        button = getattr(event, "button", "")
        step = getattr(event, "step", 0)
        if button == "up" or step > 0:
            self.zoom = max(0.25, self.zoom * 0.86)
        else:
            self.zoom = min(4.0, self.zoom / 0.86)
        self._apply_limits()
        self.canvas.draw_idle()


class CanvasFocusMap3DView:
    def __init__(self, parent: tk.Widget, colors: dict[str, str]) -> None:
        self.colors = colors
        self.widget = tk.Canvas(parent, bg="#05070a", highlightthickness=1, highlightbackground="#334155")
        self.yaw = -35.0
        self.pitch = 28.0
        self.zoom = 1.0
        self.drag_start: tuple[int, int, float, float] | None = None
        self.records: list[dict[str, object]] = []
        self.model: SamplePlaneModel | None = None
        self.widget.bind("<Configure>", lambda _event: self.render(self.records, self.model))
        self.widget.bind("<ButtonPress-1>", self._on_press)
        self.widget.bind("<B1-Motion>", self._on_drag)
        self.widget.bind("<MouseWheel>", self._on_wheel)
        self.widget.bind("<Button-4>", self._on_wheel)
        self.widget.bind("<Button-5>", self._on_wheel)

    def render(self, records: list[dict[str, object]], model: SamplePlaneModel | None) -> None:
        self.records = records
        self.model = model
        canvas = self.widget
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#05070a", outline="")
        measured = [record for record in records if record.get("measured_z") is not None]
        if not measured:
            canvas.create_text(width // 2, height // 2, text="3D view waits for measured points", fill=self.colors["muted"], font=("Segoe UI Semibold", 12))
            return

        xs = [float(record["x"]) for record in measured]
        ys = [float(record["y"]) for record in measured]
        zs = [float(record["measured_z"]) for record in measured]
        if model is not None:
            for x_value in (min(xs), max(xs)):
                for y_value in (min(ys), max(ys)):
                    zs.append(model.z_at(x_value, y_value))
        center_x = (min(xs) + max(xs)) / 2.0
        center_y = (min(ys) + max(ys)) / 2.0
        center_z = (min(zs) + max(zs)) / 2.0
        span_xy = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
        span_z = max(max(zs) - min(zs), 1.0)
        scale = min(width, height) * 0.34 * self.zoom

        def project(x_value: float, y_value: float, z_value: float) -> tuple[float, float, float]:
            x = (x_value - center_x) / span_xy
            y = (y_value - center_y) / span_xy
            z = (z_value - center_z) / span_z
            yaw = math.radians(self.yaw)
            pitch = math.radians(self.pitch)
            x1 = x * math.cos(yaw) - y * math.sin(yaw)
            y1 = x * math.sin(yaw) + y * math.cos(yaw)
            y2 = y1 * math.cos(pitch) - z * math.sin(pitch)
            z2 = y1 * math.sin(pitch) + z * math.cos(pitch)
            perspective = 1.0 / max(0.55, 1.0 + y2 * 0.35)
            return width / 2.0 + x1 * scale * perspective, height / 2.0 - z2 * scale * perspective, y2

        canvas.create_text(12, 10, text="Canvas fallback | Drag rotate | Wheel zoom", anchor="nw", fill="#cbd5e1", font=("Segoe UI", 9))
        if model is not None:
            grid_count = 7
            plane_points: list[list[tuple[float, float, float]]] = []
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            for row in range(grid_count):
                y_value = min_y + (max_y - min_y) * row / max(1, grid_count - 1)
                plane_row = []
                for col in range(grid_count):
                    x_value = min_x + (max_x - min_x) * col / max(1, grid_count - 1)
                    plane_row.append(project(x_value, y_value, model.z_at(x_value, y_value)))
                plane_points.append(plane_row)
            cells = []
            for row in range(grid_count - 1):
                for col in range(grid_count - 1):
                    corners = (plane_points[row][col], plane_points[row][col + 1], plane_points[row + 1][col + 1], plane_points[row + 1][col])
                    cells.append((sum(point[2] for point in corners) / 4.0, [coord for point in corners for coord in point[:2]]))
            for _depth, coords in sorted(cells, key=lambda item: item[0]):
                canvas.create_polygon(*coords, fill="#082f49", outline="#0ea5e9", stipple="gray25")

        residuals = [float(record.get("residual", 0.0) or 0.0) for record in measured]
        max_abs_residual = max([abs(value) for value in residuals] + [1.0])
        projected_records = []
        for record in measured:
            sx, sy, depth = project(float(record["x"]), float(record["y"]), float(record["measured_z"]))
            residual = float(record.get("residual", 0.0) or 0.0)
            projected_records.append((depth, sx, sy, residual, record))
            if model is not None:
                fx, fy, _fit_depth = project(float(record["x"]), float(record["y"]), model.z_at(float(record["x"]), float(record["y"])))
                canvas.create_line(fx, fy, sx, sy, fill="#f8fafc", dash=(2, 3))
        for _depth, sx, sy, residual, record in sorted(projected_records, key=lambda item: item[0]):
            color = _residual_color(residual, max_abs_residual)
            canvas.create_oval(sx - 5, sy - 5, sx + 5, sy + 5, fill=color, outline="#e5edf5")
            if len(projected_records) <= 25:
                canvas.create_text(sx + 7, sy - 7, text=str(record["index"]), anchor="w", fill="#e5edf5", font=("Segoe UI", 8))

    def _on_press(self, event: tk.Event) -> str:
        self.drag_start = (event.x, event.y, self.yaw, self.pitch)
        return "break"

    def _on_drag(self, event: tk.Event) -> str:
        if self.drag_start is None:
            return "break"
        start_x, start_y, start_yaw, start_pitch = self.drag_start
        self.yaw = start_yaw + (event.x - start_x) * 0.45
        self.pitch = max(-80.0, min(80.0, start_pitch - (event.y - start_y) * 0.45))
        self.render(self.records, self.model)
        return "break"

    def _on_wheel(self, event: tk.Event) -> str:
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            self.zoom = min(4.0, self.zoom * 1.12)
        else:
            self.zoom = max(0.35, self.zoom / 1.12)
        self.render(self.records, self.model)
        return "break"


def _residual_color(residual: float, max_abs_residual: float) -> str:
    ratio = min(1.0, abs(residual) / max(max_abs_residual, 1e-9))
    if ratio < 0.35:
        return "#22c55e"
    if ratio < 0.7:
        return "#fbbf24"
    return "#fb7185"
