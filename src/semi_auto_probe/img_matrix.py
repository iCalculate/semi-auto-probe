from __future__ import annotations

import math
import re
import tkinter as tk
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk
from typing import Callable

from .gds_stage_mapper import AffineCoordinateMapper, GDSCanvasViewer, GDSLayoutModel


@dataclass(frozen=True)
class ImgMatrixSettings:
    origin_u: float
    origin_v: float
    u_vector_u: float
    u_vector_v: float
    v_vector_u: float
    v_vector_v: float
    rows: int
    cols: int
    fov_width_um: float
    fov_height_um: float

    def normalized(self) -> "ImgMatrixSettings":
        values = (
            self.origin_u,
            self.origin_v,
            self.u_vector_u,
            self.u_vector_v,
            self.v_vector_u,
            self.v_vector_v,
            self.fov_width_um,
            self.fov_height_um,
        )
        if any(not math.isfinite(float(value)) for value in values):
            raise ValueError("ImgMatrix coordinates and FOV dimensions must be finite.")
        rows = int(self.rows)
        cols = int(self.cols)
        if rows <= 0 or cols <= 0:
            raise ValueError("ImgMatrix rows and columns must be positive.")
        if rows > 500 or cols > 500:
            raise ValueError("ImgMatrix rows and columns are limited to 500.")
        if self.fov_width_um <= 0 or self.fov_height_um <= 0:
            raise ValueError("ImgMatrix FOV dimensions must be positive.")
        if math.hypot(self.u_vector_u, self.u_vector_v) <= 0:
            raise ValueError("ImgMatrix U vector must be non-zero.")
        if math.hypot(self.v_vector_u, self.v_vector_v) <= 0:
            raise ValueError("ImgMatrix V vector must be non-zero.")
        return ImgMatrixSettings(
            origin_u=float(self.origin_u),
            origin_v=float(self.origin_v),
            u_vector_u=float(self.u_vector_u),
            u_vector_v=float(self.u_vector_v),
            v_vector_u=float(self.v_vector_u),
            v_vector_v=float(self.v_vector_v),
            rows=rows,
            cols=cols,
            fov_width_um=float(self.fov_width_um),
            fov_height_um=float(self.fov_height_um),
        )


@dataclass(frozen=True)
class ImgMatrixPoint:
    row: int
    col: int
    order: int
    u: float
    v: float
    stage_x_um: float
    stage_y_um: float
    fov_polygon_gds: tuple[tuple[float, float], ...]

    @property
    def filename(self) -> str:
        return imgmatrix_filename(self.row, self.col, self.u, self.v)


def generate_imgmatrix_points(settings: ImgMatrixSettings, mapper: AffineCoordinateMapper) -> tuple[ImgMatrixPoint, ...]:
    normalized = settings.normalized()
    points: list[ImgMatrixPoint] = []
    order = 1
    for row in range(normalized.rows):
        for col in range(normalized.cols):
            u = normalized.origin_u + col * normalized.u_vector_u + row * normalized.v_vector_u
            v = normalized.origin_v + col * normalized.u_vector_v + row * normalized.v_vector_v
            stage_x_um, stage_y_um = mapper.gds_to_stage(u, v)
            points.append(
                ImgMatrixPoint(
                    row=row,
                    col=col,
                    order=order,
                    u=u,
                    v=v,
                    stage_x_um=stage_x_um,
                    stage_y_um=stage_y_um,
                    fov_polygon_gds=fov_polygon_for_stage_target(
                        mapper,
                        stage_x_um,
                        stage_y_um,
                        normalized.fov_width_um,
                        normalized.fov_height_um,
                    ),
                )
            )
            order += 1
    return tuple(points)


def fov_polygon_for_stage_target(
    mapper: AffineCoordinateMapper,
    center_x_um: float,
    center_y_um: float,
    width_um: float,
    height_um: float,
) -> tuple[tuple[float, float], ...]:
    if width_um <= 0 or height_um <= 0:
        raise ValueError("FOV dimensions must be positive.")
    corners_stage = (
        (center_x_um - width_um / 2.0, center_y_um - height_um / 2.0),
        (center_x_um + width_um / 2.0, center_y_um - height_um / 2.0),
        (center_x_um + width_um / 2.0, center_y_um + height_um / 2.0),
        (center_x_um - width_um / 2.0, center_y_um + height_um / 2.0),
    )
    return tuple(mapper.stage_to_gds(x_um, y_um) for x_um, y_um in corners_stage)


def imgmatrix_filename(row: int, col: int, u: float, v: float) -> str:
    return f"r{row:03d}_c{col:03d}_u{_safe_coord(u)}_v{_safe_coord(v)}.png"


def session_manifest_path(session_dir: Path) -> Path:
    return session_dir / "manifest.json"


def _safe_coord(value: float) -> str:
    text = f"{float(value):.6f}".rstrip("0").rstrip(".")
    if text == "-0":
        text = "0"
    return re.sub(r"[^0-9A-Za-z.-]+", "_", text).replace("-", "m").replace(".", "p")


class ImgMatrixPanel:
    def __init__(
        self,
        parent: tk.Widget,
        colors: dict[str, str],
        *,
        get_stage_position_um: Callable[[], tuple[float, float] | tuple[float, float, float]],
        get_mapper: Callable[[], AffineCoordinateMapper | None],
        get_microscope_preview: Callable[[], bytes | None] | None,
        fov_width_var: tk.StringVar,
        fov_height_var: tk.StringVar,
        start_run: Callable[[ImgMatrixSettings], None],
        stop_run: Callable[[], None],
        set_status: Callable[[str], None] | None = None,
    ) -> None:
        self.colors = colors
        self.get_stage_position_um = get_stage_position_um
        self.get_mapper = get_mapper
        self.get_microscope_preview = get_microscope_preview
        self.fov_width_var = fov_width_var
        self.fov_height_var = fov_height_var
        self.start_run = start_run
        self.stop_run = stop_run
        self.set_app_status = set_status
        self.model: GDSLayoutModel | None = None
        self.pending_pick: str | None = None
        self.selected_gds: tuple[float, float] | None = None
        self.microscope_photo: tk.PhotoImage | None = None
        self.status_poll_job: str | None = None
        self.preview_labels: list[ttk.Label] = []

        self.origin_u_var = tk.StringVar(value="")
        self.origin_v_var = tk.StringVar(value="")
        self.u_vector_u_var = tk.StringVar(value="1000")
        self.u_vector_v_var = tk.StringVar(value="0")
        self.v_vector_u_var = tk.StringVar(value="0")
        self.v_vector_v_var = tk.StringVar(value="1000")
        self.rows_var = tk.StringVar(value="3")
        self.cols_var = tk.StringVar(value="3")
        self.cursor_var = tk.StringVar(value="Cursor u, v: -")
        self.selection_var = tk.StringVar(value="Selected: -")
        self.current_stage_var = tk.StringVar(value="Current stage: -")
        self.current_gds_var = tk.StringVar(value="Current GDS: -")
        self.matrix_summary_var = tk.StringVar(value="Preview: set Origin, U/V vectors, rows and columns.")
        self.status_var = tk.StringVar(value="Idle")
        self.session_var = tk.StringVar(value="Session: -")

        self.frame = ttk.Frame(parent, style="App.TFrame")
        self.frame.grid(row=0, column=0, sticky="nsew")
        self.frame.columnconfigure(0, weight=1)
        self.frame.rowconfigure(0, weight=1)
        self._build_ui()
        self._schedule_status_poll()

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
        self._build_left_panel(left_panel)

        canvas_panel = ttk.Frame(viewer_panel, style="Panel.TFrame")
        canvas_panel.grid(row=0, column=1, sticky="nsew")
        canvas_panel.columnconfigure(0, weight=1)
        canvas_panel.rowconfigure(0, weight=1)
        self.viewer = GDSCanvasViewer(
            canvas_panel,
            self.colors,
            on_cursor_gds=self._set_cursor_gds,
            on_select_gds=self._handle_gds_click,
        )
        pane.add(viewer_panel, weight=1)

        controls = ttk.Frame(pane, style="Panel.TFrame", padding=12)
        controls.columnconfigure(0, weight=1)
        self._build_controls(controls)
        pane.add(controls, weight=0)

    def _build_left_panel(self, parent: ttk.Frame) -> None:
        preview = ttk.LabelFrame(parent, text="Microscope Live", padding=8)
        preview.grid(row=0, column=0, sticky="ew")
        preview.columnconfigure(0, weight=1)
        self.microscope_label = ttk.Label(preview, text="No microscope frame", anchor="center", style="Value.TLabel", padding=8)
        self.microscope_label.grid(row=0, column=0, sticky="ew")

        stage = ttk.LabelFrame(parent, text="Stage XY", padding=8)
        stage.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        stage.columnconfigure(0, weight=1)
        ttk.Label(stage, textvariable=self.current_stage_var, style="Value.TLabel", padding=7, wraplength=180).grid(row=0, column=0, sticky="ew")
        ttk.Label(stage, textvariable=self.current_gds_var, style="Value.TLabel", padding=7, wraplength=180).grid(row=1, column=0, sticky="ew", pady=(6, 0))

        selection = ttk.LabelFrame(parent, text="GDS Pick", padding=8)
        selection.grid(row=2, column=0, sticky="ew", pady=(10, 0))
        selection.columnconfigure(0, weight=1)
        ttk.Label(selection, textvariable=self.cursor_var, style="Value.TLabel", padding=6, wraplength=180).grid(row=0, column=0, sticky="ew")
        ttk.Label(selection, textvariable=self.selection_var, style="Value.TLabel", padding=6, wraplength=180).grid(row=1, column=0, sticky="ew", pady=(5, 0))
        ttk.Button(selection, text="Fit to View", command=lambda: self.viewer.fit_to_view()).grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _build_controls(self, parent: ttk.Frame) -> None:
        row = 0
        row = self._build_point_section(parent, row)
        row = self._build_matrix_section(parent, row)
        row = self._build_run_section(parent, row)
        self._bind_preview_updates()

    def _section(self, parent: ttk.Frame, title: str, row: int) -> ttk.LabelFrame:
        section = ttk.LabelFrame(parent, text=title, padding=10)
        section.grid(row=row, column=0, sticky="ew", pady=(0, 10))
        section.columnconfigure(0, weight=1)
        return section

    def _build_point_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "GDS Basis", row)
        for column in range(4):
            section.columnconfigure(column, weight=1 if column in (1, 2) else 0)
        headings = ("Point", "U", "V", "Pick")
        for column, heading in enumerate(headings):
            ttk.Label(section, text=heading, style="Muted.TLabel").grid(row=0, column=column, sticky="w", padx=(0, 6))
        rows = (
            ("Origin", self.origin_u_var, self.origin_v_var, "origin"),
            ("U step", self.u_vector_u_var, self.u_vector_v_var, "u_vector"),
            ("V step", self.v_vector_u_var, self.v_vector_v_var, "v_vector"),
        )
        for index, (label, u_var, v_var, pick_kind) in enumerate(rows, start=1):
            ttk.Label(section, text=label, style="Panel.TLabel").grid(row=index, column=0, sticky="w", padx=(0, 6), pady=(6, 0))
            ttk.Entry(section, textvariable=u_var, width=9).grid(row=index, column=1, sticky="ew", padx=(0, 5), pady=(6, 0))
            ttk.Entry(section, textvariable=v_var, width=9).grid(row=index, column=2, sticky="ew", padx=(0, 5), pady=(6, 0))
            ttk.Button(section, text="Pick", command=lambda kind=pick_kind: self._arm_pick(kind)).grid(row=index, column=3, sticky="ew", pady=(6, 0))
        ttk.Button(section, text="Use Current Stage as Origin", command=self.use_current_stage_as_origin).grid(row=4, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        return row + 1

    def _build_matrix_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Matrix", row)
        section.columnconfigure((1, 3), weight=1)
        ttk.Label(section, text="Rows", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 6))
        ttk.Spinbox(section, from_=1, to=500, increment=1, textvariable=self.rows_var, width=8).grid(row=0, column=1, sticky="ew", padx=(0, 10))
        ttk.Label(section, text="Cols", style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=(0, 6))
        ttk.Spinbox(section, from_=1, to=500, increment=1, textvariable=self.cols_var, width=8).grid(row=0, column=3, sticky="ew")
        ttk.Label(section, text="FOV comes from Settings > LayoutBond FOV.", style="Muted.TLabel", wraplength=300).grid(row=1, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        ttk.Button(section, text="Preview Matrix", command=self.redraw_matrix_preview).grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(section, textvariable=self.matrix_summary_var, style="Value.TLabel", padding=8, wraplength=320).grid(row=3, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        return row + 1

    def _build_run_section(self, parent: ttk.Frame, row: int) -> int:
        section = self._section(parent, "Execution", row)
        section.columnconfigure((0, 1), weight=1)
        self.run_button = ttk.Button(section, text="Run ImgMatrix", style="Accent.TButton", command=self._start_run)
        self.run_button.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        self.stop_button = ttk.Button(section, text="Stop", command=self.stop_run, state="disabled")
        self.stop_button.grid(row=0, column=1, sticky="ew", padx=(5, 0))
        ttk.Label(section, textvariable=self.status_var, style="Status.TLabel", padding=8, wraplength=320).grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(section, textvariable=self.session_var, style="Value.TLabel", padding=8, wraplength=320).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        return row + 1

    def _bind_preview_updates(self) -> None:
        for variable in (
            self.origin_u_var,
            self.origin_v_var,
            self.u_vector_u_var,
            self.u_vector_v_var,
            self.v_vector_u_var,
            self.v_vector_v_var,
            self.rows_var,
            self.cols_var,
            self.fov_width_var,
            self.fov_height_var,
        ):
            variable.trace_add("write", lambda *_args: self.redraw_matrix_preview())

    def set_layout_context(
        self,
        model: GDSLayoutModel | None,
        layer_visibility: dict[tuple[int, int], bool] | None = None,
    ) -> None:
        self.model = model
        if model is None:
            self.viewer.draw_message("Load a GDS file in LayoutMap first.")
            return
        self.viewer.set_model(model)
        if layer_visibility:
            self.viewer.layer_visibility.update(layer_visibility)
        self.viewer.redraw()
        self.status_var.set(f"Synced {model.path.name} from LayoutMap.")
        self.redraw_matrix_preview()

    def _set_cursor_gds(self, point: tuple[float, float] | None) -> None:
        if point is None:
            self.cursor_var.set("Cursor u, v: -")
        else:
            self.cursor_var.set(f"Cursor u, v: {point[0]:.6g}, {point[1]:.6g}")

    def _handle_gds_click(self, u: float, v: float) -> None:
        self.selected_gds = (u, v)
        self.viewer.set_selected_gds((u, v))
        self.selection_var.set(f"Selected u, v: {u:.6g}, {v:.6g}")
        if self.pending_pick is not None:
            self._apply_pick(self.pending_pick, u, v)
            self.pending_pick = None
            self.viewer.set_pick_mode(False)

    def _arm_pick(self, kind: str) -> None:
        self.pending_pick = kind
        self.viewer.set_pick_mode(True)
        labels = {
            "origin": "Click a GDS point to set ImgMatrix origin.",
            "u_vector": "Click the next U-axis point. Vector = clicked point - origin.",
            "v_vector": "Click the next V-axis point. Vector = clicked point - origin.",
        }
        self.status_var.set(labels.get(kind, "Click a GDS point."))

    def _apply_pick(self, kind: str, u: float, v: float) -> None:
        if kind == "origin":
            self.origin_u_var.set(f"{u:.12g}")
            self.origin_v_var.set(f"{v:.12g}")
            self.status_var.set("Origin set from GDS pick.")
            return
        origin_u, origin_v = self._origin_from_ui()
        delta_u = u - origin_u
        delta_v = v - origin_v
        if kind == "u_vector":
            self.u_vector_u_var.set(f"{delta_u:.12g}")
            self.u_vector_v_var.set(f"{delta_v:.12g}")
            self.status_var.set("U vector set from GDS pick.")
        elif kind == "v_vector":
            self.v_vector_u_var.set(f"{delta_u:.12g}")
            self.v_vector_v_var.set(f"{delta_v:.12g}")
            self.status_var.set("V vector set from GDS pick.")

    def use_current_stage_as_origin(self) -> None:
        mapper = self.get_mapper()
        if mapper is None:
            self.status_var.set("Bind LayoutMap mapping before using current stage as origin.")
            return
        try:
            x_um, y_um, _z_um = self._stage_position_xyz_um()
            u, v = mapper.stage_to_gds(x_um, y_um)
        except Exception as exc:
            self.status_var.set(f"Current stage origin unavailable: {exc}")
            return
        self.origin_u_var.set(f"{u:.12g}")
        self.origin_v_var.set(f"{v:.12g}")
        self.status_var.set("Origin set from current mapped stage position.")

    def _origin_from_ui(self) -> tuple[float, float]:
        try:
            return float(self.origin_u_var.get()), float(self.origin_v_var.get())
        except ValueError as exc:
            raise ValueError("Set a numeric ImgMatrix origin first.") from exc

    def settings_from_ui(self) -> ImgMatrixSettings:
        try:
            return ImgMatrixSettings(
                origin_u=float(self.origin_u_var.get()),
                origin_v=float(self.origin_v_var.get()),
                u_vector_u=float(self.u_vector_u_var.get()),
                u_vector_v=float(self.u_vector_v_var.get()),
                v_vector_u=float(self.v_vector_u_var.get()),
                v_vector_v=float(self.v_vector_v_var.get()),
                rows=int(float(self.rows_var.get())),
                cols=int(float(self.cols_var.get())),
                fov_width_um=float(self.fov_width_var.get()),
                fov_height_um=float(self.fov_height_var.get()),
            ).normalized()
        except ValueError as exc:
            raise ValueError(f"Invalid ImgMatrix settings: {exc}") from exc

    def redraw_matrix_preview(self) -> None:
        if not hasattr(self, "viewer"):
            return
        mapper = self.get_mapper()
        if mapper is None:
            self.viewer.set_matrix_overlay([])
            self.matrix_summary_var.set("Preview: bind LayoutMap mapping first.")
            return
        try:
            settings = self.settings_from_ui()
            points = generate_imgmatrix_points(settings, mapper)
        except Exception as exc:
            self.viewer.set_matrix_overlay([])
            self.matrix_summary_var.set(f"Preview unavailable: {exc}")
            return
        overlays = [
            (list(point.fov_polygon_gds), f"{point.row},{point.col}")
            for point in points
        ]
        self.viewer.set_matrix_overlay(overlays)
        last = points[-1]
        self.matrix_summary_var.set(
            f"Preview: {settings.rows} x {settings.cols} = {len(points)} shots. "
            f"Last UV {last.u:.6g}, {last.v:.6g}."
        )

    def _start_run(self) -> None:
        try:
            settings = self.settings_from_ui()
        except Exception as exc:
            self.status_var.set(str(exc))
            return
        self.start_run(settings)

    def set_running(self, running: bool) -> None:
        self.run_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")

    def set_progress(self, current: int, total: int, message: str) -> None:
        self.status_var.set(f"{message} ({current}/{total})")

    def set_session_path(self, session_dir: Path | None) -> None:
        self.session_var.set(f"Session: {session_dir}" if session_dir else "Session: -")

    def set_status(self, message: str) -> None:
        self.status_var.set(message)
        if self.set_app_status is not None:
            self.set_app_status(message)

    def _schedule_status_poll(self) -> None:
        try:
            self._update_status_panel()
            self._update_microscope_preview()
            self.status_poll_job = self.frame.after(300, self._schedule_status_poll)
        except tk.TclError:
            return

    def _update_status_panel(self) -> None:
        try:
            x_um, y_um, z_um = self._stage_position_xyz_um()
            self.current_stage_var.set(f"Current XYZ: {x_um:.6g}, {y_um:.6g}, {z_um:.6g} um")
            mapper = self.get_mapper()
            if mapper is None:
                self.current_gds_var.set("Current GDS: bind LayoutMap first")
            else:
                u, v = mapper.stage_to_gds(x_um, y_um)
                self.current_gds_var.set(f"Current GDS u, v: {u:.6g}, {v:.6g}")
        except Exception as exc:
            self.current_stage_var.set(f"Current stage unavailable: {exc}")
            self.current_gds_var.set("Current GDS: -")

    def _update_microscope_preview(self) -> None:
        if self.get_microscope_preview is None:
            return
        try:
            payload = self.get_microscope_preview()
        except Exception:
            return
        if not payload:
            return
        try:
            self.microscope_photo = tk.PhotoImage(data=payload, format="PPM")
            self.microscope_label.configure(image=self.microscope_photo, text="")
        except tk.TclError:
            return

    def _stage_position_xyz_um(self) -> tuple[float, float, float]:
        values = self.get_stage_position_um()
        if len(values) == 2:
            x_um, y_um = values
            return float(x_um), float(y_um), 0.0
        x_um, y_um, z_um = values
        return float(x_um), float(y_um), float(z_um)
