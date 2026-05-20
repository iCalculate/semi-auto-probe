from __future__ import annotations

import queue
import hmac
import json
import math
import os
import re
import shutil
import threading
import time
import tkinter as tk
from collections.abc import Callable
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from .af_plane import (
    AFMeshPoint,
    SamplePlaneModel,
    clear_sample_plane_model,
    fit_sample_plane,
    generate_af_mesh,
    get_sample_plane_model,
    get_focus_z_at_xy,
    set_sample_plane_model,
)
from .agent import (
    AGENT_ACTION_AUTOFOCUS,
    AGENT_ACTION_IMAGE_CAPTURE,
    AGENT_ACTION_LAYOUT_OVERLAY,
    AGENT_ACTION_MOVE_GDS,
    AgentContext,
    AgentPlan,
    build_agent_planner_from_config,
)
from .camera import CameraSettings, UsbCamera
from .config import (
    AUTOFOCUS_PEAK_MODEL_GAUSSIAN,
    AUTOFOCUS_PEAK_MODEL_LABELS,
    AUTOFOCUS_PEAK_MODEL_LORENTZIAN,
    AUTOFOCUS_PEAK_MODELS,
    AUTOFOCUS_PEAK_MODEL_PARABOLIC,
    AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT,
    CAMERA_CONTROL_MODE_LABELS,
    CAMERA_CONTROL_MODES,
    DEFAULT_AGENT_BASE_URL,
    DEFAULT_AGENT_MODEL,
    DEFAULT_AGENT_TIMEOUT_SECONDS,
    DEFAULT_CONFIG_FILENAME,
    EYEPIECE_OPTIONS,
    JOG_STEP_AXES,
    KEYBOARD_MOTION_SCHEME_ARROW_PAGE,
    KEYBOARD_MOTION_SCHEME_LABELS,
    KEYBOARD_MOTION_SCHEME_WASD_QE,
    MOTOR_SPEED_PROFILE_FAST,
    MOTOR_SPEED_PROFILE_LABELS,
    MOTOR_SPEED_PROFILES,
    OBJECTIVE_OPTIONS,
    ProbeConfig,
    derive_missing_calibrations,
    format_jog_step_levels,
    load_probe_config,
    normalize_autofocus_peak_model,
    normalize_camera_control_mode,
    parse_jog_step_levels_text,
    pulses_from_um,
    save_probe_config,
)
from .focusmap_3d import create_focusmap_3d_view
from .gds_stage_mapper import GDSStageMapperPanel, stage_move_plan_from_um, stage_xyz_move_plan_from_um
from .img_stitch import (
    StitchEdgeQuality,
    StitchSession,
    StitchSettings,
    TileRecord,
    build_seam_quality_overlay,
    fit_plane,
    flat_field_correct,
    fuse_t_stack,
    fuse_z_stack,
    recompose_session,
    serpentine_indices,
    stitch_displacement_diagnostics,
    z_stack_positions,
)
from .logging_utils import colorize_hex_frame, configure_logging, print_startup_banner
from .monitor_feed import publish_camera_frame, request_web_fallback_camera_release, start_frame_publisher
from .protocol import COMM_TEST_COMMAND, FUNCTION_READ_POSITION, RESPONSE_HEAD, Axis, AxisPosition, IoStatus, hex_bytes, parse_axis_position_response
from .protocol import payload_contains_clear_position_command
from .serial_client import ControllerSerialClient, CommunicationTestResult, list_serial_ports
from .ui.calibration_dialog import PixelCalibrationDialog
from .ui.agent_panel import AgentPanel
from .ui.vision import VisionPanel


logger = configure_logging()
DEFAULT_SERIAL_PORT = "COM5"
RESULT_POLL_INTERVAL_MS = 25
RESULT_POLL_MAX_EVENTS = 30
RESULT_POLL_MAX_SECONDS = 0.012
REALTIME_POSITION_UI_INTERVAL_SECONDS = 0.05
AUTOFOCUS_POST_SETTLE_DISCARD_FRAMES = 2
FOCUSMAP_AUTOSAVE_FILENAME = "last_focusmap_mapping.json"
ADMIN_TOKEN_ENV = "SEMI_AUTO_PROBE_ADMIN_TOKEN"
WEB_ACCESS_TOKEN_ENV = "SEMI_AUTO_PROBE_WEB_TOKEN"


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


class ProbeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Semi Auto Probe")
        self.window_icon: tk.PhotoImage | None = None
        self._set_window_icon()
        self.geometry("1400x880")
        self.minsize(1040, 600)
        self.configure(bg="#0b0f14")
        self.after_idle(self._maximize_default_window)

        self.serial_client: ControllerSerialClient | None = None
        self.camera: UsbCamera | None = None
        self.camera_running = False
        self.camera_rendering = False
        self.camera_image: tk.PhotoImage | None = None
        self.vision_panel: VisionPanel | None = None
        self.gds_stage_mapper_panel: GDSStageMapperPanel | None = None
        self.agent_panel: AgentPanel | None = None
        self.agent_function_spec_path = Path.cwd() / "docs" / "agent-function-spec.md"
        self.agent_planner = None
        self.latest_camera_frame = None
        self.camera_lock = threading.Lock()
        self.focus_lock = threading.Lock()
        self.camera_thread: threading.Thread | None = None
        self.camera_session_id = 0
        self.result_queue: queue.Queue[object] = queue.Queue()
        self.realtime_stop_event = threading.Event()
        self.realtime_thread: threading.Thread | None = None
        self.home_signal_stop_event = threading.Event()
        self.home_signal_thread: threading.Thread | None = None
        self.home_signal_enabled = False
        self.autofocus_stop_event = threading.Event()
        self.autofocus_thread: threading.Thread | None = None
        self.autofocus_running = False
        self.autofocus_restore_realtime = False
        self.autofocus_restore_home_signal = False
        self.imgstitch_stop_event = threading.Event()
        self.imgstitch_thread: threading.Thread | None = None
        self.imgstitch_running = False
        self.imgstitch_restore_realtime = False
        self.imgstitch_restore_home_signal = False
        self.imgstitch_focus_sampling_required = False
        self.latest_stitch_frame = None
        self.current_page = "Main"
        self.active_page_name: str | None = None
        self.config_path = Path.cwd() / DEFAULT_CONFIG_FILENAME
        try:
            self.probe_config = load_probe_config(self.config_path)
        except Exception as exc:
            self.probe_config = ProbeConfig()
            logger.error("Failed to load probe config from %s: %s", self.config_path, exc)
        self.agent_planner = self._build_agent_planner()

        self.port_var = tk.StringVar(value=DEFAULT_SERIAL_PORT)
        self.camera_index_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="Ready")
        self.rx_var = tk.StringVar(value="-")
        self.tx_var = tk.StringVar(value="-")
        self.comm_input_mode_var = tk.StringVar(value="Hex")
        self.comm_read_length_var = tk.StringVar(value="12")
        self.comm_note_var = tk.StringVar(value="Default: communication test. Expected RX starts with A3 AA.")
        self.focus_metric_var = tk.StringVar(value="Laplacian")
        self.focus_score_var = tk.StringVar(value="-")
        self.autofocus_step_var = tk.StringVar(value="30")
        self.autofocus_min_step_var = tk.StringVar(value="5")
        self.autofocus_max_moves_var = tk.StringVar(value="300")
        self.focus_window_var = tk.StringVar(value="30")
        self.autofocus_status_var = tk.StringVar(value="Idle")
        self.autofocus_z_var = tk.StringVar(value="0")
        self.latest_focus_scores = {"Laplacian": 0.0, "Tenengrad": 0.0, "Brenner": 0.0}
        self.latest_focus_timestamp = 0.0
        self.latest_focus_frame_ppm: bytes | None = None
        self.focus_history: list[tuple[float, dict[str, float]]] = []
        self.autofocus_samples: list[tuple[float, float, int]] = []
        self.autofocus_z_score_samples: list[dict[str, object]] = []
        self.autofocus_fine_range: tuple[int, int] | None = None
        self.autofocus_history_rows: list[dict[str, object]] = []
        self.autofocus_run_start_time: float | None = None
        self.autofocus_run_end_time: float | None = None
        self.autofocus_camera_image: tk.PhotoImage | None = None
        self.af_plane_thread: threading.Thread | None = None
        self.af_plane_running = False
        self.af_plane_paused = False
        self.af_plane_stop_event = threading.Event()
        self.af_plane_pause_event = threading.Event()
        self.af_plane_restore_realtime = False
        self.af_plane_restore_home_signal = False
        self.af_plane_model_stored = False
        self.af_plane_error_active = False
        self.focusmap_realtime_bgr = None
        self.focusmap_realtime_image: tk.PhotoImage | None = None
        self.af_plane_mesh_points: list[AFMeshPoint] = []
        self.af_plane_results: list[dict[str, object]] = []
        self.af_plane_mesh_hitboxes: list[tuple[float, float, float, dict[str, object]]] = []
        self.af_plane_table_items: dict[int, str] = {}
        self.af_plane_selected_index: int | None = None
        self.af_plane_eval_labels: dict[str, tk.Label] = {}
        self.af_plane_region_p1: tuple[int, int] | None = None
        self.af_plane_region_p2: tuple[int, int] | None = None
        self.sample_plane_model: SamplePlaneModel | None = None
        self.focusmap_3d_view = None
        self.imgstitch_camera_image: tk.PhotoImage | None = None
        self.imgstitch_preview_image: tk.PhotoImage | None = None
        self.imgstitch_preview_bgr = None
        self.imgstitch_preview_scale = 1.0
        self.imgstitch_preview_pan = [0.0, 0.0]
        self.imgstitch_preview_drag_start: tuple[int, int, float, float] | None = None
        self.imgstitch_session: StitchSession | None = None
        self.imgstitch_tile_images: dict[tuple[int, int], object] = {}
        self.imgstitch_latest_positions: dict[tuple[int, int], tuple[float, float]] = {}
        self.imgstitch_latest_edges: list[StitchEdgeQuality] = []
        self.imgstitch_recompose_running = False
        self.imgstitch_recompose_button: ttk.Button | None = None
        self.imgstitch_point1: tuple[int, int] | None = None
        self.imgstitch_point2: tuple[int, int] | None = None
        self.imgstitch_session_dir = Path.cwd() / "imgstitch_session"
        self.position_vars = {
            "X": tk.StringVar(value="0"),
            "Y": tk.StringVar(value="0"),
            "Z": tk.StringVar(value="0"),
        }
        self.current_position_values = {"X": 0, "Y": 0, "Z": 0}
        self.position_edit_modes: dict[str, str | None] = {"X": None, "Y": None, "Z": None}
        self.modified_position_axes: set[str] = set()
        self.current_position_edit_mode: str | None = None
        self.position_inputs: dict[str, tk.Entry] = {}
        self.step_vars = {
            "X": tk.StringVar(value="10"),
            "Y": tk.StringVar(value="10"),
            "Z": tk.StringVar(value="10"),
        }
        self.jog_step_levels = {
            axis: tuple(self.probe_config.jog_step_levels[axis])
            for axis in JOG_STEP_AXES
        }
        self.motion_mode_var = tk.StringVar(value="Relative")
        self.main_focusmap_plane_var = tk.BooleanVar(value=False)
        self.keyboard_move_enabled_var = tk.BooleanVar(value=True)
        self.realtime_enabled = False
        self.realtime_button_var = tk.StringVar(value="Continue")
        self.home_signal_button_var = tk.StringVar(value="Home Signals")
        self.admin_mode_enabled = False
        self.admin_token_var = tk.StringVar(value="")
        self.admin_mode_status_var = tk.StringVar(value="Admin mode locked")
        self.motion_busy = False
        self.keyboard_motion_busy = False
        self.position_read_pending = False
        self.position_read_job: str | None = None
        self.held_keys: dict[str, dict[str, object]] = {}
        self.position_click_job: str | None = None
        self.resize_log_job: str | None = None
        self.last_logged_window_size: tuple[int, int] | None = None
        self.last_logged_control_width: int | None = None
        self.imgstack_mode_var = tk.StringVar(value="XY Stitch")
        self.imgstitch_tile_acquisition_var = tk.StringVar(value="Single Frame")
        self.t_stack_frame_count_var = tk.StringVar(value="4")
        self.t_stack_fusion_var = tk.StringVar(value="average")
        self.t_stack_save_raw_var = tk.BooleanVar(value=False)
        self.z_stack_step_um_var = tk.StringVar(value="2")
        self.z_stack_range_um_var = tk.StringVar(value="20")
        self.z_stack_fusion_var = tk.StringVar(value="laplacian")
        self.z_stack_return_var = tk.BooleanVar(value=True)
        self.z_stack_save_raw_var = tk.BooleanVar(value=False)
        self.imgstitch_rows_var = tk.StringVar(value="3")
        self.imgstitch_cols_var = tk.StringVar(value="3")
        self.imgstitch_overlap_x_var = tk.StringVar(value="120")
        self.imgstitch_overlap_y_var = tk.StringVar(value="90")
        self.imgstitch_step_x_var = tk.StringVar(value="1000")
        self.imgstitch_step_y_var = tk.StringVar(value="1000")
        self.imgstitch_range_mode_var = tk.StringVar(value="Array")
        self.imgstitch_width_um_var = tk.StringVar(value="2000")
        self.imgstitch_height_um_var = tk.StringVar(value="2000")
        self.imgstitch_max_correction_um_var = tk.StringVar(value="20")
        self.imgstitch_registration_weight_var = tk.StringVar(value="0")
        self.imgstitch_show_seams_var = tk.BooleanVar(value=True)
        self.imgstitch_green_edge_correction_var = tk.BooleanVar(value=True)
        self.imgstitch_white_balance_var = tk.BooleanVar(value=True)
        self.imgstitch_focusmap_plane_var = self.main_focusmap_plane_var
        self.imgstitch_quality_var = tk.StringVar(value="No seam data")
        self.imgstitch_point_status_var = tk.StringVar(value="No rectangle points")
        self.imgstitch_plane_af_var = tk.BooleanVar(value=False)
        self.imgstitch_status_var = tk.StringVar(value="Idle")
        self.af_plane_mesh_type_var = tk.StringVar(value="Rectangular")
        self.af_plane_region_mode_var = tk.StringVar(value="Center / Range")
        self.af_plane_center_x_var = tk.StringVar(value="0")
        self.af_plane_center_y_var = tk.StringVar(value="0")
        self.af_plane_x_range_var = tk.StringVar(value="1000")
        self.af_plane_y_range_var = tk.StringVar(value="1000")
        self.af_plane_cols_var = tk.StringVar(value="3")
        self.af_plane_rows_var = tk.StringVar(value="3")
        self.af_plane_p1_var = tk.StringVar(value="P1 -")
        self.af_plane_p2_var = tk.StringVar(value="P2 -")
        self.af_plane_retry_count_var = tk.StringVar(value="0")
        self.af_plane_return_to_start_var = tk.BooleanVar(value=True)
        self.af_plane_dry_run_var = tk.BooleanVar(value=False)
        self.af_plane_status_var = tk.StringVar(value="Idle")
        self.af_plane_equation_var = tk.StringVar(value="No fitted plane")
        self.af_plane_metrics_var = tk.StringVar(value="Valid 0 | Failed 0")
        self.af_plane_eval_var = tk.StringVar(value="No fitted plane")
        self.af_plane_progress_var = tk.DoubleVar(value=0.0)
        self.af_plane_pause_button_var = tk.StringVar(value="Pause")
        self.last_realtime_ui_update = 0.0
        self.last_realtime_status_update = 0.0
        self.axis_indicator_canvases: dict[str, tk.Canvas] = {}
        self.axis_indicator_items: dict[str, int] = {}
        self.axis_indicator_colors = {"X": "#60a5fa", "Y": "#34d399", "Z": "#fbbf24"}
        self.axis_control_buttons: dict[str, list[ttk.Button]] = {}
        self.objective_var = tk.StringVar(value=str(self.probe_config.objective))
        self.eyepiece_var = tk.StringVar(value=f"{self.probe_config.eyepiece:g}")
        self.microstep_var = tk.StringVar(value=str(self.probe_config.microstep))
        self.lead_xy_var = tk.StringVar(value=f"{self.probe_config.lead_xy_mm:g}")
        self.lead_z_var = tk.StringVar(value=f"{self.probe_config.lead_z_mm:g}")
        self.base_angle_var = tk.StringVar(value=f"{self.probe_config.base_angle_deg:g}")
        self.cc_speed_percent_var = tk.StringVar(value=str(self.probe_config.cc_speed_percent))
        self.fine_speed_percent_var = tk.StringVar(value=str(self.probe_config.fine_speed_percent))
        self.safe_speed_percent_var = tk.StringVar(value=str(self.probe_config.safe_speed_percent))
        self.motor_speed_profile_var = tk.StringVar(value=self._motor_speed_profile_label(self.probe_config.active_motor_speed_profile))
        self.controller_motion_parameter_vars = {
            axis: {
                field_name: tk.StringVar(value=str(self.probe_config.controller_motion_parameters[axis][field_name]))
                for field_name in ("minimum_speed", "work_speed", "acceleration")
            }
            for axis in JOG_STEP_AXES
        }
        self.controller_motion_status_var = tk.StringVar(value="D5 controller parameters not read.")
        self.controller_motion_startup_read_done = False
        self.camera_exposure_mode_var = tk.StringVar(value=self._camera_control_mode_label(self.probe_config.camera_exposure_mode))
        self.camera_exposure_var = tk.StringVar(value=f"{self.probe_config.camera_exposure:g}")
        self.camera_gain_mode_var = tk.StringVar(value=self._camera_control_mode_label(self.probe_config.camera_gain_mode))
        self.camera_gain_var = tk.StringVar(value=f"{self.probe_config.camera_gain:g}")
        self.cc_accel_time_var = tk.StringVar(value=f"{self.probe_config.cc_accel_time_s:g}")
        self.autofocus_settle_ms_var = tk.StringVar(value=str(self.probe_config.autofocus_settle_ms))
        self.autofocus_sample_count_var = tk.StringVar(value=str(self.probe_config.autofocus_sample_count))
        self.autofocus_peak_model_var = tk.StringVar(value=self._autofocus_peak_model_label(self.probe_config.autofocus_peak_model))
        self.imgstitch_settle_ms_var = tk.StringVar(value=str(self.probe_config.imgstitch_settle_ms))
        self.layoutbond_fov_width_var = tk.StringVar(value=f"{self.probe_config.layoutbond_fov_width_um:g}")
        self.layoutbond_fov_height_var = tk.StringVar(value=f"{self.probe_config.layoutbond_fov_height_um:g}")
        self.keyboard_motion_scheme_var = tk.StringVar(value=self._keyboard_motion_scheme_label(self.probe_config.keyboard_motion_scheme))
        self.jog_step_level_vars = {
            axis: tk.StringVar(value=format_jog_step_levels(self.jog_step_levels[axis]))
            for axis in JOG_STEP_AXES
        }
        self.focus_threshold_yellow_vars = {
            metric: tk.StringVar(value=f"{self.probe_config.focus_threshold_yellow[metric]:g}")
            for metric in ("Laplacian", "Tenengrad", "Brenner")
        }
        self.focus_threshold_green_vars = {
            metric: tk.StringVar(value=f"{self.probe_config.focus_threshold_green[metric]:g}")
            for metric in ("Laplacian", "Tenengrad", "Brenner")
        }
        self.calibration_status_var = tk.StringVar(value="")
        self.motor_conversion_var = tk.StringVar(value="")
        self.config_status_var = tk.StringVar(value=f"Config: {self.config_path.name}")
        self.agent_api_key_var = tk.StringVar(value=self.probe_config.agent_api_key)
        self.agent_base_url_var = tk.StringVar(value=self.probe_config.agent_base_url)
        self.agent_model_var = tk.StringVar(value=self.probe_config.agent_model)
        self.agent_timeout_var = tk.StringVar(value=f"{self.probe_config.agent_timeout_seconds:g}")
        self.set_xyz_zero_button: ttk.Button | None = None
        self.set_autofocus_z_zero_button: ttk.Button | None = None

        self._configure_theme()
        self._build_ui()
        self._bind_keyboard_controls()
        start_frame_publisher()
        self.bind("<Configure>", self._on_window_configure)
        self.port_combo["values"] = (DEFAULT_SERIAL_PORT,)
        self.start_camera()
        self.after(300, self.connect_and_test_serial)
        self.after(RESULT_POLL_INTERVAL_MS, self._poll_result_queue)

    def _set_window_icon(self) -> None:
        icon_path = Path(__file__).parent / "assets" / "logo-system-diagram.png"
        if not icon_path.exists():
            icon_path = Path.cwd() / "assets" / "logo-system-diagram.png"
        if not icon_path.exists():
            logger.warning("Window icon asset not found.")
            return
        try:
            self.window_icon = tk.PhotoImage(file=str(icon_path))
            self.iconphoto(True, self.window_icon)
        except tk.TclError as exc:
            logger.warning("Failed to load window icon from %s: %s", icon_path, exc)

    def _maximize_default_window(self) -> None:
        try:
            self.state("zoomed")
        except tk.TclError:
            try:
                self.attributes("-zoomed", True)
            except tk.TclError:
                logger.debug("Default maximize is not supported on this platform.")

    def _configure_theme(self) -> None:
        self.colors = {
            "bg": "#0b0f14",
            "surface": "#111821",
            "surface_2": "#151f2b",
            "surface_3": "#1b2735",
            "input": "#0f1722",
            "input_focus": "#102235",
            "border": "#263545",
            "border_focus": "#38bdf8",
            "text": "#e5edf5",
            "muted": "#8fa0b3",
            "accent": "#34d399",
            "accent_hover": "#4ade80",
            "blue": "#60a5fa",
            "warning": "#fbbf24",
            "danger": "#fb7185",
        }

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(".", background=self.colors["bg"], foreground=self.colors["text"], font=("Segoe UI", 10))
        style.configure("App.TFrame", background=self.colors["bg"])
        style.configure("Panel.TFrame", background=self.colors["surface"], relief="flat")
        style.configure("Toolbar.TFrame", background=self.colors["surface"])
        style.configure("TLabel", background=self.colors["bg"], foreground=self.colors["text"])
        style.configure("Panel.TLabel", background=self.colors["surface"], foreground=self.colors["text"])
        style.configure("Muted.TLabel", background=self.colors["surface"], foreground=self.colors["muted"])
        style.configure("Title.TLabel", background=self.colors["bg"], foreground=self.colors["text"], font=("Segoe UI Semibold", 18))
        style.configure("Subtitle.TLabel", background=self.colors["bg"], foreground=self.colors["muted"], font=("Segoe UI", 9))
        style.configure("Section.TLabel", background=self.colors["surface"], foreground=self.colors["muted"], font=("Segoe UI Semibold", 9))
        style.configure("Value.TLabel", background=self.colors["surface_2"], foreground=self.colors["text"], font=("Cascadia Mono", 9))
        style.configure("Position.TLabel", background=self.colors["surface_2"], foreground=self.colors["accent"], font=("Cascadia Mono", 18, "bold"))
        style.configure("Status.TLabel", background=self.colors["surface_2"], foreground=self.colors["accent"], font=("Segoe UI Semibold", 10))
        style.configure("Video.TLabel", background="#05070a", foreground=self.colors["muted"], font=("Segoe UI Semibold", 14))
        style.configure("TButton", background=self.colors["surface_3"], foreground=self.colors["text"], bordercolor=self.colors["border"], focusthickness=0, focuscolor=self.colors["border_focus"], relief="flat", borderwidth=1, padding=(11, 7))
        style.map("TButton", background=[("active", "#223144"), ("pressed", "#1d2a3a"), ("disabled", "#111827")], foreground=[("disabled", "#64748b")], bordercolor=[("focus", self.colors["border_focus"])])
        style.configure("Accent.TButton", background="#0f3b2d", foreground="#d1fae5", bordercolor="#1f7a5a", padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#14543f"), ("pressed", "#0f3b2d")])
        style.configure("Danger.TButton", background="#4c0519", foreground="#ffe4e6", bordercolor="#be123c", padding=(12, 6))
        style.map("Danger.TButton", background=[("active", "#881337"), ("pressed", "#4c0519")])
        style.configure("Ghost.TButton", background=self.colors["surface"], foreground=self.colors["muted"], bordercolor=self.colors["border"], padding=(8, 6))
        style.map("Ghost.TButton", background=[("active", self.colors["surface_2"])], foreground=[("active", self.colors["text"])])
        style.configure("TEntry", fieldbackground=self.colors["input"], background=self.colors["input"], foreground=self.colors["text"], bordercolor=self.colors["border"], lightcolor=self.colors["border"], darkcolor=self.colors["border"], insertcolor=self.colors["accent"], relief="flat", borderwidth=1, padding=(10, 7))
        style.map(
            "TEntry",
            fieldbackground=[("focus", self.colors["input_focus"]), ("readonly", self.colors["input"]), ("disabled", "#101820"), ("!disabled", self.colors["input"])],
            foreground=[("focus", self.colors["text"]), ("readonly", self.colors["text"]), ("disabled", "#64748b"), ("!disabled", self.colors["text"])],
            bordercolor=[("focus", self.colors["border_focus"]), ("invalid", "#fb7185"), ("!disabled", self.colors["border"])],
            lightcolor=[("focus", self.colors["border_focus"]), ("invalid", "#fb7185")],
            darkcolor=[("focus", self.colors["border_focus"]), ("invalid", "#fb7185")],
        )
        style.configure("TCombobox", fieldbackground=self.colors["input"], background=self.colors["surface_3"], foreground=self.colors["text"], bordercolor=self.colors["border"], lightcolor=self.colors["border"], darkcolor=self.colors["border"], arrowcolor=self.colors["muted"], relief="flat", borderwidth=1, padding=(10, 7))
        style.map(
            "TCombobox",
            fieldbackground=[("focus", self.colors["input_focus"]), ("readonly", self.colors["input"]), ("disabled", "#101820")],
            foreground=[("readonly", self.colors["text"]), ("disabled", "#64748b")],
            background=[("active", "#223144"), ("pressed", "#172536")],
            bordercolor=[("focus", self.colors["border_focus"]), ("!disabled", self.colors["border"])],
            arrowcolor=[("active", self.colors["accent"]), ("focus", self.colors["accent"]), ("!disabled", self.colors["muted"])],
        )
        style.configure("TSpinbox", fieldbackground=self.colors["input"], background=self.colors["surface_3"], foreground=self.colors["text"], bordercolor=self.colors["border"], lightcolor=self.colors["border"], darkcolor=self.colors["border"], arrowcolor=self.colors["muted"], relief="flat", borderwidth=1, padding=(10, 7), arrowsize=14)
        style.map(
            "TSpinbox",
            fieldbackground=[("focus", self.colors["input_focus"]), ("disabled", "#101820"), ("!disabled", self.colors["input"])],
            foreground=[("disabled", "#64748b"), ("!disabled", self.colors["text"])],
            background=[("active", "#223144"), ("pressed", "#172536")],
            bordercolor=[("focus", self.colors["border_focus"]), ("invalid", "#fb7185"), ("!disabled", self.colors["border"])],
            arrowcolor=[("active", self.colors["accent"]), ("focus", self.colors["accent"]), ("!disabled", self.colors["muted"])],
        )
        style.configure("Error.TSpinbox", fieldbackground="#3f1018", background="#4c0519", foreground="#fecdd3", bordercolor="#be123c", lightcolor="#be123c", darkcolor="#be123c", arrowcolor="#fecdd3", relief="flat", borderwidth=1, padding=(10, 7), arrowsize=14)
        style.configure("Treeview", background=self.colors["input"], fieldbackground=self.colors["input"], foreground=self.colors["text"], bordercolor=self.colors["border"], lightcolor=self.colors["border"], darkcolor=self.colors["border"], rowheight=28, relief="flat", borderwidth=1)
        style.configure("Treeview.Heading", background=self.colors["surface_3"], foreground=self.colors["text"], relief="flat", borderwidth=0, padding=(8, 7), font=("Segoe UI Semibold", 9))
        style.map("Treeview", background=[("selected", "#0f766e")], foreground=[("selected", "#f8fafc")])
        style.configure(
            "FocusMap.Treeview",
            background="#0f1722",
            fieldbackground="#0f1722",
            foreground=self.colors["text"],
            borderwidth=0,
            relief="flat",
            rowheight=30,
            font=("Segoe UI", 9),
        )
        style.configure(
            "FocusMap.Treeview.Heading",
            background="#203044",
            foreground="#e5edf5",
            relief="flat",
            borderwidth=0,
            padding=(8, 8),
            font=("Segoe UI Semibold", 9),
        )
        style.map(
            "FocusMap.Treeview",
            background=[("selected", "#164e3d")],
            foreground=[("selected", "#d1fae5")],
        )
        style.map("FocusMap.Treeview.Heading", background=[("active", "#293a50")])
        style.configure("Horizontal.TProgressbar", background=self.colors["accent"], troughcolor=self.colors["surface_2"], bordercolor=self.colors["border"])
        style.configure("TCheckbutton", background=self.colors["surface"], foreground=self.colors["text"], indicatorcolor=self.colors["input"], indicatormargin=6, padding=(6, 4), focuscolor=self.colors["border_focus"])
        style.map(
            "TCheckbutton",
            background=[("active", self.colors["surface"])],
            foreground=[("active", self.colors["text"]), ("disabled", "#64748b")],
            indicatorcolor=[("selected", self.colors["accent"]), ("active", "#223144"), ("!selected", self.colors["input"])],
            bordercolor=[("focus", self.colors["border_focus"])],
        )
        style.configure("TRadiobutton", background=self.colors["surface"], foreground=self.colors["text"], indicatorcolor=self.colors["input"], padding=(6, 4), focuscolor=self.colors["border_focus"])
        style.map("TRadiobutton", background=[("active", self.colors["surface"])], foreground=[("active", self.colors["text"])], indicatorcolor=[("selected", self.colors["accent"]), ("!selected", self.colors["input"])])
        style.configure("TNotebook", background=self.colors["bg"], borderwidth=0, tabmargins=(0, 4, 0, 0))
        style.configure(
            "TNotebook.Tab",
            background="#0f1722",
            foreground=self.colors["muted"],
            padding=(14, 10),
            borderwidth=0,
            font=("Segoe UI Semibold", 10),
            width=17,
        )
        style.map(
            "TNotebook.Tab",
            background=[("selected", "#172536"), ("active", self.colors["surface_3"])],
            foreground=[("selected", "#d1fae5"), ("active", self.colors["text"])],
        )
        style.configure("TLabelframe", background=self.colors["surface"], bordercolor=self.colors["border"])
        style.configure("TLabelframe.Label", background=self.colors["surface"], foreground=self.colors["muted"], font=("Segoe UI Semibold", 9))
        self.option_add("*Entry.selectBackground", "#0e7490")
        self.option_add("*Entry.selectForeground", "#f8fafc")
        self.option_add("*Spinbox.selectBackground", "#0e7490")
        self.option_add("*Spinbox.selectForeground", "#f8fafc")
        self.option_add("*TCombobox*Listbox.background", self.colors["input"])
        self.option_add("*TCombobox*Listbox.foreground", self.colors["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", "#0e7490")
        self.option_add("*TCombobox*Listbox.selectForeground", "#f8fafc")

    @staticmethod
    def _integer_text_allowed(proposed: str, minimum: int | None = None, maximum: int | None = None) -> bool:
        if proposed == "":
            return True
        allow_negative = minimum is None or minimum < 0
        if proposed == "-":
            return allow_negative
        if proposed.startswith("-"):
            if not allow_negative or not proposed[1:].isdigit():
                return False
        elif not proposed.isdigit():
            return False
        try:
            value = int(proposed)
        except ValueError:
            return False
        if maximum is not None and value > maximum:
            return False
        return True

    @staticmethod
    def _float_text_allowed(proposed: str, minimum: float | None = None, maximum: float | None = None) -> bool:
        if proposed == "":
            return True
        allow_negative = minimum is None or minimum < 0
        if proposed in {"-", ".", "-."}:
            return allow_negative or not proposed.startswith("-")
        if proposed.count(".") > 1:
            return False
        if proposed.startswith("-"):
            if not allow_negative:
                return False
            digits = proposed[1:].replace(".", "", 1)
        else:
            digits = proposed.replace(".", "", 1)
        if digits and not digits.isdigit():
            return False
        try:
            value = float(proposed)
        except ValueError:
            return False
        if maximum is not None and value > maximum:
            return False
        return True

    @staticmethod
    def _jog_step_text_allowed(proposed: str) -> bool:
        return all(char.isdigit() or char in {",", ";", " ", "\t"} for char in proposed)

    def _numeric_widget_options(self, *, font: tuple[str, int] | tuple[str, int, str] = ("Segoe UI", 10), fg: str | None = None) -> dict[str, object]:
        return {
            "relief": "flat",
            "bd": 0,
            "bg": self.colors["input"],
            "fg": fg or self.colors["text"],
            "insertbackground": self.colors["accent"],
            "selectbackground": "#0e7490",
            "selectforeground": "#f8fafc",
            "highlightthickness": 2,
            "highlightbackground": self.colors["border"],
            "highlightcolor": self.colors["border_focus"],
            "font": font,
        }

    def _numeric_entry(
        self,
        parent: tk.Widget,
        variable: tk.StringVar,
        *,
        kind: str = "int",
        minimum: float | int | None = None,
        maximum: float | int | None = None,
        width: int = 10,
        justify: str = "left",
        font: tuple[str, int] | tuple[str, int, str] = ("Segoe UI", 10),
        fg: str | None = None,
    ) -> tk.Entry:
        if kind == "float":
            validate_command = self.register(lambda proposed: self._float_text_allowed(proposed, None if minimum is None else float(minimum), None if maximum is None else float(maximum)))
        else:
            validate_command = self.register(lambda proposed: self._integer_text_allowed(proposed, None if minimum is None else int(minimum), None if maximum is None else int(maximum)))
        return tk.Entry(
            parent,
            textvariable=variable,
            width=width,
            justify=justify,
            validate="key",
            validatecommand=(validate_command, "%P"),
            **self._numeric_widget_options(font=font, fg=fg),
        )

    def _numeric_spinbox(
        self,
        parent: tk.Widget,
        variable: tk.StringVar,
        *,
        kind: str = "int",
        from_value: float | int = 0,
        to_value: float | int = 1_000_000,
        increment: float | int = 1,
        width: int = 9,
        command: Callable[[], None] | None = None,
    ) -> ttk.Spinbox:
        if kind == "float":
            validate_command = self.register(lambda proposed: self._float_text_allowed(proposed, float(from_value), float(to_value)))
        else:
            validate_command = self.register(lambda proposed: self._integer_text_allowed(proposed, int(from_value), int(to_value)))
        options: dict[str, object] = {
            "from_": from_value,
            "to": to_value,
            "increment": increment,
            "textvariable": variable,
            "width": width,
            "validate": "key",
            "validatecommand": (validate_command, "%P"),
            "style": "TSpinbox",
        }
        if command is not None:
            options["command"] = command
        spinbox = ttk.Spinbox(parent, **options)
        return spinbox

    def _jog_step_levels_entry(self, parent: tk.Widget, variable: tk.StringVar) -> tk.Entry:
        validate_command = self.register(lambda proposed: self._jog_step_text_allowed(proposed))
        return tk.Entry(
            parent,
            textvariable=variable,
            validate="key",
            validatecommand=(validate_command, "%P"),
            **self._numeric_widget_options(),
        )

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)

        header = ttk.Frame(self, style="App.TFrame", padding=(18, 12, 18, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="Semi Auto Probe", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="3-axis RS-232 motion control with synchronized USB vision", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        toolbar = ttk.Frame(header, style="Toolbar.TFrame", padding=(10, 8))
        toolbar.grid(row=0, column=1, rowspan=2, sticky="e", padx=(16, 0))

        ttk.Label(toolbar, text="SERIAL", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 6))
        self.port_combo = ttk.Combobox(toolbar, textvariable=self.port_var, width=10, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=(0, 6), ipady=1)
        ttk.Button(toolbar, text="Refresh", style="Ghost.TButton", command=self.refresh_ports).grid(row=0, column=2, padx=(0, 6))
        ttk.Button(toolbar, text="Connect", style="Accent.TButton", command=self.connect_and_test_serial).grid(row=0, column=3, padx=(0, 6))
        ttk.Button(toolbar, text="Test", command=self.run_comm_test).grid(row=0, column=4, padx=(0, 12))

        ttk.Label(toolbar, text="CAM", style="Muted.TLabel").grid(row=0, column=5, padx=(0, 6))
        self.camera_index_spinbox = self._numeric_spinbox(toolbar, self.camera_index_var, from_value=0, to_value=8, width=3)
        self.camera_index_spinbox.grid(row=0, column=6, padx=(0, 6), ipady=1)
        ttk.Button(toolbar, text="Restart", command=self.restart_camera).grid(row=0, column=7, padx=(0, 10))
        ttk.Button(toolbar, text="EMERGENCY STOP", style="Danger.TButton", command=self.emergency_stop).grid(row=0, column=8)

        content = ttk.Frame(self, style="App.TFrame")
        content.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        self.tab_buttons: dict[str, tk.Label] = {}
        module_tab_labels = {
            "Main": "🏠 MainView",
            "AutoFocus": "🎯 AutoFocus",
            "FocusMap": "🗺 FocusMap",
            "ImgStitch": "🧩 ImgStitch",
            "LayoutBond": "📐 LayoutMap",
            "AI Agent": "🤖 AI Agent",
            "Communication": "🔌 SerialIO",
            "Config": "⚙ Settings",
        }
        tab_bar = ttk.Frame(content, style="App.TFrame")
        tab_bar.grid(row=0, column=0, sticky="w")
        for col, name in enumerate(("Main", "AutoFocus", "FocusMap", "ImgStitch", "LayoutBond", "AI Agent", "Communication", "Config")):
            tab_bar.columnconfigure(col, minsize=124, uniform="top_tabs")
            label = tk.Label(
                tab_bar,
                text=module_tab_labels[name],
                anchor="center",
                bg="#17324a" if name == "Main" else "#132236",
                fg="#f8fafc" if name == "Main" else "#c7d2e1",
                font=("Segoe UI Emoji", 12),
                padx=8,
                pady=9,
                bd=0,
                highlightthickness=1,
                highlightbackground="#22d3ee" if name == "Main" else "#31506b",
                highlightcolor="#22d3ee",
                cursor="hand2",
            )
            label.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0), pady=(0, 2))
            label.bind("<Button-1>", lambda _event, page=name: self.show_page(page))
            label.bind("<Enter>", lambda _event, page=name: self._set_tab_hover(page, True))
            label.bind("<Leave>", lambda _event, page=name: self._set_tab_hover(page, False))
            self.tab_buttons[name] = label

        page_container = ttk.Frame(content, style="App.TFrame")
        page_container.grid(row=1, column=0, sticky="nsew")
        page_container.columnconfigure(0, weight=1)
        page_container.rowconfigure(0, weight=1)

        main_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        communication_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        autofocus_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        af_plane_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        imgstitch_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        gds_stage_mapper_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        agent_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        config_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        self.pages = {
            "Main": main_page,
            "Communication": communication_page,
            "AutoFocus": autofocus_page,
            "FocusMap": af_plane_page,
            "ImgStitch": imgstitch_page,
            "LayoutBond": gds_stage_mapper_page,
            "AI Agent": agent_page,
            "Config": config_page,
        }
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self._build_main_page(main_page)
        self._build_communication_page(communication_page)
        self._build_autofocus_page(autofocus_page)
        self._build_af_plane_page(af_plane_page)
        self._build_imgstitch_page(imgstitch_page)
        self._build_gds_stage_mapper_page(gds_stage_mapper_page)
        self._build_agent_page(agent_page)
        self._build_config_page(config_page)
        self._update_config_display()
        self._update_admin_mode_controls()
        self._warm_page_layouts()
        self.show_page("Main")

    def show_page(self, name: str) -> None:
        if name not in self.pages:
            return
        if name == self.active_page_name:
            return
        if name != "Main":
            self._clear_held_keyboard_moves()
            if self.vision_panel is not None:
                self.vision_panel.set_shift_down(False)
        self.current_page = name
        page = self.pages[name]
        try:
            page.update_idletasks()
        except tk.TclError:
            pass
        page.tkraise()
        self.active_page_name = name
        for page_name, button in self.tab_buttons.items():
            selected = page_name == name
            button.configure(
                bg="#17324a" if selected else "#132236",
                fg="#f8fafc" if selected else "#c7d2e1",
                highlightbackground="#22d3ee" if selected else "#31506b",
            )
        self.after_idle(lambda page_name=name: self._refresh_after_page_switch(page_name))

    def _set_tab_hover(self, name: str, is_hovered: bool) -> None:
        button = self.tab_buttons.get(name)
        if button is None or name == self.active_page_name:
            return
        button.configure(
            bg="#1b3651" if is_hovered else "#132236",
            fg="#f8fafc" if is_hovered else "#c7d2e1",
            highlightbackground="#4f7a9d" if is_hovered else "#31506b",
        )

    def _warm_page_layouts(self) -> None:
        for page in self.pages.values():
            try:
                page.update_idletasks()
            except tk.TclError:
                pass

    def _refresh_after_page_switch(self, name: str) -> None:
        if name != self.current_page:
            return
        if name == "Main" and self.vision_panel is not None:
            self.vision_panel.draw_image()
            self.vision_panel.draw_overlay()
        elif name == "FocusMap":
            self._draw_focusmap_all()
        elif name == "LayoutBond" and self.gds_stage_mapper_panel is not None:
            self.gds_stage_mapper_panel.viewer.redraw()
        elif name == "AI Agent" and self.agent_panel is not None:
            self.agent_panel.refresh_context()

    def _build_main_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        self.main_pane = ttk.PanedWindow(parent, orient=tk.HORIZONTAL)
        self.main_pane.grid(row=0, column=0, sticky="nsew")

        camera_panel = ttk.Frame(self.main_pane, style="Panel.TFrame", padding=12)
        camera_panel.columnconfigure(0, weight=1)
        camera_panel.rowconfigure(1, weight=1)

        camera_header = ttk.Frame(camera_panel, style="Panel.TFrame")
        camera_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        camera_header.columnconfigure(0, weight=1)
        ttk.Label(camera_header, text="LIVE VISION", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        keyboard_toggle = ttk.Frame(camera_header, style="Panel.TFrame")
        keyboard_toggle.grid(row=0, column=1, sticky="e", padx=(12, 10))
        ttk.Label(keyboard_toggle, text="KeyboardMove", style="Muted.TLabel").grid(row=0, column=0, sticky="e", padx=(0, 6))
        ToggleSwitch(
            keyboard_toggle,
            self.keyboard_move_enabled_var,
            self.colors,
            command=self._on_keyboard_move_toggle,
        ).grid(row=0, column=1, sticky="e")
        ttk.Label(camera_header, text="USB camera preview", style="Muted.TLabel").grid(row=0, column=2, sticky="e")

        self.vision_panel = VisionPanel(
            camera_panel,
            self.colors,
            get_um_per_px=self.probe_config.current_um_per_px,
            move_point_to_center=self.move_image_point_to_center,
            get_centering_preview=self.image_centering_preview,
        )

        controls_panel = ttk.Frame(self.main_pane, style="Panel.TFrame", padding=10)
        controls_panel.columnconfigure(0, weight=1)
        controls_panel.rowconfigure(3, weight=1)

        self._build_position_panel(controls_panel)
        self._build_axis_control_panel(controls_panel)
        self._status_value(controls_panel, 4)
        self.main_pane.add(camera_panel, weight=1)
        self.main_pane.add(controls_panel, weight=0)
        self.controls_panel = controls_panel
        self.after_idle(self._set_initial_main_pane)

    def _build_gds_stage_mapper_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)
        self.gds_stage_mapper_panel = GDSStageMapperPanel(
            parent,
            self.colors,
            get_stage_position_um=self._gds_mapper_current_stage_um,
            move_to_stage_um=self.move_gds_mapper_target,
            move_to_stage_xyz_um=self.move_gds_mapper_stage_target,
            get_focus_z_um=self._gds_mapper_focus_z_um,
            get_microscope_preview=self.agent_microscope_preview,
            fov_width_var=self.layoutbond_fov_width_var,
            fov_height_var=self.layoutbond_fov_height_var,
            use_focus_z_var=self.main_focusmap_plane_var,
            on_focus_z_toggle=self._on_main_focusmap_plane_toggle,
            set_status=self.status_var.set,
        )

    def _build_agent_planner(self):
        return build_agent_planner_from_config(
            api_key=self.probe_config.agent_api_key,
            model=self.probe_config.agent_model,
            base_url=self.probe_config.agent_base_url,
            timeout_seconds=self.probe_config.agent_timeout_seconds,
            spec_path=self.agent_function_spec_path,
        )

    def _build_agent_page(self, parent: ttk.Frame) -> None:
        self.agent_panel = AgentPanel(
            parent,
            self.colors,
            get_context=self.agent_context,
            get_microscope_preview=self.agent_microscope_preview,
            plan_instruction=self.plan_agent_instruction,
            execute_plan=self.execute_agent_plan,
            cancel_plan=self.cancel_agent_plan,
            stop_task=self.stop_agent_task,
        )

    def agent_context(self) -> AgentContext:
        gds_target_stage_um: tuple[float, float] | None = None
        gds_selected_uv: tuple[float, float] | None = None
        current_mapped_gds_uv: tuple[float, float] | None = None
        gds_mapping_ready = False
        gds_target_selected = False
        stage_position_um = {
            "X": self.current_position_values["X"] * self.probe_config.um_per_pulse("X"),
            "Y": self.current_position_values["Y"] * self.probe_config.um_per_pulse("Y"),
            "Z": self.current_position_values["Z"] * self.probe_config.um_per_pulse("Z"),
        }
        if self.gds_stage_mapper_panel is not None:
            gds_selected_uv = self.gds_stage_mapper_panel.selected_target_gds
            gds_target_stage_um = self.gds_stage_mapper_panel.selected_target_stage_um
            gds_mapping_ready = self.gds_stage_mapper_panel.mapper is not None
            gds_target_selected = gds_selected_uv is not None
            if self.gds_stage_mapper_panel.mapper is not None:
                try:
                    current_mapped_gds_uv = self.gds_stage_mapper_panel.mapper.stage_to_gds(stage_position_um["X"], stage_position_um["Y"])
                except Exception:
                    current_mapped_gds_uv = None

        last_stitch = self._latest_stitch_image_path()
        return AgentContext(
            positions=dict(self.current_position_values),
            serial_connected=bool(self.serial_client and self.serial_client.is_open),
            motion_busy=self.motion_busy,
            keyboard_motion_busy=self.keyboard_motion_busy,
            position_read_pending=self.position_read_pending,
            camera_running=self.camera_running,
            camera_frame_available=self.latest_camera_frame is not None,
            autofocus_running=self.autofocus_running,
            focusmap_running=self.af_plane_running,
            imgstitch_running=self.imgstitch_running,
            gds_target_selected=gds_target_selected,
            gds_mapping_ready=gds_mapping_ready,
            stage_position_um=stage_position_um,
            gds_selected_uv=gds_selected_uv,
            current_mapped_gds_uv=current_mapped_gds_uv,
            gds_target_stage_um=gds_target_stage_um,
            last_stitch_path=str(last_stitch) if last_stitch is not None else None,
            current_page=self.current_page,
            config_summary={
                "Objective": f"{self.probe_config.objective:g}x",
                "Eyepiece": f"{self.probe_config.eyepiece:g}x",
                "XY um/pulse": f"{self.probe_config.um_per_pulse('X'):.6g}",
                "Z um/pulse": f"{self.probe_config.um_per_pulse('Z'):.6g}",
                "Motor speed profile": self._motor_speed_profile_label(self.probe_config.active_motor_speed_profile),
                "Motor speed percent": f"{self.probe_config.motor_speed_percent()}",
            },
        )

    def _latest_stitch_image_path(self) -> Path | None:
        candidates = (
            Path.cwd() / "last_imgstitch.png",
            self.imgstitch_session_dir / "last_imgstitch.png",
            self.imgstitch_session_dir / "stack_result.png",
        )
        for path in candidates:
            if path.exists():
                return path
        return None

    def agent_microscope_preview(self) -> bytes | None:
        with self.camera_lock:
            frame = None if self.latest_stitch_frame is None else self.latest_stitch_frame.copy()
        if frame is None:
            return None
        try:
            import cv2

            height, width = frame.shape[:2]
            scale = min(520 / max(width, 1), 292 / max(height, 1), 1.0)
            if scale < 1.0:
                frame = cv2.resize(frame, (int(width * scale), int(height * scale)), interpolation=cv2.INTER_AREA)
            height, width = frame.shape[:2]
            center_x = width // 2
            center_y = height // 2
            cv2.line(frame, (center_x - 18, center_y), (center_x + 18, center_y), (45, 212, 191), 1, cv2.LINE_AA)
            cv2.line(frame, (center_x, center_y - 18), (center_x, center_y + 18), (45, 212, 191), 1, cv2.LINE_AA)
            cv2.rectangle(frame, (8, 8), (width - 9, height - 9), (51, 65, 85), 1)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            return f"P6 {width} {height} 255\n".encode("ascii") + rgb.tobytes()
        except Exception:
            return None

    def plan_agent_instruction(self, instruction: str) -> AgentPlan:
        plan = self.agent_planner.plan(instruction, self.agent_context())
        self.status_var.set(f"Agent plan: {plan.title}")
        return plan

    def cancel_agent_plan(self) -> str:
        self.status_var.set("Agent plan cancelled.")
        return "Pending Agent plan cancelled."

    def stop_agent_task(self) -> str:
        stopped: list[str] = []
        if self.imgstitch_running:
            self.stop_imgstitch()
            stopped.append("ImgStitch")
        if self.autofocus_running:
            self.stop_autofocus()
            stopped.append("AutoFocus")
        if self.af_plane_running:
            self.stop_af_plane_mapping()
            stopped.append("FocusMap")
        if stopped:
            message = f"Agent stop requested for {', '.join(stopped)}."
        else:
            message = "No Agent-controlled workflow is currently running."
        self.status_var.set(message)
        return message

    def execute_agent_plan(self, plan: AgentPlan) -> str:
        blockers = self._agent_execution_blockers(plan.action)
        if blockers:
            message = "Agent plan blocked: " + "; ".join(blockers)
            self.status_var.set(message)
            return message

        context = self.agent_context()
        if plan.action == AGENT_ACTION_MOVE_GDS:
            assert context.gds_target_stage_um is not None
            self.move_gds_mapper_target(*context.gds_target_stage_um)
            return "Agent started LayoutBond move to selected GDS target."
        if plan.action == AGENT_ACTION_AUTOFOCUS:
            self.start_autofocus()
            return "Agent started AutoFocus at the current position."
        if plan.action == AGENT_ACTION_IMAGE_CAPTURE:
            self.start_imgstitch()
            return "Agent started the current image acquisition sequence."
        if plan.action == AGENT_ACTION_LAYOUT_OVERLAY:
            last_stitch = context.last_stitch_path or "-"
            message = f"Latest image associated with LayoutBond context: {last_stitch}"
            self._set_gds_mapper_status(message)
            self.show_page("LayoutBond")
            return message

        message = "Agent plan is not executable."
        self.status_var.set(message)
        return message

    def _agent_execution_blockers(self, action: str) -> list[str]:
        context = self.agent_context()
        blockers: list[str] = []
        motion_workflow_running = context.motion_busy or context.keyboard_motion_busy or context.position_read_pending
        other_workflow_running = context.autofocus_running or context.focusmap_running or context.imgstitch_running
        if action in {AGENT_ACTION_MOVE_GDS, AGENT_ACTION_AUTOFOCUS, AGENT_ACTION_IMAGE_CAPTURE}:
            if motion_workflow_running:
                blockers.append("motion is busy")
            if context.autofocus_running:
                blockers.append("AutoFocus is already running")
            if context.focusmap_running:
                blockers.append("FocusMap is already running")
            if context.imgstitch_running:
                blockers.append("ImgStitch is already running")
            if not context.serial_connected:
                blockers.append("serial port is not connected")
        elif action == AGENT_ACTION_LAYOUT_OVERLAY:
            if motion_workflow_running or other_workflow_running:
                blockers.append("another workflow is running")

        if action == AGENT_ACTION_MOVE_GDS:
            if not context.gds_target_selected or context.gds_target_stage_um is None:
                blockers.append("no GDS target is selected")
            if not context.gds_mapping_ready:
                blockers.append("GDS binding is not ready")
        elif action == AGENT_ACTION_AUTOFOCUS:
            if not context.camera_running or not context.camera_frame_available:
                blockers.append("camera frame is not available")
        elif action == AGENT_ACTION_IMAGE_CAPTURE:
            if not context.camera_running or not context.camera_frame_available:
                blockers.append("camera frame is not available")
        elif action == AGENT_ACTION_LAYOUT_OVERLAY:
            if not context.gds_mapping_ready:
                blockers.append("GDS binding is not ready")
            if not context.last_stitch_path:
                blockers.append("no recent stitched image is available")
        else:
            blockers.append("unsupported Agent action")
        return blockers

    def _set_initial_main_pane(self) -> None:
        try:
            width = self.main_pane.winfo_width()
            if width > 500:
                self.main_pane.sashpos(0, max(width - 380, 520))
        except tk.TclError:
            pass

    def move_image_point_to_center(self, point_x: float, point_y: float, image_width: int, image_height: int) -> None:
        if self.motion_busy:
            self.status_var.set("Motion is busy; image-center move skipped.")
            return
        if not self.camera_image:
            if self.vision_panel:
                self.vision_panel.status_var.set("No camera image is available.")
            return
        um_per_px = self.probe_config.current_um_per_px()
        if um_per_px is None or um_per_px <= 0:
            if self.vision_panel:
                self.vision_panel.status_var.set("Pixel calibration is required before image-center move.")
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        try:
            plan = self._image_centering_cc_plan(point_x, point_y, image_width, image_height, um_per_px)
        except ValueError as exc:
            if self.vision_panel:
                self.vision_panel.status_var.set(str(exc))
            return
        if not plan["has_motion"]:
            if self.vision_panel:
                self.vision_panel.status_var.set("Selected point is within less than 0.5 pulse of image center.")
            return

        self.motion_busy = True
        self._show_target_positions(plan["target_positions"])
        self.status_var.set(
            "Moving image point to center by CC: "
            f"{plan['preview_text'].replace(chr(10), '; ')}"
        )
        if self.vision_panel:
            self.vision_panel.status_var.set(f"CC move running: {plan['preview_text'].replace(chr(10), '; ')}")
        threading.Thread(target=self._move_vision_center_worker, args=(plan["axis_params"], plan["target_positions"]), daemon=True).start()

    def _image_centering_move(
        self,
        point_x: float,
        point_y: float,
        image_width: int,
        image_height: int,
        um_per_px: float,
    ) -> dict[str, tuple[float, int, bool] | float]:
        if image_width <= 0 or image_height <= 0:
            raise ValueError("Camera image size is invalid.")
        if um_per_px <= 0:
            raise ValueError("Pixel calibration must be positive.")

        image_dx_px = point_x - image_width / 2.0
        image_dy_px = point_y - image_height / 2.0
        stage_x_um = image_dx_px * um_per_px
        stage_y_um = -image_dy_px * um_per_px
        return {
            "image_dx_px": image_dx_px,
            "image_dy_px": image_dy_px,
            "X": self._signed_stage_um_to_pulses(stage_x_um, "X"),
            "Y": self._signed_stage_um_to_pulses(stage_y_um, "Y"),
        }

    def _signed_stage_um_to_pulses(self, stage_um: float, axis: str) -> tuple[float, int, bool]:
        pulses_per_um = self.probe_config.pulses_per_um(axis)
        if pulses_per_um <= 0:
            raise ValueError(f"{axis} pulse-per-um must be positive.")
        pulses = int(round(abs(stage_um) * pulses_per_um))
        return stage_um, pulses, stage_um < 0

    def _cc_axis_param(self, reverse: bool, pulses: int) -> tuple[bool, int, int, int]:
        speed = self._motion_speed_percent() if pulses else 0
        return reverse, pulses, speed, self.probe_config.cc_acceleration_units()

    def _image_centering_cc_plan(
        self,
        point_x: float,
        point_y: float,
        image_width: int,
        image_height: int,
        um_per_px: float,
    ) -> dict[str, object]:
        move = self._image_centering_move(point_x, point_y, image_width, image_height, um_per_px)
        axis_params: dict[Axis, tuple[bool, int, int, int]] = {}
        target_positions: dict[str, int] = {}
        signed_pulses: dict[str, int] = {}
        has_motion = False

        for axis_name, controller_axis in (("X", Axis.X), ("Y", Axis.Y)):
            _stage_um, pulses, reverse = move[axis_name]
            signed_pulses[axis_name] = -pulses if reverse else pulses
            axis_params[controller_axis] = self._cc_axis_param(reverse, pulses)
            if pulses:
                has_motion = True
                target_positions[axis_name] = self.current_position_values[axis_name] + signed_pulses[axis_name]

        preview_text = self._format_image_centering_preview(move, signed_pulses)
        return {
            "move": move,
            "axis_params": axis_params,
            "target_positions": target_positions,
            "signed_pulses": signed_pulses,
            "has_motion": has_motion,
            "preview_text": preview_text,
        }

    def _format_image_centering_preview(self, move: dict[str, tuple[float, int, bool] | float], signed_pulses: dict[str, int]) -> str:
        x_um, _x_pulses, _x_reverse = move["X"]
        y_um, _y_pulses, _y_reverse = move["Y"]
        return (
            f"dX {move['image_dx_px']:+.1f}px  X {x_um:+.2f}um  CC {signed_pulses['X']:+d}p\n"
            f"dY {move['image_dy_px']:+.1f}px  Y {y_um:+.2f}um  CC {signed_pulses['Y']:+d}p"
        )

    def image_centering_preview(self, point_x: float, point_y: float, image_width: int, image_height: int) -> str:
        um_per_px = self.probe_config.current_um_per_px()
        if um_per_px is None or um_per_px <= 0:
            image_dx_px = point_x - image_width / 2.0
            image_dy_px = point_y - image_height / 2.0
            return f"dX {image_dx_px:+.1f}px\n" f"dY {image_dy_px:+.1f}px\nNo calibration"
        try:
            plan = self._image_centering_cc_plan(point_x, point_y, image_width, image_height, um_per_px)
        except ValueError as exc:
            return str(exc)
        return str(plan["preview_text"])

    def _move_vision_center_worker(self, axis_params: dict[Axis, tuple[bool, int, int, int]], expected_targets: dict[str, int]) -> None:
        assert self.serial_client is not None
        try:
            command, completed = self.serial_client.move_multi_axis_relative_and_wait(axis_params, timeout=self._cc_move_timeout(axis_params))
            self.result_queue.put(("motor_command", "XY", "cc image center", command, "vision"))
            self.result_queue.put(("cc_done", completed, "vision"))
            self.result_queue.put(("moving",))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "vision", expected_targets))
        except Exception as exc:
            self.result_queue.put(("motor_error", "XY", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _gds_mapper_current_stage_um(self) -> tuple[float, float, float]:
        return (
            self.current_position_values["X"] * self.probe_config.um_per_pulse("X"),
            self.current_position_values["Y"] * self.probe_config.um_per_pulse("Y"),
            self.current_position_values["Z"] * self.probe_config.um_per_pulse("Z"),
        )

    def _gds_mapper_focus_z_um(self, target_x_um: float, target_y_um: float) -> float | None:
        x_pulses = int(round(target_x_um / self.probe_config.um_per_pulse("X")))
        y_pulses = int(round(target_y_um / self.probe_config.um_per_pulse("Y")))
        z_pulses = self._focusmap_z_target_at_xy(x_pulses, y_pulses)
        if z_pulses is None:
            return None
        return z_pulses * self.probe_config.um_per_pulse("Z")

    def _gds_mapper_motion_blocker(self) -> str | None:
        if self.motion_busy or self.keyboard_motion_busy:
            return "Motion is busy; LayoutBond move skipped."
        if self.autofocus_running:
            return "AutoFocus is running; LayoutBond move skipped."
        if self.af_plane_running:
            return "FocusMap is running; LayoutBond move skipped."
        if self.imgstitch_running:
            return "ImgStitch is running; LayoutBond move skipped."
        if self.position_read_pending:
            return "Position read is pending; LayoutBond move skipped."
        return None

    def _set_gds_mapper_status(self, message: str) -> None:
        self.status_var.set(message)
        if self.gds_stage_mapper_panel is not None:
            self.gds_stage_mapper_panel.set_motion_status(message)

    def move_gds_mapper_target(self, target_x_um: float, target_y_um: float) -> None:
        target_z_um = None
        if self.main_focusmap_plane_var.get():
            target_z_um = self._gds_mapper_focus_z_um(target_x_um, target_y_um)
            if target_z_um is None:
                self._set_gds_mapper_status("FocusMap Z is enabled, but no FocusMap plane is stored.")
                return
        self.move_gds_mapper_stage_target(target_x_um, target_y_um, target_z_um)

    def move_gds_mapper_stage_target(self, target_x_um: float, target_y_um: float, target_z_um: float | None = None) -> None:
        blocker = self._gds_mapper_motion_blocker()
        if blocker:
            self._set_gds_mapper_status(blocker)
            logger.warning(blocker)
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            self._set_gds_mapper_status("Serial is not connected; LayoutBond move skipped.")
            return

        try:
            target_um = {"X": target_x_um, "Y": target_y_um}
            scales = {
                "X": self.probe_config.um_per_pulse("X"),
                "Y": self.probe_config.um_per_pulse("Y"),
            }
            if target_z_um is not None:
                target_um["Z"] = target_z_um
                scales["Z"] = self.probe_config.um_per_pulse("Z")
            plan = stage_xyz_move_plan_from_um(
                {axis: self.current_position_values[axis] for axis in target_um},
                target_um,
                scales,
            )
        except ValueError as exc:
            self._set_gds_mapper_status(f"LayoutBond target invalid: {exc}")
            logger.warning("LayoutBond target rejected: %s", exc)
            return

        if not plan.has_motion:
            self._set_gds_mapper_status("Stage is already at the selected GDS target.")
            return

        self.motion_busy = True
        self.clear_position_edits()
        self._show_target_positions(plan.target_pulses)
        z_text = "" if target_z_um is None else f", Z {target_z_um:.6g} um"
        self._set_gds_mapper_status(f"LayoutBond moving to X {target_x_um:.6g} um, Y {target_y_um:.6g} um{z_text}.")
        threading.Thread(target=self._gds_mapper_move_worker, args=(target_x_um, target_y_um, target_z_um), daemon=True).start()

    def _gds_mapper_move_worker(self, target_x_um: float, target_y_um: float, target_z_um: float | None = None) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_stable_xyz_positions()
            target_axes = {"X", "Y"} if target_z_um is None else {"X", "Y", "Z"}
            running_axes = [entry[2].axis_name for entry in entries if entry[2].axis_name in target_axes and entry[2].is_running]
            if running_axes:
                raise RuntimeError(f"Stage is currently moving on {', '.join(running_axes)}.")

            current_pulses = {axis: self._axis_from_position_entries(entries, self._controller_axis(axis)) for axis in target_axes}
            target_um = {"X": target_x_um, "Y": target_y_um}
            scales = {"X": self.probe_config.um_per_pulse("X"), "Y": self.probe_config.um_per_pulse("Y")}
            if target_z_um is not None:
                target_um["Z"] = target_z_um
                scales["Z"] = self.probe_config.um_per_pulse("Z")
            plan = stage_xyz_move_plan_from_um(
                current_pulses,
                target_um,
                scales,
            )
            if not plan.has_motion:
                self.result_queue.put(("gds_mapper_status", "Stage is already at the selected GDS target."))
                self.result_queue.put(("read_positions", entries, "gds_mapper"))
                return

            axis_params = {}
            for axis_name, delta in plan.deltas.items():
                controller_axis = self._controller_axis(axis_name)
                if controller_axis is not None:
                    axis_params[controller_axis] = self._cc_axis_param(delta < 0, abs(delta))
            command, completed = self.serial_client.move_multi_axis_relative_and_wait(axis_params, timeout=self._cc_move_timeout(axis_params))
            self.result_queue.put(("motor_command", "".join(sorted(plan.deltas)), "cc LayoutBond", command, "gds_mapper"))
            self.result_queue.put(("cc_done", completed, "gds_mapper"))
            self.result_queue.put(("moving",))
            updated_entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", updated_entries, "gds_mapper", plan.target_pulses))
        except Exception as exc:
            self.result_queue.put(("motor_error", "GDS_MAPPER", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _build_position_panel(self, parent: ttk.Frame) -> None:
        panel = ttk.Frame(parent, style="Panel.TFrame")
        panel.grid(row=0, column=0, sticky="ew")
        for col in range(3):
            panel.columnconfigure(col, weight=1, uniform="position", minsize=84)

        header = ttk.Frame(panel, style="Panel.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 8))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="POSITION", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Button(header, text="Move", style="Accent.TButton", command=self.move_edited_positions).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(header, text="Read", command=self.read_current_position).grid(row=0, column=2, padx=(6, 0))
        ttk.Button(header, textvariable=self.realtime_button_var, command=self.toggle_realtime_position).grid(row=0, column=3, padx=(6, 0))
        for col, axis in enumerate(("X", "Y", "Z")):
            cell = ttk.Frame(panel, style="Panel.TFrame")
            cell.grid(row=1, column=col, sticky="ew", padx=(0 if col == 0 else 6, 0 if col == 2 else 6))
            cell.columnconfigure(0, weight=1, minsize=84)
            ttk.Label(cell, text=axis, style="Muted.TLabel").grid(row=0, column=0, sticky="w")
            entry = self._numeric_entry(
                cell,
                self.position_vars[axis],
                minimum=None,
                maximum=None,
                justify="center",
                fg=self.colors["accent"],
                font=("Cascadia Mono", 15, "bold"),
                width=7,
            )
            entry.configure(state="readonly", readonlybackground=self.colors["surface_2"])
            entry.grid(row=1, column=0, sticky="ew", pady=(4, 0), ipady=9)
            entry.bind("<Button-1>", lambda _event, a=axis: self.schedule_position_edit(a, "Relative"))
            entry.bind("<Double-Button-1>", lambda _event, a=axis: self.begin_position_edit(a, "Absolute"))
            entry.bind("<Tab>", lambda event, a=axis: self.focus_next_position_input(a, event))
            entry.bind("<Shift-Tab>", lambda event, a=axis: self.focus_previous_position_input(a, event))
            entry.bind("<Return>", lambda _event: self.move_edited_positions())
            entry.bind("<Escape>", lambda _event: self.clear_position_edits())
            self.position_inputs[axis] = entry

    def _build_axis_control_panel(self, parent: ttk.Frame) -> None:
        axes = ttk.LabelFrame(parent, text="ELECTRODE CONTROL")
        axes.grid(row=1, column=0, sticky="ew", pady=(12, 0))
        axes.columnconfigure(0, weight=1)

        mode_bar = ttk.Frame(axes, style="Panel.TFrame")
        mode_bar.grid(row=0, column=0, sticky="ew", padx=8, pady=(8, 2))
        mode_bar.columnconfigure(0, weight=1)
        ttk.Label(mode_bar, text="Click: relative | Double-click: absolute | Enter: Move", style="Muted.TLabel", wraplength=280).grid(row=0, column=0, sticky="ew")

        for row_index, (axis, label, color) in enumerate((
            ("X", "X Electrode / Axis 1", "#60a5fa"),
            ("Y", "Y Electrode / Axis 2", "#34d399"),
            ("Z", "Z Electrode / Axis 3", "#fbbf24"),
        )):
            self._axis_control_row(axes, row_index + 1, axis, label, color)

        zero_bar = ttk.Frame(axes, style="Panel.TFrame")
        zero_bar.grid(row=4, column=0, sticky="ew", padx=8, pady=(10, 8))
        zero_bar.columnconfigure((0, 1, 2), weight=1, uniform="zero_bar")
        ttk.Button(zero_bar, textvariable=self.home_signal_button_var, command=self.toggle_home_signal_polling).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        self.set_xyz_zero_button = ttk.Button(zero_bar, text="Set New Zero", style="Accent.TButton", command=self.set_xyz_zero)
        self.set_xyz_zero_button.grid(row=0, column=1, sticky="ew", padx=(4, 4))
        ttk.Button(zero_bar, text="Go Zero", command=self.go_xyz_zero).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        speed_bar = ttk.Frame(axes, style="Panel.TFrame")
        speed_bar.grid(row=5, column=0, sticky="ew", padx=8, pady=(0, 8))
        speed_bar.columnconfigure(1, weight=1)
        ttk.Label(speed_bar, text="Speed Profile", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        speed_combo = ttk.Combobox(
            speed_bar,
            textvariable=self.motor_speed_profile_var,
            values=[MOTOR_SPEED_PROFILE_LABELS[profile] for profile in MOTOR_SPEED_PROFILES],
            state="readonly",
            width=16,
        )
        speed_combo.grid(row=0, column=1, sticky="ew", padx=(10, 0))
        speed_combo.bind("<<ComboboxSelected>>", self._on_motor_speed_profile_selected)

        focusmap_toggle = ttk.Frame(axes, style="Panel.TFrame")
        focusmap_toggle.grid(row=6, column=0, sticky="ew", padx=8, pady=(0, 10))
        focusmap_toggle.columnconfigure(0, weight=1)
        ttk.Label(focusmap_toggle, text="FocusMap Z", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ToggleSwitch(
            focusmap_toggle,
            self.main_focusmap_plane_var,
            self.colors,
            command=self._on_main_focusmap_plane_toggle,
        ).grid(row=0, column=1, sticky="e")

    def _axis_control_row(self, parent: ttk.Frame, row_index: int, axis: str, label: str, color: str) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame", padding=(8, 6))
        row.grid(row=row_index, column=0, sticky="ew", padx=8, pady=(8 if row_index == 0 else 3, 4))
        row.columnconfigure(1, weight=1)

        marker = tk.Canvas(row, width=10, height=10, bg=self.colors["surface"], highlightthickness=0)
        marker_item = marker.create_oval(1, 1, 9, 9, fill=self.colors["muted"], outline=self.colors["muted"])
        marker.grid(row=0, column=0, rowspan=2, padx=(0, 8))
        self.axis_indicator_canvases[axis] = marker
        self.axis_indicator_items[axis] = marker_item
        self.axis_indicator_colors[axis] = color

        ttk.Label(row, text=label, style="Panel.TLabel").grid(row=0, column=1, sticky="w")
        ttk.Label(row, text="Jog Step", style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(4, 0))
        self._numeric_spinbox(row, self.step_vars[axis], from_value=1, to_value=1_000_000, width=7).grid(row=1, column=2, sticky="ew", padx=(6, 6), pady=(4, 0))
        fwd_button = ttk.Button(row, text="Fwd", style="Accent.TButton", command=lambda a=axis: self.axis_forward(a))
        fwd_button.grid(row=0, column=2, sticky="ew", padx=(6, 0))
        rev_button = ttk.Button(row, text="Rev", command=lambda a=axis: self.axis_reverse(a))
        rev_button.grid(row=0, column=3, sticky="ew", padx=(6, 0))
        stop_button = ttk.Button(row, text="Stop", style="Ghost.TButton", command=lambda a=axis: self.axis_stop(a))
        stop_button.grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(4, 0))
        self.axis_control_buttons[axis] = [fwd_button, rev_button, stop_button]

    def _build_communication_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(2, weight=1)

        command_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        command_panel.grid(row=0, column=0, sticky="ew", pady=(0, 12))
        command_panel.columnconfigure(1, weight=1)
        ttk.Label(command_panel, text="MANUAL COMMAND", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(command_panel, textvariable=self.comm_note_var, style="Muted.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0))

        mode_bar = ttk.Frame(command_panel, style="Panel.TFrame")
        mode_bar.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(10, 8))
        ttk.Radiobutton(mode_bar, text="Hex", variable=self.comm_input_mode_var, value="Hex", command=self._update_comm_note).grid(row=0, column=0, sticky="w")
        ttk.Radiobutton(mode_bar, text="Text", variable=self.comm_input_mode_var, value="Text", command=self._update_comm_note).grid(row=0, column=1, sticky="w", padx=(10, 0))
        ttk.Label(mode_bar, text="Read bytes", style="Muted.TLabel").grid(row=0, column=2, sticky="w", padx=(22, 6))
        self._numeric_spinbox(mode_bar, self.comm_read_length_var, from_value=0, to_value=4096, width=6).grid(row=0, column=3, sticky="w")
        ttk.Button(mode_bar, text="Load Test", style="Ghost.TButton", command=self.load_default_comm_test).grid(row=0, column=4, sticky="e", padx=(18, 0))
        ttk.Button(mode_bar, text="Send", style="Accent.TButton", command=self.send_manual_command).grid(row=0, column=5, sticky="e", padx=(8, 0))
        ttk.Button(mode_bar, text="Clear", command=self.clear_hex_history).grid(row=0, column=6, sticky="e", padx=(8, 0))

        self.comm_input = tk.Text(
            command_panel,
            bg=self.colors["input"],
            fg=self.colors["text"],
            insertbackground=self.colors["accent"],
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["border_focus"],
            wrap="word",
            font=("Cascadia Mono", 10),
            padx=10,
            pady=8,
            height=3,
        )
        self.comm_input.grid(row=2, column=0, columnspan=2, sticky="ew")
        self.comm_input.insert("1.0", hex_bytes(COMM_TEST_COMMAND))
        self.comm_input.bind("<Control-Return>", lambda _event: self.send_manual_command())

        summary = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        summary.grid(row=1, column=0, sticky="ew", pady=(0, 12))
        summary.columnconfigure((0, 1), weight=1, uniform="comm_summary")
        ttk.Label(summary, text="LAST TX", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(summary, text="LAST RX", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(summary, textvariable=self.tx_var, style="Value.TLabel", wraplength=480, padding=10).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0))
        ttk.Label(summary, textvariable=self.rx_var, style="Value.TLabel", wraplength=480, padding=10).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))

        history_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        history_panel.grid(row=2, column=0, sticky="nsew")
        history_panel.columnconfigure(0, weight=1)
        history_panel.rowconfigure(1, weight=1)
        ttk.Label(history_panel, text="HEX COMMUNICATION HISTORY", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))

        self.hex_history = tk.Text(
            history_panel,
            bg=self.colors["input"],
            fg=self.colors["text"],
            insertbackground=self.colors["accent"],
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            relief="flat",
            bd=0,
            highlightthickness=2,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors["border_focus"],
            wrap="word",
            font=("Cascadia Mono", 10),
            padx=12,
            pady=12,
            height=12,
        )
        self.hex_history.grid(row=1, column=0, sticky="nsew")
        self.hex_history.tag_configure("tx", foreground="#4ade80")
        self.hex_history.tag_configure("rx", foreground="#60a5fa")
        self.hex_history.tag_configure("head", foreground="#67e8f9")
        self.hex_history.tag_configure("function", foreground="#fbbf24")
        self.hex_history.tag_configure("axis", foreground="#c4b5fd")
        self.hex_history.tag_configure("data", foreground="#e5edf5")
        self.hex_history.tag_configure("checksum", foreground="#fb7185")
        self.hex_history.tag_configure("tail", foreground="#8fa0b3")
        self.hex_history.tag_configure("label", foreground="#8fa0b3")
        self.hex_history.configure(state="disabled")

    def _build_autofocus_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(0, weight=1)

        monitor_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        monitor_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        monitor_panel.columnconfigure(0, weight=1)
        monitor_panel.rowconfigure(1, weight=3)
        monitor_panel.rowconfigure(3, weight=2)

        video_header = ttk.Frame(monitor_panel, style="Panel.TFrame")
        video_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        video_header.columnconfigure(0, weight=1)
        ttk.Label(video_header, text="AUTOFOCUS VISION", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(video_header, textvariable=self.focus_score_var, style="Status.TLabel", padding=(10, 4)).grid(row=0, column=1, sticky="e")
        sample_panel = ttk.Frame(monitor_panel, style="Panel.TFrame")
        sample_panel.grid(row=1, column=0, sticky="nsew")
        sample_panel.columnconfigure(0, weight=3)
        sample_panel.columnconfigure(1, weight=2)
        sample_panel.rowconfigure(0, weight=1)
        self.autofocus_video_label = ttk.Label(sample_panel, anchor="center", text="Camera preview", style="Video.TLabel")
        self.autofocus_video_label.grid(row=0, column=0, sticky="nsew", padx=(0, 10))
        self.z_score_canvas = tk.Canvas(sample_panel, bg=self.colors["surface_2"], highlightthickness=0)
        self.z_score_canvas.grid(row=0, column=1, sticky="nsew")
        self.z_score_canvas.bind("<Configure>", lambda _event: self._draw_autofocus_z_score())

        graph_header = ttk.Frame(monitor_panel, style="Panel.TFrame")
        graph_header.grid(row=2, column=0, sticky="ew", pady=(12, 10))
        graph_header.columnconfigure(0, weight=1)
        ttk.Label(graph_header, text="FOCUS SCORE HISTORY", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(graph_header, text="Window", style="Muted.TLabel").grid(row=0, column=1, sticky="e", padx=(0, 6))
        window_spin = self._numeric_spinbox(graph_header, self.focus_window_var, from_value=5, to_value=600, increment=5, width=6, command=self._draw_focus_history)
        window_spin.grid(row=0, column=2, sticky="e")
        ttk.Label(graph_header, text="s", style="Muted.TLabel").grid(row=0, column=3, sticky="e", padx=(4, 0))
        window_spin.bind("<Return>", lambda _event: self._draw_focus_history())
        window_spin.bind("<FocusOut>", lambda _event: self._draw_focus_history())
        self.focus_canvas = tk.Canvas(monitor_panel, bg=self.colors["surface_2"], highlightthickness=0)
        self.focus_canvas.grid(row=3, column=0, sticky="nsew")
        self.focus_canvas.bind("<Configure>", lambda _event: self._draw_focus_history())

        control_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        control_panel.grid(row=0, column=1, sticky="ns")
        control_panel.columnconfigure(0, weight=1)
        ttk.Label(control_panel, text="AUTOFOCUS", style="Section.TLabel").grid(row=0, column=0, sticky="w")

        self.autofocus_z_label = tk.Label(
            control_panel,
            textvariable=self.autofocus_z_var,
            anchor="center",
            bg=self.colors["surface_2"],
            fg=self.colors["accent"],
            font=("Cascadia Mono", 20, "bold"),
            padx=12,
            pady=12,
        )
        self.autofocus_z_label.grid(row=1, column=0, sticky="ew", pady=(10, 2))

        ttk.Label(control_panel, text="Metric", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=(14, 4))
        metric_combo = ttk.Combobox(control_panel, textvariable=self.focus_metric_var, values=("Laplacian", "Tenengrad", "Brenner"), state="readonly", width=16)
        metric_combo.grid(row=3, column=0, sticky="ew")
        metric_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_focus_metric_changed())

        ttk.Label(control_panel, text="Initial Step", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(12, 4))
        self._numeric_spinbox(control_panel, self.autofocus_step_var, from_value=1, to_value=10000, width=10).grid(row=5, column=0, sticky="ew")
        ttk.Label(control_panel, text="Min Step", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=(12, 4))
        self._numeric_spinbox(control_panel, self.autofocus_min_step_var, from_value=1, to_value=10000, width=10).grid(row=7, column=0, sticky="ew")
        ttk.Label(control_panel, text="Search Range (+/-)", style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=(12, 4))
        self._numeric_spinbox(control_panel, self.autofocus_max_moves_var, from_value=1, to_value=1_000_000, increment=10, width=10).grid(row=9, column=0, sticky="ew")

        manual = ttk.Frame(control_panel, style="Panel.TFrame")
        manual.grid(row=10, column=0, sticky="ew", pady=(16, 0))
        manual.columnconfigure((0, 1), weight=1, uniform="af_manual")
        ttk.Button(manual, text="Z-", command=lambda: self.autofocus_manual_z(reverse=True)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(manual, text="Z+", style="Accent.TButton", command=lambda: self.autofocus_manual_z(reverse=False)).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Button(control_panel, text="Start Auto", style="Accent.TButton", command=self.start_autofocus).grid(row=11, column=0, sticky="ew", pady=(16, 0))
        self.set_autofocus_z_zero_button = ttk.Button(control_panel, text="Set Z=0", command=self.set_autofocus_z_zero)
        self.set_autofocus_z_zero_button.grid(row=12, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(control_panel, text="Stop", style="Danger.TButton", command=self.stop_autofocus).grid(row=13, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(control_panel, textvariable=self.autofocus_status_var, style="Status.TLabel", wraplength=190, padding=10).grid(row=14, column=0, sticky="ew", pady=(16, 0))

    def _build_af_plane_page(self, parent: ttk.Frame) -> None:
        # FocusMap is a peer workflow to AutoFocus and ImgStitch:
        # mesh setup on the right, live point/result tracking on the left.
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(0, weight=1)

        result_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        result_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        result_panel.columnconfigure(0, weight=1)
        result_panel.rowconfigure(1, weight=2)
        result_panel.rowconfigure(3, weight=4)

        header = ttk.Frame(result_panel, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="FOCUSMAP", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.af_plane_status_var, style="Status.TLabel", padding=(10, 4)).grid(row=0, column=1, sticky="e")

        top_panel = ttk.Frame(result_panel, style="Panel.TFrame")
        top_panel.grid(row=1, column=0, sticky="nsew")
        top_panel.columnconfigure((0, 1, 2), weight=1, uniform="focusmap_top")
        top_panel.rowconfigure(1, weight=1)

        for column, title in enumerate(("MESH", "REALTIME", "AF SCATTER / FIT")):
            ttk.Label(top_panel, text=title, style="Section.TLabel").grid(row=0, column=column, sticky="w", padx=(0 if column == 0 else 8, 0), pady=(0, 6))
        self.focusmap_mesh_canvas = tk.Canvas(top_panel, bg="#071018", highlightthickness=1, highlightbackground="#334155")
        self.focusmap_mesh_canvas.grid(row=1, column=0, sticky="nsew", padx=(0, 8))
        self.focusmap_mesh_canvas.bind("<Configure>", lambda _event: self._draw_af_plane_mesh())
        self.focusmap_mesh_canvas.bind("<Button-1>", self._on_focusmap_mesh_click)
        self.focusmap_realtime_canvas = tk.Canvas(top_panel, bg="#071018", highlightthickness=1, highlightbackground="#334155")
        self.focusmap_realtime_canvas.grid(row=1, column=1, sticky="nsew", padx=(4, 4))
        self.focusmap_realtime_canvas.bind("<Configure>", lambda _event: self._draw_focusmap_realtime())
        self.focusmap_af_canvas = tk.Canvas(top_panel, bg=self.colors["surface_2"], highlightthickness=1, highlightbackground="#334155")
        self.focusmap_af_canvas.grid(row=1, column=2, sticky="nsew", padx=(8, 0))
        self.focusmap_af_canvas.bind("<Configure>", lambda _event: self._draw_focusmap_af_scatter())

        progress_panel = ttk.Frame(result_panel, style="Panel.TFrame")
        progress_panel.grid(row=2, column=0, sticky="ew", pady=(12, 8))
        progress_panel.columnconfigure(0, weight=1)
        self.af_plane_progress = ttk.Progressbar(progress_panel, variable=self.af_plane_progress_var, maximum=100, mode="determinate")
        self.af_plane_progress.grid(row=0, column=0, sticky="ew")

        bottom_panel = ttk.Frame(result_panel, style="Panel.TFrame")
        bottom_panel.grid(row=3, column=0, sticky="nsew")
        bottom_panel.columnconfigure(0, weight=2, uniform="focusmap_bottom")
        bottom_panel.columnconfigure(1, weight=3, uniform="focusmap_bottom")
        bottom_panel.rowconfigure(1, weight=1)
        ttk.Label(bottom_panel, text="3D SURFACE", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 6))
        ttk.Label(bottom_panel, text="POINT TABLE", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(12, 0), pady=(0, 6))
        focusmap_3d_frame = ttk.Frame(bottom_panel, style="Panel.TFrame")
        focusmap_3d_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 12))
        focusmap_3d_frame.columnconfigure(0, weight=1)
        focusmap_3d_frame.rowconfigure(0, weight=1)
        self.focusmap_3d_view = create_focusmap_3d_view(focusmap_3d_frame, self.colors)
        self.focusmap_3d_view.widget.grid(row=0, column=0, sticky="nsew")

        table_panel = ttk.Frame(bottom_panel, style="Panel.TFrame")
        table_panel.grid(row=1, column=1, sticky="nsew")
        table_panel.columnconfigure(0, weight=1)
        table_panel.rowconfigure(0, weight=1)
        table_frame = ttk.Frame(table_panel, style="Panel.TFrame")
        table_frame.grid(row=0, column=0, sticky="nsew")
        table_frame.columnconfigure(0, weight=1)
        table_frame.rowconfigure(0, weight=1)
        columns = ("index", "x", "y", "z", "fit", "residual", "status", "retry", "fit_enabled")
        self.af_plane_tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=9, style="FocusMap.Treeview")
        headings = {
            "index": "#",
            "x": "X",
            "y": "Y",
            "z": "Z",
            "fit": "Z'",
            "residual": "\u0394",
            "status": "Status",
            "retry": "Retry",
            "fit_enabled": "Fit",
        }
        widths = {"index": 44, "x": 82, "y": 82, "z": 78, "fit": 78, "residual": 74, "status": 62, "retry": 54, "fit_enabled": 50}
        for column in columns:
            self.af_plane_tree.heading(column, text=headings[column])
            self.af_plane_tree.column(column, width=widths[column], minwidth=widths[column], stretch=True, anchor="center")
        self.af_plane_tree.tag_configure("row_even", background="#0f1722")
        self.af_plane_tree.tag_configure("row_odd", background="#111c2a")
        self.af_plane_tree.tag_configure("residual_good", foreground=self.colors["accent"])
        self.af_plane_tree.tag_configure("residual_warn", foreground=self.colors["warning"])
        self.af_plane_tree.tag_configure("residual_bad", foreground=self.colors["danger"])
        self.af_plane_tree.tag_configure("residual_pending", foreground=self.colors["muted"])
        self.af_plane_tree.tag_configure("selected_point", background="#1e3a5f")
        self.af_plane_tree.bind("<Button-1>", self._on_af_plane_table_click)
        self.af_plane_tree.grid(row=0, column=0, sticky="nsew")
        scrollbar = ttk.Scrollbar(table_frame, orient=tk.VERTICAL, command=self.af_plane_tree.yview)
        scrollbar.grid(row=0, column=1, sticky="ns")
        self.af_plane_tree.configure(yscrollcommand=scrollbar.set)
        self._build_af_plane_eval_panel(table_panel)

        control_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14, width=380)
        control_panel.grid(row=0, column=1, sticky="ns")
        control_panel.grid_propagate(False)
        control_panel.columnconfigure((0, 1), weight=1, uniform="af_plane_controls")

        self.af_plane_center_range_widgets: list[tk.Widget] = []
        self.af_plane_pick_region_widgets: list[tk.Widget] = []

        def add_spinbox(
            row_index: int,
            label: str,
            variable: tk.StringVar,
            column: int,
            from_value: float = -1_000_000,
            to_value: float = 1_000_000,
            increment: float = 1,
        ) -> tuple[tk.Widget, tk.Widget]:
            label_widget = ttk.Label(control_panel, text=label, style="Muted.TLabel")
            label_widget.grid(row=row_index, column=column, sticky="w", pady=(7, 2), padx=(0, 5) if column == 0 else (5, 0))
            spinbox = self._numeric_spinbox(control_panel, variable, from_value=from_value, to_value=to_value, increment=increment, width=9)
            spinbox.grid(row=row_index + 1, column=column, sticky="ew", padx=(0, 5) if column == 0 else (5, 0))
            return label_widget, spinbox

        row = 0
        ttk.Label(control_panel, text="MESH SETUP", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w")
        row += 1
        ttk.Label(control_panel, text="Mesh type", style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(8, 2), padx=(0, 5))
        ttk.Label(control_panel, text="Region mode", style="Muted.TLabel").grid(row=row, column=1, sticky="w", pady=(8, 2), padx=(5, 0))
        row += 1
        ttk.Combobox(control_panel, textvariable=self.af_plane_mesh_type_var, values=("Rectangular", "Hexagonal"), state="readonly", width=12).grid(row=row, column=0, sticky="ew", padx=(0, 5))
        region_combo = ttk.Combobox(control_panel, textvariable=self.af_plane_region_mode_var, values=("Center / Range", "Pick P1/P2"), state="readonly", width=12)
        region_combo.grid(row=row, column=1, sticky="ew", padx=(5, 0))
        region_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_af_plane_region_controls())
        self.af_plane_region_mode_var.trace_add("write", lambda *_args: self._update_af_plane_region_controls())
        row += 1
        self.af_plane_center_range_widgets.extend(add_spinbox(row, "Center X", self.af_plane_center_x_var, 0))
        self.af_plane_center_range_widgets.extend(add_spinbox(row, "Center Y", self.af_plane_center_y_var, 1))
        row += 2
        current_button = ttk.Button(control_panel, text="Use Current XY", command=self.use_current_xy_for_af_plane_center)
        current_button.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        self.af_plane_center_range_widgets.append(current_button)
        row += 1
        self.af_plane_center_range_widgets.extend(add_spinbox(row, "X Range", self.af_plane_x_range_var, 0, from_value=0))
        self.af_plane_center_range_widgets.extend(add_spinbox(row, "Y Range", self.af_plane_y_range_var, 1, from_value=0))
        row += 2
        pick_p1_button = ttk.Button(control_panel, text="Pick P1", command=lambda: self.pick_af_plane_region_point("p1"))
        pick_p1_button.grid(row=row, column=0, sticky="ew", pady=(8, 0), padx=(0, 5))
        pick_p2_button = ttk.Button(control_panel, text="Pick P2", command=lambda: self.pick_af_plane_region_point("p2"))
        pick_p2_button.grid(row=row, column=1, sticky="ew", pady=(8, 0), padx=(5, 0))
        self.af_plane_pick_region_widgets.extend([pick_p1_button, pick_p2_button])
        row += 1
        p1_frame = ttk.Frame(control_panel, style="Panel.TFrame")
        p1_frame.grid(row=row, column=0, sticky="ew", pady=(5, 0), padx=(0, 5))
        p1_frame.columnconfigure(0, weight=1)
        p1_label = ttk.Label(p1_frame, textvariable=self.af_plane_p1_var, style="Value.TLabel", padding=6)
        p1_label.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        p1_go = ttk.Button(p1_frame, text="Go", width=3, command=lambda: self.go_to_af_plane_region_point("p1"))
        p1_go.grid(row=0, column=1, sticky="e")
        p2_frame = ttk.Frame(control_panel, style="Panel.TFrame")
        p2_frame.grid(row=row, column=1, sticky="ew", pady=(5, 0), padx=(5, 0))
        p2_frame.columnconfigure(0, weight=1)
        p2_label = ttk.Label(p2_frame, textvariable=self.af_plane_p2_var, style="Value.TLabel", padding=6)
        p2_label.grid(row=0, column=0, sticky="ew", padx=(0, 5))
        p2_go = ttk.Button(p2_frame, text="Go", width=3, command=lambda: self.go_to_af_plane_region_point("p2"))
        p2_go.grid(row=0, column=1, sticky="e")
        self.af_plane_pick_region_widgets.extend([p1_frame, p2_frame, p1_label, p2_label, p1_go, p2_go])
        row += 1
        add_spinbox(row, "Columns", self.af_plane_cols_var, 0, from_value=1)
        add_spinbox(row, "Rows", self.af_plane_rows_var, 1, from_value=1)
        row += 2
        ttk.Button(control_panel, text="Generate Mesh", command=self.generate_af_plane_mesh).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))

        row += 1
        ttk.Label(control_panel, text="AUTOFOCUS PARAMETERS", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(14, 0))
        row += 1
        ttk.Label(control_panel, text="Metric", style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(7, 2), padx=(0, 5))
        ttk.Label(control_panel, text="Retry count", style="Muted.TLabel").grid(row=row, column=1, sticky="w", pady=(7, 2), padx=(5, 0))
        row += 1
        metric_combo = ttk.Combobox(control_panel, textvariable=self.focus_metric_var, values=("Laplacian", "Tenengrad", "Brenner"), state="readonly", width=12)
        metric_combo.grid(row=row, column=0, sticky="ew", padx=(0, 5))
        self._numeric_spinbox(control_panel, self.af_plane_retry_count_var, from_value=0, to_value=10, width=9).grid(row=row, column=1, sticky="ew", padx=(5, 0))
        row += 1
        add_spinbox(row, "Initial Step", self.autofocus_step_var, 0, from_value=1, to_value=1_000_000)
        add_spinbox(row, "Min Step", self.autofocus_min_step_var, 1, from_value=1, to_value=1_000_000)
        row += 2
        add_spinbox(row, "Z Range +/-", self.autofocus_max_moves_var, 0, from_value=1, to_value=1_000_000)
        row += 2
        ttk.Checkbutton(control_panel, text="Return to start position", variable=self.af_plane_return_to_start_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        row += 1
        ttk.Checkbutton(control_panel, text="Dry run without hardware", variable=self.af_plane_dry_run_var).grid(row=row, column=0, columnspan=2, sticky="w", pady=(4, 0))

        row += 1
        ttk.Label(control_panel, text="EXECUTION", style="Section.TLabel").grid(row=row, column=0, columnspan=2, sticky="w", pady=(14, 0))
        row += 1
        ttk.Button(control_panel, text="Start FocusMap", style="Accent.TButton", command=self.start_af_plane_mapping).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        row += 1
        ttk.Button(control_panel, text="Re-Auto Focus", style="Accent.TButton", command=self.reauto_focus_selected_af_plane_point).grid(row=row, column=0, sticky="ew", pady=(8, 0), padx=(0, 5))
        ttk.Button(control_panel, text="Inject Current Z", command=self.inject_current_z_to_selected_af_plane_point).grid(row=row, column=1, sticky="ew", pady=(8, 0), padx=(5, 0))
        row += 1
        ttk.Button(control_panel, textvariable=self.af_plane_pause_button_var, command=self.toggle_af_plane_pause).grid(row=row, column=0, sticky="ew", pady=(8, 0), padx=(0, 5))
        ttk.Button(control_panel, text="Stop / Cancel", style="Danger.TButton", command=self.stop_af_plane_mapping).grid(row=row, column=1, sticky="ew", pady=(8, 0), padx=(5, 0))
        row += 1
        ttk.Button(control_panel, text="Clear Results", command=self.clear_af_plane_results).grid(row=row, column=0, sticky="ew", pady=(8, 0), padx=(0, 5))
        ttk.Button(control_panel, text="Save Results", command=self.save_af_plane_results).grid(row=row, column=1, sticky="ew", pady=(8, 0), padx=(5, 0))
        row += 1
        ttk.Button(control_panel, text="Load Results", command=self.load_af_plane_results).grid(row=row, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self._update_af_plane_region_controls()

    def _update_af_plane_region_controls(self) -> None:
        center_visible = self.af_plane_region_mode_var.get() == "Center / Range"
        for widget in getattr(self, "af_plane_center_range_widgets", []):
            if center_visible:
                widget.grid()
            else:
                widget.grid_remove()
        for widget in getattr(self, "af_plane_pick_region_widgets", []):
            if center_visible:
                widget.grid_remove()
            else:
                widget.grid()

    def _build_af_plane_eval_panel(self, parent: ttk.Frame) -> None:
        eval_panel = ttk.Frame(parent, style="Panel.TFrame")
        eval_panel.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        for column in range(4):
            eval_panel.columnconfigure(column, weight=1, uniform="af_plane_eval")

        ttk.Label(eval_panel, text="FIT EVALUATION", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        self.af_plane_eval_labels = {}
        labels = (
            ("equation", 1, 0, 4),
            ("a", 2, 0, 1),
            ("b", 2, 1, 1),
            ("c", 2, 2, 1),
            ("d", 2, 3, 1),
            ("rms", 3, 0, 1),
            ("pv", 3, 1, 1),
            ("max", 3, 2, 1),
            ("tilt", 3, 3, 1),
            ("points", 4, 0, 4),
        )
        for key, row, column, span in labels:
            label = tk.Label(
                eval_panel,
                text="-",
                anchor="w",
                justify="left",
                padx=8,
                pady=5,
                bg=self.colors["surface_2"],
                fg=self.colors["muted"],
                font=("Cascadia Mono", 9),
            )
            label.grid(row=row, column=column, columnspan=span, sticky="ew", padx=(0 if column == 0 else 5, 0), pady=(0, 5))
            self.af_plane_eval_labels[key] = label
        self._set_af_plane_eval_pending(valid=0, failed=0, running=0, total=0)

    def _build_imgstitch_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(0, weight=1)

        preview_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        preview_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_panel.columnconfigure(0, weight=1)
        preview_panel.columnconfigure(1, weight=0, minsize=145)
        preview_panel.rowconfigure(1, weight=1)

        header = ttk.Frame(preview_panel, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="IMG STITCH MOSAIC", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self.imgstitch_mosaic_canvas = tk.Canvas(preview_panel, bg="#05070a", highlightthickness=0)
        self.imgstitch_mosaic_canvas.grid(row=1, column=0, sticky="nsew")
        self.imgstitch_mosaic_canvas.create_text(20, 20, text="No mosaic yet", anchor="nw", fill=self.colors["muted"], font=("Segoe UI Semibold", 14))
        self.imgstitch_mosaic_canvas.bind("<MouseWheel>", self._on_imgstitch_preview_wheel)
        self.imgstitch_mosaic_canvas.bind("<Button-4>", self._on_imgstitch_preview_wheel)
        self.imgstitch_mosaic_canvas.bind("<Button-5>", self._on_imgstitch_preview_wheel)
        self.imgstitch_mosaic_canvas.bind("<ButtonPress-1>", self._on_imgstitch_preview_press)
        self.imgstitch_mosaic_canvas.bind("<B1-Motion>", self._on_imgstitch_preview_drag)
        self.imgstitch_mosaic_canvas.bind("<Configure>", lambda _event: self._render_imgstitch_preview())

        diagnostic_panel = ttk.Frame(preview_panel, style="Panel.TFrame")
        diagnostic_panel.grid(row=0, column=1, rowspan=3, sticky="nsew", padx=(12, 0))
        diagnostic_panel.columnconfigure(0, weight=1)
        diagnostic_panel.rowconfigure(3, weight=1)
        ttk.Label(diagnostic_panel, text="STITCH EVALUATION", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(diagnostic_panel, textvariable=self.imgstitch_quality_var, style="Status.TLabel", wraplength=130, padding=6).grid(row=1, column=0, sticky="ew", pady=(6, 10))
        ttk.Label(diagnostic_panel, text="DISPLACEMENT", style="Section.TLabel").grid(row=2, column=0, sticky="nw")
        self.imgstitch_scatter_canvas = tk.Canvas(diagnostic_panel, bg="#071018", highlightthickness=1, highlightbackground="#334155", width=145, height=190)
        self.imgstitch_scatter_canvas.grid(row=3, column=0, sticky="nsew", pady=(6, 12))
        self.imgstitch_scatter_canvas.bind("<Configure>", lambda _event: self._render_imgstitch_scatter())
        ttk.Label(diagnostic_panel, text="LIVE CAMERA", style="Section.TLabel").grid(row=4, column=0, sticky="w")
        self.imgstitch_live_label = ttk.Label(diagnostic_panel, anchor="center", text="Camera", style="Video.TLabel", width=14)
        self.imgstitch_live_label.grid(row=5, column=0, sticky="ew", pady=(6, 0))

        lower_panel = ttk.Frame(preview_panel, style="Panel.TFrame")
        lower_panel.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        lower_panel.columnconfigure(0, weight=1)
        ttk.Label(lower_panel, textvariable=self.imgstitch_point_status_var, style="Value.TLabel", wraplength=460, padding=8).grid(row=0, column=0, sticky="ew")

        control_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14, width=360)
        control_panel.grid(row=0, column=1, sticky="ns")
        control_panel.grid_propagate(False)
        control_panel.columnconfigure((0, 1), weight=1, uniform="imgstitch_controls")
        ttk.Label(control_panel, text="MODE", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        mode_select = ttk.Combobox(control_panel, textvariable=self.imgstack_mode_var, values=("XY Stitch", "T-Stack", "Z-Stack"), state="readonly", width=16)
        mode_select.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        mode_select.bind("<<ComboboxSelected>>", lambda _event: self._update_imgstitch_mode_fields())

        self.imgstitch_mode_widgets: dict[str, list[tk.Widget]] = {"Array": [], "Space": [], "Two Points": [], "Manual Step": [], "XY Stitch": []}
        self.imgstack_mode_widgets: dict[str, list[tk.Widget]] = {"T-Stack": [], "Z-Stack": []}
        self.imgstitch_tile_mode_widgets: dict[str, list[tk.Widget]] = {"T-Stack": [], "Z-Stack": []}

        range_label = ttk.Label(control_panel, text="RANGE", style="Section.TLabel")
        range_label.grid(row=2, column=0, columnspan=2, sticky="w", pady=(10, 0))
        mode_combo = ttk.Combobox(control_panel, textvariable=self.imgstitch_range_mode_var, values=("Array", "Space", "Two Points"), state="readonly", width=16)
        mode_combo.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_imgstitch_mode_fields())

        def add_spinbox(
            row_index: int,
            label: str,
            variable: tk.StringVar,
            mode: str | None = None,
            registry: dict[str, list[tk.Widget]] | None = None,
            column: int = 0,
            from_value: float = 1,
            to_value: float = 1_000_000,
            increment: float = 1,
            kind: str = "int",
        ) -> int:
            label_widget = ttk.Label(control_panel, text=label, style="Muted.TLabel")
            label_widget.grid(row=row_index, column=column, sticky="w", pady=(7, 2), padx=(0, 5) if column == 0 else (5, 0))
            spinbox = self._numeric_spinbox(control_panel, variable, kind=kind, from_value=from_value, to_value=to_value, increment=increment, width=9)
            spinbox.grid(row=row_index + 1, column=column, sticky="ew", padx=(0, 5) if column == 0 else (5, 0))
            if mode is not None and registry is not None:
                registry[mode].extend([label_widget, spinbox])
            return row_index + 2

        self.imgstitch_mode_widgets["XY Stitch"].extend([range_label, mode_combo])

        row = 4
        _ = add_spinbox(row, "Rows", self.imgstitch_rows_var, "Array", self.imgstitch_mode_widgets, column=0)
        row = add_spinbox(row, "Cols", self.imgstitch_cols_var, "Array", self.imgstitch_mode_widgets, column=1)
        _ = add_spinbox(row, "Width (um)", self.imgstitch_width_um_var, "Space", self.imgstitch_mode_widgets, column=0, kind="float")
        row = add_spinbox(row, "Height (um)", self.imgstitch_height_um_var, "Space", self.imgstitch_mode_widgets, column=1, kind="float")

        point_buttons = ttk.Frame(control_panel, style="Panel.TFrame")
        point_buttons.grid(row=row, column=0, columnspan=2, sticky="ew", pady=(7, 0))
        point_buttons.columnconfigure((0, 1), weight=1, uniform="points")
        ttk.Button(point_buttons, text="Point 1", command=lambda: self.record_imgstitch_point(1)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(point_buttons, text="Point 2", command=lambda: self.record_imgstitch_point(2)).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.imgstitch_mode_widgets["Two Points"].append(point_buttons)
        row += 1

        tile_label = ttk.Label(control_panel, text="TILE ACQUISITION", style="Section.TLabel")
        tile_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
        row += 1
        tile_combo = ttk.Combobox(control_panel, textvariable=self.imgstitch_tile_acquisition_var, values=("Single Frame", "T-Stack", "Z-Stack"), state="readonly", width=16)
        tile_combo.grid(row=row, column=0, columnspan=2, sticky="ew")
        tile_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_imgstitch_mode_fields())
        self.imgstitch_mode_widgets["XY Stitch"].extend([tile_label, tile_combo])
        row += 1

        acquisition_label = ttk.Label(control_panel, text="ACQUISITION", style="Section.TLabel")
        acquisition_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.imgstitch_mode_widgets["XY Stitch"].append(acquisition_label)
        row += 1
        _ = add_spinbox(row, "Overlap X (px)", self.imgstitch_overlap_x_var, "XY Stitch", self.imgstitch_mode_widgets, column=0)
        row = add_spinbox(row, "Overlap Y (px)", self.imgstitch_overlap_y_var, "XY Stitch", self.imgstitch_mode_widgets, column=1)
        _ = add_spinbox(row, "Step X (um)", self.imgstitch_step_x_var, "Manual Step", self.imgstitch_mode_widgets, column=0, kind="float")
        row = add_spinbox(row, "Step Y (um)", self.imgstitch_step_y_var, "Manual Step", self.imgstitch_mode_widgets, column=1, kind="float")

        t_header = ttk.Label(control_panel, text="T-STACK", style="Section.TLabel")
        t_header.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.imgstack_mode_widgets["T-Stack"].append(t_header)
        self.imgstitch_tile_mode_widgets["T-Stack"].append(t_header)
        row += 1
        _ = add_spinbox(row, "Frame Count", self.t_stack_frame_count_var, "T-Stack", self.imgstack_mode_widgets, column=0)
        self.imgstitch_tile_mode_widgets["T-Stack"].extend(self.imgstack_mode_widgets["T-Stack"][-2:])
        t_fusion_label = ttk.Label(control_panel, text="Fusion Method", style="Muted.TLabel")
        t_fusion_label.grid(row=row, column=1, sticky="w", pady=(7, 2), padx=(5, 0))
        t_fusion_combo = ttk.Combobox(control_panel, textvariable=self.t_stack_fusion_var, values=("average", "registered_average", "sharpness_fusion"), state="readonly", width=16)
        t_fusion_combo.grid(row=row + 1, column=1, sticky="ew", padx=(5, 0))
        t_raw = ttk.Checkbutton(control_panel, text="Save raw T-stack", variable=self.t_stack_save_raw_var)
        t_raw.grid(row=row + 2, column=0, columnspan=2, sticky="w", pady=(5, 0))
        for widget in (t_fusion_label, t_fusion_combo, t_raw):
            self.imgstack_mode_widgets["T-Stack"].append(widget)
            self.imgstitch_tile_mode_widgets["T-Stack"].append(widget)
        row += 3

        z_header = ttk.Label(control_panel, text="Z-STACK", style="Section.TLabel")
        z_header.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.imgstack_mode_widgets["Z-Stack"].append(z_header)
        self.imgstitch_tile_mode_widgets["Z-Stack"].append(z_header)
        row += 1
        _ = add_spinbox(row, "Z Step (um)", self.z_stack_step_um_var, "Z-Stack", self.imgstack_mode_widgets, column=0, kind="float")
        self.imgstitch_tile_mode_widgets["Z-Stack"].extend(self.imgstack_mode_widgets["Z-Stack"][-2:])
        row = add_spinbox(row, "Z Range +/- (um)", self.z_stack_range_um_var, "Z-Stack", self.imgstack_mode_widgets, column=1, kind="float")
        self.imgstitch_tile_mode_widgets["Z-Stack"].extend(self.imgstack_mode_widgets["Z-Stack"][-2:])
        z_fusion_label = ttk.Label(control_panel, text="Fusion Method", style="Muted.TLabel")
        z_fusion_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(7, 2))
        z_fusion_combo = ttk.Combobox(control_panel, textvariable=self.z_stack_fusion_var, values=("laplacian", "tenengrad"), state="readonly", width=16)
        z_fusion_combo.grid(row=row + 1, column=0, columnspan=2, sticky="ew")
        z_return = ttk.Checkbutton(control_panel, text="Return to Z0", variable=self.z_stack_return_var)
        z_return.grid(row=row + 2, column=0, sticky="w", pady=(10, 0))
        z_raw = ttk.Checkbutton(control_panel, text="Save raw Z-stack", variable=self.z_stack_save_raw_var)
        z_raw.grid(row=row + 2, column=1, sticky="w", pady=(10, 0), padx=(6, 0))
        for widget in (z_fusion_label, z_fusion_combo, z_return, z_raw):
            self.imgstack_mode_widgets["Z-Stack"].append(widget)
            self.imgstitch_tile_mode_widgets["Z-Stack"].append(widget)
        row += 3

        recompose_label = ttk.Label(control_panel, text="RECOMPOSE", style="Section.TLabel")
        recompose_label.grid(row=row, column=0, columnspan=2, sticky="w", pady=(10, 2))
        self.imgstitch_mode_widgets["XY Stitch"].append(recompose_label)
        row += 1
        _ = add_spinbox(row, "Max correction (um)", self.imgstitch_max_correction_um_var, "XY Stitch", self.imgstitch_mode_widgets, column=0, from_value=0, increment=0.5, kind="float")
        row = add_spinbox(row, "Reg weight (0-1)", self.imgstitch_registration_weight_var, "XY Stitch", self.imgstitch_mode_widgets, column=1, from_value=0, to_value=1, increment=0.05, kind="float")
        white_balance_check = ttk.Checkbutton(control_panel, text="White balance correction", variable=self.imgstitch_white_balance_var)
        white_balance_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.imgstitch_mode_widgets["XY Stitch"].append(white_balance_check)
        row += 1
        correction_check = ttk.Checkbutton(control_panel, text="Use green-edge displacement correction", variable=self.imgstitch_green_edge_correction_var)
        correction_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.imgstitch_mode_widgets["XY Stitch"].append(correction_check)
        row += 1
        seam_check = ttk.Checkbutton(control_panel, text="Show seam quality", variable=self.imgstitch_show_seams_var)
        seam_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.imgstitch_mode_widgets["XY Stitch"].append(seam_check)
        row += 1

        plane_check = ttk.Checkbutton(control_panel, text="Four-corner plane AF", variable=self.imgstitch_plane_af_var)
        plane_check.grid(row=row, column=0, columnspan=2, sticky="w", pady=(8, 0))
        focusmap_plane_check = ttk.Frame(control_panel, style="Panel.TFrame")
        focusmap_plane_check.grid(row=row + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        focusmap_plane_check.columnconfigure(0, weight=1)
        ttk.Label(focusmap_plane_check, text="FocusMap plane Z", style="Panel.TLabel").grid(row=0, column=0, sticky="w")
        ToggleSwitch(
            focusmap_plane_check,
            self.imgstitch_focusmap_plane_var,
            self.colors,
            command=self._on_imgstitch_focusmap_plane_toggle,
        ).grid(row=0, column=1, sticky="e")
        start_button = ttk.Button(control_panel, text="Start", style="Accent.TButton", command=self.start_imgstitch)
        start_button.grid(row=row + 2, column=0, sticky="ew", pady=(8, 0), padx=(0, 5))
        recompose_button = ttk.Button(control_panel, text="Apply and Recalculate", command=self.recompose_imgstitch_session)
        recompose_button.grid(row=row + 2, column=1, sticky="ew", pady=(8, 0), padx=(5, 0))
        self.imgstitch_recompose_button = recompose_button
        ttk.Button(control_panel, text="Stop", style="Danger.TButton", command=self.stop_imgstitch).grid(row=row + 3, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        ttk.Label(control_panel, textvariable=self.imgstitch_status_var, style="Status.TLabel", wraplength=300, padding=8).grid(row=row + 4, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.imgstitch_mode_widgets["XY Stitch"].extend([plane_check, focusmap_plane_check, recompose_button])
        for variable in (
            self.imgstitch_overlap_x_var,
            self.imgstitch_overlap_y_var,
            self.imgstitch_max_correction_um_var,
            self.imgstitch_registration_weight_var,
        ):
            variable.trace_add("write", lambda *_args: self._mark_imgstitch_recompose_dirty())
        self.imgstitch_show_seams_var.trace_add("write", lambda *_args: self._mark_imgstitch_recompose_dirty())
        self.imgstitch_green_edge_correction_var.trace_add("write", lambda *_args: self._mark_imgstitch_recompose_dirty())
        self.imgstitch_white_balance_var.trace_add("write", lambda *_args: self._mark_imgstitch_recompose_dirty())
        self._update_imgstitch_mode_fields()

    def _build_config_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure((0, 1, 2), weight=1, uniform="config_columns")
        parent.rowconfigure(0, weight=1)

        optical_panel = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        optical_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        optical_panel.columnconfigure(0, weight=1)
        optical_panel.columnconfigure(1, weight=1)

        motion_panel = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        motion_panel.grid(row=0, column=1, sticky="nsew", padx=(4, 4))
        motion_panel.columnconfigure(0, weight=1)
        motion_panel.columnconfigure(1, weight=1)

        system_panel = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        system_panel.grid(row=0, column=2, sticky="nsew", padx=(8, 0))
        system_panel.columnconfigure((1, 2), weight=1, uniform="af_thresholds")

        ttk.Label(optical_panel, text="OPTICAL CALIBRATION", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")
        ttk.Label(optical_panel, text="Objective", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 4))
        objective_combo = ttk.Combobox(optical_panel, values=[str(value) for value in OBJECTIVE_OPTIONS], textvariable=self.objective_var, state="readonly")
        objective_combo.grid(row=2, column=0, sticky="ew", padx=(0, 8))
        ttk.Label(optical_panel, text="Eyepiece", style="Muted.TLabel").grid(row=1, column=1, sticky="w", pady=(16, 4))
        eyepiece_combo = ttk.Combobox(optical_panel, values=[f"{value:g}" for value in EYEPIECE_OPTIONS], textvariable=self.eyepiece_var, state="readonly")
        eyepiece_combo.grid(row=2, column=1, sticky="ew", padx=(8, 0))
        objective_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_config(save=True))
        eyepiece_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_config(save=True))

        ttk.Label(optical_panel, text="CALIBRATION", style="Section.TLabel").grid(row=3, column=0, columnspan=2, sticky="w", pady=(24, 6))
        ttk.Label(optical_panel, textvariable=self.calibration_status_var, style="Value.TLabel", wraplength=360, padding=10).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(optical_panel, text="Calibrate Pixels", style="Accent.TButton", command=self.open_pixel_calibration).grid(row=5, column=0, sticky="ew", pady=(14, 0), padx=(0, 8))
        ttk.Button(optical_panel, text="Save Config", command=self.apply_config).grid(row=5, column=1, sticky="ew", pady=(14, 0), padx=(8, 0))
        ttk.Label(optical_panel, textvariable=self.config_status_var, style="Status.TLabel", wraplength=360, padding=10).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(18, 0))

        main_control_panel = ttk.Frame(optical_panel, style="Panel.TFrame")
        main_control_panel.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        main_control_panel.columnconfigure(1, weight=1)
        ttk.Label(main_control_panel, text="MAIN CONTROL", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(main_control_panel, text="Motion keys", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        keyboard_combo = ttk.Combobox(
            main_control_panel,
            values=[KEYBOARD_MOTION_SCHEME_LABELS[KEYBOARD_MOTION_SCHEME_ARROW_PAGE], KEYBOARD_MOTION_SCHEME_LABELS[KEYBOARD_MOTION_SCHEME_WASD_QE]],
            textvariable=self.keyboard_motion_scheme_var,
            state="readonly",
        )
        keyboard_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
        keyboard_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_config(save=True))

        for row_index, axis in enumerate(JOG_STEP_AXES, start=2):
            ttk.Label(main_control_panel, text=f"Alt+{axis} levels", style="Muted.TLabel").grid(row=row_index, column=0, sticky="w", pady=2)
            self._jog_step_levels_entry(main_control_panel, self.jog_step_level_vars[axis]).grid(row=row_index, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)

        ttk.Label(main_control_panel, text="LayoutBond FOV W (um)", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 2))
        self._numeric_entry(main_control_panel, self.layoutbond_fov_width_var, kind="float", minimum=0.000001, maximum=1_000_000).grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=(10, 2), ipady=5)
        ttk.Label(main_control_panel, text="LayoutBond FOV H (um)", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=2)
        self._numeric_entry(main_control_panel, self.layoutbond_fov_height_var, kind="float", minimum=0.000001, maximum=1_000_000).grid(row=6, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)

        camera_control_panel = ttk.Frame(optical_panel, style="Panel.TFrame")
        camera_control_panel.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        camera_control_panel.columnconfigure(1, weight=1)
        camera_mode_values = [CAMERA_CONTROL_MODE_LABELS[mode] for mode in CAMERA_CONTROL_MODES]
        ttk.Label(camera_control_panel, text="CAMERA CONTROL", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(camera_control_panel, text="Exposure mode", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        exposure_mode_combo = ttk.Combobox(
            camera_control_panel,
            values=camera_mode_values,
            textvariable=self.camera_exposure_mode_var,
            state="readonly",
        )
        exposure_mode_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
        ttk.Label(camera_control_panel, text="Exposure value", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self._numeric_entry(
            camera_control_panel,
            self.camera_exposure_var,
            kind="float",
            minimum=-1_000_000,
            maximum=1_000_000,
            width=8,
        ).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Label(camera_control_panel, text="Gain mode", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        gain_mode_combo = ttk.Combobox(
            camera_control_panel,
            values=camera_mode_values,
            textvariable=self.camera_gain_mode_var,
            state="readonly",
        )
        gain_mode_combo.grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=2)
        ttk.Label(camera_control_panel, text="Gain value", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=2)
        self._numeric_entry(
            camera_control_panel,
            self.camera_gain_var,
            kind="float",
            minimum=0,
            maximum=1_000_000,
            width=8,
        ).grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Button(camera_control_panel, text="Apply Camera", style="Accent.TButton", command=self.apply_camera_config).grid(row=5, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        ttk.Label(motion_panel, text="MOTOR MAPPING", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")

        fields = (
            ("Microstep", self.microstep_var, "int", 1, 1_000_000),
            ("Base angle (deg)", self.base_angle_var, "float", 0.000001, 360),
            ("X/Y lead (mm)", self.lead_xy_var, "float", 0.000001, 1_000_000),
            ("Z lead (mm)", self.lead_z_var, "float", 0.000001, 1_000_000),
            ("CC accel/decel (s)", self.cc_accel_time_var, "float", 0, 2.55),
        )
        for index, (label, variable, kind, minimum, maximum) in enumerate(fields, start=1):
            col = (index - 1) % 2
            row = 1 + ((index - 1) // 2) * 2
            ttk.Label(motion_panel, text=label, style="Muted.TLabel").grid(row=row, column=col, sticky="w", pady=(16, 4), padx=(0 if col == 0 else 8, 8 if col == 0 else 0))
            entry = self._numeric_entry(motion_panel, variable, kind=kind, minimum=minimum, maximum=maximum)
            entry.grid(row=row + 1, column=col, sticky="ew", padx=(0 if col == 0 else 8, 8 if col == 0 else 0), ipady=6)

        speed_panel = ttk.Frame(motion_panel, style="Panel.TFrame")
        speed_panel.grid(row=7, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        speed_panel.columnconfigure(1, weight=1)
        ttk.Label(speed_panel, text="MOTOR SPEED", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(speed_panel, text="Speed profile", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        speed_profile_combo = ttk.Combobox(
            speed_panel,
            values=[MOTOR_SPEED_PROFILE_LABELS[profile] for profile in MOTOR_SPEED_PROFILES],
            textvariable=self.motor_speed_profile_var,
            state="readonly",
        )
        speed_profile_combo.grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
        speed_profile_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_config(save=True))
        for row_index, (label, variable) in enumerate(
            (
                ("Fast speed (%)", self.cc_speed_percent_var),
                ("Fine speed (%)", self.fine_speed_percent_var),
                ("Safe speed (%)", self.safe_speed_percent_var),
            ),
            start=2,
        ):
            ttk.Label(speed_panel, text=label, style="Muted.TLabel").grid(row=row_index, column=0, sticky="w", pady=2)
            self._numeric_entry(speed_panel, variable, minimum=0, maximum=100, width=7).grid(row=row_index, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)

        d5_panel = ttk.Frame(motion_panel, style="Panel.TFrame")
        d5_panel.grid(row=8, column=0, columnspan=2, sticky="ew", pady=(22, 0))
        d5_panel.columnconfigure((1, 2, 3), weight=1, uniform="d5_params")
        ttk.Label(d5_panel, text="D5 CONTROLLER READBACK", style="Section.TLabel").grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        ttk.Label(d5_panel, text="Axis", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        for column, label in enumerate(("Min", "Work", "Accel"), start=1):
            ttk.Label(d5_panel, text=label, style="Muted.TLabel").grid(row=1, column=column, sticky="w", padx=(8 if column > 1 else 0, 0), pady=2)
        for row_index, axis in enumerate(JOG_STEP_AXES, start=2):
            ttk.Label(d5_panel, text=axis, style="Panel.TLabel").grid(row=row_index, column=0, sticky="w", pady=2)
            for column, field_name in enumerate(("minimum_speed", "work_speed", "acceleration"), start=1):
                self._numeric_entry(
                    d5_panel,
                    self.controller_motion_parameter_vars[axis][field_name],
                    minimum=0,
                    maximum=65535,
                    width=5,
                ).grid(row=row_index, column=column, sticky="ew", padx=(8 if column > 1 else 0, 0), pady=2, ipady=5)
        ttk.Button(d5_panel, text="Read D5 X/Y/Z", command=self.read_controller_motion_parameters).grid(row=5, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        ttk.Label(d5_panel, textvariable=self.controller_motion_status_var, style="Status.TLabel", wraplength=360, padding=8).grid(row=6, column=0, columnspan=4, sticky="ew", pady=(8, 0))

        ttk.Label(motion_panel, text="CONVERSION", style="Section.TLabel").grid(row=9, column=0, columnspan=2, sticky="w", pady=(24, 6))
        ttk.Label(motion_panel, textvariable=self.motor_conversion_var, style="Value.TLabel", wraplength=360, padding=10).grid(row=10, column=0, columnspan=2, sticky="ew")
        ttk.Button(motion_panel, text="Apply Mapping", style="Accent.TButton", command=self.apply_config).grid(row=11, column=0, columnspan=2, sticky="ew", pady=(18, 0))

        autofocus_panel = ttk.Frame(system_panel, style="Panel.TFrame")
        autofocus_panel.grid(row=0, column=0, columnspan=3, sticky="ew")
        autofocus_panel.columnconfigure((1, 2), weight=1, uniform="af_thresholds")
        ttk.Label(autofocus_panel, text="AUTOFOCUS CONFIG", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(autofocus_panel, text="Settle after Z move (ms)", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        self._numeric_entry(autofocus_panel, self.autofocus_settle_ms_var, minimum=0, maximum=10000).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=5)
        ttk.Label(autofocus_panel, text="AF integration frames", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        self._numeric_entry(autofocus_panel, self.autofocus_sample_count_var, minimum=1, maximum=1000).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=5)
        ttk.Label(autofocus_panel, text="Settle after stitch move (ms)", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        self._numeric_entry(autofocus_panel, self.imgstitch_settle_ms_var, minimum=0, maximum=10000).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=5)
        ttk.Label(autofocus_panel, text="Peak fit model", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=2)
        peak_model_combo = ttk.Combobox(
            autofocus_panel,
            textvariable=self.autofocus_peak_model_var,
            values=[AUTOFOCUS_PEAK_MODEL_LABELS[model] for model in AUTOFOCUS_PEAK_MODELS],
            state="readonly",
        )
        peak_model_combo.grid(row=4, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=2)
        peak_model_combo.bind("<<ComboboxSelected>>", lambda _event: self.apply_config(save=True))
        ttk.Label(autofocus_panel, text="Metric", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=(10, 2))
        ttk.Label(autofocus_panel, text="Yellow", style="Muted.TLabel").grid(row=5, column=1, sticky="w", padx=(8, 0), pady=(10, 2))
        ttk.Label(autofocus_panel, text="Green", style="Muted.TLabel").grid(row=5, column=2, sticky="w", padx=(8, 0), pady=(10, 2))
        for row_index, metric_name in enumerate(("Laplacian", "Tenengrad", "Brenner"), start=6):
            ttk.Label(autofocus_panel, text=metric_name, style="Muted.TLabel").grid(row=row_index, column=0, sticky="w", pady=2)
            for column, variable in (
                (1, self.focus_threshold_yellow_vars[metric_name]),
                (2, self.focus_threshold_green_vars[metric_name]),
            ):
                self._numeric_entry(autofocus_panel, variable, kind="float", minimum=0, maximum=1_000_000_000).grid(row=row_index, column=column, sticky="ew", padx=(8, 0), pady=2, ipady=5)

        agent_config_panel = ttk.Frame(system_panel, style="Panel.TFrame")
        agent_config_panel.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        agent_config_panel.columnconfigure(1, weight=1)
        ttk.Label(agent_config_panel, text="AI AGENT API", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(agent_config_panel, text="Provider", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        ttk.Label(agent_config_panel, text="DeepSeek / OpenAI-compatible", style="Value.TLabel", padding=(8, 4)).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2)
        ttk.Label(agent_config_panel, text="API key", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        tk.Entry(
            agent_config_panel,
            textvariable=self.agent_api_key_var,
            show="*",
            **self._numeric_widget_options(),
        ).grid(row=2, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Label(agent_config_panel, text="Base URL", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        tk.Entry(
            agent_config_panel,
            textvariable=self.agent_base_url_var,
            **self._numeric_widget_options(),
        ).grid(row=3, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Label(agent_config_panel, text="Model", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=2)
        tk.Entry(
            agent_config_panel,
            textvariable=self.agent_model_var,
            **self._numeric_widget_options(),
        ).grid(row=4, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Label(agent_config_panel, text="Timeout (s)", style="Muted.TLabel").grid(row=5, column=0, sticky="w", pady=2)
        self._numeric_entry(agent_config_panel, self.agent_timeout_var, kind="float", minimum=1, maximum=300).grid(row=5, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Button(agent_config_panel, text="Save Agent API", style="Accent.TButton", command=self.apply_config).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(10, 0))

        admin_panel = ttk.Frame(system_panel, style="Panel.TFrame")
        admin_panel.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(24, 0))
        admin_panel.columnconfigure(1, weight=1)
        ttk.Label(admin_panel, text="ADMIN MODE", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        ttk.Label(admin_panel, text="Token", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(
            admin_panel,
            textvariable=self.admin_token_var,
            show="*",
            **self._numeric_widget_options(),
        ).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=2, ipady=5)
        ttk.Button(admin_panel, text="Enable Admin", style="Accent.TButton", command=self.enable_admin_mode).grid(row=2, column=0, columnspan=2, sticky="ew", pady=(10, 0))
        ttk.Label(admin_panel, textvariable=self.admin_mode_status_var, style="Status.TLabel", wraplength=360, padding=10).grid(row=3, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    def _update_comm_note(self) -> None:
        if self.comm_input_mode_var.get() == "Hex":
            self.comm_note_var.set("Hex mode: spaces, newlines and 0x prefixes are accepted. Ctrl+Enter sends.")
        else:
            self.comm_note_var.set("Text mode: sends UTF-8 bytes exactly as typed. Ctrl+Enter sends.")

    def load_default_comm_test(self) -> None:
        self.comm_input_mode_var.set("Hex")
        self.comm_read_length_var.set("12")
        self.comm_note_var.set("Default: communication test. TX 3A 55 ... expects controller RX A3 AA ...")
        self.comm_input.delete("1.0", "end")
        self.comm_input.insert("1.0", hex_bytes(COMM_TEST_COMMAND))

    def clear_hex_history(self) -> None:
        self.hex_history.configure(state="normal")
        self.hex_history.delete("1.0", "end")
        self.hex_history.configure(state="disabled")
        self.status_var.set("Communication history cleared.")

    def send_manual_command(self) -> str:
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return "break"

        try:
            payload = self._parse_manual_command_input()
            read_length = int(self.comm_read_length_var.get())
        except ValueError as exc:
            self.status_var.set(str(exc))
            logger.warning("Manual command rejected: %s", exc)
            return "break"

        if read_length < 0:
            self.status_var.set("Read bytes must be zero or positive.")
            return "break"
        if payload_contains_clear_position_command(payload) and not self._require_admin_mode("Manual clear-position command"):
            return "break"

        self.status_var.set("Sending manual command...")
        threading.Thread(target=self._manual_command_worker, args=(payload, read_length), daemon=True).start()
        return "break"

    def _parse_manual_command_input(self) -> bytes:
        raw_text = self.comm_input.get("1.0", "end-1c")
        if not raw_text:
            raise ValueError("Manual command input is empty.")

        if self.comm_input_mode_var.get() == "Text":
            return raw_text.encode("utf-8")

        cleaned = re.sub(r"0x", "", raw_text.strip(), flags=re.IGNORECASE)
        cleaned = re.sub(r"[^0-9A-Fa-f]", "", cleaned)
        if not cleaned:
            raise ValueError("Hex command input does not contain hex digits.")
        if len(cleaned) % 2:
            raise ValueError("Hex command input must contain an even number of digits.")
        return bytes.fromhex(" ".join(cleaned[index : index + 2] for index in range(0, len(cleaned), 2)))

    def _manual_command_worker(self, payload: bytes, read_length: int) -> None:
        assert self.serial_client is not None
        try:
            response = self.serial_client.send_raw(payload, read_length=read_length)
            self.result_queue.put(("manual_command", payload, response, read_length))
        except Exception as exc:
            self.result_queue.put(("manual_command_error", exc))

    def _on_window_configure(self, event: tk.Event) -> None:
        if event.widget is not self:
            return
        if self.resize_log_job is not None:
            try:
                self.after_cancel(self.resize_log_job)
            except tk.TclError:
                pass
        try:
            self.resize_log_job = self.after(250, self._log_window_layout)
        except tk.TclError:
            self.resize_log_job = None

    def _log_window_layout(self) -> None:
        self.resize_log_job = None
        try:
            window_size = (self.winfo_width(), self.winfo_height())
            controls_panel = getattr(self, "controls_panel", None)
            control_width = controls_panel.winfo_width() if controls_panel is not None and controls_panel.winfo_exists() else None
        except tk.TclError:
            return

        if window_size != self.last_logged_window_size:
            logger.info("Window resized: %sx%s.", window_size[0], window_size[1])
            self.last_logged_window_size = window_size

        if control_width is not None and control_width != self.last_logged_control_width:
            logger.info("Control panel width: %spx.", control_width)
            self.last_logged_control_width = control_width

    def _bind_keyboard_controls(self) -> None:
        self.key_bindings = self._keyboard_bindings_for_configured_scheme()
        self.bind_all("<KeyPress>", self._on_key_press)
        self.bind_all("<KeyRelease>", self._on_key_release)
        self.bind_all("<Alt-x>", lambda _event: self.cycle_jog_step("X"))
        self.bind_all("<Alt-X>", lambda _event: self.cycle_jog_step("X"))
        self.bind_all("<Alt-y>", lambda _event: self.cycle_jog_step("Y"))
        self.bind_all("<Alt-Y>", lambda _event: self.cycle_jog_step("Y"))
        self.bind_all("<Alt-z>", lambda _event: self.cycle_jog_step("Z"))
        self.bind_all("<Alt-Z>", lambda _event: self.cycle_jog_step("Z"))
        self.bind_all("<MouseWheel>", self._on_mouse_wheel)
        self.bind_all("<Button-4>", self._on_mouse_wheel)
        self.bind_all("<Button-5>", self._on_mouse_wheel)

    def _keyboard_bindings_for_configured_scheme(self) -> dict[str, tuple[str, bool]]:
        scheme = getattr(self.probe_config, "keyboard_motion_scheme", KEYBOARD_MOTION_SCHEME_ARROW_PAGE)
        if scheme == KEYBOARD_MOTION_SCHEME_WASD_QE:
            return {
                "d": ("X", False),
                "a": ("X", True),
                "w": ("Y", False),
                "s": ("Y", True),
                "q": ("Z", False),
                "e": ("Z", True),
            }
        return {
            "Right": ("X", False),
            "Left": ("X", True),
            "Up": ("Y", False),
            "Down": ("Y", True),
            "Prior": ("Z", False),
            "Next": ("Z", True),
        }

    def _refresh_keyboard_bindings(self) -> None:
        self._clear_held_keyboard_moves()
        self.key_bindings = self._keyboard_bindings_for_configured_scheme()

    def _keyboard_motion_scheme_label(self, scheme: str) -> str:
        return KEYBOARD_MOTION_SCHEME_LABELS.get(scheme, KEYBOARD_MOTION_SCHEME_LABELS[KEYBOARD_MOTION_SCHEME_ARROW_PAGE])

    def _keyboard_motion_scheme_from_label(self, label: str) -> str:
        for scheme, scheme_label in KEYBOARD_MOTION_SCHEME_LABELS.items():
            if label == scheme_label or label == scheme:
                return scheme
        return KEYBOARD_MOTION_SCHEME_ARROW_PAGE

    @staticmethod
    def _camera_control_mode_label(mode: str) -> str:
        normalized = normalize_camera_control_mode(mode)
        return CAMERA_CONTROL_MODE_LABELS[normalized]

    @staticmethod
    def _camera_control_mode_from_label(label: str) -> str:
        normalized_label = label.strip().lower()
        for mode, mode_label in CAMERA_CONTROL_MODE_LABELS.items():
            if normalized_label == mode_label.lower() or normalized_label == mode:
                return mode
        return normalize_camera_control_mode(label)

    def _expected_admin_tokens(self) -> tuple[str, ...]:
        tokens: list[str] = []
        for variable_name in (ADMIN_TOKEN_ENV, WEB_ACCESS_TOKEN_ENV):
            token = os.environ.get(variable_name, "").strip()
            if token:
                tokens.append(token)
        return tuple(tokens)

    def enable_admin_mode(self) -> None:
        candidate = self.admin_token_var.get().strip()
        expected_tokens = self._expected_admin_tokens()
        if not expected_tokens:
            self.admin_mode_enabled = False
            self.admin_mode_status_var.set(f"Admin token missing. Set {ADMIN_TOKEN_ENV} or {WEB_ACCESS_TOKEN_ENV}.")
            self.status_var.set("Admin mode unavailable: no token configured.")
            self._update_admin_mode_controls()
            return
        if not candidate or not any(hmac.compare_digest(candidate, expected) for expected in expected_tokens):
            self.admin_mode_enabled = False
            self.admin_mode_status_var.set("Admin mode locked: invalid token.")
            self.status_var.set("Admin mode token rejected.")
            self._update_admin_mode_controls()
            return

        self.admin_mode_enabled = True
        self.admin_token_var.set("")
        self.admin_mode_status_var.set("Admin mode enabled for this session. Set-zero commands are unlocked.")
        self.status_var.set("Admin mode enabled.")
        self._update_admin_mode_controls()

    def _update_admin_mode_controls(self) -> None:
        state = "normal" if self.admin_mode_enabled else "disabled"
        for button_name in ("set_xyz_zero_button", "set_autofocus_z_zero_button"):
            button = getattr(self, button_name, None)
            if button is not None:
                button.configure(state=state)
        if self.serial_client is not None:
            self.serial_client.set_admin_mode_enabled(self.admin_mode_enabled)
        if not self.admin_mode_enabled and self.admin_mode_status_var.get() == "Admin mode locked":
            self.admin_mode_status_var.set("Admin mode locked. Set-zero commands are disabled.")

    def _require_admin_mode(self, action: str) -> bool:
        if self.admin_mode_enabled:
            return True
        message = f"{action} requires Config admin mode."
        self.status_var.set(message)
        logger.warning(message)
        self._update_admin_mode_controls()
        return False

    @staticmethod
    def _motor_speed_profile_label(profile: str) -> str:
        return MOTOR_SPEED_PROFILE_LABELS.get(profile, MOTOR_SPEED_PROFILE_LABELS[MOTOR_SPEED_PROFILE_FAST])

    @staticmethod
    def _motor_speed_profile_from_label(label: str) -> str:
        normalized = label.strip().lower()
        for profile, profile_label in MOTOR_SPEED_PROFILE_LABELS.items():
            if normalized == profile_label.lower() or normalized == profile:
                return profile
        return MOTOR_SPEED_PROFILE_FAST

    def _motion_speed_percent(self, profile: str | None = None) -> int:
        return self.probe_config.motor_speed_percent(profile)

    def _on_motor_speed_profile_selected(self, _event: tk.Event | None = None) -> None:
        profile = self._motor_speed_profile_from_label(self.motor_speed_profile_var.get())
        self.probe_config.active_motor_speed_profile = profile
        self.motor_speed_profile_var.set(self._motor_speed_profile_label(profile))
        self._update_config_display()
        try:
            save_probe_config(self.probe_config, self.config_path)
        except Exception as exc:
            self.status_var.set(f"Motor speed profile save failed: {exc}")
            self.config_status_var.set(f"Save failed: {exc}")
            logger.error("Failed to save motor speed profile: %s", exc)
            return
        label = self._motor_speed_profile_label(profile)
        self.status_var.set(f"Motor speed profile: {label} ({self.probe_config.motor_speed_percent()}%).")
        self.config_status_var.set(f"Saved {self.config_path.name}")

    def read_controller_motion_parameters(self) -> None:
        self._start_controller_motion_parameters_read("manual")

    def _start_controller_motion_parameters_read(self, source: str) -> None:
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            self.controller_motion_status_var.set("D5 read skipped: serial is not connected.")
            return

        self.controller_motion_status_var.set("Reading D5 controller parameters...")
        threading.Thread(target=self._read_controller_motion_parameters_worker, args=(source,), daemon=True).start()

    def _read_controller_motion_parameters_worker(self, source: str) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_xyz_motion_parameters()
            self.result_queue.put(("controller_motion_parameters", entries, source))
        except Exception as exc:
            self.result_queue.put(("controller_motion_parameters_error", exc, source))

    def _keyboard_event_key(self, event: tk.Event) -> str:
        keysym = str(getattr(event, "keysym", ""))
        return keysym.lower() if len(keysym) == 1 else keysym

    def _keyboard_event_targets_text_input(self, event: tk.Event) -> bool:
        widget = getattr(event, "widget", None)
        return isinstance(widget, (tk.Entry, tk.Text, tk.Spinbox, ttk.Entry, ttk.Spinbox, ttk.Combobox))

    def _keyboard_controls_enabled(self) -> bool:
        enabled_var = self.__dict__.get("keyboard_move_enabled_var")
        enabled = True if enabled_var is None else bool(enabled_var.get())
        return self.current_page == "Main" and enabled

    def _clear_held_keyboard_moves(self) -> None:
        for state in list(self.held_keys.values()):
            job = state.get("job")
            if job is not None:
                try:
                    self.after_cancel(str(job))
                except tk.TclError:
                    pass
        self.held_keys.clear()

    def _on_keyboard_move_toggle(self) -> None:
        if self.keyboard_move_enabled_var.get():
            self.status_var.set("KeyboardMove enabled.")
        else:
            self._clear_held_keyboard_moves()
            self.status_var.set("KeyboardMove disabled.")

    def cycle_jog_step(self, axis: str) -> str:
        levels = self.jog_step_levels[axis]
        try:
            current = int(self.step_vars[axis].get())
        except ValueError:
            current = levels[0]
        next_step = levels[0]
        for candidate in levels:
            if candidate > current:
                next_step = candidate
                break
        self.step_vars[axis].set(str(next_step))
        self.status_var.set(f"{axis} jog step set to {next_step}.")
        logger.info("%s jog step set to %s.", axis, next_step)
        return "break"

    def _on_key_press(self, event: tk.Event) -> str | None:
        if event.keysym in ("Shift_L", "Shift_R"):
            if self.current_page == "Main" and self.vision_panel:
                self.vision_panel.set_shift_down(True)
            return None
        if self._keyboard_event_targets_text_input(event):
            return None
        if not self._keyboard_controls_enabled():
            return None
        key = self._keyboard_event_key(event)
        binding = self.key_bindings.get(key)
        if binding is None:
            return None
        if key in self.held_keys:
            return "break"

        axis, reverse = binding
        self.held_keys[key] = {
            "axis": axis,
            "reverse": reverse,
            "interval_ms": 420,
            "job": None,
        }
        self._keyboard_move(key)
        return "break"

    def _on_key_release(self, event: tk.Event) -> str | None:
        if event.keysym in ("Shift_L", "Shift_R"):
            if self.current_page == "Main" and self.vision_panel:
                self.vision_panel.set_shift_down(False)
            return None
        key = self._keyboard_event_key(event)
        state = self.held_keys.pop(key, None)
        if state is None:
            return None

        job = state.get("job")
        if job is not None:
            try:
                self.after_cancel(str(job))
            except tk.TclError:
                pass
        return "break"

    def _on_mouse_wheel(self, event: tk.Event) -> str | None:
        if not self._z_wheel_enabled_for_event(event):
            return None
        if getattr(event, "num", None) == 4:
            return self._move_z_from_wheel(reverse=False)
        if getattr(event, "num", None) == 5:
            return self._move_z_from_wheel(reverse=True)
        delta = getattr(event, "delta", 0)
        if delta == 0:
            return None
        return self._move_z_from_wheel(reverse=delta < 0)

    def _z_wheel_enabled_for_event(self, event: tk.Event) -> bool:
        return (
            self.current_page == "Main"
            and self.vision_panel is not None
            and event.widget is self.vision_panel.canvas
        )

    def _move_z_from_wheel(self, reverse: bool) -> str:
        self._move_axis(axis="Z", reverse=reverse, source="wheel")
        return "break"

    def _keyboard_move(self, keysym: str) -> None:
        state = self.held_keys.get(keysym)
        if state is None:
            return
        if not self._keyboard_controls_enabled():
            self.held_keys.pop(keysym, None)
            return

        axis = str(state["axis"])
        reverse = bool(state["reverse"])
        self._move_axis(axis=axis, reverse=reverse, source="keyboard")

        next_interval = max(95, int(state["interval_ms"] * 0.78))
        state["interval_ms"] = next_interval
        state["job"] = self.after(next_interval, lambda k=keysym: self._keyboard_move(k))

    def schedule_position_edit(self, axis: str, mode: str) -> str:
        if self.position_click_job is not None:
            self.after_cancel(self.position_click_job)
        self.position_click_job = self.after(180, lambda a=axis, m=mode: self.begin_position_edit(a, m))
        return "break"

    def begin_position_edit(self, axis: str, mode: str) -> str:
        if self.position_click_job is not None:
            self.after_cancel(self.position_click_job)
            self.position_click_job = None
        if axis == "Z" and self.main_focusmap_plane_var.get():
            self._update_main_focusmap_z_display()
            self.status_var.set("FocusMap Z is enabled; Z follows the mapped plane.")
            return "break"

        starting_new_mode = self.current_position_edit_mode != mode
        self.current_position_edit_mode = mode

        if starting_new_mode:
            self.modified_position_axes.clear()
            for target_axis in ("X", "Y", "Z"):
                if target_axis == "Z" and self.main_focusmap_plane_var.get():
                    self.position_edit_modes[target_axis] = None
                    self._apply_focusmap_z_lock_to_position_entry()
                    continue
                self.position_inputs[target_axis].configure(state="normal")
                if mode == "Relative":
                    self.position_edit_modes[target_axis] = None
                    self.position_vars[target_axis].set(str(self.current_position_values[target_axis]))
                    self.position_inputs[target_axis].configure(fg=self.colors["accent"])
                    self.position_inputs[target_axis].configure(state="readonly", readonlybackground=self.colors["surface_2"])
                else:
                    self.position_edit_modes[target_axis] = None
                    self.position_vars[target_axis].set(str(self.current_position_values[target_axis]))
                    self.position_inputs[target_axis].configure(fg=self.colors["blue"])

        first_axis_edit = axis not in self.modified_position_axes
        self.position_edit_modes[axis] = mode
        self.modified_position_axes.add(axis)
        self.position_inputs[axis].configure(state="normal")
        self.position_inputs[axis].focus_set()
        if mode == "Relative":
            if first_axis_edit:
                self.position_vars[axis].set("")
            self.position_inputs[axis].configure(fg=self.colors["warning"])
            self.status_var.set("Relative coordinate input. Empty fields default to 0.")
        else:
            self.position_inputs[axis].configure(fg=self.colors["blue"])
            self.status_var.set("Absolute coordinate input. Empty fields default to current position.")
        self.position_inputs[axis].after_idle(lambda a=axis: self.position_inputs[a].icursor("end"))
        return "break"

    def focus_next_position_input(self, axis: str, _event: tk.Event) -> str:
        axes = ("X", "Y", "Z")
        self._fill_empty_position_default(axis)
        next_axis = axes[(axes.index(axis) + 1) % len(axes)]
        if next_axis == "Z" and self.main_focusmap_plane_var.get():
            next_axis = axes[(axes.index(next_axis) + 1) % len(axes)]
        self.begin_position_edit(next_axis, self.current_position_edit_mode or "Relative")
        return "break"

    def focus_previous_position_input(self, axis: str, _event: tk.Event) -> str:
        axes = ("X", "Y", "Z")
        self._fill_empty_position_default(axis)
        previous_axis = axes[(axes.index(axis) - 1) % len(axes)]
        if previous_axis == "Z" and self.main_focusmap_plane_var.get():
            previous_axis = axes[(axes.index(previous_axis) - 1) % len(axes)]
        self.begin_position_edit(previous_axis, self.current_position_edit_mode or "Relative")
        return "break"

    def _fill_empty_position_default(self, axis: str) -> None:
        if self.position_vars[axis].get().strip():
            return
        mode = self.position_edit_modes[axis] or self.current_position_edit_mode
        default_value = "0" if mode == "Relative" else str(self.current_position_values[axis])
        self.position_vars[axis].set(default_value)

    def clear_position_edits(self) -> None:
        self.modified_position_axes.clear()
        for axis in ("X", "Y", "Z"):
            self.position_edit_modes[axis] = None
            if axis in self.position_inputs:
                self.position_inputs[axis].configure(state="normal")
            self.position_vars[axis].set(str(self.current_position_values[axis]))
            if axis in self.position_inputs:
                self.position_inputs[axis].configure(fg=self.colors["accent"])
                self.position_inputs[axis].configure(state="readonly", readonlybackground=self.colors["surface_2"])
        self._update_main_focusmap_z_display()

    def _axis_is_actively_editing(self, axis: str) -> bool:
        return (
            axis in self.modified_position_axes
            and axis in self.position_inputs
            and self.focus_get() is self.position_inputs[axis]
        )

    def _telemetry_value(self, parent: ttk.Frame, title: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=title, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(16, 6))
        value = ttk.Label(parent, textvariable=variable, style="Value.TLabel", wraplength=300, padding=10)
        value.grid(row=row + 1, column=0, sticky="ew")

    def _status_value(self, parent: ttk.Frame, row: int) -> None:
        ttk.Label(parent, text="STATUS", style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(16, 6))
        value = ttk.Label(parent, textvariable=self.status_var, style="Status.TLabel", wraplength=300, padding=10)
        value.grid(row=row + 1, column=0, sticky="ew")

    def _bgr_with_scalebar(self, image_bgr):
        import cv2

        image = image_bgr.copy()
        height, width = image.shape[:2]
        um_per_px = self.probe_config.current_um_per_px()
        bar_color = (245, 245, 245)
        shadow_color = (0, 0, 0)
        x0 = 24
        y0 = height - 32
        if um_per_px is None or um_per_px <= 0:
            label = "N/A config"
            cv2.putText(image, label, (x0 + 1, y0 + 1), cv2.FONT_HERSHEY_SIMPLEX, 0.58, shadow_color, 3, cv2.LINE_AA)
            cv2.putText(image, label, (x0, y0), cv2.FONT_HERSHEY_SIMPLEX, 0.58, (80, 220, 255), 2, cv2.LINE_AA)
        else:
            target_px = width * 0.18
            candidates_um = (5, 10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000)
            best_um = min(candidates_um, key=lambda value: abs((value / um_per_px) - target_px))
            if self.probe_config.objective == 20 and self.probe_config.eyepiece == 1.5:
                default_px = 100.0 / um_per_px
                if 50 <= default_px <= width * 0.45:
                    best_um = 100
            bar_px = int(round(best_um / um_per_px))
            bar_px = max(20, min(bar_px, width - 2 * x0))
            x1 = x0 + bar_px
            cv2.line(image, (x0, y0), (x1, y0), shadow_color, 6, cv2.LINE_AA)
            cv2.line(image, (x0, y0), (x1, y0), bar_color, 3, cv2.LINE_AA)
            cv2.line(image, (x0, y0 - 7), (x0, y0 + 7), bar_color, 2, cv2.LINE_AA)
            cv2.line(image, (x1, y0 - 7), (x1, y0 + 7), bar_color, 2, cv2.LINE_AA)
            label = f"{best_um:g} um"
            cv2.putText(image, label, (x0 + 1, y0 - 13), cv2.FONT_HERSHEY_SIMPLEX, 0.58, shadow_color, 3, cv2.LINE_AA)
            cv2.putText(image, label, (x0, y0 - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.58, bar_color, 2, cv2.LINE_AA)

        return image

    def _ppm_with_scalebar(self, image_bgr) -> bytes:
        import cv2

        image = self._bgr_with_scalebar(image_bgr)
        rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
        out_height, out_width = rgb.shape[:2]
        header = f"P6 {out_width} {out_height} 255\n".encode("ascii")
        return header + rgb.tobytes()

    def apply_config(self, save: bool = True) -> bool:
        try:
            updated = ProbeConfig(
                objective=int(self.objective_var.get()),
                eyepiece=float(self.eyepiece_var.get()),
                microstep=int(self.microstep_var.get()),
                lead_xy_mm=float(self.lead_xy_var.get()),
                lead_z_mm=float(self.lead_z_var.get()),
                base_angle_deg=float(self.base_angle_var.get()),
                cc_speed_percent=int(self.cc_speed_percent_var.get()),
                fine_speed_percent=int(self.fine_speed_percent_var.get()),
                safe_speed_percent=int(self.safe_speed_percent_var.get()),
                active_motor_speed_profile=self._motor_speed_profile_from_label(self.motor_speed_profile_var.get()),
                controller_motion_parameters={
                    axis: {
                        field_name: int(self.controller_motion_parameter_vars[axis][field_name].get())
                        for field_name in ("minimum_speed", "work_speed", "acceleration")
                    }
                    for axis in JOG_STEP_AXES
                },
                camera_exposure_mode=self._camera_control_mode_from_label(self.camera_exposure_mode_var.get()),
                camera_exposure=float(self.camera_exposure_var.get() or 0.0),
                camera_gain_mode=self._camera_control_mode_from_label(self.camera_gain_mode_var.get()),
                camera_gain=float(self.camera_gain_var.get() or 0.0),
                cc_accel_time_s=float(self.cc_accel_time_var.get()),
                autofocus_settle_ms=int(self.autofocus_settle_ms_var.get()),
                autofocus_sample_count=int(self.autofocus_sample_count_var.get()),
                autofocus_peak_model=self._autofocus_peak_model_from_label(self.autofocus_peak_model_var.get()),
                imgstitch_settle_ms=int(self.imgstitch_settle_ms_var.get()),
                layoutbond_fov_width_um=float(self.layoutbond_fov_width_var.get()),
                layoutbond_fov_height_um=float(self.layoutbond_fov_height_var.get()),
                keyboard_motion_scheme=self._keyboard_motion_scheme_from_label(self.keyboard_motion_scheme_var.get()),
                jog_step_levels={
                    axis: parse_jog_step_levels_text(self.jog_step_level_vars[axis].get())
                    for axis in JOG_STEP_AXES
                },
                focus_threshold_yellow={
                    metric: float(self.focus_threshold_yellow_vars[metric].get())
                    for metric in ("Laplacian", "Tenengrad", "Brenner")
                },
                focus_threshold_green={
                    metric: float(self.focus_threshold_green_vars[metric].get())
                    for metric in ("Laplacian", "Tenengrad", "Brenner")
                },
                calibrations=dict(self.probe_config.calibrations),
                agent_api_key=self.agent_api_key_var.get(),
                agent_base_url=self.agent_base_url_var.get() or DEFAULT_AGENT_BASE_URL,
                agent_model=self.agent_model_var.get() or DEFAULT_AGENT_MODEL,
                agent_timeout_seconds=float(self.agent_timeout_var.get() or DEFAULT_AGENT_TIMEOUT_SECONDS),
            )
            updated.validate()
        except ValueError as exc:
            self.config_status_var.set(f"Invalid config: {exc}")
            self.status_var.set(f"Config invalid: {exc}")
            return False

        self.probe_config = updated
        derive_missing_calibrations(self.probe_config)
        self._sync_config_vars_from_config()
        self.agent_planner = self._build_agent_planner()
        self._update_config_display()
        self._refresh_keyboard_bindings()
        if save:
            try:
                save_probe_config(self.probe_config, self.config_path)
            except Exception as exc:
                self.config_status_var.set(f"Save failed: {exc}")
                self.status_var.set(f"Config save failed: {exc}")
                logger.error("Failed to save probe config: %s", exc)
                return False
            self.config_status_var.set(f"Saved {self.config_path.name}")
            self.status_var.set("Config saved.")
        return True

    def apply_camera_config(self) -> None:
        if not self.apply_config(save=True):
            return
        self.restart_camera()
        self.config_status_var.set(f"Saved {self.config_path.name}; camera restarted.")
        self.status_var.set("Camera config saved and applied.")

    def _camera_settings_from_config(self) -> CameraSettings:
        return CameraSettings(
            exposure_mode=self.probe_config.camera_exposure_mode,
            exposure=self.probe_config.camera_exposure,
            gain_mode=self.probe_config.camera_gain_mode,
            gain=self.probe_config.camera_gain,
        )

    def _sync_config_vars_from_config(self) -> None:
        self.objective_var.set(str(self.probe_config.objective))
        self.eyepiece_var.set(f"{self.probe_config.eyepiece:g}")
        self.microstep_var.set(str(self.probe_config.microstep))
        self.lead_xy_var.set(f"{self.probe_config.lead_xy_mm:g}")
        self.lead_z_var.set(f"{self.probe_config.lead_z_mm:g}")
        self.base_angle_var.set(f"{self.probe_config.base_angle_deg:g}")
        self.cc_speed_percent_var.set(str(self.probe_config.cc_speed_percent))
        self.fine_speed_percent_var.set(str(self.probe_config.fine_speed_percent))
        self.safe_speed_percent_var.set(str(self.probe_config.safe_speed_percent))
        self.motor_speed_profile_var.set(self._motor_speed_profile_label(self.probe_config.active_motor_speed_profile))
        for axis in JOG_STEP_AXES:
            for field_name in ("minimum_speed", "work_speed", "acceleration"):
                self.controller_motion_parameter_vars[axis][field_name].set(str(self.probe_config.controller_motion_parameters[axis][field_name]))
        self.camera_exposure_mode_var.set(self._camera_control_mode_label(self.probe_config.camera_exposure_mode))
        self.camera_exposure_var.set(f"{self.probe_config.camera_exposure:g}")
        self.camera_gain_mode_var.set(self._camera_control_mode_label(self.probe_config.camera_gain_mode))
        self.camera_gain_var.set(f"{self.probe_config.camera_gain:g}")
        self.cc_accel_time_var.set(f"{self.probe_config.cc_accel_time_s:g}")
        self.autofocus_settle_ms_var.set(str(self.probe_config.autofocus_settle_ms))
        self.autofocus_sample_count_var.set(str(self.probe_config.autofocus_sample_count))
        self.autofocus_peak_model_var.set(self._autofocus_peak_model_label(self.probe_config.autofocus_peak_model))
        self.imgstitch_settle_ms_var.set(str(self.probe_config.imgstitch_settle_ms))
        self.layoutbond_fov_width_var.set(f"{self.probe_config.layoutbond_fov_width_um:g}")
        self.layoutbond_fov_height_var.set(f"{self.probe_config.layoutbond_fov_height_um:g}")
        self.keyboard_motion_scheme_var.set(self._keyboard_motion_scheme_label(self.probe_config.keyboard_motion_scheme))
        self.jog_step_levels = {
            axis: tuple(self.probe_config.jog_step_levels[axis])
            for axis in JOG_STEP_AXES
        }
        for axis in JOG_STEP_AXES:
            self.jog_step_level_vars[axis].set(format_jog_step_levels(self.jog_step_levels[axis]))
        for metric in ("Laplacian", "Tenengrad", "Brenner"):
            self.focus_threshold_yellow_vars[metric].set(f"{self.probe_config.focus_threshold_yellow[metric]:g}")
            self.focus_threshold_green_vars[metric].set(f"{self.probe_config.focus_threshold_green[metric]:g}")
        self.agent_api_key_var.set(self.probe_config.agent_api_key)
        self.agent_base_url_var.set(self.probe_config.agent_base_url)
        self.agent_model_var.set(self.probe_config.agent_model)
        self.agent_timeout_var.set(f"{self.probe_config.agent_timeout_seconds:g}")

    @staticmethod
    def _autofocus_peak_model_label(model: str) -> str:
        normalized = normalize_autofocus_peak_model(model)
        return AUTOFOCUS_PEAK_MODEL_LABELS[normalized]

    @staticmethod
    def _autofocus_peak_model_from_label(label: str) -> str:
        normalized_label = label.strip().lower()
        for model, model_label in AUTOFOCUS_PEAK_MODEL_LABELS.items():
            if normalized_label == model_label.lower():
                return model
        return normalize_autofocus_peak_model(label)

    def _update_config_display(self) -> None:
        um_per_px = self.probe_config.current_um_per_px()
        lens_text = f"{self.probe_config.objective:g}x objective / {self.probe_config.eyepiece:g}x eyepiece"
        if um_per_px is None:
            self.calibration_status_var.set(f"{lens_text}: not calibrated")
        else:
            self.calibration_status_var.set(f"{lens_text}: {um_per_px:.6g} um/px")
        conversion_lines = [
            f"Steps/rev: {self.probe_config.steps_per_revolution:.6g}",
            f"X: {self.probe_config.um_per_pulse('X'):.6g} um/pulse, {self.probe_config.pulses_per_um('X'):.6g} pulse/um",
            f"Y: {self.probe_config.um_per_pulse('Y'):.6g} um/pulse, {self.probe_config.pulses_per_um('Y'):.6g} pulse/um",
            f"Z: {self.probe_config.um_per_pulse('Z'):.6g} um/pulse, {self.probe_config.pulses_per_um('Z'):.6g} pulse/um",
            (
                f"Motor speed: {self._motor_speed_profile_label(self.probe_config.active_motor_speed_profile)} "
                f"{self.probe_config.motor_speed_percent()}% "
                f"(Fast {self.probe_config.cc_speed_percent}%, Fine {self.probe_config.fine_speed_percent}%, Safe {self.probe_config.safe_speed_percent}%)"
            ),
            (
                "D5 controller: "
                + "; ".join(
                    f"{axis} min {params['minimum_speed']}, work {params['work_speed']}, accel {params['acceleration']}"
                    for axis, params in self.probe_config.controller_motion_parameters.items()
                )
            ),
            (
                "Camera: "
                f"exposure {self._camera_control_mode_label(self.probe_config.camera_exposure_mode)} "
                f"{self.probe_config.camera_exposure:g}, "
                f"gain {self._camera_control_mode_label(self.probe_config.camera_gain_mode)} "
                f"{self.probe_config.camera_gain:g}"
            ),
            f"CC accel/decel: {self.probe_config.cc_accel_time_s:.3g}s ({self.probe_config.cc_acceleration_units()} units)",
            f"AF settle: {self.probe_config.autofocus_settle_ms} ms",
            f"AF integration: {self.probe_config.autofocus_sample_count} frame(s)",
            f"AF peak model: {self._autofocus_peak_model_label(self.probe_config.autofocus_peak_model)}",
            f"Stitch settle: {self.probe_config.imgstitch_settle_ms} ms",
            f"LayoutBond FOV: {self.probe_config.layoutbond_fov_width_um:g} x {self.probe_config.layoutbond_fov_height_um:g} um",
            f"Keyboard: {self._keyboard_motion_scheme_label(self.probe_config.keyboard_motion_scheme)}",
            "Jog levels: "
            + "; ".join(
                f"{axis} {format_jog_step_levels(self.probe_config.jog_step_levels[axis])}"
                for axis in JOG_STEP_AXES
            ),
        ]
        self.motor_conversion_var.set("\n".join(conversion_lines))

    def open_pixel_calibration(self) -> None:
        if not self.apply_config(save=True):
            return
        with self.camera_lock:
            image_bgr = None if self.latest_stitch_frame is None else self.latest_stitch_frame.copy()
        if image_bgr is None:
            self.config_status_var.set("No camera frame available for calibration.")
            self.status_var.set("No camera frame available for calibration.")
            return

        dialog = PixelCalibrationDialog(self, image_bgr, self.colors)
        self.wait_window(dialog)
        if dialog.result_um_per_px is None:
            return

        try:
            self.probe_config.set_calibration(self.probe_config.objective, self.probe_config.eyepiece, dialog.result_um_per_px)
            derive_missing_calibrations(self.probe_config)
            save_probe_config(self.probe_config, self.config_path)
        except Exception as exc:
            self.config_status_var.set(f"Calibration save failed: {exc}")
            self.status_var.set(f"Calibration save failed: {exc}")
            logger.error("Failed to save calibration: %s", exc)
            return
        self._update_config_display()
        self.config_status_var.set(f"Calibration saved: {dialog.result_um_per_px:.6g} um/px")
        self.status_var.set("Pixel calibration saved.")

    def _append_hex_history(self, direction: str, message: str) -> None:
        if not hasattr(self, "hex_history"):
            return

        self.hex_history.configure(state="normal")
        self.hex_history.insert("end", f"{direction:<3} ", direction.lower())
        self._insert_hex_frame(message)
        self.hex_history.insert("end", "\n")
        self.hex_history.see("end")
        self.hex_history.configure(state="disabled")

    def _insert_hex_frame(self, message: str) -> None:
        parts = message.split()
        if len(parts) not in (12, 33):
            self.hex_history.insert("end", message or "-", "data")
            return

        for index, part in enumerate(parts):
            if index > 0:
                self.hex_history.insert("end", " ")
            if index == 0:
                tag = "head"
            elif index == 1:
                tag = "function"
            elif len(parts) == 12 and index == 2:
                tag = "axis"
            elif len(parts) == 12 and 3 <= index <= 8:
                tag = "data"
            elif len(parts) == 33 and 2 <= index <= 29:
                tag = "data"
            elif index == len(parts) - 3:
                tag = "checksum"
            else:
                tag = "tail"
            self.hex_history.insert("end", part, tag)

    @staticmethod
    def _record_hex_history_for_source(source: str) -> bool:
        motion_sources = {
            "af_plane",
            "autofocus",
            "autofocus manual",
            "button",
            "focusmap",
            "gds_mapper",
            "go_zero",
            "imgstitch",
            "keyboard",
            "vision",
            "wheel",
        }
        return source not in motion_sources

    @staticmethod
    def _is_low_latency_jog_source(source: str) -> bool:
        return source in {"keyboard", "wheel"}

    def _update_axis_position(self, position: AxisPosition) -> None:
        axis_name = position.axis_name
        if axis_name in self.position_vars:
            self.current_position_values[axis_name] = position.position
            if axis_name == "Z":
                self.autofocus_z_var.set(str(position.position))
                if hasattr(self, "autofocus_z_label"):
                    self.autofocus_z_label.configure(fg=self.colors["accent"])
            if not self._axis_is_actively_editing(axis_name):
                self.modified_position_axes.discard(axis_name)
                self.position_edit_modes[axis_name] = None
                if axis_name in self.position_inputs:
                    self.position_inputs[axis_name].configure(state="normal")
                    self.position_inputs[axis_name].configure(fg=self.colors["accent"])
                self.position_vars[axis_name].set(str(position.position))
                if axis_name in self.position_inputs:
                    self.position_inputs[axis_name].configure(state="readonly", readonlybackground=self.colors["surface_2"])
        if self.main_focusmap_plane_var.get() and axis_name in {"X", "Y", "Z"}:
            self._update_main_focusmap_z_display()

    def _show_target_positions(self, targets: dict[str, int]) -> None:
        for axis, value in targets.items():
            if axis not in self.position_vars:
                continue
            self.position_vars[axis].set(str(value))
            if axis == "Z":
                self.autofocus_z_var.set(str(value))
                if hasattr(self, "autofocus_z_label"):
                    self.autofocus_z_label.configure(fg=self.colors["danger"])
            if axis in self.position_inputs:
                self.position_inputs[axis].configure(state="normal")
                self.position_inputs[axis].configure(fg=self.colors["danger"])
                self.position_inputs[axis].configure(state="readonly", readonlybackground=self.colors["surface_2"])
        self._apply_focusmap_z_lock_to_position_entry()

    def _update_focus_scores(self, scores: dict[str, float], timestamp: float | None = None) -> None:
        metric = self.focus_metric_var.get()
        score = float(scores.get(metric, 0.0))
        now = float(timestamp if timestamp is not None else time.monotonic())
        with self.focus_lock:
            self.latest_focus_scores = dict(scores)
            self.latest_focus_timestamp = now
            if not self.autofocus_running:
                self.focus_history.append((now, dict(scores)))
                cutoff = now - self._focus_window_seconds(default=30)
                self.focus_history = [(timestamp, values) for timestamp, values in self.focus_history if timestamp >= cutoff]
                self.autofocus_samples = [(timestamp, value, direction) for timestamp, value, direction in self.autofocus_samples if timestamp >= cutoff]
        self.focus_score_var.set(f"{metric}: {score:.2f}")
        if not self.autofocus_running:
            self._draw_focus_history()

    def _on_focus_metric_changed(self) -> None:
        metric = self.focus_metric_var.get()
        with self.focus_lock:
            score = float(self.latest_focus_scores.get(metric, 0.0))
        self.focus_score_var.set(f"{metric}: {score:.2f}")
        self._draw_focus_history()
        self._draw_autofocus_z_score()

    def _focus_window_seconds(self, default: int = 30) -> int:
        try:
            return max(5, int(self.focus_window_var.get()))
        except ValueError:
            return default

    def _focus_threshold(self, metric: str) -> float:
        return self._focus_green_threshold(metric)

    def _focus_yellow_threshold(self, metric: str) -> float:
        return float(self.probe_config.focus_threshold_yellow.get(metric, 0.0))

    def _focus_green_threshold(self, metric: str) -> float:
        return float(self.probe_config.focus_threshold_green.get(metric, 0.0))

    def _focus_score_color(self, metric: str, value: float) -> str:
        if value >= self._focus_green_threshold(metric):
            return self.colors["accent"]
        if value >= self._focus_yellow_threshold(metric):
            return self.colors["warning"]
        return self.colors["danger"]

    def _draw_focus_history(self) -> None:
        if not hasattr(self, "focus_canvas"):
            return
        metric = self.focus_metric_var.get()
        now = time.monotonic()
        window_seconds = self._focus_window_seconds()
        yellow_threshold = self._focus_yellow_threshold(metric)
        green_threshold = self._focus_green_threshold(metric)
        start_time = now - window_seconds
        end_time = now
        canvas = self.focus_canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=self.colors["surface_2"], outline="")
        canvas.create_text(12, 12, text=f"{metric} | last {window_seconds} s | yellow {yellow_threshold:g} | green {green_threshold:g}", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))

        with self.focus_lock:
            history = [
                (timestamp, values.get(metric, 0.0))
                for timestamp, values in self.focus_history
                if timestamp >= start_time
            ]
            samples = [(timestamp, value, direction) for timestamp, value, direction in self.autofocus_samples if timestamp >= start_time]
            run_start = self.autofocus_run_start_time
            run_end = self.autofocus_run_end_time
        if len(history) < 2:
            canvas.create_text(width // 2, height // 2, text="Waiting for camera frames", fill=self.colors["muted"], font=("Segoe UI Semibold", 12))
            return

        values = [value for _, value in history]
        min_value = 0.0
        max_value = max(values)
        if samples:
            max_value = max(max_value, max(value for _, value, _ in samples))
        span = max(max_value - min_value, 1.0)
        time_span = max(end_time - start_time, 1.0)
        left, top, right, bottom = 42, 28, width - 14, height - 28
        canvas.create_rectangle(left, top, right, bottom, fill="", outline=self.colors["border"])
        canvas.create_line(left, bottom, right, bottom, fill=self.colors["border"])
        canvas.create_line(left, top, left, bottom, fill=self.colors["border"])
        if run_start is not None:
            shade_start = max(run_start, start_time)
            shade_end = min(run_end or now, end_time)
            if shade_end > shade_start:
                x1 = left + (shade_start - start_time) / time_span * (right - left)
                x2 = left + (shade_end - start_time) / time_span * (right - left)
                canvas.create_rectangle(x1, top, x2, bottom, fill="#102a24", outline="")
                canvas.create_text(x1 + 6, top + 6, text="AF", anchor="nw", fill=self.colors["accent"], font=("Segoe UI Semibold", 8))
        points_by_segment: list[tuple[list[float], str]] = []
        current_points: list[float] = []
        current_color = ""
        for timestamp, value in history:
            x = left + (timestamp - start_time) / time_span * (right - left)
            y = bottom - (value - min_value) / span * (bottom - top)
            color = self._focus_score_color(metric, value)
            if current_color and color != current_color:
                if len(current_points) >= 4:
                    points_by_segment.append((current_points, current_color))
                current_points = current_points[-2:]
            current_color = color
            current_points.extend((x, y))
        if len(current_points) >= 4:
            points_by_segment.append((current_points, current_color))
        for points, color in points_by_segment:
            canvas.create_line(*points, fill=color, width=2, smooth=True)
        for timestamp, value, direction in samples:
            if timestamp < start_time or timestamp > end_time:
                continue
            x = left + (timestamp - start_time) / time_span * (right - left)
            y = bottom - (value - min_value) / span * (bottom - top)
            color = "#60a5fa" if direction >= 0 else "#fbbf24"
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="")
            canvas.create_line(x, top, x, bottom, fill="#263545", dash=(2, 4))
        canvas.create_text(left, top, text=f"{max_value:.1f}", anchor="sw", fill=self.colors["muted"], font=("Segoe UI", 8))
        canvas.create_text(left, bottom, text="0", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 8))
        canvas.create_text(left, bottom + 8, text=f"-{window_seconds}s", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 8))
        canvas.create_text(right, bottom + 8, text="now", anchor="ne", fill=self.colors["muted"], font=("Segoe UI", 8))

    def _draw_autofocus_z_score(self) -> None:
        if not hasattr(self, "z_score_canvas"):
            return
        self._draw_af_z_score_plot(self.z_score_canvas, compact=False)

    @staticmethod
    def _scores_by_z_from_af_samples(samples: list[dict[str, object]]) -> dict[int, float]:
        grouped_scores: dict[int, list[float]] = {}
        for sample in samples:
            z_value = int(sample["z"])
            grouped_scores.setdefault(z_value, []).append(float(sample["score"]))
        return {
            z_value: sum(scores) / len(scores)
            for z_value, scores in grouped_scores.items()
            if scores
        }

    def _draw_af_z_score_plot(self, canvas: tk.Canvas, compact: bool = False) -> None:
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=self.colors["surface_2"], outline="")
        title_y = 10 if compact else 12
        canvas.create_text(12, title_y, text="AF Z vs Score", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))
        with self.focus_lock:
            samples = [dict(sample) for sample in self.autofocus_z_score_samples]
            fine_range = self.autofocus_fine_range
        if len(samples) < 2:
            canvas.create_text(width // 2, height // 2, text="Waiting for AF samples", fill=self.colors["muted"], font=("Segoe UI Semibold", 11))
            return

        z_values = [float(sample["z"]) for sample in samples]
        score_values = [float(sample["score"]) for sample in samples]
        min_z, max_z = min(z_values), max(z_values)
        fine_samples = [sample for sample in samples if str(sample.get("stage", "")) == "fine"]
        # Fit only the precision sweep, independent of the display color thresholds.
        fit_scores = self._scores_by_z_from_af_samples(fine_samples)
        fit_model = self._fit_focus_peak_model(fit_scores, self.probe_config.autofocus_peak_model)
        min_score, max_score = 0.0, max(score_values)
        if fit_model is not None:
            max_score = max(max_score, fit_model["baseline"] + fit_model["amplitude"])
        z_span = max(max_z - min_z, 1)
        score_span = max(max_score - min_score, 1.0)
        left = 46 if compact else 50
        top = 30 if compact else 28
        right = width - (16 if compact else 18)
        bottom = height - (30 if compact else 34)
        metric = self.focus_metric_var.get()

        def point_xy(z_value: float, score: float) -> tuple[float, float]:
            x_value = left + (z_value - min_z) / z_span * (right - left)
            y_value = bottom - (score - min_score) / score_span * (bottom - top)
            return x_value, y_value

        if fine_range is not None:
            range_start, range_end = sorted((float(fine_range[0]), float(fine_range[1])))
            range_x1, _ = point_xy(range_start, min_score)
            range_x2, _ = point_xy(range_end, min_score)
            range_x1 = max(left, min(right, range_x1))
            range_x2 = max(left, min(right, range_x2))
            if range_x2 > range_x1:
                canvas.create_rectangle(range_x1, top, range_x2, bottom, fill="#38bdf8", outline="", stipple="gray12")

        canvas.create_rectangle(left, top, right, bottom, outline=self.colors["border"])

        if fit_model is not None:
            curve_points: list[float] = []
            curve_start = min_z
            curve_end = max_z
            if fine_range is not None:
                curve_start = max(curve_start, min(fine_range))
                curve_end = min(curve_end, max(fine_range))
            for index in range(80):
                z_value = curve_start + index * max(curve_end - curve_start, 1.0) / 79
                fit_score = self._focus_peak_score_at(z_value, fit_model)
                x, y = point_xy(z_value, fit_score)
                curve_points.extend((x, y))
            if len(curve_points) >= 4:
                canvas.create_line(*curve_points, fill="#e5e7eb", width=2, smooth=True)
            mu_x, _ = point_xy(fit_model["mu"], min_score)
            canvas.create_line(mu_x, top, mu_x, bottom, fill="#e5e7eb", dash=(4, 3))
            canvas.create_text(
                right - 8,
                top + (8 if not compact else 18),
                text=f"{fit_model['label']} | mu {fit_model['mu']:.1f} | w {fit_model['width']:.1f}",
                anchor="ne",
                fill="#e5e7eb",
                font=("Segoe UI", 8),
            )

        def draw_stage_points(stage_names: set[str], radius: int, color_override: str | None = None) -> None:
            for sample in samples:
                stage = str(sample.get("stage", "sample"))
                if stage not in stage_names:
                    continue
                z_value = float(sample["z"])
                score = float(sample["score"])
                x, y = point_xy(z_value, score)
                color = color_override or self._focus_score_color(metric, score)
                outline = "#0b0f14" if color_override else "#e5edf5"
                canvas.create_oval(x - radius, y - radius, x + radius, y + radius, fill=color, outline=outline)

        radius = 3 if compact else 4
        draw_stage_points({"center", "coarse"}, radius, "#64748b")
        draw_stage_points({"fine"}, radius + 1, None)
        draw_stage_points({"final"}, radius + 1, "#e5e7eb")
        draw_stage_points({"return_center", "sample"}, radius, "#94a3b8")
        canvas.create_text(left, bottom + 10, text=str(min_z), anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 8))
        canvas.create_text(right, bottom + 10, text=str(max_z), anchor="ne", fill=self.colors["muted"], font=("Segoe UI", 8))
        canvas.create_text(left, top, text=f"{max_score:.1f}", anchor="sw", fill=self.colors["muted"], font=("Segoe UI", 8))

    def autofocus_manual_z(self, reverse: bool) -> None:
        if self.motion_busy:
            self.autofocus_status_var.set("Motion busy")
            self.status_var.set("Motion is busy; Z manual focus move skipped.")
            logger.warning("AutoFocus manual Z move skipped because motion is busy.")
            return
        try:
            pulses = int(self.autofocus_step_var.get())
        except ValueError:
            self.autofocus_status_var.set("Initial step must be an integer.")
            return
        pulses = max(pulses, 1)
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            self.autofocus_status_var.set("Serial not connected")
            return

        target = self.current_position_values["Z"] + (-pulses if reverse else pulses)
        self._show_target_positions({"Z": target})
        self.motion_busy = True
        direction = "Z-" if reverse else "Z+"
        self.autofocus_status_var.set(f"Manual {direction}: {pulses} pulses")
        self.status_var.set(f"AutoFocus manual {direction}: {pulses} pulses.")
        logger.info("AutoFocus manual %s requested: %s pulses.", direction, pulses)
        threading.Thread(target=self._move_axis_worker, args=("Z", Axis.Z, reverse, pulses, "autofocus manual", "Relative", {"Z": target}), daemon=True).start()

    def set_autofocus_z_zero(self) -> None:
        if not self._require_admin_mode("Set Z=0"):
            return
        if self.motion_busy:
            self.autofocus_status_var.set("Motion busy")
            self.status_var.set("Motion is busy; Set Z=0 skipped.")
            logger.warning("Set Z=0 skipped because motion is busy.")
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            self.autofocus_status_var.set("Serial not connected")
            return

        self.motion_busy = True
        self.autofocus_status_var.set("Setting Z to 0")
        self.status_var.set("Setting current Z position to 0.")
        threading.Thread(target=self._set_autofocus_z_zero_worker, daemon=True).start()

    def _set_autofocus_z_zero_worker(self) -> None:
        assert self.serial_client is not None
        try:
            if not self.admin_mode_enabled:
                raise PermissionError("Set Z=0 requires Config admin mode.")
            command = self.serial_client.clear_position(Axis.Z)
            self.result_queue.put(("zero_z_command", command))
            time.sleep(0.1)
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "zero_z"))
            logger.info("Set Z=0 command sent: %s", colorize_hex_frame(hex_bytes(command), "TX"))
        except Exception as exc:
            self.result_queue.put(("motor_error", "ZERO_Z", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def set_xyz_zero(self) -> None:
        if not self._require_admin_mode("Set New Zero"):
            return
        if self.motion_busy:
            self.status_var.set("Motion is busy; Set New Zero skipped.")
            logger.warning("Set New Zero skipped because motion is busy.")
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.motion_busy = True
        self.clear_position_edits()
        self.status_var.set("Setting current X/Y/Z position to zero.")
        threading.Thread(target=self._set_xyz_zero_worker, daemon=True).start()

    def _set_xyz_zero_worker(self) -> None:
        assert self.serial_client is not None
        try:
            if not self.admin_mode_enabled:
                raise PermissionError("Set New Zero requires Config admin mode.")
            command = self.serial_client.clear_position(Axis.ALL)
            self.result_queue.put(("zero_xyz_command", command))
            time.sleep(0.1)
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "zero_xyz"))
            logger.info("Set XYZ=0 command sent: %s", colorize_hex_frame(hex_bytes(command), "TX"))
        except Exception as exc:
            self.result_queue.put(("motor_error", "ZERO_XYZ", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def go_xyz_zero(self) -> None:
        if self.motion_busy:
            self.status_var.set("Motion is busy; Go Zero skipped.")
            logger.warning("Go Zero skipped because motion is busy.")
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        axes = tuple(axis for axis in ("X", "Y", "Z") if self.current_position_values[axis] != 0)
        if not axes:
            self.status_var.set("Already at X/Y/Z zero.")
            return

        self.motion_busy = True
        self.clear_position_edits()
        targets = {axis: 0 for axis in axes}
        self._show_target_positions(targets)
        self.status_var.set("Moving X/Y/Z to zero.")
        threading.Thread(target=self._go_xyz_zero_worker, args=(axes, targets), daemon=True).start()

    def _go_xyz_zero_worker(self, axes: tuple[str, ...], expected_targets: dict[str, int]) -> None:
        assert self.serial_client is not None
        try:
            axis_params: dict[Axis, tuple[bool, int, int, int]] = {}
            for axis_name in axes:
                controller_axis = self._controller_axis(axis_name)
                if controller_axis is None:
                    continue
                delta = -self.current_position_values[axis_name]
                axis_params[controller_axis] = self._cc_axis_param(delta < 0, abs(delta))
            if not any(params[1] for params in axis_params.values()):
                raise ValueError("Go Zero requires at least one non-zero axis position.")

            command, completed = self.serial_client.move_multi_axis_relative_and_wait(axis_params, timeout=self._cc_move_timeout(axis_params))
            self.result_queue.put(("motor_command", "XYZ", "go zero", command, "go_zero"))
            self.result_queue.put(("cc_done", completed, "go_zero"))
            self.result_queue.put(("moving",))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "go_zero", expected_targets))
        except Exception as exc:
            self.result_queue.put(("motor_error", "GO_ZERO", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def start_autofocus(self) -> None:
        if self.autofocus_running:
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return
        try:
            metric = self.focus_metric_var.get()
            initial_step = int(self.autofocus_step_var.get())
            min_step = int(self.autofocus_min_step_var.get())
            search_range = int(self.autofocus_max_moves_var.get())
            yellow_threshold = self._focus_yellow_threshold(metric)
            green_threshold = self._focus_green_threshold(metric)
        except ValueError:
            self.autofocus_status_var.set("AutoFocus settings must be integers.")
            return
        if initial_step <= 0 or min_step <= 0 or search_range <= 0:
            self.autofocus_status_var.set("AutoFocus settings must be positive.")
            return
        if green_threshold < yellow_threshold:
            self.autofocus_status_var.set("Config threshold invalid")
            self.status_var.set("Config invalid: green focus threshold must be >= yellow.")
            return

        self.autofocus_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.autofocus_restore_home_signal = self.home_signal_enabled
        if self.home_signal_enabled:
            self.disable_home_signal_polling()

        self.autofocus_running = True
        run_start = time.monotonic()
        with self.focus_lock:
            self.autofocus_samples.clear()
            self.autofocus_z_score_samples.clear()
            self.autofocus_fine_range = None
            self.autofocus_history_rows.clear()
            self.focus_history.clear()
            self.autofocus_run_start_time = run_start
            self.autofocus_run_end_time = None
        self._draw_autofocus_z_score()
        self._draw_focus_history()
        self.autofocus_stop_event.clear()
        self.motion_busy = True
        self.autofocus_status_var.set("Running")
        self.status_var.set(f"AutoFocus running on Z axis within +/-{search_range}.")
        self.autofocus_thread = threading.Thread(target=self._autofocus_worker, args=(metric, initial_step, min_step, search_range), daemon=True)
        self.autofocus_thread.start()

    def stop_autofocus(self) -> None:
        self.autofocus_stop_event.set()
        self.autofocus_status_var.set("Stopping")

    def _current_focus_score(self, metric: str) -> float:
        with self.focus_lock:
            return float(self.latest_focus_scores.get(metric, 0.0))

    def _current_focus_snapshot(self, metric: str) -> tuple[float, float]:
        with self.focus_lock:
            return self.latest_focus_timestamp, float(self.latest_focus_scores.get(metric, 0.0))

    def _sample_focus_scores(self, after_time: float | None = None, settle_delay: float = 0.0, duration: float = 0.36) -> dict[str, float]:
        if settle_delay > 0:
            time.sleep(settle_delay)
        deadline = time.monotonic() + max(duration, 0.12)
        samples: list[dict[str, float]] = []
        while time.monotonic() < deadline and not self.autofocus_stop_event.is_set():
            with self.focus_lock:
                timestamp = self.latest_focus_timestamp
                scores = dict(self.latest_focus_scores)
            if after_time is None or timestamp > after_time:
                samples.append(scores)
            time.sleep(0.03)
        if samples:
            return {
                metric_name: sum(sample.get(metric_name, 0.0) for sample in samples) / len(samples)
                for metric_name in ("Laplacian", "Tenengrad", "Brenner")
            }

        fallback_deadline = time.monotonic() + 0.8
        while time.monotonic() < fallback_deadline and not self.autofocus_stop_event.is_set():
            with self.focus_lock:
                timestamp = self.latest_focus_timestamp
                scores = dict(self.latest_focus_scores)
            if after_time is None or timestamp > after_time:
                return scores
            time.sleep(0.03)
        with self.focus_lock:
            return dict(self.latest_focus_scores)

    def _sample_focus_score_frames(
        self,
        after_time: float,
        sample_count: int,
        timeout: float = 2.0,
        discard_count: int = 0,
    ) -> tuple[dict[str, float], dict[str, object]]:
        sample_count = max(1, int(sample_count))
        discard_count = max(0, int(discard_count))
        deadline = time.monotonic() + timeout
        samples: list[dict[str, float]] = []
        sample_timestamps: list[float] = []
        last_timestamp = after_time
        discarded = 0
        latest_fresh_scores: dict[str, float] | None = None

        while len(samples) < sample_count and time.monotonic() < deadline and not self.autofocus_stop_event.is_set():
            with self.focus_lock:
                timestamp = self.latest_focus_timestamp
                scores = dict(self.latest_focus_scores)
            if timestamp > last_timestamp:
                last_timestamp = timestamp
                latest_fresh_scores = scores
                if discarded < discard_count:
                    discarded += 1
                    continue
                samples.append(scores)
                sample_timestamps.append(timestamp)
            else:
                time.sleep(0.01)

        stats = {
            "requested_samples": sample_count,
            "discard_requested": discard_count,
            "discarded_frames": discarded,
            "sampled_frames": len(samples),
            "sample_after": after_time,
            "first_sample_delay_ms": ((sample_timestamps[0] - after_time) * 1000.0) if sample_timestamps else None,
            "last_sample_delay_ms": ((sample_timestamps[-1] - after_time) * 1000.0) if sample_timestamps else None,
            "sample_timed_out": len(samples) < sample_count,
        }

        if not samples:
            if latest_fresh_scores is not None:
                return latest_fresh_scores, stats
            with self.focus_lock:
                return dict(self.latest_focus_scores), stats

        return {
            metric_name: sum(sample.get(metric_name, 0.0) for sample in samples) / len(samples)
            for metric_name in ("Laplacian", "Tenengrad", "Brenner")
        }, stats

    def _sample_focus_score(self, metric: str, duration: float = 0.36, after_time: float | None = None) -> float:
        return self._sample_focus_scores(after_time=after_time, duration=duration).get(metric, 0.0)

    @staticmethod
    def _format_optional_float(value: object) -> str:
        if value is None or value == "":
            return ""
        try:
            return f"{float(value):.1f}"
        except (TypeError, ValueError):
            return str(value)

    def _record_autofocus_sample(
        self,
        metric: str,
        z_position: int,
        score: float,
        direction: int,
        command_hex: str = "",
        reached_hex: str = "",
        stage: str = "sample",
        scores: dict[str, float] | None = None,
        target_z: int | None = None,
        readback_z: int | None = None,
        move_delta: int = 0,
        reached_wait_seconds: float | None = None,
        sample_stats: dict[str, object] | None = None,
    ) -> None:
        sample_stats = dict(sample_stats or {})
        target_z = z_position if target_z is None else target_z
        readback_z = z_position if readback_z is None else readback_z
        with self.focus_lock:
            timestamp = self.latest_focus_timestamp or time.monotonic()
            scores = dict(scores or self.latest_focus_scores)
            self.autofocus_samples.append((timestamp, score, direction))
            self.focus_history.append((timestamp, scores))
            self.autofocus_z_score_samples.append({"z": z_position, "score": score, "direction": direction, "stage": stage})
            self.autofocus_history_rows.append(
                {
                    "timestamp": f"{timestamp:.6f}",
                    "stage": stage,
                    "z_position": z_position,
                    "target_z": target_z,
                    "readback_z": readback_z,
                    "move_delta": move_delta,
                    "direction": direction,
                    "selected_metric": metric,
                    "selected_score": f"{score:.6f}",
                    "laplacian": f"{scores.get('Laplacian', 0.0):.6f}",
                    "tenengrad": f"{scores.get('Tenengrad', 0.0):.6f}",
                    "brenner": f"{scores.get('Brenner', 0.0):.6f}",
                    "reached_wait_ms": f"{reached_wait_seconds * 1000.0:.1f}" if reached_wait_seconds is not None else "",
                    "settle_ms": f"{float(sample_stats.get('settle_seconds', 0.0)) * 1000.0:.1f}",
                    "discarded_frames": sample_stats.get("discarded_frames", ""),
                    "sampled_frames": sample_stats.get("sampled_frames", ""),
                    "first_frame_delay_ms": self._format_optional_float(sample_stats.get("first_sample_delay_ms")),
                    "last_frame_delay_ms": self._format_optional_float(sample_stats.get("last_sample_delay_ms")),
                    "sample_timed_out": sample_stats.get("sample_timed_out", ""),
                    "command_hex": command_hex,
                    "reached_hex": reached_hex,
                }
            )
            ppm_bytes = self.latest_focus_frame_ppm
        logger.info(
            "AF sample %s target=%s readback=%s delta=%s reached=%s ms sampled=%s discarded=%s %s=%.3f",
            stage,
            target_z,
            readback_z,
            move_delta,
            f"{reached_wait_seconds * 1000.0:.1f}" if reached_wait_seconds is not None else "-",
            sample_stats.get("sampled_frames", "-"),
            sample_stats.get("discarded_frames", "-"),
            metric,
            score,
        )
        self.result_queue.put(("autofocus_sample", z_position, score, direction, ppm_bytes))

    def _autofocus_worker(self, metric: str, initial_step: int, min_step: int, search_range: int) -> None:
        assert self.serial_client is not None
        try:
            self._run_autofocus_sequence(metric, initial_step, min_step, search_range, source="autofocus", status_event="autofocus_status")
        except Exception as exc:
            self.result_queue.put(("autofocus_error", exc))
        finally:
            self._write_autofocus_history_file()
            self.result_queue.put(("autofocus_done",))

    def _run_autofocus_sequence(
        self,
        metric: str,
        initial_step: int,
        min_step: int,
        search_range: int,
        source: str = "autofocus",
        status_event: str = "autofocus_status",
    ) -> dict[str, float | int | bool]:
        assert self.serial_client is not None
        best_score = -1.0
        best_z = self.current_position_values["Z"]
        yellow_threshold = self._focus_yellow_threshold(metric)
        entries = self.serial_client.read_xyz_positions()
        self.result_queue.put(("read_positions", entries, source))
        center_z = self._z_from_position_entries(entries)
        lower_bound = center_z - search_range
        upper_bound = center_z + search_range
        current_z = center_z
        coarse_scores: dict[int, float] = {}
        fine_scores: dict[int, float] = {}

        center_scores, center_sample_stats = self._sample_after_motion_settles()
        best_score = center_scores.get(metric, 0.0)
        coarse_scores[center_z] = best_score
        self._record_autofocus_sample(
            metric,
            center_z,
            best_score,
            0,
            stage="center",
            scores=center_scores,
            target_z=center_z,
            readback_z=center_z,
            sample_stats=center_sample_stats,
        )

        self.result_queue.put((status_event, f"Center {center_z}, coarse step {initial_step}, min step {min_step}"))
        coarse_offsets = self._coarse_wobble_offsets(initial_step, search_range)

        for offset in coarse_offsets:
            if self._quick_autofocus_stop_requested():
                break
            target_z = center_z + offset
            if target_z < lower_bound or target_z > upper_bound or target_z in coarse_scores:
                continue
            score, current_z = self._autofocus_move_to_z(target_z, current_z, metric, stage="coarse", source=source)
            coarse_scores[current_z] = score
            if score > best_score:
                best_score = score
                best_z = current_z
            self.result_queue.put((status_event, f"Coarse Z {current_z}, {metric} {score:.2f}, best {best_score:.2f}"))
            if self._coarse_peak_is_confirmed(coarse_scores, best_z, initial_step):
                self.result_queue.put((status_event, f"Coarse peak confirmed near Z={best_z}; stop expanding range."))
                break

        fine_start, fine_end = self._fine_scan_bounds(
            best_z=best_z,
            initial_step=initial_step,
            min_step=min_step,
            lower_bound=lower_bound,
            upper_bound=upper_bound,
        )
        with self.focus_lock:
            self.autofocus_fine_range = (fine_start, fine_end)
        self.result_queue.put(("autofocus_fine_range", fine_start, fine_end))
        self.result_queue.put((status_event, f"Fine scan {fine_start}..{fine_end}, step {min_step}"))
        for target_z in self._fine_scan_positions(fine_start, fine_end, min_step):
            if self._quick_autofocus_stop_requested():
                break
            score, current_z = self._autofocus_move_to_z(target_z, current_z, metric, stage="fine", source=source)
            fine_scores[current_z] = score
            if score > best_score:
                best_score = score
                best_z = current_z
            self.result_queue.put((status_event, f"Fine Z {current_z}, {metric} {score:.2f}, best {best_score:.2f}"))

        fit_scores = fine_scores or coarse_scores
        fine_best_z, fine_best_score = max(fit_scores.items(), key=lambda item: item[1])
        boundary_margin = max(min_step, initial_step)
        best_near_edge = fine_best_z <= center_z - search_range + boundary_margin or fine_best_z >= center_z + search_range - boundary_margin
        fitted_z = self._fit_focus_peak(fit_scores, self.probe_config.autofocus_peak_model)
        result_z = int(round(fitted_z))
        result_z = max(lower_bound, min(upper_bound, result_z))
        threshold_passed = fine_best_score >= yellow_threshold
        result_is_usable = threshold_passed and not best_near_edge

        if result_is_usable:
            if current_z != result_z and not self._quick_autofocus_stop_requested():
                _, current_z = self._autofocus_move_to_z(result_z, current_z, metric, stage="final", source=source)
            state = "GREEN" if fine_best_score >= self._focus_green_threshold(metric) else "YELLOW"
            self.result_queue.put((status_event, f"Done {state}. Fine-fit Z={result_z}, fine peak Z={fine_best_z}, {metric}={fine_best_score:.2f}"))
        else:
            if current_z != center_z and not self._quick_autofocus_stop_requested():
                _, current_z = self._autofocus_move_to_z(center_z, current_z, metric, stage="return_center", source=source)
            if best_near_edge:
                self.result_queue.put((status_event, f"Fine peak near range edge: Z={fine_best_z}. Returned to {center_z}; increase range or recenter."))
            else:
                self.result_queue.put((status_event, f"No {metric} >= yellow threshold {yellow_threshold:g}. Returned to {center_z}; adjust optics or threshold."))
        return {
            "result_z": result_z,
            "fine_best_z": fine_best_z,
            "best_score": fine_best_score,
            "usable": result_is_usable,
            "threshold_passed": threshold_passed,
            "edge_limited": best_near_edge,
            "stopped": self._quick_autofocus_stop_requested(),
        }

    def _write_autofocus_history_file(self) -> None:
        output_path = Path.cwd() / "last_autofocus_history.csv"
        fieldnames = [
            "timestamp",
            "stage",
            "z_position",
            "target_z",
            "readback_z",
            "move_delta",
            "direction",
            "selected_metric",
            "selected_score",
            "laplacian",
            "tenengrad",
            "brenner",
            "reached_wait_ms",
            "settle_ms",
            "discarded_frames",
            "sampled_frames",
            "first_frame_delay_ms",
            "last_frame_delay_ms",
            "sample_timed_out",
            "command_hex",
            "reached_hex",
        ]
        with self.focus_lock:
            rows = list(self.autofocus_history_rows)
        lines = [",".join(fieldnames)]
        for row in rows:
            values = [str(row.get(field, "")).replace('"', '""') for field in fieldnames]
            lines.append(",".join(f'"{value}"' if "," in value or " " in value else value for value in values))
        output_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        logger.info("AutoFocus history written to %s (%s samples).", output_path, len(rows))

    def _sample_after_motion_settles(
        self,
        duration: float = 0.36,
        discard_frames: int = AUTOFOCUS_POST_SETTLE_DISCARD_FRAMES,
    ) -> tuple[dict[str, float], dict[str, object]]:
        settle_seconds = max(0, self.probe_config.autofocus_settle_ms) / 1000.0
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        sample_after = time.monotonic()
        scores, stats = self._sample_focus_score_frames(
            after_time=sample_after,
            sample_count=self.probe_config.autofocus_sample_count,
            timeout=max(duration, 0.2) + max(1.0, self.probe_config.autofocus_sample_count * 0.2),
            discard_count=discard_frames,
        )
        stats["settle_seconds"] = settle_seconds
        return scores, stats

    @staticmethod
    def _coarse_wobble_offsets(initial_step: int, search_range: int) -> list[int]:
        offsets: list[int] = []
        for distance in range(initial_step, search_range + 1, initial_step):
            offsets.extend((distance, -distance))
        if search_range not in {abs(offset) for offset in offsets}:
            offsets.extend((search_range, -search_range))
        return offsets

    @staticmethod
    def _fine_scan_bounds(
        best_z: int,
        initial_step: int,
        min_step: int,
        lower_bound: int,
        upper_bound: int,
    ) -> tuple[int, int]:
        half_range = max(initial_step, min_step * 8)
        return max(lower_bound, best_z - half_range), min(upper_bound, best_z + half_range)

    @staticmethod
    def _fine_scan_positions(start_z: int, end_z: int, step: int) -> list[int]:
        if step <= 0:
            raise ValueError("Fine scan step must be positive.")
        positions = list(range(start_z, end_z + 1, step))
        if not positions or positions[-1] != end_z:
            positions.append(end_z)
        return positions

    @staticmethod
    def _gaussian_score_at(z_value: float, model: dict[str, float]) -> float:
        gaussian_model = dict(model)
        gaussian_model["type"] = AUTOFOCUS_PEAK_MODEL_GAUSSIAN
        return ProbeApp._focus_peak_score_at(z_value, gaussian_model)

    @staticmethod
    def _focus_peak_score_at(z_value: float, model: dict[str, float | str]) -> float:
        peak_model = normalize_autofocus_peak_model(model.get("type", AUTOFOCUS_PEAK_MODEL_GAUSSIAN))
        width = max(float(model.get("width", model.get("sigma", 1.0))), 1e-9)
        mu = float(model["mu"])
        amplitude = float(model["amplitude"])
        eta = float(model.get("eta", 0.5))
        shape_value = ProbeApp._peak_shape_value(peak_model, z_value, mu, width, eta)
        return amplitude * shape_value

    @staticmethod
    def _fit_gaussian_focus_model(scores_by_z: dict[int, float]) -> dict[str, float] | None:
        return ProbeApp._fit_focus_peak_model(scores_by_z, AUTOFOCUS_PEAK_MODEL_GAUSSIAN)  # type: ignore[return-value]

    @staticmethod
    def _fit_focus_peak_model(scores_by_z: dict[int, float], peak_model: str = AUTOFOCUS_PEAK_MODEL_GAUSSIAN) -> dict[str, float | str] | None:
        peak_model = normalize_autofocus_peak_model(peak_model)
        if len(scores_by_z) < 3:
            return None
        if peak_model != AUTOFOCUS_PEAK_MODEL_GAUSSIAN:
            return ProbeApp._fit_grid_focus_peak_model(scores_by_z, peak_model)

        import math
        import numpy as np

        sorted_points = sorted(scores_by_z.items())
        best_z, _best_score = max(sorted_points, key=lambda item: item[1])
        z_values = np.array([z for z, _score in sorted_points], dtype=np.float64)
        scores = np.array([score for _z, score in sorted_points], dtype=np.float64)
        score_span = float(np.ptp(scores))
        if score_span <= 1e-9:
            return None

        baseline = 0.0
        adjusted = np.maximum(scores, 1e-6)
        x = z_values - float(best_z)
        try:
            a, b, c = np.polyfit(x, np.log(adjusted), 2)
        except Exception:
            return None
        if a >= -1e-12:
            return None

        mu = float(best_z - b / (2.0 * a))
        mu = max(float(np.min(z_values)), min(float(np.max(z_values)), mu))
        sigma = math.sqrt(-1.0 / (2.0 * float(a)))
        log_amplitude = float(c - (b * b) / (4.0 * a))
        amplitude = math.exp(log_amplitude)
        if not all(math.isfinite(value) for value in (mu, sigma, amplitude, baseline)):
            return None
        if sigma <= 0 or amplitude <= 0:
            return None
        return {
            "type": AUTOFOCUS_PEAK_MODEL_GAUSSIAN,
            "label": AUTOFOCUS_PEAK_MODEL_LABELS[AUTOFOCUS_PEAK_MODEL_GAUSSIAN],
            "mu": mu,
            "sigma": float(sigma),
            "width": float(sigma),
            "amplitude": float(amplitude),
            "baseline": 0.0,
        }

    @staticmethod
    def _fit_gaussian_focus_peak(scores_by_z: dict[int, float]) -> float:
        return ProbeApp._fit_focus_peak(scores_by_z, AUTOFOCUS_PEAK_MODEL_GAUSSIAN)

    @staticmethod
    def _fit_focus_peak(scores_by_z: dict[int, float], peak_model: str = AUTOFOCUS_PEAK_MODEL_GAUSSIAN) -> float:
        if not scores_by_z:
            raise ValueError("At least one focus sample is required.")
        if len(scores_by_z) < 3:
            return float(max(scores_by_z, key=scores_by_z.get))
        model = ProbeApp._fit_focus_peak_model(scores_by_z, peak_model)
        if model is None:
            return float(max(scores_by_z, key=scores_by_z.get))
        return float(model["mu"])

    @staticmethod
    def _fit_grid_focus_peak_model(scores_by_z: dict[int, float], peak_model: str) -> dict[str, float | str] | None:
        import math
        import numpy as np

        sorted_points = sorted((float(z), max(0.0, float(score))) for z, score in scores_by_z.items())
        z_values = np.array([z for z, _score in sorted_points], dtype=np.float64)
        scores = np.array([score for _z, score in sorted_points], dtype=np.float64)
        if float(np.ptp(scores)) <= 1e-9:
            return None

        min_z = float(np.min(z_values))
        max_z = float(np.max(z_values))
        span = max(max_z - min_z, 1.0)
        positive_diffs = [abs(b - a) for a, b in zip(z_values[:-1], z_values[1:]) if abs(b - a) > 1e-9]
        min_step = min(positive_diffs) if positive_diffs else span / 20.0
        min_width = max(min_step / 2.0, span / 40.0, 1e-6)
        max_width = max(span * 2.0, min_width * 2.0)
        mu_candidates = np.unique(np.concatenate((z_values, np.linspace(min_z, max_z, max(41, min(161, len(z_values) * 12))))))
        width_candidates = np.geomspace(min_width, max_width, 36)
        eta_candidates = (0.0, 0.25, 0.5, 0.75, 1.0) if peak_model == AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT else (0.0,)

        best: tuple[float, float, float, float, float] | None = None
        for mu in mu_candidates:
            for width in width_candidates:
                for eta in eta_candidates:
                    shape = np.array([ProbeApp._peak_shape_value(peak_model, z, float(mu), float(width), float(eta)) for z in z_values], dtype=np.float64)
                    denom = float(np.dot(shape, shape))
                    if denom <= 1e-12:
                        continue
                    amplitude = max(0.0, float(np.dot(scores, shape) / denom))
                    if amplitude <= 0.0:
                        continue
                    residuals = scores - amplitude * shape
                    sse = float(np.dot(residuals, residuals))
                    if best is None or sse < best[0]:
                        best = (sse, float(mu), float(width), amplitude, float(eta))

        if best is None:
            return None
        _sse, mu, width, amplitude, eta = best
        if not all(math.isfinite(value) for value in (mu, width, amplitude, eta)):
            return None
        return {
            "type": peak_model,
            "label": AUTOFOCUS_PEAK_MODEL_LABELS[peak_model],
            "mu": mu,
            "sigma": width,
            "width": width,
            "amplitude": amplitude,
            "baseline": 0.0,
            "eta": eta,
        }

    @staticmethod
    def _peak_shape_value(peak_model: str, z_value: float, mu: float, width: float, eta: float = 0.5) -> float:
        import math

        peak_model = normalize_autofocus_peak_model(peak_model)
        width = max(width, 1e-9)
        normalized = (z_value - mu) / width
        if peak_model == AUTOFOCUS_PEAK_MODEL_GAUSSIAN:
            return math.exp(-(normalized * normalized) / 2.0)
        if peak_model == AUTOFOCUS_PEAK_MODEL_LORENTZIAN:
            return 1.0 / (1.0 + normalized * normalized)
        if peak_model == AUTOFOCUS_PEAK_MODEL_PARABOLIC:
            return max(0.0, 1.0 - normalized * normalized)
        if peak_model == AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT:
            gaussian = math.exp(-(normalized * normalized) / 2.0)
            lorentzian = 1.0 / (1.0 + normalized * normalized)
            eta = max(0.0, min(1.0, eta))
            return eta * lorentzian + (1.0 - eta) * gaussian
        raise ValueError(f"Unsupported peak model: {peak_model}")

    def _wobble_offsets(self, start: int, limit: int) -> list[int]:
        offsets: list[int] = []
        for distance in range(start, limit + 1, start):
            offsets.extend((distance, -distance))
        return offsets

    def _coarse_peak_is_confirmed(self, scores_by_z: dict[int, float], best_z: int, step: int) -> bool:
        if len(scores_by_z) < 7:
            return False
        best_score = scores_by_z[best_z]
        lower_points = sorted((z_value, score) for z_value, score in scores_by_z.items() if z_value < best_z)
        upper_points = sorted((z_value, score) for z_value, score in scores_by_z.items() if z_value > best_z)
        if len(lower_points) < 2 or len(upper_points) < 2:
            return False

        left = [score for _z, score in lower_points[-2:]]
        right = [score for _z, score in upper_points[:2]]
        local_noise = max(1.0, best_score * 0.08)
        return all(best_score - score > local_noise for score in left + right)

    def _z_from_position_entries(self, entries: list[tuple[bytes, bytes, AxisPosition]]) -> int:
        for _command, _response, position in entries:
            if position.axis == Axis.Z:
                return position.position
        return self.current_position_values["Z"]

    def _autofocus_move_to_z(self, target_z: int, current_z: int, metric: str, stage: str = "sample", source: str = "autofocus") -> tuple[float, int]:
        assert self.serial_client is not None
        if self._quick_autofocus_stop_requested():
            return self._current_focus_score(metric), current_z
        delta = target_z - current_z
        command_hex = ""
        reached_hex = ""
        reached_wait_seconds: float | None = None
        if delta:
            speed_percent = self._motion_speed_percent()
            command = self.serial_client.move_relative(axis=Axis.Z, reverse=delta < 0, pulses=abs(delta), speed_percent=speed_percent)
            command_hex = hex_bytes(command)
            self.result_queue.put(("motor_command", "Z", "autofocus", command, source))
            self.result_queue.put(("moving",))
            reached_start = time.monotonic()
            reached = self.serial_client.wait_axis_reached(Axis.Z, timeout=self._axis_move_timeout(abs(delta), speed_percent))
            reached_wait_seconds = time.monotonic() - reached_start
            reached_hex = hex_bytes(reached)
            logger.info("AutoFocus Z reached feedback: %s", colorize_hex_frame(reached_hex, "RX"))
            entries = self.serial_client.read_stable_xyz_positions(required_repeats=2, max_attempts=10, interval_seconds=0.02)
            self.result_queue.put(("read_positions", entries, source, {"Z": target_z}))
            current_z = self._z_from_position_entries(entries)
        scores, sample_stats = self._sample_after_motion_settles(duration=0.36)
        score = scores.get(metric, 0.0)
        self._record_autofocus_sample(
            metric,
            current_z,
            score,
            1 if delta >= 0 else -1,
            command_hex=command_hex,
            reached_hex=reached_hex,
            stage=stage,
            scores=scores,
            target_z=target_z,
            readback_z=current_z,
            move_delta=delta,
            reached_wait_seconds=reached_wait_seconds,
            sample_stats=sample_stats,
        )
        return score, current_z

    def use_current_xy_for_af_plane_center(self) -> None:
        self.af_plane_region_mode_var.set("Center / Range")
        self.af_plane_center_x_var.set(str(self.current_position_values["X"]))
        self.af_plane_center_y_var.set(str(self.current_position_values["Y"]))
        self.af_plane_status_var.set("Center set from current XY.")

    def pick_af_plane_region_point(self, point_name: str) -> None:
        if self.af_plane_running:
            self.af_plane_status_var.set("Stop mapping before changing the region.")
            return
        point = (int(self.current_position_values["X"]), int(self.current_position_values["Y"]))
        if point_name == "p1":
            self.af_plane_region_p1 = point
            self.af_plane_p1_var.set(self._format_af_plane_pick("P1", point))
        else:
            self.af_plane_region_p2 = point
            self.af_plane_p2_var.set(self._format_af_plane_pick("P2", point))
        self.af_plane_region_mode_var.set("Pick P1/P2")
        self._sync_af_plane_center_range_from_picks()
        self.af_plane_status_var.set(f"{point_name.upper()} picked from current XY.")

    def go_to_af_plane_region_point(self, point_name: str) -> None:
        point = self.af_plane_region_p1 if point_name == "p1" else self.af_plane_region_p2
        if point is None:
            self.af_plane_status_var.set(f"Pick {point_name.upper()} before using Go.")
            return
        self._go_to_focusmap_xy(point[0], point[1], point_name.upper())

    def _go_to_focusmap_xy(self, x_value: int, y_value: int, label: str = "FocusMap point") -> None:
        if self.af_plane_running:
            self.af_plane_status_var.set("Stop FocusMap before moving to a point.")
            return
        if self.motion_busy or self.keyboard_motion_busy:
            self.af_plane_status_var.set("Motion is busy; point Go skipped.")
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return
        target_z = self.current_position_values["Z"]
        if self.main_focusmap_plane_var.get():
            mapped_z = self._focusmap_z_target_at_xy(x_value, y_value)
            if mapped_z is None:
                self.status_var.set(self._focusmap_plane_missing_message())
                return
            target_z = mapped_z
        targets = {"X": int(x_value), "Y": int(y_value)}
        if self.main_focusmap_plane_var.get():
            targets["Z"] = target_z
        self.motion_busy = True
        self._show_target_positions(targets)
        self.af_plane_status_var.set(f"Moving to {label}: X={x_value} Y={y_value}.")
        self.status_var.set(f"Moving to {label}: X={x_value} Y={y_value}.")
        threading.Thread(target=self._focusmap_go_to_xy_worker, args=(int(x_value), int(y_value), int(target_z), label), daemon=True).start()

    def _focusmap_go_to_xy_worker(self, x_value: int, y_value: int, z_value: int, label: str) -> None:
        assert self.serial_client is not None
        try:
            entries = self._move_absolute_stage(x_value, y_value, z_value, source="focusmap")
            targets = {"X": x_value, "Y": y_value}
            if self.main_focusmap_plane_var.get():
                targets["Z"] = z_value
            self.result_queue.put(("read_positions", entries, "focusmap", targets))
            self.result_queue.put(("focusmap_go_done", label, x_value, y_value))
        except Exception as exc:
            self.result_queue.put(("motor_error", "FOCUSMAP_GO", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _sync_af_plane_center_range_from_picks(self) -> None:
        if self.af_plane_region_p1 is None or self.af_plane_region_p2 is None:
            return
        x1, y1 = self.af_plane_region_p1
        x2, y2 = self.af_plane_region_p2
        self.af_plane_center_x_var.set(str(int(round((x1 + x2) / 2.0))))
        self.af_plane_center_y_var.set(str(int(round((y1 + y2) / 2.0))))
        self.af_plane_x_range_var.set(str(abs(x2 - x1)))
        self.af_plane_y_range_var.set(str(abs(y2 - y1)))

    def _af_plane_region_params(self) -> tuple[int, int, int, int, int, int]:
        rows = int(float(self.af_plane_rows_var.get()))
        cols = int(float(self.af_plane_cols_var.get()))
        if rows <= 0 or cols <= 0:
            raise ValueError("Rows and columns must be positive.")
        if self.af_plane_region_mode_var.get() == "Pick P1/P2":
            if self.af_plane_region_p1 is None or self.af_plane_region_p2 is None:
                raise ValueError("Pick both P1 and P2 before generating the mesh.")
            self._sync_af_plane_center_range_from_picks()
            x1, y1 = self.af_plane_region_p1
            x2, y2 = self.af_plane_region_p2
            x_range = abs(x2 - x1)
            y_range = abs(y2 - y1)
            if x_range == 0 and cols > 1:
                raise ValueError("P1 and P2 need different X values for multiple columns.")
            if y_range == 0 and rows > 1:
                raise ValueError("P1 and P2 need different Y values for multiple rows.")
            center_x = int(round((x1 + x2) / 2.0))
            center_y = int(round((y1 + y2) / 2.0))
            return center_x, center_y, x_range, y_range, rows, cols
        return (
            int(float(self.af_plane_center_x_var.get())),
            int(float(self.af_plane_center_y_var.get())),
            int(float(self.af_plane_x_range_var.get())),
            int(float(self.af_plane_y_range_var.get())),
            rows,
            cols,
        )

    def generate_af_plane_mesh(self) -> bool:
        if self.af_plane_running:
            self.af_plane_status_var.set("Stop mapping before changing the mesh.")
            return False
        try:
            center_x, center_y, x_range, y_range, rows, cols = self._af_plane_region_params()
            points = generate_af_mesh(
                mesh_type=self.af_plane_mesh_type_var.get(),
                center_x=center_x,
                center_y=center_y,
                x_range=x_range,
                y_range=y_range,
                rows=rows,
                cols=cols,
                use_step_spacing=False,
            )
        except Exception as exc:
            self.af_plane_status_var.set(f"Invalid mesh: {exc}")
            return False
        self.af_plane_mesh_points = points
        self.af_plane_results = [self._new_af_plane_record(point) for point in points]
        self.af_plane_selected_index = None
        self.sample_plane_model = None
        self.af_plane_model_stored = False
        self.af_plane_error_active = False
        self.af_plane_progress_var.set(0.0)
        self.af_plane_equation_var.set("No fitted plane")
        self.af_plane_eval_var.set("No fitted plane")
        self._update_af_plane_metrics()
        self._refresh_af_plane_table()
        self._draw_focusmap_all()
        self.af_plane_status_var.set(f"Generated {len(points)} mesh point(s).")
        return True

    def _new_af_plane_record(self, point: AFMeshPoint) -> dict[str, object]:
        return {
            "index": point.index,
            "row": point.row,
            "col": point.col,
            "x": point.x,
            "y": point.y,
            "measured_z": None,
            "fitted_z": None,
            "residual": None,
            "status": "pending",
            "retry_count": 0,
            "message": "",
            "fit_enabled": True,
        }

    def start_af_plane_mapping(self) -> None:
        if self.af_plane_running or self.motion_busy or self.autofocus_running or self.imgstitch_running:
            self.af_plane_status_var.set("Another motion workflow is running.")
            return
        if not self.af_plane_mesh_points and not self.generate_af_plane_mesh():
            return
        if not self.af_plane_mesh_points:
            self.af_plane_status_var.set("Generate a mesh before starting.")
            return
        try:
            metric = self.focus_metric_var.get()
            initial_step = int(float(self.autofocus_step_var.get()))
            min_step = int(float(self.autofocus_min_step_var.get()))
            search_range = int(float(self.autofocus_max_moves_var.get()))
            retry_count = int(float(self.af_plane_retry_count_var.get()))
        except ValueError:
            self.af_plane_status_var.set("FocusMap settings must be numeric.")
            return
        if min(initial_step, min_step, search_range) <= 0 or retry_count < 0:
            self.af_plane_status_var.set("AF step/range must be positive and retries non-negative.")
            return
        dry_run = self.af_plane_dry_run_var.get()
        if not dry_run:
            if not self.serial_client:
                self.connect_serial()
            if not self.serial_client:
                self.af_plane_status_var.set("Serial not connected. Enable dry run to test without hardware.")
                return

        self.af_plane_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.af_plane_restore_home_signal = self.home_signal_enabled
        if self.home_signal_enabled:
            self.disable_home_signal_polling()

        self.af_plane_results = [self._new_af_plane_record(point) for point in self.af_plane_mesh_points]
        self.af_plane_selected_index = None
        self.sample_plane_model = None
        self.af_plane_model_stored = False
        self.af_plane_error_active = False
        clear_sample_plane_model()
        self._disable_focusmap_plane_z_controls_if_unavailable()
        self._refresh_af_plane_table()
        self._draw_focusmap_all()
        self._update_af_plane_metrics()
        self.af_plane_stop_event.clear()
        self.af_plane_pause_event.clear()
        self.autofocus_stop_event.clear()
        self.af_plane_paused = False
        self.af_plane_pause_button_var.set("Pause")
        self.af_plane_running = True
        self.motion_busy = True
        with self.focus_lock:
            self.autofocus_samples.clear()
            self.autofocus_z_score_samples.clear()
            self.autofocus_fine_range = None
            self.autofocus_history_rows.clear()
            self.focus_history.clear()
            self.autofocus_run_start_time = time.monotonic()
            self.autofocus_run_end_time = None
        self.af_plane_status_var.set("Running FocusMap.")
        self.status_var.set("FocusMap running.")
        af_params = {
            "metric": metric,
            "initial_step": initial_step,
            "min_step": min_step,
            "search_range": search_range,
            "retry_count": retry_count,
        }
        self.af_plane_thread = threading.Thread(
            target=self._af_plane_worker,
            args=(
                tuple(self.af_plane_mesh_points),
                [dict(record) for record in self.af_plane_results],
                af_params,
                dry_run,
                self.af_plane_return_to_start_var.get(),
            ),
            daemon=True,
        )
        self.af_plane_thread.start()

    def toggle_af_plane_pause(self) -> None:
        if not self.af_plane_running:
            self.af_plane_status_var.set("FocusMap is not running.")
            return
        self.af_plane_paused = not self.af_plane_paused
        if self.af_plane_paused:
            self.af_plane_pause_event.set()
            self.af_plane_pause_button_var.set("Resume")
            self.af_plane_status_var.set("Pause requested. Current safe operation will finish first.")
        else:
            self.af_plane_pause_event.clear()
            self.af_plane_pause_button_var.set("Pause")
            self.af_plane_status_var.set("Resuming FocusMap.")

    def stop_af_plane_mapping(self) -> None:
        self.af_plane_stop_event.set()
        self.autofocus_stop_event.set()
        self.af_plane_pause_event.clear()
        self.af_plane_paused = False
        self.af_plane_pause_button_var.set("Pause")
        self.af_plane_status_var.set("Stopping after current safe operation.")

    def clear_af_plane_results(self) -> None:
        if self.af_plane_running:
            self.af_plane_status_var.set("Stop mapping before clearing results.")
            return
        self.af_plane_results = [self._new_af_plane_record(point) for point in self.af_plane_mesh_points]
        self.af_plane_selected_index = None
        self.sample_plane_model = None
        self.af_plane_model_stored = False
        self.af_plane_error_active = False
        clear_sample_plane_model()
        self._disable_focusmap_plane_z_controls_if_unavailable()
        self.af_plane_progress_var.set(0.0)
        self.af_plane_equation_var.set("No fitted plane")
        self.af_plane_eval_var.set("No fitted plane")
        self._update_af_plane_metrics()
        self._refresh_af_plane_table()
        self._draw_focusmap_all()
        self.af_plane_status_var.set("FocusMap results cleared.")

    def reauto_focus_selected_af_plane_point(self) -> None:
        if self.af_plane_running or self.motion_busy or self.autofocus_running or self.imgstitch_running:
            self.af_plane_status_var.set("Another motion workflow is running.")
            return
        if self.af_plane_selected_index is None:
            self.af_plane_status_var.set("Select a mesh point before Re-Auto Focus.")
            return
        record = self._af_plane_record_by_index(self.af_plane_selected_index)
        if record is None:
            self.af_plane_status_var.set("Selected FocusMap point is no longer available.")
            return
        try:
            point = AFMeshPoint(
                index=int(record["index"]),
                row=int(record.get("row", 0)),
                col=int(record.get("col", 0)),
                x=int(record["x"]),
                y=int(record["y"]),
            )
            af_params = {
                "metric": self.focus_metric_var.get(),
                "initial_step": int(float(self.autofocus_step_var.get())),
                "min_step": int(float(self.autofocus_min_step_var.get())),
                "search_range": int(float(self.autofocus_max_moves_var.get())),
                "retry_count": int(float(self.af_plane_retry_count_var.get())),
            }
        except (KeyError, ValueError):
            self.af_plane_status_var.set("Selected point or AF settings are invalid.")
            return
        if min(int(af_params["initial_step"]), int(af_params["min_step"]), int(af_params["search_range"])) <= 0 or int(af_params["retry_count"]) < 0:
            self.af_plane_status_var.set("AF step/range must be positive and retries non-negative.")
            return
        dry_run = self.af_plane_dry_run_var.get()
        if not dry_run:
            if not self.serial_client:
                self.connect_serial()
            if not self.serial_client:
                self.af_plane_status_var.set("Serial not connected. Enable dry run to test without hardware.")
                return

        self.af_plane_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.af_plane_restore_home_signal = self.home_signal_enabled
        if self.home_signal_enabled:
            self.disable_home_signal_polling()

        self.af_plane_stop_event.clear()
        self.af_plane_pause_event.clear()
        self.autofocus_stop_event.clear()
        self.af_plane_paused = False
        self.af_plane_pause_button_var.set("Pause")
        self.af_plane_running = True
        self.motion_busy = True
        record["status"] = "running"
        record["message"] = "Running AF"
        self._update_af_plane_table_record(record)
        self._draw_focusmap_all()
        self.af_plane_status_var.set(f"Re-Auto Focus point {point.index}.")
        threading.Thread(target=self._reauto_focus_af_plane_point_worker, args=(point, dict(record), af_params, dry_run), daemon=True).start()

    def inject_current_z_to_selected_af_plane_point(self) -> None:
        if self.af_plane_running or self.motion_busy or self.autofocus_running or self.imgstitch_running:
            self.af_plane_status_var.set("Another motion workflow is running.")
            return
        if self.af_plane_selected_index is None:
            self.af_plane_status_var.set("Select a mesh point before injecting current Z.")
            return
        record = self._af_plane_record_by_index(self.af_plane_selected_index)
        if record is None:
            self.af_plane_status_var.set("Selected FocusMap point is no longer available.")
            return
        current_z = int(self.current_position_values["Z"])
        record["measured_z"] = current_z
        record["status"] = "success"
        record["message"] = "Injected current Z"
        record["retry_count"] = record.get("retry_count", 0)
        record.setdefault("fit_enabled", True)
        self._refit_af_plane_from_current_results(
            final=True,
            status_prefix=f"Point {self.af_plane_selected_index} updated from current Z={current_z}",
        )

    def _reauto_focus_af_plane_point_worker(
        self,
        point: AFMeshPoint,
        record: dict[str, object],
        af_params: dict[str, object],
        dry_run: bool,
    ) -> None:
        origin: tuple[int, int, int] | None = None
        try:
            assert dry_run or self.serial_client is not None
            if dry_run:
                origin = (
                    self.current_position_values["X"],
                    self.current_position_values["Y"],
                    self.current_position_values["Z"],
                )
            else:
                entries = self.serial_client.read_stable_xyz_positions()
                origin = (
                    self._axis_from_position_entries(entries, Axis.X),
                    self._axis_from_position_entries(entries, Axis.Y),
                    self._axis_from_position_entries(entries, Axis.Z),
                )
                self.result_queue.put(("read_positions", entries, "af_plane"))
            measured_z, score, retry_count, message = self._measure_af_plane_point(point, origin, af_params, dry_run)
            record["retry_count"] = retry_count
            if measured_z is None:
                record["status"] = "failed"
                record["measured_z"] = None
                record["message"] = message
            else:
                record["status"] = "success"
                record["measured_z"] = measured_z
                record["message"] = f"{af_params['metric']} {score:.2f}"
            self.result_queue.put(("af_plane_reauto_point_done", dict(record)))
        except Exception as exc:
            self.result_queue.put(("af_plane_error", exc))
        finally:
            try:
                self._write_autofocus_history_file()
            except Exception as exc:
                logger.warning("FocusMap re-AF history write failed: %s", exc)
            self.result_queue.put(("af_plane_reauto_done", self.af_plane_stop_event.is_set()))

    def _af_plane_worker(
        self,
        mesh_points: tuple[AFMeshPoint, ...],
        records: list[dict[str, object]],
        af_params: dict[str, object],
        dry_run: bool,
        return_to_start: bool,
    ) -> None:
        # Hardware workflow: move XY, run the existing autofocus-at-current-XY
        # routine, record Z, and refit the shared plane as valid points arrive.
        origin: tuple[int, int, int] | None = None
        try:
            assert dry_run or self.serial_client is not None
            if dry_run:
                origin = (
                    self.current_position_values["X"],
                    self.current_position_values["Y"],
                    self.current_position_values["Z"],
                )
            else:
                entries = self.serial_client.read_stable_xyz_positions()
                origin = (
                    self._axis_from_position_entries(entries, Axis.X),
                    self._axis_from_position_entries(entries, Axis.Y),
                    self._axis_from_position_entries(entries, Axis.Z),
                )
                self.result_queue.put(("read_positions", entries, "af_plane"))

            for point_index, point in enumerate(mesh_points, start=1):
                if self.af_plane_stop_event.is_set():
                    break
                self._wait_if_af_plane_paused()
                if self.af_plane_stop_event.is_set():
                    break
                record = records[point_index - 1]
                record["status"] = "running"
                record["message"] = "Running AF"
                self.result_queue.put(("af_plane_point_update", dict(record), point_index, len(mesh_points)))
                measured_z, score, retry_count, message = self._measure_af_plane_point(point, origin, af_params, dry_run)
                record["retry_count"] = retry_count
                if measured_z is None:
                    record["status"] = "failed"
                    record["message"] = message
                else:
                    record["status"] = "success"
                    record["measured_z"] = measured_z
                    record["message"] = f"{af_params['metric']} {score:.2f}"
                self.result_queue.put(("af_plane_point_update", dict(record), point_index, len(mesh_points)))
                model = self._fit_af_plane_records(records, mesh_points, final=False)
                if model is not None:
                    self.result_queue.put(("af_plane_fit_update", model.to_dict(), [dict(item) for item in records], False))

            final_model = self._fit_af_plane_records(records, mesh_points, final=True)
            if final_model is not None:
                self.result_queue.put(("af_plane_fit_update", final_model.to_dict(), [dict(item) for item in records], True))
            elif not self.af_plane_stop_event.is_set():
                self.result_queue.put(("af_plane_status", "Need at least 3 valid AF points to fit a plane."))
            if return_to_start and origin is not None and not dry_run and not self.af_plane_stop_event.is_set():
                self._move_absolute_stage(*origin, source="af_plane")
                self.result_queue.put(("af_plane_status", "Returned to starting XYZ."))
        except Exception as exc:
            self.result_queue.put(("af_plane_error", exc))
        finally:
            try:
                self._write_autofocus_history_file()
            except Exception as exc:
                logger.warning("FocusMap history write failed: %s", exc)
            self.result_queue.put(("af_plane_done", self.af_plane_stop_event.is_set()))

    def _wait_if_af_plane_paused(self) -> None:
        reported = False
        while self.af_plane_pause_event.is_set() and not self.af_plane_stop_event.is_set():
            if not reported:
                self.result_queue.put(("af_plane_status", "Paused."))
                reported = True
            time.sleep(0.1)

    def _reset_focusmap_current_af_samples(self) -> None:
        with self.focus_lock:
            self.autofocus_samples.clear()
            self.autofocus_z_score_samples.clear()
            self.autofocus_fine_range = None

    def _measure_af_plane_point(
        self,
        point: AFMeshPoint,
        origin: tuple[int, int, int] | None,
        af_params: dict[str, object],
        dry_run: bool,
    ) -> tuple[int | None, float, int, str]:
        metric = str(af_params["metric"])
        retry_limit = int(af_params["retry_count"])
        last_score = 0.0
        last_message = ""
        for attempt in range(retry_limit + 1):
            if self.af_plane_stop_event.is_set():
                return None, last_score, attempt, "Stopped"
            if dry_run:
                assert origin is not None
                measured_z = self._simulate_af_plane_z(point, origin)
                time.sleep(0.08)
                return measured_z, last_score, attempt, "Dry run"
            assert self.serial_client is not None
            entries = self.serial_client.read_stable_xyz_positions()
            current_z = self._axis_from_position_entries(entries, Axis.Z)
            self.result_queue.put(("af_plane_status", f"Point {point.index}: moving XY to X={point.x} Y={point.y}"))
            self._move_absolute_stage(point.x, point.y, current_z, source="af_plane")
            self._wait_after_af_plane_motion()
            self._reset_focusmap_current_af_samples()
            self.result_queue.put(("focusmap_af_reset", point.index))
            self.result_queue.put(("af_plane_status", f"Point {point.index}: running AutoFocus"))
            result = self._quick_autofocus_result_at_current_xy(
                metric=metric,
                initial_step=int(af_params["initial_step"]),
                min_step=int(af_params["min_step"]),
                search_range=int(af_params["search_range"]),
                source="af_plane",
                status_event="af_plane_status",
            )
            measured_z = int(result["best_z"])
            last_score = float(result["best_score"])
            if self.af_plane_stop_event.is_set():
                return None, last_score, attempt, "Stopped"
            if not bool(result.get("stopped", False)) and not bool(result.get("edge_limited", False)):
                return measured_z, last_score, attempt, "OK"
            last_message = "Fine peak near range edge; increase AF range or recenter."
        return None, last_score, retry_limit, last_message or "AutoFocus failed"

    def _simulate_af_plane_z(self, point: AFMeshPoint, origin: tuple[int, int, int]) -> int:
        origin_x, origin_y, origin_z = origin
        residual = ((point.index % 5) - 2) * 2
        return int(round(origin_z + 0.0025 * (point.x - origin_x) - 0.0015 * (point.y - origin_y) + residual))

    def _fit_af_plane_records(self, records: list[dict[str, object]], mesh_points: tuple[AFMeshPoint, ...], final: bool) -> SamplePlaneModel | None:
        valid_samples = [
            (float(record["x"]), float(record["y"]), float(record["measured_z"]))
            for record in records
            if record.get("status") == "success" and record.get("measured_z") is not None and bool(record.get("fit_enabled", True))
        ]
        if len(valid_samples) < 3:
            self._clear_af_plane_fit_from_records(records)
            return None
        failed_points = sum(1 for record in records if record.get("status") == "failed")
        model = fit_sample_plane(valid_samples, failed_points=failed_points)
        self._apply_af_plane_fit_to_records(model, records)
        model.mesh_points = [point.to_dict() for point in mesh_points]
        model.measured_points = [dict(record) for record in records]
        if final:
            logger.info(
                "FocusMap fitted: z = %.9g*x + %.9g*y + %.9g, RMS %.3f, PV %.3f.",
                model.a,
                model.b,
                model.c,
                model.rms_residual,
                model.pv_residual,
            )
        return model

    def _refit_af_plane_from_current_results(self, final: bool, status_prefix: str = "FocusMap refit") -> None:
        for record in self.af_plane_results:
            record.setdefault("fit_enabled", True)
        model = self._fit_af_plane_records(self.af_plane_results, tuple(self.af_plane_mesh_points), final=final)
        if model is None:
            self.sample_plane_model = None
            self.af_plane_model_stored = False
            clear_sample_plane_model()
            self._disable_focusmap_plane_z_controls_if_unavailable()
            self.af_plane_equation_var.set("No fitted plane")
            self.af_plane_eval_var.set("Need at least 3 included successful points to fit.")
            self.af_plane_status_var.set(f"{status_prefix}; need at least 3 included successful points.")
        else:
            self.sample_plane_model = model
            if final:
                set_sample_plane_model(model)
                self.af_plane_model_stored = True
                try:
                    self._autosave_af_plane_results()
                except Exception as exc:
                    logger.warning("FocusMap autosave failed after refit: %s", exc)
            self._set_af_plane_fit_display(model)
            self.af_plane_status_var.set(f"{status_prefix}; plane refit updated.")
        self._refresh_af_plane_table()
        self._update_af_plane_metrics()
        self._draw_focusmap_all()

    @staticmethod
    def _clear_af_plane_fit_from_records(records: list[dict[str, object]]) -> None:
        for record in records:
            record["fitted_z"] = None
            record["residual"] = None

    @staticmethod
    def _apply_af_plane_fit_to_records(model: SamplePlaneModel, records: list[dict[str, object]]) -> None:
        for record in records:
            if record.get("measured_z") is None:
                record["fitted_z"] = None
                record["residual"] = None
                continue
            fitted_z = model.z_at(float(record["x"]), float(record["y"]))
            measured_z = float(record["measured_z"])
            record["fitted_z"] = fitted_z
            record["residual"] = measured_z - fitted_z

    def _wait_after_af_plane_motion(self) -> None:
        settle_seconds = max(0, self.probe_config.imgstitch_settle_ms) / 1000.0
        if settle_seconds > 0:
            end_time = time.monotonic() + settle_seconds
            while time.monotonic() < end_time and not self.af_plane_stop_event.is_set():
                time.sleep(max(0.0, min(0.05, end_time - time.monotonic())))

    def save_af_plane_results(self) -> None:
        if not self.af_plane_results:
            self.af_plane_status_var.set("No FocusMap results to save.")
            return
        output = filedialog.asksaveasfilename(
            title="Save FocusMap",
            initialfile="last_af_plane_mapping.json",
            defaultextension=".json",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not output:
            return
        payload = self._af_plane_result_payload()
        Path(output).write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self.af_plane_status_var.set(f"Saved {Path(output).name}.")

    def _af_plane_result_payload(self) -> dict[str, object]:
        return {
            "timestamp": time.time(),
            "mesh_settings": self._af_plane_mesh_settings_dict(),
            "autofocus_parameters": self._af_plane_af_params_dict(),
            "mesh_points": [point.to_dict() for point in self.af_plane_mesh_points],
            "measured_points": [dict(record) for record in self.af_plane_results],
            "sample_plane_model": self.sample_plane_model.to_dict() if self.sample_plane_model is not None else None,
        }

    def _autosave_af_plane_results(self) -> Path | None:
        if self.sample_plane_model is None or not self.af_plane_results:
            return None
        output_path = Path.cwd() / FOCUSMAP_AUTOSAVE_FILENAME
        output_path.write_text(json.dumps(self._af_plane_result_payload(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return output_path

    def load_af_plane_results(self) -> None:
        if self.af_plane_running:
            self.af_plane_status_var.set("Stop mapping before loading results.")
            return
        path = filedialog.askopenfilename(
            title="Load FocusMap",
            filetypes=(("JSON files", "*.json"), ("All files", "*.*")),
        )
        if not path:
            return
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            mesh_points = [AFMeshPoint.from_dict(item) for item in payload.get("mesh_points", [])]
            records = [dict(item) for item in payload.get("measured_points", [])]
            for record in records:
                record.setdefault("fit_enabled", True)
            model_payload = payload.get("sample_plane_model")
            model = SamplePlaneModel.from_dict(model_payload) if isinstance(model_payload, dict) else None
            mesh_settings = payload.get("mesh_settings", {})
        except Exception as exc:
            self.af_plane_status_var.set(f"Load failed: {exc}")
            return
        if isinstance(mesh_settings, dict):
            self._apply_af_plane_mesh_settings(mesh_settings)
        self.af_plane_mesh_points = mesh_points
        self.af_plane_results = records or [self._new_af_plane_record(point) for point in mesh_points]
        self.sample_plane_model = model
        self.af_plane_error_active = False
        if model is not None:
            set_sample_plane_model(model)
            self.af_plane_model_stored = True
            self._set_af_plane_fit_display(model)
        else:
            clear_sample_plane_model()
            self._disable_focusmap_plane_z_controls_if_unavailable()
            self.af_plane_model_stored = False
            self.af_plane_equation_var.set("No fitted plane")
            self.af_plane_eval_var.set("No fitted plane")
        self._update_af_plane_metrics()
        self._refresh_af_plane_table()
        self._draw_focusmap_all()
        self.af_plane_status_var.set(f"Loaded {Path(path).name}.")

    def _apply_af_plane_mesh_settings(self, settings: dict[str, object]) -> None:
        self.af_plane_mesh_type_var.set(str(settings.get("mesh_type", self.af_plane_mesh_type_var.get())))
        self.af_plane_region_mode_var.set(str(settings.get("region_mode", settings.get("range_mode", self.af_plane_region_mode_var.get()))))
        if self.af_plane_region_mode_var.get() not in {"Center / Range", "Pick P1/P2"}:
            self.af_plane_region_mode_var.set("Center / Range")
        self.af_plane_center_x_var.set(str(settings.get("center_x", self.af_plane_center_x_var.get())))
        self.af_plane_center_y_var.set(str(settings.get("center_y", self.af_plane_center_y_var.get())))
        self.af_plane_x_range_var.set(str(settings.get("x_range", self.af_plane_x_range_var.get())))
        self.af_plane_y_range_var.set(str(settings.get("y_range", self.af_plane_y_range_var.get())))
        self.af_plane_cols_var.set(str(settings.get("cols", self.af_plane_cols_var.get())))
        self.af_plane_rows_var.set(str(settings.get("rows", self.af_plane_rows_var.get())))
        self.af_plane_region_p1 = self._af_plane_region_point_from_payload(settings.get("p1"))
        self.af_plane_region_p2 = self._af_plane_region_point_from_payload(settings.get("p2"))
        self.af_plane_p1_var.set(self._format_af_plane_pick("P1", self.af_plane_region_p1))
        self.af_plane_p2_var.set(self._format_af_plane_pick("P2", self.af_plane_region_p2))

    def _af_plane_mesh_settings_dict(self) -> dict[str, object]:
        return {
            "mesh_type": self.af_plane_mesh_type_var.get(),
            "region_mode": self.af_plane_region_mode_var.get(),
            "center_x": self.af_plane_center_x_var.get(),
            "center_y": self.af_plane_center_y_var.get(),
            "x_range": self.af_plane_x_range_var.get(),
            "y_range": self.af_plane_y_range_var.get(),
            "cols": self.af_plane_cols_var.get(),
            "rows": self.af_plane_rows_var.get(),
            "p1": self._af_plane_region_point_dict(self.af_plane_region_p1),
            "p2": self._af_plane_region_point_dict(self.af_plane_region_p2),
        }

    @staticmethod
    def _af_plane_region_point_dict(point: tuple[int, int] | None) -> dict[str, int] | None:
        if point is None:
            return None
        return {"x": point[0], "y": point[1]}

    @staticmethod
    def _af_plane_region_point_from_payload(payload: object) -> tuple[int, int] | None:
        if not isinstance(payload, dict):
            return None
        return int(payload["x"]), int(payload["y"])

    @staticmethod
    def _format_af_plane_pick(label: str, point: tuple[int, int] | None) -> str:
        if point is None:
            return f"{label} -"
        return f"{label} X={point[0]} Y={point[1]}"

    def _af_plane_af_params_dict(self) -> dict[str, object]:
        return {
            "metric": self.focus_metric_var.get(),
            "initial_step": self.autofocus_step_var.get(),
            "min_step": self.autofocus_min_step_var.get(),
            "search_range": self.autofocus_max_moves_var.get(),
            "settle_ms": self.probe_config.autofocus_settle_ms,
            "sample_count": self.probe_config.autofocus_sample_count,
            "retry_count": self.af_plane_retry_count_var.get(),
            "dry_run": self.af_plane_dry_run_var.get(),
        }

    def _refresh_af_plane_table(self) -> None:
        if not hasattr(self, "af_plane_tree"):
            return
        self.af_plane_tree.delete(*self.af_plane_tree.get_children())
        self.af_plane_table_items.clear()
        for record in self.af_plane_results:
            record.setdefault("fit_enabled", True)
            item_id = self.af_plane_tree.insert("", "end", values=self._af_plane_table_values(record), tags=self._af_plane_table_tags(record))
            self.af_plane_table_items[int(record["index"])] = item_id
            if self.af_plane_selected_index == int(record["index"]):
                self.af_plane_tree.selection_set(item_id)
                self.af_plane_tree.focus(item_id)

    def _update_af_plane_table_record(self, record: dict[str, object]) -> None:
        if not hasattr(self, "af_plane_tree"):
            return
        index = int(record["index"])
        item_id = self.af_plane_table_items.get(index)
        if item_id is None:
            self._refresh_af_plane_table()
            return
        self.af_plane_tree.item(item_id, values=self._af_plane_table_values(record), tags=self._af_plane_table_tags(record))
        self.af_plane_tree.see(item_id)

    def _af_plane_table_values(self, record: dict[str, object]) -> tuple[object, ...]:
        return (
            record.get("index", ""),
            self._format_af_plane_value(record.get("x"), digits=0),
            self._format_af_plane_value(record.get("y"), digits=0),
            self._format_af_plane_value(record.get("measured_z"), digits=0),
            self._format_af_plane_value(record.get("fitted_z"), digits=2),
            self._format_af_plane_value(record.get("residual"), digits=2),
            self._af_plane_status_symbol(str(record.get("status", ""))),
            record.get("retry_count", 0),
            "\u2611" if bool(record.get("fit_enabled", True)) else "\u2610",
        )

    def _af_plane_table_tags(self, record: dict[str, object]) -> tuple[str, ...]:
        parity_tag = "row_even" if int(record.get("index", 0)) % 2 == 0 else "row_odd"
        tags = [parity_tag, self._af_plane_residual_tag(record)]
        if self.af_plane_selected_index == int(record.get("index", 0)):
            tags.append("selected_point")
        return tuple(tags)

    def _af_plane_residual_tag(self, record: dict[str, object]) -> str:
        status = str(record.get("status", "pending"))
        if status == "failed":
            return "residual_bad"
        if status == "running":
            return "residual_warn"
        residual = record.get("residual")
        if residual is None or self.sample_plane_model is None:
            return "residual_pending"
        abs_residual = abs(float(residual))
        quality_unit = max(1.0, float(self.sample_plane_model.rms_residual))
        if abs_residual <= quality_unit:
            return "residual_good"
        if abs_residual <= quality_unit * 2.0:
            return "residual_warn"
        return "residual_bad"

    def _on_af_plane_table_click(self, event: tk.Event) -> str | None:
        if not hasattr(self, "af_plane_tree"):
            return None
        item_id = self.af_plane_tree.identify_row(event.y)
        if not item_id:
            return None
        column_id = self.af_plane_tree.identify_column(event.x)
        index_text = self.af_plane_tree.set(item_id, "index")
        if not index_text:
            return None
        index = int(index_text)
        self._select_af_plane_point(index)
        column_name = ""
        if column_id.startswith("#") and column_id[1:].isdigit():
            column_index = int(column_id[1:]) - 1
            columns = tuple(self.af_plane_tree["columns"])
            if 0 <= column_index < len(columns):
                column_name = str(columns[column_index])
        if column_name == "fit_enabled":
            self._toggle_af_plane_record_fit_enabled(index)
            return "break"
        return None

    def _select_af_plane_point(self, index: int) -> None:
        self.af_plane_selected_index = index
        item_id = self.af_plane_table_items.get(index)
        if item_id is not None and hasattr(self, "af_plane_tree"):
            self.af_plane_tree.selection_set(item_id)
            self.af_plane_tree.focus(item_id)
            self.af_plane_tree.see(item_id)
        self._refresh_af_plane_table()
        self._draw_af_plane_mesh()

    def _af_plane_record_by_index(self, index: int) -> dict[str, object] | None:
        for record in self.af_plane_results:
            if int(record.get("index", 0)) == index:
                return record
        return None

    def _toggle_af_plane_record_fit_enabled(self, index: int) -> None:
        if self.af_plane_running:
            self.af_plane_status_var.set("Stop FocusMap before changing fit inclusion.")
            return
        record = self._af_plane_record_by_index(index)
        if record is None:
            return
        record["fit_enabled"] = not bool(record.get("fit_enabled", True))
        self._refit_af_plane_from_current_results(final=True, status_prefix=f"Point {index} fit inclusion updated")

    @staticmethod
    def _af_plane_status_symbol(status: str) -> str:
        if status == "success":
            return "\u221a"
        if status == "failed":
            return "\u00d7"
        if status == "running":
            return "..."
        return ""

    @staticmethod
    def _format_af_plane_value(value: object, digits: int = 2) -> str:
        if value is None or value == "":
            return "-"
        number = float(value)
        if digits <= 0:
            return f"{number:.0f}"
        return f"{number:.{digits}f}"

    def _draw_focusmap_all(self) -> None:
        self._draw_af_plane_mesh()
        self._draw_focusmap_realtime()
        self._draw_focusmap_af_scatter()
        self._draw_focusmap_3d()

    def _draw_af_plane_mesh(self) -> None:
        if not hasattr(self, "focusmap_mesh_canvas"):
            return
        canvas = self.focusmap_mesh_canvas
        canvas.delete("all")
        self.af_plane_mesh_hitboxes = []
        records = self.af_plane_results
        if not records:
            canvas.create_text(16, 16, text="Generate a mesh to preview AF points", anchor="nw", fill=self.colors["muted"], font=("Segoe UI Semibold", 13))
            return
        width = max(canvas.winfo_width(), 1)
        height = max(canvas.winfo_height(), 1)
        padding = 34
        xs = [float(record["x"]) for record in records]
        ys = [float(record["y"]) for record in records]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)
        if abs(max_x - min_x) < 1e-9:
            min_x -= 1.0
            max_x += 1.0
        if abs(max_y - min_y) < 1e-9:
            min_y -= 1.0
            max_y += 1.0
        plot_w = max(1, width - padding * 2)
        plot_h = max(1, height - padding * 2)
        canvas.create_rectangle(padding, padding, width - padding, height - padding, outline="#334155")
        canvas.create_text(padding, 12, text="Mesh status", anchor="nw", fill="#cbd5e1", font=("Segoe UI Semibold", 10))
        status_colors = {
            "pending": "#64748b",
            "running": "#fbbf24",
            "success": "#22c55e",
            "failed": "#fb7185",
        }
        for record in records:
            px = padding + (float(record["x"]) - min_x) / (max_x - min_x) * plot_w
            py = height - padding - (float(record["y"]) - min_y) / (max_y - min_y) * plot_h
            status = str(record.get("status", "pending"))
            color = status_colors.get(status, "#94a3b8")
            radius = 6 if status == "running" else 5
            selected = self.af_plane_selected_index == int(record.get("index", 0))
            if selected:
                canvas.create_oval(px - radius - 5, py - radius - 5, px + radius + 5, py + radius + 5, fill="", outline="#38bdf8", width=2)
            outline = "#e5edf5" if status == "running" else ("#38bdf8" if selected else color)
            if not bool(record.get("fit_enabled", True)):
                outline = "#94a3b8"
            canvas.create_oval(px - radius, py - radius, px + radius, py + radius, fill=color, outline=outline, width=2 if selected else 1)
            if not bool(record.get("fit_enabled", True)):
                canvas.create_line(px - radius - 2, py + radius + 2, px + radius + 2, py - radius - 2, fill="#94a3b8", width=2)
            self.af_plane_mesh_hitboxes.append((px, py, max(10.0, radius + 5.0), record))
            if len(records) <= 25:
                canvas.create_text(px + 8, py - 8, text=str(record["index"]), anchor="w", fill="#cbd5e1", font=("Segoe UI", 8))
        valid_residuals = [float(record["residual"]) for record in records if record.get("residual") is not None]
        if valid_residuals:
            canvas.create_text(
                width - padding,
                12,
                text=f"Residual min {min(valid_residuals):.2f} / max {max(valid_residuals):.2f}",
                anchor="ne",
                fill="#cbd5e1",
                font=("Segoe UI", 9),
            )

    def _on_focusmap_mesh_click(self, event: tk.Event) -> str:
        if not self.af_plane_mesh_hitboxes:
            return "break"
        best_record = None
        best_distance = float("inf")
        for px, py, radius, record in self.af_plane_mesh_hitboxes:
            distance = math.hypot(float(event.x) - px, float(event.y) - py)
            if distance <= radius and distance < best_distance:
                best_distance = distance
                best_record = record
        if best_record is None:
            return "break"
        self._select_af_plane_point(int(best_record.get("index", 0)))
        self._go_to_focusmap_xy(
            int(round(float(best_record["x"]))),
            int(round(float(best_record["y"]))),
            f"FocusMap point #{best_record.get('index', '')}",
        )
        return "break"

    def _draw_focusmap_realtime(self) -> None:
        if not hasattr(self, "focusmap_realtime_canvas"):
            return
        canvas = self.focusmap_realtime_canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#071018", outline="")
        canvas.create_text(12, 10, text="Realtime video", anchor="nw", fill="#cbd5e1", font=("Segoe UI Semibold", 10))
        frame = self.focusmap_realtime_bgr
        if frame is None:
            canvas.create_text(width // 2, height // 2, text="Waiting for camera frame", fill=self.colors["muted"], font=("Segoe UI Semibold", 11))
            return
        import cv2

        source = frame.copy()
        frame_h, frame_w = source.shape[:2]
        available_w = max(1, width - 20)
        available_h = max(1, height - 34)
        scale = min(available_w / max(1, frame_w), available_h / max(1, frame_h), 1.0)
        render_w = max(1, int(round(frame_w * scale)))
        render_h = max(1, int(round(frame_h * scale)))
        if render_w != frame_w or render_h != frame_h:
            source = cv2.resize(source, (render_w, render_h), interpolation=cv2.INTER_AREA)
        rgb = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
        header = f"P6 {render_w} {render_h} 255\n".encode("ascii")
        self.focusmap_realtime_image = tk.PhotoImage(data=header + rgb.tobytes(), format="PPM")
        canvas.create_image(width / 2, 28 + available_h / 2, image=self.focusmap_realtime_image, anchor="center")

    def _draw_focusmap_af_scatter(self) -> None:
        if not hasattr(self, "focusmap_af_canvas"):
            return
        self._draw_af_z_score_plot(self.focusmap_af_canvas, compact=True)

    def _draw_focusmap_3d(self) -> None:
        if self.focusmap_3d_view is not None:
            self.focusmap_3d_view.render(self.af_plane_results, self.sample_plane_model)

    def _handle_af_plane_point_update(self, record: dict[str, object], point_index: int, total_points: int) -> None:
        index = int(record["index"])
        if index - 1 < len(self.af_plane_results):
            self.af_plane_results[index - 1] = record
        else:
            self.af_plane_results.append(record)
        complete_count = sum(1 for item in self.af_plane_results if item.get("status") in {"success", "failed"})
        self.af_plane_progress_var.set(100.0 * complete_count / max(1, total_points))
        self.af_plane_status_var.set(f"Point {point_index}/{total_points}: X={record['x']} Y={record['y']} {record['status']}")
        self._update_af_plane_table_record(record)
        self._update_af_plane_metrics()
        self._draw_focusmap_all()

    def _handle_af_plane_reauto_point_done(self, record: dict[str, object]) -> None:
        index = int(record["index"])
        existing = self._af_plane_record_by_index(index)
        if existing is not None:
            record["fit_enabled"] = bool(existing.get("fit_enabled", True))
        if index - 1 < len(self.af_plane_results):
            self.af_plane_results[index - 1] = record
        else:
            self.af_plane_results.append(record)
        self.af_plane_selected_index = index
        self._refit_af_plane_from_current_results(final=True, status_prefix=f"Point {index} Re-Auto Focus completed")

    def _handle_af_plane_fit_update(self, model_payload: dict[str, object], records: list[dict[str, object]], final: bool) -> None:
        model = SamplePlaneModel.from_dict(model_payload)
        self.sample_plane_model = model
        for record in records:
            record.setdefault("fit_enabled", True)
        self.af_plane_results = records
        if final:
            set_sample_plane_model(model)
            self.af_plane_model_stored = True
            try:
                autosave_path = self._autosave_af_plane_results()
            except Exception as exc:
                autosave_path = None
                logger.warning("FocusMap autosave failed: %s", exc)
            if autosave_path is not None:
                self.af_plane_status_var.set(f"FocusMap model stored and autosaved to {autosave_path.name}.")
            else:
                self.af_plane_status_var.set("FocusMap model stored for shared lookup.")
            if self.main_focusmap_plane_var.get():
                self._update_main_focusmap_z_display()
        self._set_af_plane_fit_display(model)
        self._refresh_af_plane_table()
        self._update_af_plane_metrics()
        self._draw_focusmap_all()

    def _set_af_plane_fit_display(self, model: SamplePlaneModel) -> None:
        equation = self._format_af_plane_implicit_equation(model)
        self.af_plane_equation_var.set(equation)
        self.af_plane_metrics_var.set(
            f"RMS {model.rms_residual:.3f} | PV {model.pv_residual:.3f} | Max abs {model.max_abs_residual:.3f} | "
            f"Tilt X {model.tilt_x_deg:.4f} deg, Y {model.tilt_y_deg:.4f} deg | Valid {model.valid_points} | Failed {model.failed_points}"
        )
        self.af_plane_eval_var.set(
            f"Fit: {equation}\n"
            f"RMS {model.rms_residual:.3f} | PV {model.pv_residual:.3f} | Max {model.max_abs_residual:.3f} | "
            f"Tilt X {model.tilt_x_deg:.4f} deg, Y {model.tilt_y_deg:.4f} deg | Valid {model.valid_points} | Failed {model.failed_points}"
        )
        self._set_af_plane_eval_model(model)

    def _update_af_plane_metrics(self) -> None:
        if self.sample_plane_model is not None:
            self._set_af_plane_fit_display(self.sample_plane_model)
            return
        valid = sum(1 for record in self.af_plane_results if record.get("status") == "success")
        failed = sum(1 for record in self.af_plane_results if record.get("status") == "failed")
        running = sum(1 for record in self.af_plane_results if record.get("status") == "running")
        total = len(self.af_plane_results)
        self.af_plane_metrics_var.set(f"Valid {valid} | Failed {failed} | Running {running} | Total {total}")
        self.af_plane_eval_var.set(f"Fit pending | Valid {valid} | Failed {failed} | Running {running} | Total {total}")
        self._set_af_plane_eval_pending(valid, failed, running, total)

    def _format_af_plane_implicit_equation(self, model: SamplePlaneModel) -> str:
        a, b, c, d = model.implicit_coefficients()
        return f"{self._format_signed_plane_term(a, 'x', first=True)} {self._format_signed_plane_term(b, 'y')} {self._format_signed_plane_term(c, 'z')} {self._format_signed_plane_term(d, '')} = 0"

    def _format_signed_plane_term(self, value: float, suffix: str, first: bool = False) -> str:
        sign = "-" if value < 0 else "+"
        formatted = self._format_sig(abs(value))
        if first:
            return f"-{formatted}{suffix}" if value < 0 else f"{formatted}{suffix}"
        return f"{sign} {formatted}{suffix}"

    @staticmethod
    def _format_sig(value: float, digits: int = 3) -> str:
        if abs(value) < 1e-12:
            return "0.00" if digits == 3 else "0"
        abs_value = abs(value)
        if not math.isfinite(value) or abs_value >= 10_000 or abs_value < 0.001:
            return f"{value:.{digits - 1}e}"
        decimals = max(0, digits - int(math.floor(math.log10(abs_value))) - 1)
        return f"{value:.{decimals}f}"

    def _set_af_plane_eval_model(self, model: SamplePlaneModel) -> None:
        a, b, c, d = model.implicit_coefficients()
        self._set_af_plane_eval_chip("equation", self._format_af_plane_implicit_equation(model), "neutral")
        self._set_af_plane_eval_chip("a", f"a {self._format_sig(a)}", self._tilt_quality(model.tilt_x_deg))
        self._set_af_plane_eval_chip("b", f"b {self._format_sig(b)}", self._tilt_quality(model.tilt_y_deg))
        self._set_af_plane_eval_chip("c", f"c {self._format_sig(c)}", "blue")
        self._set_af_plane_eval_chip("d", f"d {self._format_sig(d)}", "violet")
        self._set_af_plane_eval_chip("rms", f"RMS {model.rms_residual:.3f}", self._residual_metric_quality(model.rms_residual))
        self._set_af_plane_eval_chip("pv", f"PV {model.pv_residual:.3f}", self._residual_metric_quality(model.pv_residual))
        self._set_af_plane_eval_chip("max", f"Max {model.max_abs_residual:.3f}", self._residual_metric_quality(model.max_abs_residual))
        self._set_af_plane_eval_chip("tilt", f"Tilt {model.tilt_x_deg:.3f}/{model.tilt_y_deg:.3f} deg", self._tilt_quality(max(abs(model.tilt_x_deg), abs(model.tilt_y_deg))))
        self._set_af_plane_eval_chip("points", f"Valid {model.valid_points}   Failed {model.failed_points}", "neutral")

    def _set_af_plane_eval_pending(self, valid: int, failed: int, running: int, total: int) -> None:
        self._set_af_plane_eval_chip("equation", "Fit pending: need at least 3 valid points", "neutral")
        for key in ("a", "b", "c", "d", "rms", "pv", "max", "tilt"):
            self._set_af_plane_eval_chip(key, f"{key} -", "muted")
        self._set_af_plane_eval_chip("points", f"Valid {valid}   Failed {failed}   Running {running}   Total {total}", "neutral")

    def _set_af_plane_eval_chip(self, key: str, text: str, tone: str) -> None:
        label = self.af_plane_eval_labels.get(key)
        if label is None:
            return
        foreground, background = self._af_plane_eval_colors(tone)
        label.configure(text=text, fg=foreground, bg=background)

    def _af_plane_eval_colors(self, tone: str) -> tuple[str, str]:
        palette = {
            "good": ("#bbf7d0", "#052e24"),
            "warn": ("#fde68a", "#3a2e05"),
            "bad": ("#fecdd3", "#4c0519"),
            "blue": ("#bfdbfe", "#0f2441"),
            "violet": ("#ddd6fe", "#271543"),
            "neutral": ("#dbe7f3", "#111c2a"),
            "muted": ("#8fa0b3", "#0f1722"),
        }
        return palette.get(tone, palette["neutral"])

    def _residual_metric_quality(self, value: float) -> str:
        try:
            z_step = max(1.0, abs(float(self.autofocus_step_var.get())))
        except ValueError:
            z_step = 1.0
        if abs(value) <= z_step:
            return "good"
        if abs(value) <= z_step * 2.0:
            return "warn"
        return "bad"

    @staticmethod
    def _tilt_quality(value: float) -> str:
        value = abs(value)
        if value <= 1.0:
            return "good"
        if value <= 3.0:
            return "warn"
        return "bad"

    @staticmethod
    def _focusmap_plane_missing_message() -> str:
        return "No FocusMap plane stored. Run or load FocusMap first."

    def _on_main_focusmap_plane_toggle(self) -> None:
        if not self.main_focusmap_plane_var.get():
            self.clear_position_edits()
            return
        if get_sample_plane_model() is not None:
            self._update_main_focusmap_z_display()
            self._start_focusmap_z_sync()
            return
        self.main_focusmap_plane_var.set(False)
        self._apply_focusmap_z_lock_to_position_entry()
        self.status_var.set(self._focusmap_plane_missing_message())

    def _on_imgstitch_focusmap_plane_toggle(self) -> None:
        if not self.imgstitch_focusmap_plane_var.get():
            self.clear_position_edits()
            return
        if get_sample_plane_model() is not None:
            self._on_main_focusmap_plane_toggle()
            return
        message = self._focusmap_plane_missing_message()
        self.imgstitch_focusmap_plane_var.set(False)
        self.imgstitch_status_var.set(message)
        self.status_var.set(message)

    def _disable_focusmap_plane_z_controls_if_unavailable(self) -> None:
        if get_sample_plane_model() is not None:
            return
        if self.main_focusmap_plane_var.get():
            self.main_focusmap_plane_var.set(False)
        if self.imgstitch_focusmap_plane_var.get():
            self.imgstitch_focusmap_plane_var.set(False)
        self._apply_focusmap_z_lock_to_position_entry()

    def _apply_focusmap_z_lock_to_position_entry(self) -> None:
        entry = self.position_inputs.get("Z")
        if entry is None:
            return
        if self.main_focusmap_plane_var.get():
            entry.configure(state="normal")
            entry.configure(fg=self.colors["muted"])
            entry.configure(state="readonly", readonlybackground=self.colors["surface_3"])
            self.position_edit_modes["Z"] = None
            self.modified_position_axes.discard("Z")
            for button in self.axis_control_buttons.get("Z", []):
                button.configure(state="disabled")
        else:
            entry.configure(state="normal")
            entry.configure(fg=self.colors["accent"])
            entry.configure(state="readonly", readonlybackground=self.colors["surface_2"])
            for button in self.axis_control_buttons.get("Z", []):
                button.configure(state="normal")

    def _update_main_focusmap_z_display(self) -> int | None:
        if not self.main_focusmap_plane_var.get():
            self._apply_focusmap_z_lock_to_position_entry()
            return None
        target_z = self._focusmap_z_target_at_xy(self.current_position_values["X"], self.current_position_values["Y"])
        if target_z is None:
            self.main_focusmap_plane_var.set(False)
            self.status_var.set(self._focusmap_plane_missing_message())
            return None
        self.position_vars["Z"].set(str(target_z))
        self.autofocus_z_var.set(str(target_z))
        self._apply_focusmap_z_lock_to_position_entry()
        return target_z

    def _start_focusmap_z_sync(self) -> None:
        if not self.main_focusmap_plane_var.get():
            return
        if get_sample_plane_model() is None:
            self.main_focusmap_plane_var.set(False)
            self.clear_position_edits()
            self.status_var.set(self._focusmap_plane_missing_message())
            return
        if self.motion_busy or self.keyboard_motion_busy:
            self.main_focusmap_plane_var.set(False)
            self.clear_position_edits()
            self.status_var.set("Motion is busy; enable FocusMap Z after the current move completes.")
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            self.main_focusmap_plane_var.set(False)
            self.clear_position_edits()
            return

        self.motion_busy = True
        self.status_var.set("Checking current position before enabling FocusMap Z...")
        threading.Thread(target=self._focusmap_z_sync_worker, daemon=True).start()

    def _focusmap_z_sync_worker(self) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_stable_xyz_positions()
            positions = {position.axis_name: position.position for _command, _response, position in entries}
            self.result_queue.put(("read_positions", entries, "focusmap"))

            x_position = positions.get("X", self.current_position_values["X"])
            y_position = positions.get("Y", self.current_position_values["Y"])
            current_z = positions.get("Z", self.current_position_values["Z"])
            target_z = self._focusmap_z_target_at_xy(x_position, y_position)
            if target_z is None:
                self.result_queue.put(("focusmap_z_unavailable",))
                return
            if current_z == target_z:
                self.result_queue.put(("focusmap_z_synced", target_z, False))
                return

            delta = target_z - current_z
            speed_percent = self._motion_speed_percent()
            if target_z >= 0:
                command = self.serial_client.move_absolute(axis=Axis.Z, target_position=target_z, speed_percent=speed_percent)
                action = "focusmap absolute"
            else:
                command = self.serial_client.move_relative(axis=Axis.Z, reverse=delta < 0, pulses=abs(delta), speed_percent=speed_percent)
                action = "focusmap relative"
            self.result_queue.put(("motor_command", "Z", action, command, "focusmap"))
            reached = self.serial_client.wait_axis_reached(Axis.Z, timeout=self._axis_move_timeout(abs(delta), speed_percent))
            self.result_queue.put(("axis_done", "Z", reached, "focusmap"))
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "focusmap", {"Z": target_z}))
            self.result_queue.put(("focusmap_z_synced", target_z, True))
        except Exception as exc:
            self.result_queue.put(("motor_error", "FOCUSMAP_Z", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def get_focus_z_at_xy(self, x: float, y: float) -> float | None:
        return get_focus_z_at_xy(x, y)

    def _focusmap_z_target_at_xy(self, x: float, y: float) -> int | None:
        z_value = get_focus_z_at_xy(float(x), float(y))
        if z_value is None:
            return None
        return int(round(z_value))

    def record_imgstitch_point(self, point_index: int) -> None:
        point = (self.current_position_values["X"], self.current_position_values["Y"])
        if point_index == 1:
            self.imgstitch_point1 = point
        else:
            self.imgstitch_point2 = point
        self._update_imgstitch_point_status()

    def _update_imgstitch_mode_fields(self) -> None:
        widgets_by_mode = getattr(self, "imgstitch_mode_widgets", {})
        stack_widgets_by_mode = getattr(self, "imgstack_mode_widgets", {})
        tile_widgets_by_mode = getattr(self, "imgstitch_tile_mode_widgets", {})
        top_mode = self.imgstack_mode_var.get()
        active_mode = self.imgstitch_range_mode_var.get()
        for mode, widgets in widgets_by_mode.items():
            for widget in widgets:
                if top_mode != "XY Stitch":
                    widget.grid_remove()
                elif mode == active_mode or mode == "XY Stitch" or (mode == "Manual Step" and active_mode != "Array"):
                    widget.grid()
                else:
                    widget.grid_remove()
        tile_mode = self.imgstitch_tile_acquisition_var.get() if top_mode == "XY Stitch" else ""
        for mode, widgets in stack_widgets_by_mode.items():
            for widget in widgets:
                if top_mode == mode:
                    widget.grid()
                else:
                    widget.grid_remove()
        for mode, widgets in tile_widgets_by_mode.items():
            for widget in widgets:
                if top_mode == "XY Stitch" and tile_mode == mode:
                    widget.grid()
                elif top_mode != mode:
                    widget.grid_remove()

    def _update_imgstitch_point_status(self) -> None:
        parts = []
        if self.imgstitch_point1 is not None:
            parts.append(f"P1 X={self.imgstitch_point1[0]} Y={self.imgstitch_point1[1]}")
        if self.imgstitch_point2 is not None:
            parts.append(f"P2 X={self.imgstitch_point2[0]} Y={self.imgstitch_point2[1]}")
        self.imgstitch_point_status_var.set(" | ".join(parts) if parts else "No rectangle points")

    def _mark_imgstitch_recompose_dirty(self) -> None:
        if self.imgstitch_session is not None and not self.imgstitch_recompose_running:
            self.imgstitch_status_var.set("Parameters changed. Click Apply and Recalculate.")

    def _imgstitch_settings_from_ui(self) -> StitchSettings:
        return StitchSettings(
            overlap_x=int(float(self.imgstitch_overlap_x_var.get())),
            overlap_y=int(float(self.imgstitch_overlap_y_var.get())),
            max_correction_um=float(self.imgstitch_max_correction_um_var.get()),
            registration_weight=float(self.imgstitch_registration_weight_var.get()),
            show_seams=self.imgstitch_show_seams_var.get(),
            seam_response_yellow=self.probe_config.imgstitch_seam_response_yellow,
            seam_response_green=self.probe_config.imgstitch_seam_response_green,
            use_green_edge_correction=self.imgstitch_green_edge_correction_var.get(),
            white_balance_correction=self.imgstitch_white_balance_var.get(),
        ).normalized()

    def _current_stitch_frame_size(self) -> tuple[int, int]:
        with self.camera_lock:
            frame = None if self.latest_stitch_frame is None else self.latest_stitch_frame.copy()
        if frame is None:
            raise ValueError("No camera frame available for automatic Array step calculation.")
        height, width = frame.shape[:2]
        return width, height

    def _array_step_um_from_overlap(self, overlap_x: int, overlap_y: int, um_per_px: float) -> tuple[float, float]:
        width, height = self._current_stitch_frame_size()
        if overlap_x >= width or overlap_y >= height:
            raise ValueError(f"Overlap must be smaller than current frame size ({width}x{height} px).")
        return (width - overlap_x) * um_per_px, (height - overlap_y) * um_per_px

    def _resolve_imgstitch_range(self, settings: StitchSettings, um_per_px: float) -> tuple[int, int, float, float, str]:
        mode = self.imgstitch_range_mode_var.get()
        if mode == "Array":
            rows = int(self.imgstitch_rows_var.get())
            cols = int(self.imgstitch_cols_var.get())
            step_x_um, step_y_um = self._array_step_um_from_overlap(settings.overlap_x, settings.overlap_y, um_per_px)
        elif mode == "Space":
            step_x_um = float(self.imgstitch_step_x_var.get())
            step_y_um = float(self.imgstitch_step_y_var.get())
            if step_x_um <= 0 or step_y_um <= 0:
                raise ValueError("Step X/Y must be positive.")
            width_um = float(self.imgstitch_width_um_var.get())
            height_um = float(self.imgstitch_height_um_var.get())
            if width_um <= 0 or height_um <= 0:
                raise ValueError("Space width/height must be positive.")
            cols = max(1, int((width_um + step_x_um - 1) // step_x_um) + 1)
            rows = max(1, int((height_um + step_y_um - 1) // step_y_um) + 1)
        elif mode == "Two Points":
            step_x_um = float(self.imgstitch_step_x_var.get())
            step_y_um = float(self.imgstitch_step_y_var.get())
            if step_x_um <= 0 or step_y_um <= 0:
                raise ValueError("Step X/Y must be positive.")
            if self.imgstitch_point1 is None or self.imgstitch_point2 is None:
                raise ValueError("Record both rectangle points before using Two Points mode.")
            width_um = abs(self.imgstitch_point2[0] - self.imgstitch_point1[0]) * self.probe_config.um_per_pulse("X")
            height_um = abs(self.imgstitch_point2[1] - self.imgstitch_point1[1]) * self.probe_config.um_per_pulse("Y")
            cols = max(1, int((width_um + step_x_um - 1) // step_x_um) + 1)
            rows = max(1, int((height_um + step_y_um - 1) // step_y_um) + 1)
        else:
            raise ValueError(f"Unsupported range mode: {mode}")
        if rows <= 0 or cols <= 0:
            raise ValueError("Rows and columns must be positive.")
        return rows, cols, step_x_um, step_y_um, mode

    def _imgstitch_scan_origin_override(self) -> tuple[int, int] | None:
        if self.imgstitch_range_mode_var.get() != "Two Points" or self.imgstitch_point1 is None or self.imgstitch_point2 is None:
            return None
        return min(self.imgstitch_point1[0], self.imgstitch_point2[0]), min(self.imgstitch_point1[1], self.imgstitch_point2[1])

    def _prepare_imgstitch_session_dir(self) -> None:
        resolved = self.imgstitch_session_dir.resolve()
        root = Path.cwd().resolve()
        if root not in (resolved, *resolved.parents):
            raise RuntimeError(f"Refusing to clear session directory outside workspace: {resolved}")
        if self.imgstitch_session_dir.exists():
            shutil.rmtree(self.imgstitch_session_dir)
        (self.imgstitch_session_dir / "tiles").mkdir(parents=True, exist_ok=True)

    def _imgstitch_quality_summary(self, edges: list[StitchEdgeQuality]) -> str:
        if not edges:
            return "No seam data"
        avg_response = sum(edge.response for edge in edges) / len(edges)
        max_correction = max(edge.correction_um for edge in edges)
        diagnostics = stitch_displacement_diagnostics(edges)
        counts = diagnostics["counts"]
        corrected = diagnostics["corrected"]
        return (
            f"Seams {len(edges)} | G/Y/R {counts['good']}/{counts['warning']}/{counts['bad']} | "
            f"response {avg_response:.3f} | max {max_correction:.2f} um | corrected {corrected}"
        )

    def _imgstitch_displacement_status(self, edges: list[StitchEdgeQuality], correction_enabled: bool | None = None) -> str:
        if not edges:
            return "No seam data."
        diagnostics = stitch_displacement_diagnostics(edges)
        counts = diagnostics["counts"]
        centers = diagnostics["centers"]
        corrected = diagnostics["corrected"]
        center_parts = []
        for direction in ("right", "left", "up", "down"):
            center = centers.get(direction)
            if center is not None:
                center_parts.append(f"{direction} dx={center[0]:.2f}, dy={center[1]:.2f}, MAD={center[2]:.2f}, n={center[3]}")
        if correction_enabled is None:
            correction_enabled = self.imgstitch_green_edge_correction_var.get()
        if correction_enabled:
            correction_state = f"corrected {corrected} edge(s)" if corrected else "correction skipped: not enough high-quality edges"
        else:
            correction_state = "correction disabled"
        centers_text = "; ".join(center_parts) if center_parts else "no reliable green-edge center"
        return f"Edges G/Y/R {counts['good']}/{counts['warning']}/{counts['bad']}; {centers_text}; {correction_state}."

    def recompose_imgstitch_session(self) -> None:
        if self.imgstitch_session is None:
            self.imgstitch_status_var.set("No captured session to recompose.")
            return
        if self.imgstitch_recompose_running:
            self.imgstitch_status_var.set("Recalculation is already running.")
            return
        try:
            settings = self._imgstitch_settings_from_ui()
        except Exception as exc:
            self.imgstitch_status_var.set(f"Recompose failed: {exc}")
            self.status_var.set(f"ImgStitch recompose failed: {exc}")
            return
        session = self.imgstitch_session
        tile_images = dict(self.imgstitch_tile_images)
        self.imgstitch_recompose_running = True
        if self.imgstitch_recompose_button is not None:
            self.imgstitch_recompose_button.configure(state="disabled", text="Recalculating...")
        self.imgstitch_status_var.set("Recalculating stitch...")
        # Recompose can run phase correlation over every edge, so keep it off
        # the Tk thread and update the canvas through the normal result queue.
        threading.Thread(target=self._imgstitch_recompose_worker, args=(session, settings, tile_images), daemon=True).start()

    def _imgstitch_recompose_worker(self, session: StitchSession, settings: StitchSettings, tile_images: dict[tuple[int, int], object]) -> None:
        try:
            mosaic, positions, edges = recompose_session(session, settings, tile_images)  # type: ignore[arg-type]
            display = build_seam_quality_overlay(mosaic, positions, edges, (session.tile_width, session.tile_height)) if settings.show_seams else mosaic
            import cv2

            cv2.imwrite(str(self.imgstitch_session_dir / "recomposed_imgstitch.png"), display)
            self.result_queue.put(("imgstitch_recompose_done", display, positions, edges, self._imgstitch_displacement_status(edges, settings.use_green_edge_correction)))
        except Exception as exc:
            self.result_queue.put(("imgstitch_recompose_error", exc))

    def _imgstack_params_from_ui(self, active_mode: str | None = None) -> dict[str, object]:
        needs_t = active_mode in (None, "T-Stack")
        needs_z = active_mode in (None, "Z-Stack")
        frame_count = int(float(self.t_stack_frame_count_var.get())) if needs_t else 1
        if needs_t and frame_count <= 0:
            raise ValueError("T-stack frame count must be positive.")
        z_step_um = float(self.z_stack_step_um_var.get()) if needs_z else 1.0
        z_range_um = float(self.z_stack_range_um_var.get()) if needs_z else 0.0
        if needs_z and z_step_um <= 0:
            raise ValueError("Z step must be positive.")
        if needs_z and z_range_um < 0:
            raise ValueError("Z range must be non-negative.")
        return {
            "frame_count": frame_count,
            "t_fusion_method": self.t_stack_fusion_var.get(),
            "t_save_raw": self.t_stack_save_raw_var.get(),
            "z_step_um": z_step_um,
            "z_range_um": z_range_um,
            "z_fusion_method": self.z_stack_fusion_var.get(),
            "z_return": self.z_stack_return_var.get(),
            "z_save_raw": self.z_stack_save_raw_var.get(),
        }

    def acquire_single_frame_tile(self, progress_prefix: str = ""):
        if progress_prefix:
            self.result_queue.put(("imgstitch_status", f"{progress_prefix}, single frame"))
        return self._capture_stitch_frame()

    def _raw_stack_dir(self, stack_name: str, progress_prefix: str) -> Path:
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "_", progress_prefix.strip()).strip("_") or "single"
        return self.imgstitch_session_dir / "raw_stack" / stack_name / safe_prefix

    def acquire_t_stack_tile(self, frame_count: int, fusion_method: str, save_raw_stack: bool = False, progress_prefix: str = "", preview_updates: bool = True):
        if frame_count <= 0:
            raise ValueError("T-stack frame count must be positive.")
        frames = []
        raw_dir = self._raw_stack_dir("t_stack", progress_prefix) if save_raw_stack else None
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
        for frame_index in range(1, frame_count + 1):
            if self.imgstitch_stop_event.is_set():
                raise RuntimeError("T-stack acquisition stopped.")
            self.result_queue.put(("imgstitch_status", f"{progress_prefix}, frame {frame_index}/{frame_count}" if progress_prefix else f"Frame {frame_index}/{frame_count}"))
            frame = self._capture_stitch_frame()
            frames.append(frame)
            preview = fuse_t_stack(frames, fusion_method)
            if preview_updates:
                self.result_queue.put(("imgstitch_preview", preview))
            if raw_dir is not None:
                import cv2

                cv2.imwrite(str(raw_dir / f"t_frame_{frame_index:03d}.png"), frame)
        return preview

    def acquire_z_stack_tile(
        self,
        z_step_um: float,
        z_range_um: float,
        fusion_method: str,
        return_to_original_z: bool = True,
        save_raw_stack: bool = False,
        progress_prefix: str = "",
        preview_updates: bool = True,
    ):
        assert self.serial_client is not None
        entries = self.serial_client.read_stable_xyz_positions()
        current_x = self._axis_from_position_entries(entries, Axis.X)
        current_y = self._axis_from_position_entries(entries, Axis.Y)
        center_z = self._axis_from_position_entries(entries, Axis.Z)
        positions = z_stack_positions(center_z, z_range_um, z_step_um, self.probe_config)
        frames = []
        raw_dir = self._raw_stack_dir("z_stack", progress_prefix) if save_raw_stack else None
        if raw_dir is not None:
            raw_dir.mkdir(parents=True, exist_ok=True)
        try:
            for z_index, target_z in enumerate(positions, start=1):
                if self.imgstitch_stop_event.is_set():
                    raise RuntimeError("Z-stack acquisition stopped.")
                self.result_queue.put(("imgstitch_status", f"{progress_prefix}, Z {z_index}/{len(positions)}" if progress_prefix else f"Z {z_index}/{len(positions)}"))
                self._move_absolute_stage(current_x, current_y, target_z)
                self._wait_after_imgstitch_motion()
                frame = self._capture_stitch_frame()
                frames.append(frame)
                preview = fuse_z_stack(frames, fusion_method)
                if preview_updates:
                    self.result_queue.put(("imgstitch_preview", preview))
                if raw_dir is not None:
                    import cv2

                    cv2.imwrite(str(raw_dir / f"z_{z_index:03d}_{target_z}.png"), frame)
        finally:
            if return_to_original_z and not self.imgstitch_stop_event.is_set():
                self._move_absolute_stage(current_x, current_y, center_z)
                self._wait_after_imgstitch_motion()
        return preview

    def acquire_tile(self, tile_mode: str, params: dict[str, object], progress_prefix: str = "", preview_updates: bool = True):
        if tile_mode == "Single Frame":
            return self.acquire_single_frame_tile(progress_prefix)
        if tile_mode == "T-Stack":
            return self.acquire_t_stack_tile(
                frame_count=int(params["frame_count"]),
                fusion_method=str(params["t_fusion_method"]),
                save_raw_stack=bool(params["t_save_raw"]),
                progress_prefix=progress_prefix,
                preview_updates=preview_updates,
            )
        if tile_mode == "Z-Stack":
            return self.acquire_z_stack_tile(
                z_step_um=float(params["z_step_um"]),
                z_range_um=float(params["z_range_um"]),
                fusion_method=str(params["z_fusion_method"]),
                return_to_original_z=bool(params["z_return"]),
                save_raw_stack=bool(params["z_save_raw"]),
                progress_prefix=progress_prefix,
                preview_updates=preview_updates,
            )
        raise ValueError(f"Unsupported tile acquisition mode: {tile_mode}")

    def start_imgstitch(self) -> None:
        if self.imgstack_mode_var.get() != "XY Stitch":
            self.start_imgstack()
            return
        if self.imgstitch_running or self.motion_busy:
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return
        try:
            derive_missing_calibrations(self.probe_config)
            um_per_px = self.probe_config.current_um_per_px()
            if um_per_px is None or um_per_px <= 0:
                raise ValueError("Current optical configuration must have a positive um/px calibration.")
            settings = self._imgstitch_settings_from_ui()
            tile_acquisition_mode = self.imgstitch_tile_acquisition_var.get()
            stack_params = self._imgstack_params_from_ui(tile_acquisition_mode if tile_acquisition_mode != "Single Frame" else "")
            rows, cols, step_x_um, step_y_um, range_mode = self._resolve_imgstitch_range(settings, um_per_px)
            scan_origin_override = self._imgstitch_scan_origin_override()
            step_x = pulses_from_um(step_x_um, self.probe_config, "X")
            step_y = pulses_from_um(step_y_um, self.probe_config, "Y")
            self._prepare_imgstitch_session_dir()
        except Exception as exc:
            self.imgstitch_status_var.set(f"Invalid stitch settings: {exc}")
            return
        if min(rows, cols, settings.overlap_x, settings.overlap_y, step_x, step_y, step_x_um, step_y_um) <= 0:
            self.imgstitch_status_var.set("Stitch settings must be positive.")
            return
        if self.imgstitch_plane_af_var.get() and (rows < 2 or cols < 2):
            self.imgstitch_status_var.set("Plane AF requires at least a 2x2 grid.")
            return
        if self.imgstitch_plane_af_var.get() and self.imgstitch_focusmap_plane_var.get():
            self.imgstitch_status_var.set("Choose either Four-corner plane AF or FocusMap plane Z.")
            return
        if self.imgstitch_focusmap_plane_var.get() and get_sample_plane_model() is None:
            self.imgstitch_status_var.set("No FocusMap plane stored. Run or load FocusMap first.")
            return

        self.imgstitch_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.imgstitch_restore_home_signal = self.home_signal_enabled
        if self.home_signal_enabled:
            self.disable_home_signal_polling()
        self.autofocus_stop_event.clear()
        self.af_plane_stop_event.clear()
        self.imgstitch_running = True
        self.imgstitch_focus_sampling_required = self.imgstitch_plane_af_var.get()
        self.motion_busy = True
        self.imgstitch_stop_event.clear()
        self.imgstitch_session = None
        self.imgstitch_tile_images = {}
        self.imgstitch_latest_positions = {}
        self.imgstitch_latest_edges = []
        self.imgstitch_quality_var.set("No seam data")
        self.imgstitch_status_var.set(f"Running: X {step_x_um:g} um -> {step_x} pulse, Y {step_y_um:g} um -> {step_y} pulse")
        self.status_var.set(f"ImgStitch running: X {step_x_um:g} um -> {step_x} pulse, Y {step_y_um:g} um -> {step_y} pulse.")
        self.imgstitch_thread = threading.Thread(
            target=self._imgstitch_worker,
            args=(
                rows,
                cols,
                settings,
                step_x,
                step_y,
                self.imgstitch_plane_af_var.get(),
                self.imgstitch_focusmap_plane_var.get(),
                step_x_um,
                step_y_um,
                um_per_px,
                range_mode,
                scan_origin_override,
                tile_acquisition_mode,
                stack_params,
            ),
            daemon=True,
        )
        self.imgstitch_thread.start()

    def start_imgstack(self) -> None:
        if self.imgstitch_running or self.motion_busy:
            return
        mode = self.imgstack_mode_var.get()
        if mode == "Z-Stack" and not self.serial_client:
            self.connect_serial()
        if mode == "Z-Stack" and not self.serial_client:
            return
        try:
            stack_params = self._imgstack_params_from_ui(self.imgstack_mode_var.get())
            self._prepare_imgstitch_session_dir()
        except Exception as exc:
            self.imgstitch_status_var.set(f"Invalid stack settings: {exc}")
            return

        if mode not in ("T-Stack", "Z-Stack"):
            self.imgstitch_status_var.set(f"Unsupported stack mode: {mode}")
            return
        self.imgstitch_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.imgstitch_restore_home_signal = self.home_signal_enabled
        if self.home_signal_enabled:
            self.disable_home_signal_polling()
        self.imgstitch_running = True
        self.motion_busy = True
        self.imgstitch_stop_event.clear()
        self.imgstitch_status_var.set(f"Running {mode}")
        self.status_var.set(f"ImgStitch {mode} running.")
        self.imgstitch_thread = threading.Thread(
            target=self._imgstack_worker,
            args=(mode, stack_params),
            daemon=True,
        )
        self.imgstitch_thread.start()

    def _imgstack_worker(self, mode: str, stack_params: dict[str, object]) -> None:
        try:
            import cv2

            tile_mode = "T-Stack" if mode == "T-Stack" else "Z-Stack"
            image = self.acquire_tile(tile_mode, stack_params)
            output_path = self.imgstitch_session_dir / "stack_result.png"
            cv2.imwrite(str(output_path), image)
            self.result_queue.put(("imgstitch_preview", image))
            self.result_queue.put(("imgstitch_done", output_path))
        except Exception as exc:
            self.result_queue.put(("imgstitch_error", exc))
        finally:
            self.result_queue.put(("imgstitch_finished",))

    def stop_imgstitch(self) -> None:
        self.imgstitch_stop_event.set()
        self.autofocus_stop_event.set()
        self.imgstitch_status_var.set("Stopping")

    def _imgstitch_worker(
        self,
        rows: int,
        cols: int,
        settings: StitchSettings,
        step_x: int,
        step_y: int,
        use_plane_af: bool,
        use_focusmap_plane: bool,
        step_x_um: float,
        step_y_um: float,
        um_per_px: float,
        range_mode: str,
        scan_origin_override: tuple[int, int] | None,
        tile_acquisition_mode: str,
        stack_params: dict[str, object],
    ) -> None:
        assert self.serial_client is not None
        origin: tuple[int, int, int] | None = None
        try:
            import cv2

            entries = self.serial_client.read_stable_xyz_positions()
            origin_x = self._axis_from_position_entries(entries, Axis.X)
            origin_y = self._axis_from_position_entries(entries, Axis.Y)
            origin_z = self._axis_from_position_entries(entries, Axis.Z)
            if scan_origin_override is not None:
                origin_x, origin_y = scan_origin_override
            origin = (origin_x, origin_y, origin_z)
            plane = None
            focusmap_plane = get_sample_plane_model() if use_focusmap_plane else None
            if use_focusmap_plane and focusmap_plane is None:
                raise RuntimeError("No FocusMap plane stored.")
            if use_plane_af:
                self.result_queue.put(("imgstitch_status", "Running four-corner AF"))
                plane = self._fit_imgstitch_plane(origin_x, origin_y, origin_z, rows, cols, step_x, step_y, range_mode)
                self._move_absolute_stage(origin_x, origin_y, origin_z)
            elif focusmap_plane is not None:
                self.result_queue.put(("imgstitch_status", "Using stored FocusMap plane Z"))

            tiles: dict[tuple[int, int], object] = {}
            records: list[TileRecord] = []
            session: StitchSession | None = None
            for index, key in enumerate(serpentine_indices(rows, cols), start=1):
                if self.imgstitch_stop_event.is_set():
                    break
                row, col = key
                target_x, target_y = self._imgstitch_tile_target(
                    origin_x,
                    origin_y,
                    row,
                    col,
                    rows,
                    cols,
                    step_x,
                    step_y,
                    range_mode,
                )
                if focusmap_plane is not None:
                    target_z = round(focusmap_plane.z_at(target_x, target_y))
                elif plane is not None:
                    target_z = round(plane.z_at(target_x, target_y))
                else:
                    target_z = origin_z
                moved_entries = self._move_absolute_stage(target_x, target_y, target_z)
                actual_x = self._axis_from_position_entries(moved_entries, Axis.X)
                actual_y = self._axis_from_position_entries(moved_entries, Axis.Y)
                actual_z = self._axis_from_position_entries(moved_entries, Axis.Z)
                self._wait_after_imgstitch_motion()
                progress_prefix = f"Tile {index}/{rows * cols}"
                image = self.acquire_tile(tile_acquisition_mode, stack_params, progress_prefix, preview_updates=False)
                corrected = flat_field_correct(image)
                tiles[key] = corrected
                tile_path = self.imgstitch_session_dir / "tiles" / f"tile_r{row:03d}_c{col:03d}.png"
                cv2.imwrite(str(tile_path), corrected)
                records.append(
                    TileRecord(
                        row=row,
                        col=col,
                        order=index,
                        image_path=str(tile_path),
                        stage_x=actual_x,
                        stage_y=actual_y,
                        stage_z=actual_z,
                        stage_x_um=(actual_x - origin_x) * self.probe_config.um_per_pulse("X"),
                        stage_y_um=(actual_y - origin_y) * self.probe_config.um_per_pulse("Y"),
                    )
                )
                session = StitchSession(
                    rows=rows,
                    cols=cols,
                    tile_width=corrected.shape[1],
                    tile_height=corrected.shape[0],
                    um_per_px=um_per_px,
                    objective=self.probe_config.objective,
                    eyepiece=self.probe_config.eyepiece,
                    range_mode=range_mode,
                    step_x_um=step_x_um,
                    step_y_um=step_y_um,
                    origin_stage_x=origin_x,
                    origin_stage_y=origin_y,
                    origin_stage_z=origin_z,
                    settings=settings,
                    tiles=tuple(records),
                )
                session.save(self.imgstitch_session_dir / "session.json")
                mosaic, positions, edges = recompose_session(session, settings, tiles)
                display = build_seam_quality_overlay(mosaic, positions, edges, (session.tile_width, session.tile_height)) if settings.show_seams else mosaic
                self.result_queue.put(("imgstitch_preview", display, session, dict(tiles), positions, edges))
                self.result_queue.put(("imgstitch_status", f"Tile {index}/{rows * cols}, step X {step_x_um:g} um/{step_x} pulse Y {step_y_um:g} um/{step_y} pulse"))
            if tiles and not self.imgstitch_stop_event.is_set():
                assert session is not None
                mosaic, positions, edges = recompose_session(session, settings, tiles)
                output_path = Path.cwd() / "last_imgstitch.png"

                cv2.imwrite(str(output_path), mosaic)
                cv2.imwrite(str(self.imgstitch_session_dir / "last_imgstitch.png"), mosaic)
                self.result_queue.put(("imgstitch_done", output_path))
            elif self.imgstitch_stop_event.is_set():
                self.result_queue.put(("imgstitch_status", "Stopped"))
        except Exception as exc:
            self.result_queue.put(("imgstitch_error", exc))
        finally:
            if origin is not None and self.serial_client is not None:
                try:
                    self._move_absolute_stage(*origin)
                    self.result_queue.put(("imgstitch_status", "Returned to stitch origin"))
                except Exception as exc:
                    self.result_queue.put(("imgstitch_status", f"Return to origin failed: {exc}"))
            self.result_queue.put(("imgstitch_finished",))

    @staticmethod
    def _imgstitch_tile_target(
        origin_x: int,
        origin_y: int,
        row: int,
        col: int,
        rows: int,
        cols: int,
        step_x: int,
        step_y: int,
        range_mode: str,
    ) -> tuple[int, int]:
        if range_mode == "Array":
            col_offset = col - (cols - 1) / 2.0
            row_offset = row - (rows - 1) / 2.0
            return int(round(origin_x + col_offset * step_x)), int(round(origin_y + row_offset * step_y))
        return origin_x + col * step_x, origin_y + row * step_y

    def _imgstitch_corner_targets(
        self,
        origin_x: int,
        origin_y: int,
        rows: int,
        cols: int,
        step_x: int,
        step_y: int,
        range_mode: str,
    ) -> tuple[tuple[int, int], ...]:
        return (
            self._imgstitch_tile_target(origin_x, origin_y, 0, 0, rows, cols, step_x, step_y, range_mode),
            self._imgstitch_tile_target(origin_x, origin_y, 0, cols - 1, rows, cols, step_x, step_y, range_mode),
            self._imgstitch_tile_target(origin_x, origin_y, rows - 1, 0, rows, cols, step_x, step_y, range_mode),
            self._imgstitch_tile_target(origin_x, origin_y, rows - 1, cols - 1, rows, cols, step_x, step_y, range_mode),
        )

    def _fit_imgstitch_plane(self, origin_x: int, origin_y: int, origin_z: int, rows: int, cols: int, step_x: int, step_y: int, range_mode: str):
        corners = self._imgstitch_corner_targets(origin_x, origin_y, rows, cols, step_x, step_y, range_mode)
        samples: list[tuple[float, float, float]] = []
        for index, (x_value, y_value) in enumerate(corners, start=1):
            if self.imgstitch_stop_event.is_set():
                break
            self.result_queue.put(("imgstitch_status", f"Plane AF corner {index}/4"))
            self._move_absolute_stage(x_value, y_value, origin_z)
            best_z = self._quick_autofocus_at_current_xy()
            samples.append((x_value, y_value, best_z))
        if len(samples) < 3:
            raise RuntimeError("Plane AF stopped before enough samples were collected.")
        return fit_plane(samples)

    def _quick_autofocus_at_current_xy(self) -> int:
        return int(self._quick_autofocus_result_at_current_xy(source="imgstitch", status_event="imgstitch_status")["best_z"])

    def _quick_autofocus_result_at_current_xy(
        self,
        metric: str | None = None,
        initial_step: int | None = None,
        min_step: int | None = None,
        search_range: int | None = None,
        source: str = "autofocus",
        status_event: str = "autofocus_status",
    ) -> dict[str, float | int | bool]:
        metric = metric or self.focus_metric_var.get()
        initial_step = max(1, int(initial_step if initial_step is not None else self.autofocus_step_var.get()))
        min_step = max(1, int(min_step if min_step is not None else self.autofocus_min_step_var.get()))
        search_range = max(initial_step, int(search_range if search_range is not None else self.autofocus_max_moves_var.get()))
        result = self._run_autofocus_sequence(metric, initial_step, min_step, search_range, source=source, status_event=status_event)
        return {
            "best_z": int(result["result_z"]),
            "best_score": float(result["best_score"]),
            "usable": bool(result["usable"]),
            "threshold_passed": bool(result.get("threshold_passed", False)),
            "edge_limited": bool(result.get("edge_limited", False)),
            "stopped": bool(result.get("stopped", False)),
        }

    def _quick_autofocus_stop_requested(self) -> bool:
        autofocus_event = self.__dict__.get("autofocus_stop_event")
        if autofocus_event is not None and autofocus_event.is_set():
            return True
        imgstitch_event = self.__dict__.get("imgstitch_stop_event")
        if self.__dict__.get("imgstitch_running", False) and imgstitch_event is not None and imgstitch_event.is_set():
            return True
        af_plane_event = self.__dict__.get("af_plane_stop_event")
        if self.__dict__.get("af_plane_running", False) and af_plane_event is not None and af_plane_event.is_set():
            return True
        return False

    def _move_absolute_stage(self, x_value: int, y_value: int, z_value: int, source: str = "imgstitch"):
        assert self.serial_client is not None
        entries = self.serial_client.read_stable_xyz_positions()
        current_x = self._axis_from_position_entries(entries, Axis.X)
        current_y = self._axis_from_position_entries(entries, Axis.Y)
        current_z = self._axis_from_position_entries(entries, Axis.Z)
        speed_percent = self._motion_speed_percent()
        for axis, target, current in ((Axis.X, x_value, current_x), (Axis.Y, y_value, current_y), (Axis.Z, z_value, current_z)):
            delta = target - current
            if not delta:
                continue
            self.serial_client.move_relative(axis=axis, reverse=delta < 0, pulses=abs(delta), speed_percent=speed_percent)
            self.serial_client.wait_axis_reached(axis, timeout=self._axis_move_timeout(abs(delta), speed_percent))
        entries = self.serial_client.read_stable_xyz_positions()
        self.result_queue.put(("read_positions", entries, source))
        return entries

    def _wait_after_imgstitch_motion(self) -> None:
        settle_seconds = max(0, self.probe_config.imgstitch_settle_ms) / 1000.0
        if settle_seconds > 0:
            self.imgstitch_stop_event.wait(settle_seconds)

    def _capture_stitch_frame(self):
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and not self.imgstitch_stop_event.is_set():
            with self.camera_lock:
                frame = self.latest_stitch_frame
            if frame is not None:
                return frame.copy()
            time.sleep(0.03)
        raise RuntimeError("No camera frame available for stitching.")

    def _axis_from_position_entries(self, entries: list[tuple[bytes, bytes, AxisPosition]], axis: Axis) -> int:
        for _command, _response, position in entries:
            if position.axis == axis:
                return position.position
        return self.current_position_values[axis.name]

    def _show_imgstitch_preview(self, image_bgr) -> None:
        self.imgstitch_preview_bgr = image_bgr.copy()
        self.imgstitch_preview_scale = 1.0
        self.imgstitch_preview_pan = [0.0, 0.0]
        self._fit_imgstitch_preview_to_canvas()
        self._render_imgstitch_preview()

    def _fit_imgstitch_preview_to_canvas(self) -> None:
        if self.imgstitch_preview_bgr is None or not hasattr(self, "imgstitch_mosaic_canvas"):
            return
        canvas_w = max(self.imgstitch_mosaic_canvas.winfo_width(), 1)
        canvas_h = max(self.imgstitch_mosaic_canvas.winfo_height(), 1)
        image_h, image_w = self.imgstitch_preview_bgr.shape[:2]
        if image_w <= 0 or image_h <= 0:
            return
        self.imgstitch_preview_scale = min(canvas_w / image_w, canvas_h / image_h, 1.0)
        render_w = image_w * self.imgstitch_preview_scale
        render_h = image_h * self.imgstitch_preview_scale
        self.imgstitch_preview_pan = [(canvas_w - render_w) / 2.0, (canvas_h - render_h) / 2.0]

    def _render_imgstitch_preview(self) -> None:
        if not hasattr(self, "imgstitch_mosaic_canvas"):
            return
        canvas = self.imgstitch_mosaic_canvas
        canvas.delete("all")
        if self.imgstitch_preview_bgr is None:
            canvas.create_text(20, 20, text="No mosaic yet", anchor="nw", fill=self.colors["muted"], font=("Segoe UI Semibold", 14))
            return
        import cv2

        scale = max(0.05, min(self.imgstitch_preview_scale, 20.0))
        source = self.imgstitch_preview_bgr
        rgb = cv2.cvtColor(source, cv2.COLOR_BGR2RGB)
        new_width = max(1, int(round(rgb.shape[1] * scale)))
        new_height = max(1, int(round(rgb.shape[0] * scale)))
        if new_width != rgb.shape[1] or new_height != rgb.shape[0]:
            interpolation = cv2.INTER_AREA if scale < 1.0 else cv2.INTER_LINEAR
            rgb = cv2.resize(rgb, (new_width, new_height), interpolation=interpolation)
        height, width = rgb.shape[:2]
        header = f"P6 {width} {height} 255\n".encode("ascii")
        self.imgstitch_preview_image = tk.PhotoImage(data=header + rgb.tobytes(), format="PPM")
        canvas.create_image(self.imgstitch_preview_pan[0], self.imgstitch_preview_pan[1], image=self.imgstitch_preview_image, anchor="nw")
        self._render_imgstitch_scatter()

    def _render_imgstitch_scatter(self) -> None:
        if not hasattr(self, "imgstitch_scatter_canvas"):
            return
        canvas = self.imgstitch_scatter_canvas
        canvas.delete("all")
        grouped_points: dict[str, list[tuple[StitchEdgeQuality, tuple[float, float]]]] = {"Horizontal": [], "Vertical": []}
        for edge in self.imgstitch_latest_edges:
            shift = edge.raw_shift_px or edge.measured_shift_px
            if abs(shift[0]) <= 1e-6 and abs(shift[1]) <= 1e-6:
                continue
            group = "Horizontal" if edge.direction in ("right", "left") else "Vertical"
            grouped_points[group].append((edge, shift))
        if not grouped_points["Horizontal"] and not grouped_points["Vertical"]:
            canvas.create_text(12, 12, text="No displacement data", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))
            return
        canvas_w = max(canvas.winfo_width(), 1)
        canvas_h = max(canvas.winfo_height(), 1)
        colors = {"good": "#22c55e", "warning": "#facc15", "bad": "#ef4444"}

        def draw_group(title: str, points: list[tuple[StitchEdgeQuality, tuple[float, float]]], top: int, bottom: int) -> None:
            canvas.create_text(10, top + 4, text=title, anchor="nw", fill="#e5e7eb", font=("Segoe UI", 8, "bold"))
            plot_x0 = 44
            plot_y0 = top + 24
            plot_x1 = max(plot_x0 + 20, canvas_w - 16)
            plot_y1 = max(plot_y0 + 20, bottom - 22)
            canvas.create_line(plot_x0, plot_y1, plot_x1, plot_y1, fill="#94a3b8")
            canvas.create_line(plot_x0, plot_y0, plot_x0, plot_y1, fill="#94a3b8")
            if not points:
                canvas.create_text(plot_x0 + 6, plot_y0 + 6, text="No edges", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 8))
                return
            shifts = [shift for _edge, shift in points]
            xs = [shift[0] for shift in shifts]
            ys = [shift[1] for shift in shifts]
            min_x, max_x = min(xs), max(xs)
            min_y, max_y = min(ys), max(ys)
            if abs(max_x - min_x) < 1e-6:
                min_x -= 1.0
                max_x += 1.0
            if abs(max_y - min_y) < 1e-6:
                min_y -= 1.0
                max_y += 1.0
            x_ticks = (min_x, (min_x + max_x) / 2.0, max_x)
            y_ticks = (min_y, (min_y + max_y) / 2.0, max_y)
            for tick in x_ticks:
                px = plot_x0 + (tick - min_x) / (max_x - min_x) * max(1, plot_x1 - plot_x0)
                canvas.create_line(px, plot_y1, px, plot_y1 + 3, fill="#94a3b8")
                canvas.create_text(px, plot_y1 + 5, text=f"{tick:.0f}", anchor="n", fill="#cbd5e1", font=("Segoe UI", 6))
            for tick in y_ticks:
                py = plot_y1 - (tick - min_y) / (max_y - min_y) * max(1, plot_y1 - plot_y0)
                canvas.create_line(plot_x0 - 3, py, plot_x0, py, fill="#94a3b8")
                canvas.create_text(plot_x0 - 5, py, text=f"{tick:.0f}", anchor="e", fill="#cbd5e1", font=("Segoe UI", 6))
            for edge, shift in points:
                px = plot_x0 + (shift[0] - min_x) / (max_x - min_x) * max(1, plot_x1 - plot_x0)
                py = plot_y1 - (shift[1] - min_y) / (max_y - min_y) * max(1, plot_y1 - plot_y0)
                color = colors.get(edge.quality, "#facc15")
                radius = 3 if edge.quality == "good" else 4
                canvas.create_oval(px - radius, py - radius, px + radius, py + radius, fill=color, outline="#f8fafc" if edge.was_corrected else color)
                if edge.was_corrected:
                    canvas.create_line(px - 4, py - 4, px + 4, py + 4, fill="#f8fafc")
                    canvas.create_line(px - 4, py + 4, px + 4, py - 4, fill="#f8fafc")

        split_y = canvas_h // 2
        draw_group("Horizontal", grouped_points["Horizontal"], 0, split_y - 2)
        canvas.create_line(8, split_y, canvas_w - 8, split_y, fill="#334155")
        draw_group("Vertical", grouped_points["Vertical"], split_y + 2, canvas_h)

    def _on_imgstitch_preview_wheel(self, event: tk.Event) -> str:
        if self.imgstitch_preview_bgr is None:
            return "break"
        old_scale = self.imgstitch_preview_scale
        if getattr(event, "num", None) == 4 or getattr(event, "delta", 0) > 0:
            factor = 1.15
        else:
            factor = 1.0 / 1.15
        new_scale = max(0.05, min(old_scale * factor, 20.0))
        if abs(new_scale - old_scale) < 1e-9:
            return "break"
        mouse_x = float(event.x)
        mouse_y = float(event.y)
        image_x = (mouse_x - self.imgstitch_preview_pan[0]) / old_scale
        image_y = (mouse_y - self.imgstitch_preview_pan[1]) / old_scale
        self.imgstitch_preview_scale = new_scale
        self.imgstitch_preview_pan = [mouse_x - image_x * new_scale, mouse_y - image_y * new_scale]
        self._render_imgstitch_preview()
        return "break"

    def _on_imgstitch_preview_press(self, event: tk.Event) -> str:
        self.imgstitch_preview_drag_start = (event.x, event.y, self.imgstitch_preview_pan[0], self.imgstitch_preview_pan[1])
        return "break"

    def _on_imgstitch_preview_drag(self, event: tk.Event) -> str:
        if self.imgstitch_preview_drag_start is None:
            return "break"
        start_x, start_y, pan_x, pan_y = self.imgstitch_preview_drag_start
        self.imgstitch_preview_pan = [pan_x + event.x - start_x, pan_y + event.y - start_y]
        self._render_imgstitch_preview()
        return "break"

    def toggle_realtime_position(self) -> None:
        if self.realtime_enabled:
            self.disable_realtime_position()
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        try:
            command = self.serial_client.enable_realtime_position()
        except Exception as exc:
            self.status_var.set(f"Enable realtime position failed: {exc}")
            logger.error("Enable realtime position failed: %s", exc)
            return

        command_hex = hex_bytes(command)
        self.tx_var.set(command_hex)
        self._append_hex_history("TX", command_hex)
        self.realtime_enabled = True
        self.realtime_stop_event.clear()
        self.realtime_button_var.set("Pause")
        self.status_var.set("Realtime position display enabled.")
        self.realtime_thread = threading.Thread(target=self._realtime_position_worker, daemon=True)
        self.realtime_thread.start()
        logger.info("Realtime position display enabled.")

    def disable_realtime_position(self) -> None:
        self.realtime_stop_event.set()
        if self.serial_client:
            try:
                command = self.serial_client.disable_realtime_position()
                command_hex = hex_bytes(command)
                self.tx_var.set(command_hex)
                self._append_hex_history("TX", command_hex)
            except Exception as exc:
                self.status_var.set(f"Disable realtime position failed: {exc}")
                logger.error("Disable realtime position failed: %s", exc)
                return

        self.realtime_enabled = False
        self.realtime_button_var.set("Continue")
        self.status_var.set("Realtime position display disabled.")
        logger.info("Realtime position display disabled.")

    def toggle_home_signal_polling(self) -> None:
        if self.home_signal_enabled:
            self.disable_home_signal_polling()
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.home_signal_enabled = True
        self.home_signal_stop_event.clear()
        self.home_signal_button_var.set("Stop Home Signals")
        self.status_var.set("Home signal polling enabled.")
        self.home_signal_thread = threading.Thread(target=self._home_signal_worker, daemon=True)
        self.home_signal_thread.start()
        logger.info("Home signal polling enabled.")

    def disable_home_signal_polling(self) -> None:
        self.home_signal_stop_event.set()
        current_thread = threading.current_thread()
        if self.home_signal_thread is not None and self.home_signal_thread is not current_thread:
            self.home_signal_thread.join(timeout=1.0)
            if not self.home_signal_thread.is_alive():
                self.home_signal_thread = None
        self.home_signal_enabled = False
        self.home_signal_button_var.set("Home Signals")
        self.status_var.set("Home signal polling disabled.")
        logger.info("Home signal polling disabled.")

    def _home_signal_worker(self) -> None:
        assert self.serial_client is not None
        while not self.home_signal_stop_event.is_set():
            try:
                command, response, status = self.serial_client.read_io_status()
                self.result_queue.put(("home_signals", command, response, status))
            except Exception as exc:
                self.result_queue.put(("home_signal_error", exc))
                return
            self.home_signal_stop_event.wait(0.3)

    def _update_home_indicators(self, status: IoStatus | None) -> None:
        for axis_name, axis in (("X", Axis.X), ("Y", Axis.Y), ("Z", Axis.Z)):
            canvas = self.axis_indicator_canvases.get(axis_name)
            item = self.axis_indicator_items.get(axis_name)
            if canvas is None or item is None:
                continue
            active = bool(status and status.home_triggered(axis))
            color = self.axis_indicator_colors[axis_name] if active else self.colors["muted"]
            canvas.itemconfigure(item, fill=color, outline=color)

    def _realtime_position_worker(self) -> None:
        assert self.serial_client is not None
        last_emit_by_axis: dict[str, float] = {}
        while not self.realtime_stop_event.is_set():
            try:
                response = self.serial_client.read_frame()
                if not response:
                    continue
                if len(response) != 12:
                    continue
                if response[0] != RESPONSE_HEAD or response[1] != FUNCTION_READ_POSITION:
                    continue
                position = parse_axis_position_response(response)
                now = time.monotonic()
                last_emit_at = last_emit_by_axis.get(position.axis_name, 0.0)
                if now - last_emit_at < REALTIME_POSITION_UI_INTERVAL_SECONDS:
                    continue
                last_emit_by_axis[position.axis_name] = now
                self.result_queue.put(("realtime_position", response, position))
            except Exception as exc:
                self.result_queue.put(("realtime_error", exc))
                return

    def read_current_position(self) -> None:
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.clear_position_edits()
        self.status_var.set("Reading current X/Y/Z positions...")
        logger.info("Reading current X/Y/Z positions.")
        threading.Thread(target=self._read_current_position_worker, args=("button",), daemon=True).start()

    def _read_current_position_worker(self, source: str, stable: bool = True) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_stable_xyz_positions() if stable else self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, source))
        except Exception as exc:
            self.result_queue.put(("read_position_error", exc))

    def schedule_position_read(self, source: str) -> None:
        if self.position_read_job is not None:
            self.after_cancel(self.position_read_job)
        self.position_read_job = self.after(120, lambda s=source: self._start_scheduled_position_read(s))

    def _start_scheduled_position_read(self, source: str) -> None:
        self.position_read_job = None
        if self.position_read_pending or not self.serial_client:
            return
        self.position_read_pending = True
        threading.Thread(target=self._scheduled_position_read_worker, args=(source,), daemon=True).start()

    def _scheduled_position_read_worker(self, source: str) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, source))
        except Exception as exc:
            self.result_queue.put(("read_position_error", exc))

    def axis_forward(self, axis: str) -> None:
        self._move_axis(axis=axis, reverse=False, source="button", mode="Relative")

    def axis_reverse(self, axis: str) -> None:
        self._move_axis(axis=axis, reverse=True, source="button", mode="Relative")

    def axis_stop(self, axis: str) -> None:
        controller_axis = self._controller_axis(axis)
        if not controller_axis:
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.status_var.set(f"Stopping {axis} axis...")
        threading.Thread(target=self._stop_axis_worker, args=(axis, controller_axis), daemon=True).start()

    def move_edited_positions(self) -> None:
        if self.motion_busy:
            return
        if self.main_focusmap_plane_var.get() and "Z" in self.modified_position_axes:
            self.modified_position_axes.discard("Z")
            self.position_edit_modes["Z"] = None
            self._update_main_focusmap_z_display()
        if not self.modified_position_axes:
            self.status_var.set("No coordinate input has been modified.")
            return

        try:
            values = {}
            for axis in self.modified_position_axes:
                self._fill_empty_position_default(axis)
                values[axis] = int(self.position_vars[axis].get())
        except ValueError:
            self.status_var.set("Move requires integer coordinate input values.")
            logger.warning("Move rejected because at least one coordinate input is not an integer.")
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        candidate_axes = tuple(axis for axis in ("X", "Y", "Z") if axis in self.modified_position_axes)
        modes = {axis: self.position_edit_modes[axis] or "Relative" for axis in candidate_axes}
        axes = tuple(
            axis
            for axis in candidate_axes
            if (modes[axis] == "Relative" and values[axis] != 0)
            or (modes[axis] == "Absolute" and values[axis] != self.current_position_values[axis])
        )
        if not axes:
            self.status_var.set("No coordinate change to move.")
            return

        targets = {}
        for axis in axes:
            targets[axis] = values[axis] if modes[axis] == "Absolute" else self.current_position_values[axis] + values[axis]
        if self.main_focusmap_plane_var.get() and any(axis in targets for axis in ("X", "Y")):
            target_x = targets.get("X", self.current_position_values["X"])
            target_y = targets.get("Y", self.current_position_values["Y"])
            target_z = self._focusmap_z_target_at_xy(target_x, target_y)
            if target_z is None:
                self.status_var.set("FocusMap Z is enabled, but no FocusMap plane is stored.")
                return
            targets["Z"] = target_z
            if target_z != self.current_position_values["Z"]:
                axes = tuple((*axes, "Z"))
                modes["Z"] = "Absolute"
                values["Z"] = target_z
        self.motion_busy = True
        self.status_var.set("Running coordinate move...")
        self.modified_position_axes.clear()
        for axis in ("X", "Y", "Z"):
            self.position_edit_modes[axis] = None
            self.position_inputs[axis].configure(fg=self.colors["accent"], state="readonly", readonlybackground=self.colors["surface_2"])
        self._show_target_positions(targets)
        threading.Thread(target=self._move_edited_positions_worker, args=(axes, modes, values, targets), daemon=True).start()

    def move_xyz_cc(self) -> None:
        if self.motion_busy:
            return

        try:
            steps = {axis: int(self.step_vars[axis].get()) for axis in ("X", "Y", "Z")}
        except ValueError:
            self.status_var.set("CC move requires integer X/Y/Z step values.")
            logger.warning("CC move rejected because at least one step value is not an integer.")
            return

        invalid_axes = [axis for axis, value in steps.items() if value < 0]
        if invalid_axes:
            self.status_var.set("CC move step values must be zero or positive.")
            logger.warning("CC move rejected because negative step values were provided: %s", ", ".join(invalid_axes))
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        if self.main_focusmap_plane_var.get():
            target_x = self.current_position_values["X"] + steps["X"]
            target_y = self.current_position_values["Y"] + steps["Y"]
            target_z = self._focusmap_z_target_at_xy(target_x, target_y)
            if target_z is None:
                self.main_focusmap_plane_var.set(False)
                self._apply_focusmap_z_lock_to_position_entry()
                self.status_var.set(self._focusmap_plane_missing_message())
                return
            deltas = {
                "X": steps["X"],
                "Y": steps["Y"],
                "Z": target_z - self.current_position_values["Z"],
            }
            axes = tuple(axis for axis in ("X", "Y", "Z") if deltas[axis] != 0)
            if not axes:
                self._update_main_focusmap_z_display()
                self.status_var.set("FocusMap Z already matches the mapped plane.")
                return
            modes = {axis: "Relative" for axis in axes}
            values = {axis: deltas[axis] for axis in axes}
            targets = {
                "X": target_x,
                "Y": target_y,
                "Z": target_z,
            }
            self.motion_busy = True
            self.status_var.set(f"Running CC move with FocusMap Z={target_z}.")
            self._show_target_positions(targets)
            threading.Thread(target=self._move_edited_positions_worker, args=(axes, modes, values, targets), daemon=True).start()
            return

        self.motion_busy = True
        targets = {axis: self.current_position_values[axis] + steps[axis] for axis in ("X", "Y", "Z") if steps[axis]}
        self.status_var.set(f"Running CC multi-axis relative move: X={steps['X']} Y={steps['Y']} Z={steps['Z']}.")
        self._show_target_positions(targets)
        threading.Thread(target=self._move_xyz_cc_worker, args=(steps, targets), daemon=True).start()

    def emergency_stop(self) -> None:
        self.realtime_stop_event.set()
        self.af_plane_stop_event.set()
        self.autofocus_stop_event.set()
        self.realtime_enabled = False
        self.realtime_button_var.set("Continue")
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.status_var.set("Emergency stop all axes...")
        threading.Thread(target=self._emergency_stop_worker, daemon=True).start()

    def _move_axis(self, axis: str, reverse: bool, source: str = "button", mode: str = "Relative") -> None:
        if self.motion_busy:
            self.status_var.set("Motion is busy; command skipped.")
            logger.warning("%s move skipped because motion is busy.", axis)
            return
        if self._is_low_latency_jog_source(source) and self.keyboard_motion_busy:
            return
        if self.main_focusmap_plane_var.get() and axis == "Z":
            self._update_main_focusmap_z_display()
            self.status_var.set("FocusMap Z is enabled; manual Z movement is locked.")
            logger.info("Manual Z move skipped because FocusMap Z is enabled.")
            return

        controller_axis = self._controller_axis(axis)
        if not controller_axis:
            return

        try:
            pulses = int(self.step_vars[axis].get())
        except ValueError:
            self.status_var.set(f"{axis} step must be an integer.")
            logger.warning("%s move rejected because step is not an integer: %s", axis, self.step_vars[axis].get())
            return
        if pulses <= 0:
            self.status_var.set(f"{axis} step must be greater than 0.")
            logger.warning("%s move rejected because step is not positive: %s", axis, pulses)
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        normalized_mode = "Absolute" if mode == "Absolute" and source != "keyboard" else "Relative"
        direction = "reverse" if reverse else "forward"
        action_text = f"absolute target {pulses}" if normalized_mode == "Absolute" else f"{direction}: {pulses} pulses"
        if normalized_mode == "Absolute":
            target = pulses
        else:
            target = self.current_position_values[axis] + (-pulses if reverse else pulses)
        if self.main_focusmap_plane_var.get() and axis in {"X", "Y"}:
            target_x = target if axis == "X" else self.current_position_values["X"]
            target_y = target if axis == "Y" else self.current_position_values["Y"]
            target_z = self._focusmap_z_target_at_xy(target_x, target_y)
            if target_z is None:
                self.main_focusmap_plane_var.set(False)
                self.status_var.set(self._focusmap_plane_missing_message())
                return
            modes = {axis: normalized_mode}
            values = {axis: pulses if normalized_mode == "Absolute" else (-pulses if reverse else pulses)}
            axes = (axis,)
            if target_z != self.current_position_values["Z"]:
                modes["Z"] = "Absolute"
                values["Z"] = target_z
                axes = (axis, "Z")
            targets = {axis: target, "Z": target_z}
            self._show_target_positions(targets)
            if self._is_low_latency_jog_source(source):
                self.keyboard_motion_busy = True
            else:
                self.motion_busy = True
            self.status_var.set(f"Moving {axis} with FocusMap Z={target_z}.")
            threading.Thread(target=self._move_edited_positions_worker, args=(axes, modes, values, targets), daemon=True).start()
            return
        self._show_target_positions({axis: target})
        if self._is_low_latency_jog_source(source):
            self.keyboard_motion_busy = True
        else:
            self.motion_busy = True
        self.status_var.set(f"Moving {axis} {action_text}.")
        threading.Thread(target=self._move_axis_worker, args=(axis, controller_axis, reverse, pulses, source, normalized_mode, {axis: target}), daemon=True).start()

    def _move_axis_worker(self, axis: str, controller_axis: Axis, reverse: bool, pulses: int, source: str, mode: str, expected_targets: dict[str, int]) -> None:
        assert self.serial_client is not None
        try:
            speed_percent = self._motion_speed_percent()
            if mode == "Absolute":
                command = self.serial_client.move_absolute(axis=controller_axis, target_position=pulses, speed_percent=speed_percent)
                action = "absolute"
            else:
                command = self.serial_client.move_relative(axis=controller_axis, reverse=reverse, pulses=pulses, speed_percent=speed_percent)
                action = "reverse" if reverse else "forward"
            self.result_queue.put(("motor_command", axis, action, command, source))
            reached = self.serial_client.wait_axis_reached(controller_axis, timeout=self._axis_move_timeout(pulses, speed_percent))
            self.result_queue.put(("axis_done", axis, reached, source))
            self.result_queue.put(("moving",))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, source, expected_targets))
        except Exception as exc:
            self.result_queue.put(("motor_error", axis, exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _stop_axis_worker(self, axis: str, controller_axis: Axis) -> None:
        assert self.serial_client is not None
        try:
            command = self.serial_client.stop_axis(axis=controller_axis)
            self.result_queue.put(("motor_command", axis, "stop", command, "button"))
            self.result_queue.put(("moving",))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button", expected_targets))
        except Exception as exc:
            self.result_queue.put(("motor_error", axis, exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _move_edited_positions_worker(self, axes: tuple[str, ...], modes: dict[str, str], values: dict[str, int], expected_targets: dict[str, int]) -> None:
        assert self.serial_client is not None
        try:
            speed_percent = self._motion_speed_percent()
            if len(axes) == 1:
                axis_name = axes[0]
                controller_axis = self._controller_axis(axis_name)
                if controller_axis is None:
                    return
                value = values[axis_name]
                if modes[axis_name] == "Absolute":
                    command = self.serial_client.move_absolute(axis=controller_axis, target_position=value, speed_percent=speed_percent)
                    action = "absolute"
                else:
                    if value == 0:
                        raise ValueError("Relative move value must be non-zero.")
                    command = self.serial_client.move_relative(axis=controller_axis, reverse=value < 0, pulses=abs(value), speed_percent=speed_percent)
                    action = "relative"
                self.result_queue.put(("motor_command", axis_name, action, command, "button"))
                wait_pulses = abs(value) if modes[axis_name] == "Relative" else abs(value - self.current_position_values[axis_name])
                reached = self.serial_client.wait_axis_reached(controller_axis, timeout=self._axis_move_timeout(wait_pulses, speed_percent))
                self.result_queue.put(("axis_done", axis_name, reached, "button"))
            else:
                axis_params: dict[Axis, tuple[bool, int, int, int]] = {}
                for axis_name in axes:
                    controller_axis = self._controller_axis(axis_name)
                    if controller_axis is None:
                        continue
                    if modes[axis_name] == "Absolute":
                        delta = values[axis_name] - self.current_position_values[axis_name]
                    else:
                        delta = values[axis_name]
                    axis_params[controller_axis] = self._cc_axis_param(delta < 0, abs(delta))
                if not any(params[1] for params in axis_params.values()):
                    raise ValueError("CC move requires at least one non-zero relative delta.")
                command, completed = self.serial_client.move_multi_axis_relative_and_wait(axis_params, timeout=self._cc_move_timeout(axis_params))
                self.result_queue.put(("motor_command", "XYZ", "cc relative", command, "button"))
                self.result_queue.put(("cc_done", completed, "button"))

            self.result_queue.put(("moving",))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button", expected_targets))
        except Exception as exc:
            self.result_queue.put(("motor_error", "MOVE", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _move_xyz_cc_worker(self, steps: dict[str, int], expected_targets: dict[str, int]) -> None:
        assert self.serial_client is not None
        try:
            axis_params = {
                Axis.X: self._cc_axis_param(False, steps["X"]),
                Axis.Y: self._cc_axis_param(False, steps["Y"]),
                Axis.Z: self._cc_axis_param(False, steps["Z"]),
            }
            command, completed = self.serial_client.move_multi_axis_relative_and_wait(axis_params, timeout=self._cc_move_timeout(axis_params))
            self.result_queue.put(("motor_command", "XYZ", "cc relative", command, "button"))
            self.result_queue.put(("cc_done", completed, "button"))
            self.result_queue.put(("moving",))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button", expected_targets))
        except Exception as exc:
            self.result_queue.put(("motor_error", "XYZ", exc))
        finally:
            self.result_queue.put(("motor_done",))

    @staticmethod
    def _axis_move_timeout(pulses: int, speed_percent: int) -> float:
        return max(5.0, abs(pulses) / max(1, speed_percent))

    @staticmethod
    def _cc_move_timeout(axis_params: dict[Axis, tuple[bool, int, int, int]]) -> float:
        max_seconds = max(
            (pulses / max(1, speed) for _reverse, pulses, speed, _acceleration in axis_params.values()),
            default=0.0,
        )
        return max(5.0, max_seconds)

    def _emergency_stop_worker(self) -> None:
        assert self.serial_client is not None
        try:
            command = self.serial_client.emergency_stop_all()
            self.result_queue.put(("motor_command", "ALL", "emergency stop", command, "button"))
        except Exception as exc:
            self.result_queue.put(("motor_error", "ALL", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _controller_axis(self, axis: str) -> Axis | None:
        axis_map = {"X": Axis.X, "Y": Axis.Y, "Z": Axis.Z}
        controller_axis = axis_map.get(axis)
        if controller_axis is None:
            self.status_var.set(f"Unknown axis: {axis}")
            logger.error("Unknown axis requested: %s", axis)
        return controller_axis

    def refresh_ports(self) -> None:
        logger.info("Scanning available serial ports...")
        ports = list_serial_ports()
        self._apply_serial_ports(ports)

    def _apply_serial_ports(self, ports: list[str]) -> None:
        if self.serial_client and self.serial_client.is_open and self.serial_client.port not in ports:
            ports.insert(0, self.serial_client.port)

        self.port_combo["values"] = ports
        current_port = self.port_var.get()
        if ports and current_port not in ports:
            self.port_var.set(ports[0])
        elif not ports:
            self.port_var.set("")

        if not ports:
            self.status_var.set("No serial ports found. Install pyserial and check the USB-RS232 adapter.")
            logger.warning("No available COM ports found.")
        else:
            self.status_var.set(f"Available serial ports: {', '.join(ports)}")
            logger.info("Available serial ports: %s", ", ".join(ports))

    def connect_serial(self) -> bool:
        port = self.port_var.get().strip()
        if not port:
            self.status_var.set("Select a serial port first.")
            logger.warning("Serial connection skipped because no port is selected.")
            return False

        try:
            self.serial_client = ControllerSerialClient(port)
            self.serial_client.set_admin_mode_enabled(self.admin_mode_enabled)
            self.serial_client.open()
        except Exception as exc:
            self.status_var.set(f"Serial connection failed: {exc}")
            logger.error("Serial connection failed on %s: %s", port, exc)
            threading.Thread(target=self._refresh_ports_after_connect_failure, daemon=True).start()
            return False

        self.status_var.set(f"Connected to {port} at 115200,N,8,1.")
        logger.info("Connected to %s at 115200,N,8,1.", port)
        return True

    def connect_and_test_serial(self) -> None:
        if self.connect_serial():
            self.run_comm_test()

    def _refresh_ports_after_connect_failure(self) -> None:
        logger.info("Scanning available serial ports after default connection failed...")
        self.result_queue.put(("ports_refreshed", list_serial_ports()))

    def disconnect_serial(self) -> None:
        self.home_signal_stop_event.set()
        self.home_signal_enabled = False
        self.home_signal_button_var.set("Home Signals")
        if self.serial_client:
            logger.info("Disconnecting serial port %s.", self.serial_client.port)
            self.serial_client.close()
        self.serial_client = None
        self.status_var.set("Serial disconnected.")
        logger.info("Serial disconnected.")

    def run_comm_test(self) -> None:
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.status_var.set("Running communication test...")
        logger.info("Running communication test.")
        threading.Thread(target=self._comm_test_worker, daemon=True).start()

    def _comm_test_worker(self) -> None:
        assert self.serial_client is not None
        try:
            self.result_queue.put(self.serial_client.communication_test())
        except Exception as exc:
            self.result_queue.put(exc)

    def _poll_result_queue(self) -> None:
        started_at = time.monotonic()
        processed = 0
        try:
            while processed < RESULT_POLL_MAX_EVENTS and time.monotonic() - started_at < RESULT_POLL_MAX_SECONDS:
                result = self.result_queue.get_nowait()
                processed += 1
                if isinstance(result, Exception):
                    self.status_var.set(f"Communication test failed: {result}")
                    logger.error("Communication test failed: %s", result)
                    continue
                if isinstance(result, tuple):
                    self._handle_worker_event(result)
                    continue
                self.tx_var.set(result.request_hex)
                self.rx_var.set(result.response_hex or "-")
                self.status_var.set(result.message)
                self._append_hex_history("TX", result.request_hex)
                self._append_hex_history("RX", result.response_hex or "-")
                if result.ok:
                    logger.info("Communication test passed. %s %s", colorize_hex_frame(result.request_hex, "TX"), colorize_hex_frame(result.response_hex, "RX"))
                    self.clear_position_edits()
                    self.status_var.set("Communication test passed. Reading current X/Y/Z positions...")
                    if not self.controller_motion_startup_read_done:
                        self.controller_motion_startup_read_done = True
                        self._start_controller_motion_parameters_read("startup")
                    threading.Thread(target=self._read_current_position_worker, args=("comm_test", False), daemon=True).start()
                else:
                    logger.warning("Communication test did not pass. %s %s Detail=%s", colorize_hex_frame(result.request_hex, "TX"), colorize_hex_frame(result.response_hex or "-", "RX"), result.message)
        except queue.Empty:
            pass
        next_interval = 1 if not self.result_queue.empty() else RESULT_POLL_INTERVAL_MS
        self.after(next_interval, self._poll_result_queue)

    def _set_agent_status(self, message: str) -> None:
        agent_panel = self.__dict__.get("agent_panel")
        if agent_panel is not None:
            agent_panel.set_status(message)

    def _handle_worker_event(self, event: tuple) -> None:
        event_type = event[0]
        if event_type == "read_positions":
            _, entries, source, *rest = event
            expected_targets = rest[0] if rest else None
            self.position_read_pending = False
            record_history = self._record_hex_history_for_source(source)
            positions: dict[str, int] = {}
            for command, response, position in entries:
                command_hex = hex_bytes(command)
                response_hex = hex_bytes(response)
                self.tx_var.set(command_hex)
                self.rx_var.set(response_hex)
                if record_history:
                    self._append_hex_history("TX", command_hex)
                    self._append_hex_history("RX", response_hex)
                self._update_axis_position(position)
                positions[position.axis_name] = position.position
            self.status_var.set("Current position read completed.")
            if source == "autofocus":
                self.autofocus_status_var.set("Moving, score sampling")
            elif source == "autofocus manual":
                self.autofocus_status_var.set("Manual Z move completed")
            elif source == "af_plane":
                self.af_plane_status_var.set("Stage moved")
            elif source == "focusmap":
                self.status_var.set("FocusMap Z position checked.")
            elif source == "imgstitch":
                self.imgstitch_status_var.set("Stage moved")
                self._set_agent_status("ImgStitch stage moved.")
            elif source == "gds_mapper":
                self._set_gds_mapper_status("LayoutBond move completed.")
                self._set_agent_status("LayoutBond move completed.")
            elif source == "zero_z":
                self.autofocus_status_var.set("Z set to 0")
                self.status_var.set("Z position set to 0.")
            elif source == "zero_xyz":
                self.autofocus_z_var.set("0")
                self.status_var.set("X/Y/Z positions set to 0.")
            elif source == "go_zero":
                self.status_var.set("Go Zero completed.")
            if expected_targets:
                mismatches = {
                    axis: (expected, positions.get(axis))
                    for axis, expected in expected_targets.items()
                    if positions.get(axis) != expected
                }
                if mismatches:
                    details = ", ".join(f"{axis} expected {expected} read {actual}" for axis, (expected, actual) in mismatches.items())
                    self.status_var.set(f"Position mismatch after move: {details}")
                    logger.warning("Position mismatch after %s move: %s.", source, details)
            logger.info(
                "Position read: X=%s Y=%s Z=%s.",
                positions.get("X", "-"),
                positions.get("Y", "-"),
                positions.get("Z", "-"),
                extra={"repeat_key": "keyboard_motion"} if self._is_low_latency_jog_source(source) else None,
            )
            return

        if event_type == "focusmap_z_unavailable":
            self.main_focusmap_plane_var.set(False)
            self.clear_position_edits()
            self.status_var.set(self._focusmap_plane_missing_message())
            return

        if event_type == "focusmap_z_synced":
            _, target_z, moved = event
            self._update_main_focusmap_z_display()
            if moved:
                self.status_var.set(f"FocusMap Z enabled; Z moved to mapped plane ({target_z}).")
            else:
                self.status_var.set(f"FocusMap Z enabled; Z already matches mapped plane ({target_z}).")
            return

        if event_type == "focusmap_go_done":
            _, label, x_value, y_value = event
            self.af_plane_status_var.set(f"Arrived at {label}: X={x_value} Y={y_value}.")
            self.status_var.set(f"Arrived at {label}: X={x_value} Y={y_value}.")
            return

        if event_type == "ports_refreshed":
            _, ports = event
            self._apply_serial_ports(ports)
            return

        if event_type == "zero_z_command":
            _, command = event
            command_hex = hex_bytes(command)
            self.tx_var.set(command_hex)
            self._append_hex_history("TX", command_hex)
            self.autofocus_status_var.set("Set Z=0 command sent")
            logger.info("Set Z=0 command sent: %s No response expected.", colorize_hex_frame(command_hex, "TX"))
            return

        if event_type == "zero_xyz_command":
            _, command = event
            command_hex = hex_bytes(command)
            self.tx_var.set(command_hex)
            self._append_hex_history("TX", command_hex)
            self.status_var.set("Set New Zero command sent.")
            logger.info("Set XYZ=0 command sent: %s No response expected.", colorize_hex_frame(command_hex, "TX"))
            return

        if event_type == "read_position_error":
            _, exc = event
            self.position_read_pending = False
            self.status_var.set(f"Read current position failed: {exc}")
            logger.error("Read current position failed: %s", exc)
            return

        if event_type == "home_signals":
            _, command, response, io_status = event
            self.tx_var.set(hex_bytes(command))
            self.rx_var.set(hex_bytes(response))
            self._update_home_indicators(io_status)
            self.status_var.set("Home signal status updated.")
            return

        if event_type == "home_signal_error":
            _, exc = event
            self.home_signal_enabled = False
            self.home_signal_button_var.set("Home Signals")
            self.status_var.set(f"Home signal polling stopped: {exc}")
            logger.error("Home signal polling stopped: %s", exc)
            return

        if event_type == "controller_motion_parameters":
            _, entries, source = event
            summary_parts: list[str] = []
            last_command_hex = "-"
            last_response_hex = "-"
            for command, response, parameters in entries:
                command_hex = hex_bytes(command)
                response_hex = hex_bytes(response)
                last_command_hex = command_hex
                last_response_hex = response_hex
                self._append_hex_history("TX", command_hex)
                self._append_hex_history("RX", response_hex)
                axis_name = parameters.axis.name
                self.probe_config.controller_motion_parameters[axis_name] = {
                    "minimum_speed": int(parameters.minimum_speed),
                    "work_speed": int(parameters.work_speed),
                    "acceleration": int(parameters.acceleration),
                }
                summary_parts.append(
                    f"{axis_name} min {parameters.minimum_speed}, work {parameters.work_speed}, accel {parameters.acceleration}"
                )
            self.tx_var.set(last_command_hex)
            self.rx_var.set(last_response_hex)
            self._sync_config_vars_from_config()
            self._update_config_display()
            message = "D5 readback: " + "; ".join(summary_parts) + "."
            self.controller_motion_status_var.set(message)
            self.status_var.set("Controller D5 parameters updated." if source == "startup" else message)
            try:
                save_probe_config(self.probe_config, self.config_path)
                self.config_status_var.set(f"Saved {self.config_path.name}")
            except Exception as exc:
                self.config_status_var.set(f"Save failed: {exc}")
                logger.error("Failed to save D5 controller parameters: %s", exc)
            logger.info("Controller D5 parameters updated. %s", message)
            return

        if event_type == "controller_motion_parameters_error":
            _, exc, source = event
            message = f"D5 read failed: {exc}"
            self.controller_motion_status_var.set(message)
            if source != "startup":
                self.status_var.set(message)
            logger.error("Controller D5 parameter read failed: %s", exc)
            return

        if event_type == "moving":
            self.status_var.set("Moving")
            return

        if event_type == "gds_mapper_status":
            _, message = event
            self._set_gds_mapper_status(str(message))
            self._set_agent_status(str(message))
            logger.info("LayoutBond: %s", message)
            return

        if event_type == "schedule_position_read":
            _, source = event
            self.schedule_position_read(source)
            return

        if event_type == "camera_error":
            _, session_id, exc = event
            if session_id != self.camera_session_id:
                return
            self._set_camera_index_error(True)
            if self.vision_panel:
                self.vision_panel.show_message(f"Camera unavailable: {exc}")
            self.status_var.set(str(exc))
            self.camera_running = False
            self.camera_rendering = False
            logger.error("Camera unavailable: %s", exc)
            return

        if event_type == "camera_ready":
            _, session_id = event
            if session_id != self.camera_session_id:
                return
            self._set_camera_index_error(False)
            return

        if event_type == "realtime_position":
            _, response, position = event
            self._update_axis_position(position)
            now = time.monotonic()
            if now - self.last_realtime_ui_update >= 0.5:
                self.rx_var.set(hex_bytes(response))
                self.last_realtime_ui_update = now
            return

        if event_type == "realtime_raw":
            return

        if event_type == "realtime_error":
            _, exc = event
            self.realtime_enabled = False
            self.realtime_button_var.set("Continue")
            self.status_var.set(f"Realtime position stopped: {exc}")
            logger.error("Realtime position stopped: %s", exc)
            return

        if event_type == "manual_command":
            _, payload, response, read_length = event
            command_hex = hex_bytes(payload)
            response_hex = hex_bytes(response) if response else "-"
            self.tx_var.set(command_hex)
            self.rx_var.set(response_hex)
            self._append_hex_history("TX", command_hex)
            if read_length > 0:
                self._append_hex_history("RX", response_hex)
            self.status_var.set(f"Manual command sent. RX {len(response)} byte(s).")
            logger.info("Manual command sent. %s %s", colorize_hex_frame(command_hex, "TX"), colorize_hex_frame(response_hex, "RX"))
            return

        if event_type == "manual_command_error":
            _, exc = event
            self.status_var.set(f"Manual command failed: {exc}")
            logger.error("Manual command failed: %s", exc)
            return

        if event_type == "af_plane_status":
            _, message = event
            self.af_plane_status_var.set(str(message))
            self.status_var.set(str(message))
            self._set_agent_status(str(message))
            logger.info("FocusMap: %s", message)
            return

        if event_type == "af_plane_point_update":
            _, record, point_index, total_points = event
            self._handle_af_plane_point_update(record, point_index, total_points)
            return

        if event_type == "af_plane_reauto_point_done":
            _, record = event
            self._handle_af_plane_reauto_point_done(record)
            return

        if event_type == "focusmap_af_reset":
            _, point_index = event
            self.af_plane_status_var.set(f"Point {point_index}: AF fit reset.")
            self._draw_focusmap_af_scatter()
            return

        if event_type == "af_plane_fit_update":
            _, model_payload, records, final = event
            self._handle_af_plane_fit_update(model_payload, records, bool(final))
            return

        if event_type == "af_plane_error":
            _, exc = event
            self.af_plane_error_active = True
            self.af_plane_status_var.set(f"Failed: {exc}")
            self.status_var.set(f"FocusMap failed: {exc}")
            logger.error("FocusMap failed: %s", exc)
            return

        if event_type == "af_plane_reauto_done":
            _, stopped = event
            self.af_plane_running = False
            self.af_plane_paused = False
            self.af_plane_pause_event.clear()
            self.af_plane_pause_button_var.set("Pause")
            self.motion_busy = False
            with self.focus_lock:
                self.autofocus_run_end_time = time.monotonic()
            if stopped:
                self.af_plane_status_var.set("Re-Auto Focus stopped.")
                self.status_var.set("FocusMap Re-Auto Focus stopped.")
            if self.af_plane_restore_realtime and not self.realtime_enabled and self.serial_client:
                self.af_plane_restore_realtime = False
                self.toggle_realtime_position()
            if self.af_plane_restore_home_signal and not self.home_signal_enabled and self.serial_client:
                self.af_plane_restore_home_signal = False
                self.toggle_home_signal_polling()
            return

        if event_type == "af_plane_done":
            _, stopped = event
            self.af_plane_running = False
            self.af_plane_paused = False
            self.af_plane_pause_event.clear()
            self.af_plane_pause_button_var.set("Pause")
            self.motion_busy = False
            with self.focus_lock:
                self.autofocus_run_end_time = time.monotonic()
            if stopped:
                self.af_plane_status_var.set("Stopped")
                self.status_var.set("FocusMap stopped.")
            elif self.af_plane_error_active:
                self.status_var.set("FocusMap failed.")
            elif self.af_plane_model_stored:
                self.af_plane_status_var.set("Done. Plane model stored.")
                self.status_var.set("FocusMap completed.")
            else:
                self.af_plane_status_var.set("Done without a valid plane fit.")
                self.status_var.set("FocusMap completed without a valid plane fit.")
            if self.af_plane_restore_realtime and not self.realtime_enabled and self.serial_client:
                self.af_plane_restore_realtime = False
                self.toggle_realtime_position()
            if self.af_plane_restore_home_signal and not self.home_signal_enabled and self.serial_client:
                self.af_plane_restore_home_signal = False
                self.toggle_home_signal_polling()
            logger.info("FocusMap stopped.")
            return

        if event_type == "autofocus_status":
            _, message = event
            self.autofocus_status_var.set(str(message))
            self.status_var.set(str(message))
            self._set_agent_status(str(message))
            logger.info("AutoFocus: %s", message)
            return

        if event_type == "autofocus_sample":
            _, z_position, score, _direction, ppm_bytes = event
            if self.current_page == "AutoFocus" and ppm_bytes and hasattr(self, "autofocus_video_label"):
                self.autofocus_camera_image = tk.PhotoImage(data=ppm_bytes, format="PPM")
                self.autofocus_video_label.configure(image=self.autofocus_camera_image, text="")
            if self.autofocus_running or self.current_page == "AutoFocus":
                self.autofocus_status_var.set(f"Sample Z={z_position}, score={score:.2f}")
                self._draw_autofocus_z_score()
            self._draw_focusmap_af_scatter()
            return

        if event_type == "autofocus_fine_range":
            self._draw_autofocus_z_score()
            self._draw_focusmap_af_scatter()
            return

        if event_type == "autofocus_error":
            _, exc = event
            self.autofocus_status_var.set(f"Failed: {exc}")
            self.status_var.set(f"AutoFocus failed: {exc}")
            self._set_agent_status(f"AutoFocus failed: {exc}")
            logger.error("AutoFocus failed: %s", exc)
            return

        if event_type == "autofocus_done":
            self.autofocus_running = False
            self.motion_busy = False
            with self.focus_lock:
                self.autofocus_run_end_time = time.monotonic()
            if self.autofocus_status_var.get() == "Stopping":
                self.autofocus_status_var.set("Stopped")
            if self.autofocus_restore_realtime and not self.realtime_enabled and self.serial_client:
                self.autofocus_restore_realtime = False
                self.toggle_realtime_position()
            if self.autofocus_restore_home_signal and not self.home_signal_enabled and self.serial_client:
                self.autofocus_restore_home_signal = False
                self.toggle_home_signal_polling()
            self._set_agent_status(self.autofocus_status_var.get())
            logger.info("AutoFocus stopped.")
            return

        if event_type == "imgstitch_status":
            _, message = event
            self.imgstitch_status_var.set(str(message))
            self.status_var.set(str(message))
            self._set_agent_status(str(message))
            logger.info("ImgStitch: %s", message)
            return

        if event_type == "imgstitch_preview":
            mosaic = event[1]
            if len(event) >= 6:
                self.imgstitch_session = event[2]
                self.imgstitch_tile_images = event[3]
                self.imgstitch_latest_positions = event[4]
                self.imgstitch_latest_edges = event[5]
                self.imgstitch_quality_var.set(self._imgstitch_quality_summary(self.imgstitch_latest_edges))
                self.imgstitch_status_var.set(self._imgstitch_displacement_status(self.imgstitch_latest_edges))
            self._show_imgstitch_preview(mosaic)
            return

        if event_type == "imgstitch_recompose_done":
            _, mosaic, positions, edges, status = event
            self.imgstitch_recompose_running = False
            if self.imgstitch_recompose_button is not None:
                self.imgstitch_recompose_button.configure(state="normal", text="Apply and Recalculate")
            self.imgstitch_latest_positions = positions
            self.imgstitch_latest_edges = edges
            self.imgstitch_quality_var.set(self._imgstitch_quality_summary(edges))
            self.imgstitch_status_var.set(str(status))
            self.status_var.set("ImgStitch recalculated.")
            self._show_imgstitch_preview(mosaic)
            return

        if event_type == "imgstitch_recompose_error":
            _, exc = event
            self.imgstitch_recompose_running = False
            if self.imgstitch_recompose_button is not None:
                self.imgstitch_recompose_button.configure(state="normal", text="Apply and Recalculate")
            self.imgstitch_status_var.set(f"Recompose failed: {exc}")
            self.status_var.set(f"ImgStitch recompose failed: {exc}")
            logger.error("ImgStitch recompose failed: %s", exc)
            return

        if event_type == "imgstitch_done":
            _, output_path = event
            self.imgstitch_status_var.set(f"Saved {output_path.name}")
            self.status_var.set(f"ImgStitch saved: {output_path}")
            self._set_agent_status(f"ImgStitch saved: {output_path}")
            logger.info("ImgStitch saved to %s.", output_path)
            return

        if event_type == "imgstitch_error":
            _, exc = event
            self.imgstitch_status_var.set(f"Failed: {exc}")
            self.status_var.set(f"ImgStitch failed: {exc}")
            self._set_agent_status(f"ImgStitch failed: {exc}")
            logger.error("ImgStitch failed: %s", exc)
            return

        if event_type == "imgstitch_finished":
            self.imgstitch_running = False
            self.imgstitch_focus_sampling_required = False
            self.motion_busy = False
            if self.imgstitch_status_var.get() == "Stopping":
                self.imgstitch_status_var.set("Stopped")
            if self.imgstitch_restore_realtime and not self.realtime_enabled and self.serial_client:
                self.imgstitch_restore_realtime = False
                self.toggle_realtime_position()
            if self.imgstitch_restore_home_signal and not self.home_signal_enabled and self.serial_client:
                self.imgstitch_restore_home_signal = False
                self.toggle_home_signal_polling()
            self._set_agent_status(self.imgstitch_status_var.get())
            return

        if event_type == "motor_command":
            _, axis, action, command, source = event
            command_hex = hex_bytes(command)
            self.tx_var.set(command_hex)
            if self._record_hex_history_for_source(source):
                self._append_hex_history("TX", command_hex)
            self.status_var.set(f"{axis} {action} command sent.")
            if source == "autofocus manual":
                self.autofocus_status_var.set(f"{axis} manual command sent")
            logger.info(
                "%s %s command sent from %s: %s",
                axis,
                action,
                source,
                colorize_hex_frame(command_hex, "TX"),
                extra={"repeat_key": "keyboard_motion"} if self._is_low_latency_jog_source(source) else None,
            )
            return

        if event_type == "axis_done":
            _, axis, response, source = event
            response_hex = hex_bytes(response)
            self.rx_var.set(response_hex)
            if self._record_hex_history_for_source(source):
                self._append_hex_history("RX", response_hex)
            self.status_var.set(f"{axis} reached-position feedback received.")
            logger.info(
                "%s reached-position feedback from %s: %s",
                axis,
                source,
                colorize_hex_frame(response_hex, "RX"),
                extra={"repeat_key": "keyboard_motion"} if self._is_low_latency_jog_source(source) else None,
            )
            return

        if event_type == "cc_done":
            _, response, source = event
            response_hex = hex_bytes(response)
            self.rx_var.set(response_hex)
            self._append_hex_history("RX", response_hex)
            self.status_var.set("CC multi-axis move completed.")
            logger.info("CC completed feedback from %s: %s", source, colorize_hex_frame(response_hex, "RX"))
            return

        if event_type == "motor_error":
            _, axis, exc = event
            if axis == "FOCUSMAP_Z":
                self.main_focusmap_plane_var.set(False)
                self.clear_position_edits()
            if axis == "GDS_MAPPER":
                self._set_gds_mapper_status(f"LayoutBond move failed: {exc}")
                self._set_agent_status(f"LayoutBond move failed: {exc}")
                logger.error("LayoutBond move failed: %s", exc)
                return
            self.status_var.set(f"{axis} motor command failed: {exc}")
            logger.error("%s motor command failed: %s", axis, exc)
            return

        if event_type == "motor_done":
            self.motion_busy = False
            self.keyboard_motion_busy = False

    def start_camera(self) -> None:
        try:
            index = int(self.camera_index_var.get())
        except ValueError:
            self.status_var.set("Camera index must be an integer.")
            self._set_camera_index_error(True)
            logger.warning("Camera start skipped because index is not an integer: %s", self.camera_index_var.get())
            return

        self.camera_session_id += 1
        session_id = self.camera_session_id
        self._set_camera_index_error(False)
        with self.camera_lock:
            self.latest_camera_frame = None
        self.camera = UsbCamera(index=index, width=800, height=450, settings=self._camera_settings_from_config())
        self.camera_running = True
        self.camera_rendering = True
        logger.info("Starting USB camera preview on index %s.", index)
        self.camera_thread = threading.Thread(target=self._camera_worker, args=(session_id, self.camera), daemon=True)
        self.camera_thread.start()
        self._update_camera_frame()

    def restart_camera(self) -> None:
        logger.info("Restarting USB camera preview.")
        self.stop_camera()
        self.start_camera()

    def stop_camera(self) -> None:
        self.camera_session_id += 1
        self.camera_running = False
        self.camera_rendering = False
        if self.camera:
            self.camera.close()
        self.camera = None

    def _camera_worker(self, session_id: int, camera: UsbCamera) -> None:
        reported_ready = False
        release_attempted = False
        while self.camera_running and session_id == self.camera_session_id:
            try:
                frame = camera.read(calculate_focus_scores=self._should_process_focus_scores())
            except Exception as exc:
                if not release_attempted:
                    release_attempted = True
                    if request_web_fallback_camera_release():
                        time.sleep(0.5)
                        continue
                self.result_queue.put(("camera_error", session_id, exc))
                return
            if frame:
                if not reported_ready:
                    self.result_queue.put(("camera_ready", session_id))
                    reported_ready = True
                with self.camera_lock:
                    self.latest_camera_frame = frame
        camera.close()

    def _should_process_focus_scores(self) -> bool:
        return (
            self.current_page == "AutoFocus"
            or self.autofocus_running
            or self.__dict__.get("af_plane_running", False)
            or self.imgstitch_focus_sampling_required
        )

    def _should_update_autofocus_preview(self) -> bool:
        return self.current_page == "AutoFocus" or self.autofocus_running

    def _should_update_imgstitch_preview(self) -> bool:
        return self.current_page == "ImgStitch" or self.imgstitch_running

    def _set_camera_index_error(self, is_error: bool) -> None:
        if hasattr(self, "camera_index_spinbox"):
            self.camera_index_spinbox.configure(style="Error.TSpinbox" if is_error else "TSpinbox")

    def _update_camera_frame(self) -> None:
        if not self.camera_rendering:
            return
        with self.camera_lock:
            frame = self.latest_camera_frame
            self.latest_camera_frame = None

        if frame:
            publish_camera_frame(frame.image_bgr)
            if self.current_page == "Main" and self.vision_panel:
                self.vision_panel.set_image_bgr(self._bgr_with_scalebar(frame.image_bgr))
                self.camera_image = self.vision_panel.photo
            with self.focus_lock:
                self.latest_focus_frame_ppm = frame.ppm_bytes
            with self.camera_lock:
                self.latest_stitch_frame = frame.image_bgr
            if (self.current_page == "FocusMap" or self.af_plane_running) and hasattr(self, "focusmap_realtime_canvas"):
                self.focusmap_realtime_bgr = frame.image_bgr.copy()
                self._draw_focusmap_realtime()
            if self._should_update_autofocus_preview() and hasattr(self, "autofocus_video_label") and not self.autofocus_running:
                self.autofocus_camera_image = tk.PhotoImage(data=frame.ppm_bytes, format="PPM")
                self.autofocus_video_label.configure(image=self.autofocus_camera_image, text="")
            if self._should_update_imgstitch_preview() and hasattr(self, "imgstitch_live_label") and not self.imgstitch_running:
                import cv2

                live = frame.image_bgr
                scale = min(220 / live.shape[1], 124 / live.shape[0], 1.0)
                if scale < 1.0:
                    live = cv2.resize(live, (int(live.shape[1] * scale), int(live.shape[0] * scale)), interpolation=cv2.INTER_AREA)
                rgb_live = cv2.cvtColor(live, cv2.COLOR_BGR2RGB)
                height, width = rgb_live.shape[:2]
                self.imgstitch_camera_image = tk.PhotoImage(data=f"P6 {width} {height} 255\n".encode("ascii") + rgb_live.tobytes(), format="PPM")
                self.imgstitch_live_label.configure(image=self.imgstitch_camera_image, text="")
            if frame.focus_scores:
                self._update_focus_scores(frame.focus_scores, timestamp=frame.captured_at)

        self.after(15, self._update_camera_frame)

    def destroy(self) -> None:
        self.autofocus_stop_event.set()
        self.af_plane_stop_event.set()
        self.imgstitch_stop_event.set()
        self.realtime_stop_event.set()
        self.home_signal_stop_event.set()
        if self.realtime_enabled and self.serial_client:
            try:
                self.serial_client.disable_realtime_position()
            except Exception:
                logger.exception("Failed to disable realtime position during shutdown.")
        self.stop_camera()
        if self.serial_client:
            self.serial_client.close()
        super().destroy()


def main() -> None:
    print_startup_banner()
    logger.info("Application starting.")
    app = ProbeApp()
    try:
        app.mainloop()
    finally:
        logger.info("Application exited.")
