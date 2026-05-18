from __future__ import annotations

import queue
import re
import shutil
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from .camera import UsbCamera
from .config import (
    DEFAULT_CONFIG_FILENAME,
    EYEPIECE_OPTIONS,
    OBJECTIVE_OPTIONS,
    ProbeConfig,
    derive_missing_calibrations,
    load_probe_config,
    pulses_from_um,
    save_probe_config,
)
from .img_stitch import (
    StitchEdgeQuality,
    StitchSession,
    StitchSettings,
    TileRecord,
    build_seam_quality_overlay,
    fit_plane,
    flat_field_correct,
    recompose_session,
    serpentine_indices,
)
from .logging_utils import colorize_hex_frame, configure_logging, print_startup_banner
from .monitor_feed import publish_camera_frame, request_web_fallback_camera_release, start_frame_publisher
from .protocol import COMM_TEST_COMMAND, FUNCTION_READ_POSITION, RESPONSE_HEAD, Axis, AxisPosition, IoStatus, hex_bytes, parse_axis_position_response
from .serial_client import ControllerSerialClient, CommunicationTestResult, list_serial_ports
from .ui.calibration_dialog import PixelCalibrationDialog
from .ui.vision import VisionPanel


logger = configure_logging()
DEFAULT_SERIAL_PORT = "COM5"
RESULT_POLL_INTERVAL_MS = 25
RESULT_POLL_MAX_EVENTS = 30
RESULT_POLL_MAX_SECONDS = 0.012
REALTIME_POSITION_UI_INTERVAL_SECONDS = 0.05


class ProbeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Semi Auto Probe")
        self.geometry("1400x880")
        self.minsize(1040, 600)
        self.configure(bg="#0b0f14")

        self.serial_client: ControllerSerialClient | None = None
        self.camera: UsbCamera | None = None
        self.camera_running = False
        self.camera_rendering = False
        self.camera_image: tk.PhotoImage | None = None
        self.vision_panel: VisionPanel | None = None
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
        self.config_path = Path.cwd() / DEFAULT_CONFIG_FILENAME
        try:
            self.probe_config = load_probe_config(self.config_path)
        except Exception as exc:
            self.probe_config = ProbeConfig()
            logger.error("Failed to load probe config from %s: %s", self.config_path, exc)

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
        self.autofocus_z_score_samples: list[tuple[int, float, int]] = []
        self.autofocus_history_rows: list[dict[str, object]] = []
        self.autofocus_run_start_time: float | None = None
        self.autofocus_run_end_time: float | None = None
        self.autofocus_camera_image: tk.PhotoImage | None = None
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
            "X": (1, 10, 100, 1000),
            "Y": (1, 10, 100, 1000),
            "Z": (1, 10, 50),
        }
        self.motion_mode_var = tk.StringVar(value="Relative")
        self.realtime_enabled = False
        self.realtime_button_var = tk.StringVar(value="Continue")
        self.home_signal_button_var = tk.StringVar(value="Home Signals")
        self.motion_busy = False
        self.keyboard_motion_busy = False
        self.position_read_pending = False
        self.position_read_job: str | None = None
        self.held_keys: dict[str, dict[str, object]] = {}
        self.position_click_job: str | None = None
        self.resize_log_job: str | None = None
        self.last_logged_window_size: tuple[int, int] | None = None
        self.last_logged_control_width: int | None = None
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
        self.imgstitch_quality_var = tk.StringVar(value="No seam data")
        self.imgstitch_point_status_var = tk.StringVar(value="No rectangle points")
        self.imgstitch_plane_af_var = tk.BooleanVar(value=False)
        self.imgstitch_status_var = tk.StringVar(value="Idle")
        self.last_realtime_ui_update = 0.0
        self.last_realtime_status_update = 0.0
        self.axis_indicator_canvases: dict[str, tk.Canvas] = {}
        self.axis_indicator_items: dict[str, int] = {}
        self.axis_indicator_colors = {"X": "#60a5fa", "Y": "#34d399", "Z": "#fbbf24"}
        self.objective_var = tk.StringVar(value=str(self.probe_config.objective))
        self.eyepiece_var = tk.StringVar(value=f"{self.probe_config.eyepiece:g}")
        self.microstep_var = tk.StringVar(value=str(self.probe_config.microstep))
        self.lead_xy_var = tk.StringVar(value=f"{self.probe_config.lead_xy_mm:g}")
        self.lead_z_var = tk.StringVar(value=f"{self.probe_config.lead_z_mm:g}")
        self.base_angle_var = tk.StringVar(value=f"{self.probe_config.base_angle_deg:g}")
        self.cc_speed_percent_var = tk.StringVar(value=str(self.probe_config.cc_speed_percent))
        self.cc_accel_time_var = tk.StringVar(value=f"{self.probe_config.cc_accel_time_s:g}")
        self.autofocus_settle_ms_var = tk.StringVar(value=str(self.probe_config.autofocus_settle_ms))
        self.autofocus_sample_count_var = tk.StringVar(value=str(self.probe_config.autofocus_sample_count))
        self.imgstitch_settle_ms_var = tk.StringVar(value=str(self.probe_config.imgstitch_settle_ms))
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

        self._configure_theme()
        self._build_ui()
        self._bind_keyboard_controls()
        start_frame_publisher()
        self.bind("<Configure>", self._on_window_configure)
        self.port_combo["values"] = (DEFAULT_SERIAL_PORT,)
        self.start_camera()
        self.after(300, self.connect_and_test_serial)
        self.after(RESULT_POLL_INTERVAL_MS, self._poll_result_queue)

    def _configure_theme(self) -> None:
        self.colors = {
            "bg": "#0b0f14",
            "surface": "#111821",
            "surface_2": "#151f2b",
            "surface_3": "#1b2735",
            "border": "#263545",
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
        style.configure("TButton", background=self.colors["surface_3"], foreground=self.colors["text"], bordercolor=self.colors["border"], focusthickness=0, padding=(10, 6))
        style.map("TButton", background=[("active", "#223144"), ("pressed", "#1d2a3a")])
        style.configure("Accent.TButton", background="#0f3b2d", foreground="#d1fae5", bordercolor="#1f7a5a", padding=(12, 6))
        style.map("Accent.TButton", background=[("active", "#14543f"), ("pressed", "#0f3b2d")])
        style.configure("Danger.TButton", background="#4c0519", foreground="#ffe4e6", bordercolor="#be123c", padding=(12, 6))
        style.map("Danger.TButton", background=[("active", "#881337"), ("pressed", "#4c0519")])
        style.configure("Ghost.TButton", background=self.colors["surface"], foreground=self.colors["muted"], bordercolor=self.colors["border"], padding=(8, 6))
        style.map("Ghost.TButton", background=[("active", self.colors["surface_2"])], foreground=[("active", self.colors["text"])])
        style.configure("TEntry", fieldbackground=self.colors["surface_2"], background=self.colors["surface_2"], foreground=self.colors["text"], bordercolor=self.colors["border"], insertcolor=self.colors["text"], padding=5)
        style.map(
            "TEntry",
            fieldbackground=[("focus", self.colors["surface_2"]), ("!disabled", self.colors["surface_2"])],
            foreground=[("focus", self.colors["text"]), ("!disabled", self.colors["text"])],
        )
        style.configure("TCombobox", fieldbackground=self.colors["surface_2"], background=self.colors["surface_2"], foreground=self.colors["text"], bordercolor=self.colors["border"], arrowcolor=self.colors["muted"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", self.colors["surface_2"])], foreground=[("readonly", self.colors["text"])])
        style.configure("TSpinbox", fieldbackground=self.colors["surface_2"], background=self.colors["surface_2"], foreground=self.colors["text"], bordercolor=self.colors["border"], arrowcolor=self.colors["muted"], padding=5)
        style.configure("Error.TSpinbox", fieldbackground="#3f1018", background="#3f1018", foreground="#fecdd3", bordercolor="#be123c", arrowcolor="#fecdd3", padding=5)
        style.configure("TRadiobutton", background=self.colors["surface"], foreground=self.colors["text"], indicatorcolor=self.colors["surface_2"], padding=(4, 2))
        style.map("TRadiobutton", background=[("active", self.colors["surface"])], foreground=[("active", self.colors["text"])], indicatorcolor=[("selected", self.colors["accent"])])
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
        self.option_add("*TCombobox*Listbox.background", self.colors["surface_2"])
        self.option_add("*TCombobox*Listbox.foreground", self.colors["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", self.colors["surface_3"])
        self.option_add("*TCombobox*Listbox.selectForeground", self.colors["text"])

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
        self.camera_index_spinbox = ttk.Spinbox(toolbar, from_=0, to=8, textvariable=self.camera_index_var, width=3)
        self.camera_index_spinbox.grid(row=0, column=6, padx=(0, 6), ipady=1)
        ttk.Button(toolbar, text="Restart", command=self.restart_camera).grid(row=0, column=7, padx=(0, 10))
        ttk.Button(toolbar, text="EMERGENCY STOP", style="Danger.TButton", command=self.emergency_stop).grid(row=0, column=8)

        content = ttk.Frame(self, style="App.TFrame")
        content.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))
        content.columnconfigure(0, weight=1)
        content.rowconfigure(1, weight=1)

        self.tab_buttons: dict[str, tk.Label] = {}
        tab_bar = ttk.Frame(content, style="App.TFrame")
        tab_bar.grid(row=0, column=0, sticky="w")
        for col, name in enumerate(("Main", "Communication", "AutoFocus", "ImgStitch", "Config")):
            tab_bar.columnconfigure(col, weight=1, uniform="top_tabs", minsize=156)
            label = tk.Label(
                tab_bar,
                text=name,
                anchor="center",
                bg="#172536" if name == "Main" else "#0f1722",
                fg="#d1fae5" if name == "Main" else self.colors["muted"],
                font=("Segoe UI Semibold", 10),
                padx=14,
                pady=10,
                bd=0,
                highlightthickness=1,
                highlightbackground=self.colors["border"],
                highlightcolor=self.colors["border"],
                cursor="hand2",
            )
            label.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 1, 0))
            label.bind("<Button-1>", lambda _event, page=name: self.show_page(page))
            self.tab_buttons[name] = label

        page_container = ttk.Frame(content, style="App.TFrame")
        page_container.grid(row=1, column=0, sticky="nsew")
        page_container.columnconfigure(0, weight=1)
        page_container.rowconfigure(0, weight=1)

        main_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        communication_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        autofocus_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        imgstitch_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        config_page = ttk.Frame(page_container, style="App.TFrame", padding=(0, 10, 0, 0))
        self.pages = {"Main": main_page, "Communication": communication_page, "AutoFocus": autofocus_page, "ImgStitch": imgstitch_page, "Config": config_page}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self._build_main_page(main_page)
        self._build_communication_page(communication_page)
        self._build_autofocus_page(autofocus_page)
        self._build_imgstitch_page(imgstitch_page)
        self._build_config_page(config_page)
        self._update_config_display()
        self.show_page("Main")

    def show_page(self, name: str) -> None:
        self.current_page = name
        self.pages[name].tkraise()
        for page_name, button in self.tab_buttons.items():
            selected = page_name == name
            button.configure(
                bg="#172536" if selected else "#0f1722",
                fg="#d1fae5" if selected else self.colors["muted"],
                highlightbackground="#2dd4bf" if selected else self.colors["border"],
            )

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
        ttk.Label(camera_header, text="USB camera preview", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

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
        speed = self.probe_config.cc_speed_percent if pulses else 0
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
            entry = tk.Entry(
                cell,
                textvariable=self.position_vars[axis],
                justify="center",
                relief="flat",
                bd=0,
                bg=self.colors["surface_2"],
                fg=self.colors["accent"],
                insertbackground=self.colors["text"],
                selectbackground=self.colors["surface_3"],
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
        ttk.Button(zero_bar, text="Set New Zero", style="Accent.TButton", command=self.set_xyz_zero).grid(row=0, column=1, sticky="ew", padx=(4, 4))
        ttk.Button(zero_bar, text="Go Zero", command=self.go_xyz_zero).grid(row=0, column=2, sticky="ew", padx=(4, 0))

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
        ttk.Spinbox(row, from_=1, to=1_000_000, increment=1, textvariable=self.step_vars[axis], width=7).grid(row=1, column=2, sticky="ew", padx=(6, 6), pady=(4, 0))
        ttk.Button(row, text="Fwd", style="Accent.TButton", command=lambda a=axis: self.axis_forward(a)).grid(row=0, column=2, sticky="ew", padx=(6, 0))
        ttk.Button(row, text="Rev", command=lambda a=axis: self.axis_reverse(a)).grid(row=0, column=3, sticky="ew", padx=(6, 0))
        ttk.Button(row, text="Stop", style="Ghost.TButton", command=lambda a=axis: self.axis_stop(a)).grid(row=1, column=3, sticky="ew", padx=(8, 0), pady=(4, 0))

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
        ttk.Spinbox(mode_bar, from_=0, to=4096, increment=1, width=6, textvariable=self.comm_read_length_var).grid(row=0, column=3, sticky="w")
        ttk.Button(mode_bar, text="Load Test", style="Ghost.TButton", command=self.load_default_comm_test).grid(row=0, column=4, sticky="e", padx=(18, 0))
        ttk.Button(mode_bar, text="Send", style="Accent.TButton", command=self.send_manual_command).grid(row=0, column=5, sticky="e", padx=(8, 0))
        ttk.Button(mode_bar, text="Clear", command=self.clear_hex_history).grid(row=0, column=6, sticky="e", padx=(8, 0))

        self.comm_input = tk.Text(
            command_panel,
            bg=self.colors["surface_2"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground=self.colors["surface_3"],
            relief="flat",
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
            bg=self.colors["surface_2"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground=self.colors["surface_3"],
            relief="flat",
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
        window_spin = ttk.Spinbox(graph_header, from_=5, to=600, increment=5, width=6, textvariable=self.focus_window_var, command=self._draw_focus_history)
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
        ttk.Spinbox(control_panel, from_=1, to=10000, increment=1, textvariable=self.autofocus_step_var, width=10).grid(row=5, column=0, sticky="ew")
        ttk.Label(control_panel, text="Min Step", style="Muted.TLabel").grid(row=6, column=0, sticky="w", pady=(12, 4))
        ttk.Spinbox(control_panel, from_=1, to=10000, increment=1, textvariable=self.autofocus_min_step_var, width=10).grid(row=7, column=0, sticky="ew")
        ttk.Label(control_panel, text="Search Range (+/-)", style="Muted.TLabel").grid(row=8, column=0, sticky="w", pady=(12, 4))
        ttk.Spinbox(control_panel, from_=1, to=1000000, increment=10, textvariable=self.autofocus_max_moves_var, width=10).grid(row=9, column=0, sticky="ew")

        manual = ttk.Frame(control_panel, style="Panel.TFrame")
        manual.grid(row=10, column=0, sticky="ew", pady=(16, 0))
        manual.columnconfigure((0, 1), weight=1, uniform="af_manual")
        ttk.Button(manual, text="Z-", command=lambda: self.autofocus_manual_z(reverse=True)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(manual, text="Z+", style="Accent.TButton", command=lambda: self.autofocus_manual_z(reverse=False)).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Button(control_panel, text="Start Auto", style="Accent.TButton", command=self.start_autofocus).grid(row=11, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(control_panel, text="Set Z=0", command=self.set_autofocus_z_zero).grid(row=12, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(control_panel, text="Stop", style="Danger.TButton", command=self.stop_autofocus).grid(row=13, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(control_panel, textvariable=self.autofocus_status_var, style="Status.TLabel", wraplength=190, padding=10).grid(row=14, column=0, sticky="ew", pady=(16, 0))

    def _build_imgstitch_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(0, weight=1)

        preview_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        preview_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_panel.columnconfigure(0, weight=1)
        preview_panel.rowconfigure(1, weight=1)

        header = ttk.Frame(preview_panel, style="Panel.TFrame")
        header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="IMG STITCH MOSAIC", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, textvariable=self.imgstitch_quality_var, style="Status.TLabel", padding=(10, 4)).grid(row=0, column=1, sticky="e")
        self.imgstitch_mosaic_canvas = tk.Canvas(preview_panel, bg="#05070a", highlightthickness=0)
        self.imgstitch_mosaic_canvas.grid(row=1, column=0, sticky="nsew")
        self.imgstitch_mosaic_canvas.create_text(20, 20, text="No mosaic yet", anchor="nw", fill=self.colors["muted"], font=("Segoe UI Semibold", 14))
        self.imgstitch_mosaic_canvas.bind("<MouseWheel>", self._on_imgstitch_preview_wheel)
        self.imgstitch_mosaic_canvas.bind("<Button-4>", self._on_imgstitch_preview_wheel)
        self.imgstitch_mosaic_canvas.bind("<Button-5>", self._on_imgstitch_preview_wheel)
        self.imgstitch_mosaic_canvas.bind("<ButtonPress-1>", self._on_imgstitch_preview_press)
        self.imgstitch_mosaic_canvas.bind("<B1-Motion>", self._on_imgstitch_preview_drag)
        self.imgstitch_mosaic_canvas.bind("<Configure>", lambda _event: self._render_imgstitch_preview())

        lower_panel = ttk.Frame(preview_panel, style="Panel.TFrame")
        lower_panel.grid(row=2, column=0, sticky="ew", pady=(12, 0))
        lower_panel.columnconfigure(0, weight=1)
        lower_panel.columnconfigure(1, weight=0)
        ttk.Label(lower_panel, textvariable=self.imgstitch_point_status_var, style="Value.TLabel", wraplength=460, padding=8).grid(row=0, column=0, sticky="ew", padx=(0, 10))
        self.imgstitch_live_label = ttk.Label(lower_panel, anchor="center", text="Camera", style="Video.TLabel", width=22)
        self.imgstitch_live_label.grid(row=0, column=1, sticky="e")

        control_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        control_panel.grid(row=0, column=1, sticky="ns")
        control_panel.columnconfigure(0, weight=1)
        ttk.Label(control_panel, text="RANGE", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        mode_combo = ttk.Combobox(control_panel, textvariable=self.imgstitch_range_mode_var, values=("Array", "Space", "Two Points"), state="readonly", width=16)
        mode_combo.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._update_imgstitch_mode_fields())

        self.imgstitch_mode_widgets: dict[str, list[tk.Widget]] = {"Array": [], "Space": [], "Two Points": [], "Manual Step": []}

        def add_spinbox(row_index: int, label: str, variable: tk.StringVar, mode: str | None = None) -> int:
            label_widget = ttk.Label(control_panel, text=label, style="Muted.TLabel")
            label_widget.grid(row=row_index, column=0, sticky="w", pady=(12, 4))
            spinbox = ttk.Spinbox(control_panel, from_=1, to=1_000_000, increment=1, textvariable=variable, width=14)
            spinbox.grid(row=row_index + 1, column=0, sticky="ew")
            if mode is not None:
                self.imgstitch_mode_widgets[mode].extend([label_widget, spinbox])
            return row_index + 2

        row = 2
        row = add_spinbox(row, "Rows", self.imgstitch_rows_var, "Array")
        row = add_spinbox(row, "Cols", self.imgstitch_cols_var, "Array")
        row = add_spinbox(row, "Width (um)", self.imgstitch_width_um_var, "Space")
        row = add_spinbox(row, "Height (um)", self.imgstitch_height_um_var, "Space")

        point_buttons = ttk.Frame(control_panel, style="Panel.TFrame")
        point_buttons.grid(row=row, column=0, sticky="ew", pady=(12, 0))
        point_buttons.columnconfigure((0, 1), weight=1, uniform="points")
        ttk.Button(point_buttons, text="Point 1", command=lambda: self.record_imgstitch_point(1)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(point_buttons, text="Point 2", command=lambda: self.record_imgstitch_point(2)).grid(row=0, column=1, sticky="ew", padx=(4, 0))
        self.imgstitch_mode_widgets["Two Points"].append(point_buttons)
        row += 1

        ttk.Label(control_panel, text="ACQUISITION", style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(18, 4))
        row += 1
        for label, variable in (
            ("Overlap X (px)", self.imgstitch_overlap_x_var),
            ("Overlap Y (px)", self.imgstitch_overlap_y_var),
        ):
            row = add_spinbox(row, label, variable)
        row = add_spinbox(row, "Step X (um)", self.imgstitch_step_x_var, "Manual Step")
        row = add_spinbox(row, "Step Y (um)", self.imgstitch_step_y_var, "Manual Step")

        ttk.Label(control_panel, text="RECOMPOSE", style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(18, 4))
        row += 1
        for label, variable in (
            ("Max correction (um)", self.imgstitch_max_correction_um_var),
            ("Registration weight", self.imgstitch_registration_weight_var),
        ):
            ttk.Label(control_panel, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(10, 4))
            ttk.Spinbox(control_panel, from_=0, to=100000, increment=0.1, textvariable=variable, width=14, command=self.recompose_imgstitch_session).grid(row=row + 1, column=0, sticky="ew")
            row += 2
        ttk.Checkbutton(control_panel, text="Show seam quality", variable=self.imgstitch_show_seams_var, command=self.recompose_imgstitch_session).grid(row=row, column=0, sticky="w", pady=(10, 0))
        row += 1

        ttk.Checkbutton(control_panel, text="Four-corner plane AF", variable=self.imgstitch_plane_af_var).grid(row=row, column=0, sticky="w", pady=(16, 0))
        ttk.Button(control_panel, text="Start Stitch", style="Accent.TButton", command=self.start_imgstitch).grid(row=row + 1, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(control_panel, text="Recompose", command=self.recompose_imgstitch_session).grid(row=row + 2, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(control_panel, text="Stop", style="Danger.TButton", command=self.stop_imgstitch).grid(row=row + 3, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(control_panel, textvariable=self.imgstitch_status_var, style="Status.TLabel", wraplength=190, padding=10).grid(row=row + 4, column=0, sticky="ew", pady=(16, 0))
        self._update_imgstitch_mode_fields()

    def _build_config_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(0, weight=1)

        optical_panel = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        optical_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        optical_panel.columnconfigure(0, weight=1)
        optical_panel.columnconfigure(1, weight=1)

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
        ttk.Label(optical_panel, textvariable=self.calibration_status_var, style="Value.TLabel", wraplength=560, padding=10).grid(row=4, column=0, columnspan=2, sticky="ew")
        ttk.Button(optical_panel, text="Calibrate Pixels", style="Accent.TButton", command=self.open_pixel_calibration).grid(row=5, column=0, sticky="ew", pady=(14, 0), padx=(0, 8))
        ttk.Button(optical_panel, text="Save Config", command=self.apply_config).grid(row=5, column=1, sticky="ew", pady=(14, 0), padx=(8, 0))
        ttk.Label(optical_panel, textvariable=self.config_status_var, style="Status.TLabel", wraplength=560, padding=10).grid(row=6, column=0, columnspan=2, sticky="ew", pady=(18, 0))

        motor_panel = ttk.Frame(parent, style="Panel.TFrame", padding=16)
        motor_panel.grid(row=0, column=1, sticky="nsew")
        motor_panel.columnconfigure(0, weight=1)
        motor_panel.columnconfigure(1, weight=1)
        ttk.Label(motor_panel, text="MOTOR MAPPING", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w")

        fields = (
            ("Microstep", self.microstep_var),
            ("Base angle (deg)", self.base_angle_var),
            ("X/Y lead (mm)", self.lead_xy_var),
            ("Z lead (mm)", self.lead_z_var),
            ("CC speed (%)", self.cc_speed_percent_var),
            ("CC accel/decel (s)", self.cc_accel_time_var),
        )
        for index, (label, variable) in enumerate(fields, start=1):
            col = (index - 1) % 2
            row = 1 + ((index - 1) // 2) * 2
            ttk.Label(motor_panel, text=label, style="Muted.TLabel").grid(row=row, column=col, sticky="w", pady=(16, 4), padx=(0 if col == 0 else 8, 8 if col == 0 else 0))
            entry = tk.Entry(
                motor_panel,
                textvariable=variable,
                relief="flat",
                bd=0,
                bg=self.colors["surface_2"],
                fg=self.colors["text"],
                insertbackground=self.colors["text"],
                selectbackground=self.colors["surface_3"],
                font=("Segoe UI", 10),
            )
            entry.grid(row=row + 1, column=col, sticky="ew", padx=(0 if col == 0 else 8, 8 if col == 0 else 0), ipady=6)

        ttk.Label(motor_panel, text="CONVERSION", style="Section.TLabel").grid(row=7, column=0, columnspan=2, sticky="w", pady=(24, 6))
        ttk.Label(motor_panel, textvariable=self.motor_conversion_var, style="Value.TLabel", wraplength=560, padding=10).grid(row=8, column=0, columnspan=2, sticky="ew")
        autofocus_panel = ttk.Frame(motor_panel, style="Panel.TFrame")
        autofocus_panel.grid(row=9, column=0, columnspan=2, sticky="ew", pady=(24, 0))
        autofocus_panel.columnconfigure((1, 2), weight=1, uniform="af_thresholds")
        ttk.Label(autofocus_panel, text="AUTOFOCUS CONFIG", style="Section.TLabel").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 6))
        ttk.Label(autofocus_panel, text="Settle after Z move (ms)", style="Muted.TLabel").grid(row=1, column=0, sticky="w", pady=2)
        tk.Entry(
            autofocus_panel,
            textvariable=self.autofocus_settle_ms_var,
            relief="flat",
            bd=0,
            bg=self.colors["surface_2"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground=self.colors["surface_3"],
            font=("Segoe UI", 10),
        ).grid(row=1, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=5)
        ttk.Label(autofocus_panel, text="AF integration frames", style="Muted.TLabel").grid(row=2, column=0, sticky="w", pady=2)
        tk.Entry(
            autofocus_panel,
            textvariable=self.autofocus_sample_count_var,
            relief="flat",
            bd=0,
            bg=self.colors["surface_2"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground=self.colors["surface_3"],
            font=("Segoe UI", 10),
        ).grid(row=2, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=5)
        ttk.Label(autofocus_panel, text="Settle after stitch move (ms)", style="Muted.TLabel").grid(row=3, column=0, sticky="w", pady=2)
        tk.Entry(
            autofocus_panel,
            textvariable=self.imgstitch_settle_ms_var,
            relief="flat",
            bd=0,
            bg=self.colors["surface_2"],
            fg=self.colors["text"],
            insertbackground=self.colors["text"],
            selectbackground=self.colors["surface_3"],
            font=("Segoe UI", 10),
        ).grid(row=3, column=1, columnspan=2, sticky="ew", padx=(8, 0), ipady=5)
        ttk.Label(autofocus_panel, text="Metric", style="Muted.TLabel").grid(row=4, column=0, sticky="w", pady=(10, 2))
        ttk.Label(autofocus_panel, text="Yellow", style="Muted.TLabel").grid(row=4, column=1, sticky="w", padx=(8, 0), pady=(10, 2))
        ttk.Label(autofocus_panel, text="Green", style="Muted.TLabel").grid(row=4, column=2, sticky="w", padx=(8, 0), pady=(10, 2))
        for row_index, metric_name in enumerate(("Laplacian", "Tenengrad", "Brenner"), start=5):
            ttk.Label(autofocus_panel, text=metric_name, style="Muted.TLabel").grid(row=row_index, column=0, sticky="w", pady=2)
            for column, variable in (
                (1, self.focus_threshold_yellow_vars[metric_name]),
                (2, self.focus_threshold_green_vars[metric_name]),
            ):
                tk.Entry(
                    autofocus_panel,
                    textvariable=variable,
                    relief="flat",
                    bd=0,
                    bg=self.colors["surface_2"],
                    fg=self.colors["text"],
                    insertbackground=self.colors["text"],
                    selectbackground=self.colors["surface_3"],
                    font=("Segoe UI", 10),
                ).grid(row=row_index, column=column, sticky="ew", padx=(8, 0), pady=2, ipady=5)

        ttk.Button(motor_panel, text="Apply Mapping", style="Accent.TButton", command=self.apply_config).grid(row=10, column=0, columnspan=2, sticky="ew", pady=(18, 0))

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
        self.key_bindings = {
            "Right": ("X", False),
            "Left": ("X", True),
            "Up": ("Y", False),
            "Down": ("Y", True),
            "Prior": ("Z", False),
            "Next": ("Z", True),
        }
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
            if self.vision_panel:
                self.vision_panel.set_shift_down(True)
            return None
        if isinstance(event.widget, tk.Entry):
            return None
        binding = self.key_bindings.get(event.keysym)
        if binding is None:
            return None
        if event.keysym in self.held_keys:
            return "break"

        axis, reverse = binding
        self.held_keys[event.keysym] = {
            "axis": axis,
            "reverse": reverse,
            "interval_ms": 420,
            "job": None,
        }
        self._keyboard_move(event.keysym)
        return "break"

    def _on_key_release(self, event: tk.Event) -> str | None:
        if event.keysym in ("Shift_L", "Shift_R"):
            if self.vision_panel:
                self.vision_panel.set_shift_down(False)
            return None
        if isinstance(event.widget, tk.Entry):
            return None
        state = self.held_keys.pop(event.keysym, None)
        if state is None:
            return None

        job = state.get("job")
        if job is not None:
            self.after_cancel(str(job))
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

        starting_new_mode = self.current_position_edit_mode != mode
        self.current_position_edit_mode = mode

        if starting_new_mode:
            self.modified_position_axes.clear()
            for target_axis in ("X", "Y", "Z"):
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
        self.begin_position_edit(next_axis, self.current_position_edit_mode or "Relative")
        return "break"

    def focus_previous_position_input(self, axis: str, _event: tk.Event) -> str:
        axes = ("X", "Y", "Z")
        self._fill_empty_position_default(axis)
        previous_axis = axes[(axes.index(axis) - 1) % len(axes)]
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

    def _ppm_with_scalebar(self, image_bgr) -> bytes:
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
                cc_accel_time_s=float(self.cc_accel_time_var.get()),
                autofocus_settle_ms=int(self.autofocus_settle_ms_var.get()),
                autofocus_sample_count=int(self.autofocus_sample_count_var.get()),
                imgstitch_settle_ms=int(self.imgstitch_settle_ms_var.get()),
                focus_threshold_yellow={
                    metric: float(self.focus_threshold_yellow_vars[metric].get())
                    for metric in ("Laplacian", "Tenengrad", "Brenner")
                },
                focus_threshold_green={
                    metric: float(self.focus_threshold_green_vars[metric].get())
                    for metric in ("Laplacian", "Tenengrad", "Brenner")
                },
                calibrations=dict(self.probe_config.calibrations),
            )
            updated.validate()
        except ValueError as exc:
            self.config_status_var.set(f"Invalid config: {exc}")
            self.status_var.set(f"Config invalid: {exc}")
            return False

        self.probe_config = updated
        derive_missing_calibrations(self.probe_config)
        self._sync_config_vars_from_config()
        self._update_config_display()
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

    def _sync_config_vars_from_config(self) -> None:
        self.objective_var.set(str(self.probe_config.objective))
        self.eyepiece_var.set(f"{self.probe_config.eyepiece:g}")
        self.microstep_var.set(str(self.probe_config.microstep))
        self.lead_xy_var.set(f"{self.probe_config.lead_xy_mm:g}")
        self.lead_z_var.set(f"{self.probe_config.lead_z_mm:g}")
        self.base_angle_var.set(f"{self.probe_config.base_angle_deg:g}")
        self.cc_speed_percent_var.set(str(self.probe_config.cc_speed_percent))
        self.cc_accel_time_var.set(f"{self.probe_config.cc_accel_time_s:g}")
        self.autofocus_settle_ms_var.set(str(self.probe_config.autofocus_settle_ms))
        self.autofocus_sample_count_var.set(str(self.probe_config.autofocus_sample_count))
        self.imgstitch_settle_ms_var.set(str(self.probe_config.imgstitch_settle_ms))
        for metric in ("Laplacian", "Tenengrad", "Brenner"):
            self.focus_threshold_yellow_vars[metric].set(f"{self.probe_config.focus_threshold_yellow[metric]:g}")
            self.focus_threshold_green_vars[metric].set(f"{self.probe_config.focus_threshold_green[metric]:g}")

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
            f"CC: speed {self.probe_config.cc_speed_percent}%, accel/decel {self.probe_config.cc_accel_time_s:.3g}s ({self.probe_config.cc_acceleration_units()} units)",
            f"AF settle: {self.probe_config.autofocus_settle_ms} ms",
            f"AF integration: {self.probe_config.autofocus_sample_count} frame(s)",
            f"Stitch settle: {self.probe_config.imgstitch_settle_ms} ms",
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
            "autofocus",
            "autofocus manual",
            "button",
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

    def _update_focus_scores(self, scores: dict[str, float]) -> None:
        metric = self.focus_metric_var.get()
        score = float(scores.get(metric, 0.0))
        now = time.monotonic()
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
        canvas = self.z_score_canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=self.colors["surface_2"], outline="")
        canvas.create_text(12, 12, text="AF Z vs Score", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))
        with self.focus_lock:
            samples = list(self.autofocus_z_score_samples)
        if len(samples) < 2:
            canvas.create_text(width // 2, height // 2, text="Waiting for AF samples", fill=self.colors["muted"], font=("Segoe UI Semibold", 11))
            return

        z_values = [sample[0] for sample in samples]
        score_values = [sample[1] for sample in samples]
        min_z, max_z = min(z_values), max(z_values)
        fit_model = self._fit_gaussian_focus_model({z: score for z, score, _direction in samples})
        min_score, max_score = 0.0, max(score_values)
        if fit_model is not None:
            max_score = max(max_score, fit_model["baseline"] + fit_model["amplitude"])
        z_span = max(max_z - min_z, 1)
        score_span = max(max_score - min_score, 1.0)
        left, top, right, bottom = 50, 28, width - 18, height - 34
        canvas.create_rectangle(left, top, right, bottom, outline=self.colors["border"])
        metric = self.focus_metric_var.get()
        points: list[float] = []
        for z_value, score, direction in samples:
            x = left + (z_value - min_z) / z_span * (right - left)
            y = bottom - (score - min_score) / score_span * (bottom - top)
            points.extend((x, y))
        if len(points) >= 4:
            canvas.create_line(*points, fill="#94a3b8", width=1, dash=(3, 3))
        if fit_model is not None:
            curve_points: list[float] = []
            for index in range(80):
                z_value = min_z + index * z_span / 79
                fit_score = self._gaussian_score_at(z_value, fit_model)
                x = left + (z_value - min_z) / z_span * (right - left)
                y = bottom - (fit_score - min_score) / score_span * (bottom - top)
                curve_points.extend((x, y))
            if len(curve_points) >= 4:
                canvas.create_line(*curve_points, fill="#e5e7eb", width=2, smooth=True)
            mu_x = left + (fit_model["mu"] - min_z) / z_span * (right - left)
            canvas.create_line(mu_x, top, mu_x, bottom, fill="#e5e7eb", dash=(4, 3))
            canvas.create_text(
                right - 8,
                top + 8,
                text=f"mu {fit_model['mu']:.1f} | sigma {fit_model['sigma']:.1f}",
                anchor="ne",
                fill="#e5e7eb",
                font=("Segoe UI", 8),
            )
        for z_value, score, direction in samples:
            x = left + (z_value - min_z) / z_span * (right - left)
            y = bottom - (score - min_score) / score_span * (bottom - top)
            color = self._focus_score_color(metric, score)
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="")
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

    def _sample_focus_score_frames(self, after_time: float, sample_count: int, timeout: float = 2.0) -> dict[str, float]:
        sample_count = max(1, int(sample_count))
        deadline = time.monotonic() + timeout
        samples: list[dict[str, float]] = []
        last_timestamp = after_time

        while len(samples) < sample_count and time.monotonic() < deadline and not self.autofocus_stop_event.is_set():
            with self.focus_lock:
                timestamp = self.latest_focus_timestamp
                scores = dict(self.latest_focus_scores)
            if timestamp > last_timestamp:
                samples.append(scores)
                last_timestamp = timestamp
            else:
                time.sleep(0.01)

        if not samples:
            with self.focus_lock:
                return dict(self.latest_focus_scores)

        return {
            metric_name: sum(sample.get(metric_name, 0.0) for sample in samples) / len(samples)
            for metric_name in ("Laplacian", "Tenengrad", "Brenner")
        }

    def _sample_focus_score(self, metric: str, duration: float = 0.36, after_time: float | None = None) -> float:
        return self._sample_focus_scores(after_time=after_time, duration=duration).get(metric, 0.0)

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
    ) -> None:
        with self.focus_lock:
            timestamp = self.latest_focus_timestamp or time.monotonic()
            scores = dict(scores or self.latest_focus_scores)
            self.autofocus_samples.append((timestamp, score, direction))
            self.focus_history.append((timestamp, scores))
            self.autofocus_z_score_samples.append((z_position, score, direction))
            self.autofocus_history_rows.append(
                {
                    "timestamp": f"{timestamp:.6f}",
                    "stage": stage,
                    "z_position": z_position,
                    "direction": direction,
                    "selected_metric": metric,
                    "selected_score": f"{score:.6f}",
                    "laplacian": f"{scores.get('Laplacian', 0.0):.6f}",
                    "tenengrad": f"{scores.get('Tenengrad', 0.0):.6f}",
                    "brenner": f"{scores.get('Brenner', 0.0):.6f}",
                    "command_hex": command_hex,
                    "reached_hex": reached_hex,
                }
            )
            ppm_bytes = self.latest_focus_frame_ppm
        self.result_queue.put(("autofocus_sample", z_position, score, direction, ppm_bytes))

    def _autofocus_worker(self, metric: str, initial_step: int, min_step: int, search_range: int) -> None:
        assert self.serial_client is not None
        best_score = -1.0
        best_z = self.current_position_values["Z"]
        yellow_threshold = self._focus_yellow_threshold(metric)
        try:
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "autofocus"))
            center_z = self._z_from_position_entries(entries)
            lower_bound = center_z - search_range
            upper_bound = center_z + search_range
            current_z = center_z
            coarse_scores: dict[int, float] = {}
            fine_scores: dict[int, float] = {}

            center_scores = self._sample_after_motion_settles()
            best_score = center_scores.get(metric, 0.0)
            coarse_scores[center_z] = best_score
            self._record_autofocus_sample(metric, center_z, best_score, 0, stage="center", scores=center_scores)

            self.result_queue.put(("autofocus_status", f"Center {center_z}, coarse step {initial_step}, min step {min_step}"))
            coarse_offsets = self._coarse_wobble_offsets(initial_step, search_range)

            for offset in coarse_offsets:
                if self.autofocus_stop_event.is_set():
                    break
                target_z = center_z + offset
                if target_z < lower_bound or target_z > upper_bound or target_z in coarse_scores:
                    continue
                score, current_z = self._autofocus_move_to_z(target_z, current_z, metric, stage="coarse")
                coarse_scores[current_z] = score
                if score > best_score:
                    best_score = score
                    best_z = current_z
                self.result_queue.put(("autofocus_status", f"Coarse Z {current_z}, {metric} {score:.2f}, best {best_score:.2f}"))
                if self._coarse_peak_is_confirmed(coarse_scores, best_z, initial_step):
                    self.result_queue.put(("autofocus_status", f"Coarse peak confirmed near Z={best_z}; stop expanding range."))
                    break

            fine_start, fine_end = self._fine_scan_bounds(
                best_z=best_z,
                initial_step=initial_step,
                min_step=min_step,
                lower_bound=lower_bound,
                upper_bound=upper_bound,
            )
            self.result_queue.put(("autofocus_status", f"Fine scan {fine_start}..{fine_end}, step {min_step}"))
            for target_z in self._fine_scan_positions(fine_start, fine_end, min_step):
                if self.autofocus_stop_event.is_set():
                    break
                score, current_z = self._autofocus_move_to_z(target_z, current_z, metric, stage="fine")
                fine_scores[current_z] = score
                if score > best_score:
                    best_score = score
                    best_z = current_z
                self.result_queue.put(("autofocus_status", f"Fine Z {current_z}, {metric} {score:.2f}, best {best_score:.2f}"))

            fit_scores = fine_scores or coarse_scores
            fine_best_z, fine_best_score = max(fit_scores.items(), key=lambda item: item[1])
            boundary_margin = max(min_step, initial_step)
            best_near_edge = fine_best_z <= center_z - search_range + boundary_margin or fine_best_z >= center_z + search_range - boundary_margin
            fitted_z = self._fit_gaussian_focus_peak(fit_scores)
            result_z = int(round(fitted_z))
            result_z = max(lower_bound, min(upper_bound, result_z))
            result_is_usable = fine_best_score >= yellow_threshold and not best_near_edge

            if result_is_usable:
                if current_z != result_z and not self.autofocus_stop_event.is_set():
                    _, current_z = self._autofocus_move_to_z(result_z, current_z, metric, stage="final")
                state = "GREEN" if fine_best_score >= self._focus_green_threshold(metric) else "YELLOW"
                self.result_queue.put(("autofocus_status", f"Done {state}. Fine-fit Z={result_z}, fine peak Z={fine_best_z}, {metric}={fine_best_score:.2f}"))
            else:
                if current_z != center_z and not self.autofocus_stop_event.is_set():
                    _, current_z = self._autofocus_move_to_z(center_z, current_z, metric, stage="return_center")
                if best_near_edge:
                    self.result_queue.put(("autofocus_status", f"Fine peak near range edge: Z={fine_best_z}. Returned to {center_z}; increase range or recenter."))
                else:
                    self.result_queue.put(("autofocus_status", f"No {metric} >= yellow threshold {yellow_threshold:g}. Returned to {center_z}; adjust optics or threshold."))
        except Exception as exc:
            self.result_queue.put(("autofocus_error", exc))
        finally:
            self._write_autofocus_history_file()
            self.result_queue.put(("autofocus_done",))

    def _write_autofocus_history_file(self) -> None:
        output_path = Path.cwd() / "last_autofocus_history.csv"
        fieldnames = [
            "timestamp",
            "stage",
            "z_position",
            "direction",
            "selected_metric",
            "selected_score",
            "laplacian",
            "tenengrad",
            "brenner",
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

    def _sample_after_motion_settles(self, duration: float = 0.36) -> dict[str, float]:
        settle_seconds = max(0, self.probe_config.autofocus_settle_ms) / 1000.0
        if settle_seconds > 0:
            time.sleep(settle_seconds)
        sample_after = time.monotonic()
        return self._sample_focus_score_frames(
            after_time=sample_after,
            sample_count=self.probe_config.autofocus_sample_count,
            timeout=max(duration, 0.2) + max(1.0, self.probe_config.autofocus_sample_count * 0.2),
        )

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
        import math

        sigma = max(model["sigma"], 1e-9)
        exponent = -((z_value - model["mu"]) ** 2) / (2.0 * sigma * sigma)
        return model["baseline"] + model["amplitude"] * math.exp(exponent)

    @staticmethod
    def _fit_gaussian_focus_model(scores_by_z: dict[int, float]) -> dict[str, float] | None:
        if len(scores_by_z) < 3:
            return None

        import math
        import numpy as np

        sorted_points = sorted(scores_by_z.items())
        best_z, _best_score = max(sorted_points, key=lambda item: item[1])
        nearby = sorted(sorted_points, key=lambda item: (abs(item[0] - best_z), item[0]))[: min(7, len(sorted_points))]
        nearby = sorted(nearby)
        z_values = np.array([z for z, _score in nearby], dtype=np.float64)
        scores = np.array([score for _z, score in nearby], dtype=np.float64)
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
            "mu": mu,
            "sigma": float(sigma),
            "amplitude": float(amplitude),
            "baseline": baseline,
        }

    @staticmethod
    def _fit_gaussian_focus_peak(scores_by_z: dict[int, float]) -> float:
        if not scores_by_z:
            raise ValueError("At least one focus sample is required.")
        if len(scores_by_z) < 3:
            return float(max(scores_by_z, key=scores_by_z.get))
        model = ProbeApp._fit_gaussian_focus_model(scores_by_z)
        if model is None:
            return float(max(scores_by_z, key=scores_by_z.get))
        return model["mu"]

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

    def _autofocus_move_to_z(self, target_z: int, current_z: int, metric: str, stage: str = "sample") -> tuple[float, int]:
        assert self.serial_client is not None
        if self.autofocus_stop_event.is_set():
            return self._current_focus_score(metric), current_z
        delta = target_z - current_z
        command_hex = ""
        reached_hex = ""
        if delta:
            command = self.serial_client.move_relative(axis=Axis.Z, reverse=delta < 0, pulses=abs(delta), speed_percent=100)
            command_hex = hex_bytes(command)
            self.result_queue.put(("motor_command", "Z", "autofocus", command, "autofocus"))
            self.result_queue.put(("moving",))
            reached = self.serial_client.wait_axis_reached(Axis.Z, timeout=max(5.0, abs(delta) / 100.0))
            reached_hex = hex_bytes(reached)
            logger.info("AutoFocus Z reached feedback: %s", colorize_hex_frame(reached_hex, "RX"))
            entries = self.serial_client.read_xyz_positions()
            self.result_queue.put(("read_positions", entries, "autofocus", {"Z": target_z}))
            current_z = self._z_from_position_entries(entries)
        scores = self._sample_after_motion_settles(duration=0.36)
        score = scores.get(metric, 0.0)
        self._record_autofocus_sample(metric, current_z, score, 1 if delta >= 0 else -1, command_hex=command_hex, reached_hex=reached_hex, stage=stage, scores=scores)
        return score, current_z

    def record_imgstitch_point(self, point_index: int) -> None:
        point = (self.current_position_values["X"], self.current_position_values["Y"])
        if point_index == 1:
            self.imgstitch_point1 = point
        else:
            self.imgstitch_point2 = point
        self._update_imgstitch_point_status()

    def _update_imgstitch_mode_fields(self) -> None:
        widgets_by_mode = getattr(self, "imgstitch_mode_widgets", {})
        active_mode = self.imgstitch_range_mode_var.get()
        for mode, widgets in widgets_by_mode.items():
            for widget in widgets:
                if mode == active_mode or (mode == "Manual Step" and active_mode != "Array"):
                    widget.grid()
                else:
                    widget.grid_remove()

    def _update_imgstitch_point_status(self) -> None:
        parts = []
        if self.imgstitch_point1 is not None:
            parts.append(f"P1 X={self.imgstitch_point1[0]} Y={self.imgstitch_point1[1]}")
        if self.imgstitch_point2 is not None:
            parts.append(f"P2 X={self.imgstitch_point2[0]} Y={self.imgstitch_point2[1]}")
        self.imgstitch_point_status_var.set(" | ".join(parts) if parts else "No rectangle points")

    def _imgstitch_settings_from_ui(self) -> StitchSettings:
        return StitchSettings(
            overlap_x=int(float(self.imgstitch_overlap_x_var.get())),
            overlap_y=int(float(self.imgstitch_overlap_y_var.get())),
            max_correction_um=float(self.imgstitch_max_correction_um_var.get()),
            registration_weight=float(self.imgstitch_registration_weight_var.get()),
            show_seams=self.imgstitch_show_seams_var.get(),
            seam_response_yellow=self.probe_config.imgstitch_seam_response_yellow,
            seam_response_green=self.probe_config.imgstitch_seam_response_green,
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
        warning_count = sum(1 for edge in edges if edge.quality != "good")
        return f"Seams {len(edges)} | response {avg_response:.3f} | max {max_correction:.2f} um | warn {warning_count}"

    def recompose_imgstitch_session(self) -> None:
        if self.imgstitch_session is None:
            self.imgstitch_status_var.set("No captured session to recompose.")
            return
        try:
            settings = self._imgstitch_settings_from_ui()
            mosaic, positions, edges = recompose_session(self.imgstitch_session, settings, self.imgstitch_tile_images)
            if settings.show_seams:
                mosaic = build_seam_quality_overlay(mosaic, positions, edges, (self.imgstitch_session.tile_width, self.imgstitch_session.tile_height))
            import cv2

            cv2.imwrite(str(self.imgstitch_session_dir / "recomposed_imgstitch.png"), mosaic)
            self.imgstitch_latest_positions = positions
            self.imgstitch_latest_edges = edges
            self._show_imgstitch_preview(mosaic)
            self.imgstitch_quality_var.set(self._imgstitch_quality_summary(edges))
            self.imgstitch_status_var.set("Recomposed from captured tiles.")
        except Exception as exc:
            self.imgstitch_status_var.set(f"Recompose failed: {exc}")
            self.status_var.set(f"ImgStitch recompose failed: {exc}")

    def start_imgstitch(self) -> None:
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

        self.imgstitch_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.imgstitch_restore_home_signal = self.home_signal_enabled
        if self.home_signal_enabled:
            self.disable_home_signal_polling()
        self.autofocus_stop_event.clear()
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
            args=(rows, cols, settings, step_x, step_y, self.imgstitch_plane_af_var.get(), step_x_um, step_y_um, um_per_px, range_mode, scan_origin_override),
            daemon=True,
        )
        self.imgstitch_thread.start()

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
        step_x_um: float,
        step_y_um: float,
        um_per_px: float,
        range_mode: str,
        scan_origin_override: tuple[int, int] | None,
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
            if use_plane_af:
                self.result_queue.put(("imgstitch_status", "Running four-corner AF"))
                plane = self._fit_imgstitch_plane(origin_x, origin_y, origin_z, rows, cols, step_x, step_y, range_mode)
                self._move_absolute_stage(origin_x, origin_y, origin_z)

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
                target_z = round(plane.z_at(target_x, target_y)) if plane else origin_z
                moved_entries = self._move_absolute_stage(target_x, target_y, target_z)
                actual_x = self._axis_from_position_entries(moved_entries, Axis.X)
                actual_y = self._axis_from_position_entries(moved_entries, Axis.Y)
                actual_z = self._axis_from_position_entries(moved_entries, Axis.Z)
                self._wait_after_imgstitch_motion()
                image = self._capture_stitch_frame()
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
        metric = self.focus_metric_var.get()
        initial_step = max(1, int(self.autofocus_step_var.get()))
        min_step = max(1, int(self.autofocus_min_step_var.get()))
        search_range = max(initial_step, int(self.autofocus_max_moves_var.get()))
        entries = self.serial_client.read_xyz_positions()
        center_z = self._z_from_position_entries(entries)
        current_z = center_z
        best_z = center_z
        best_score = self._sample_focus_score(metric)
        sampled_positions = {center_z}
        step = initial_step
        for offset in range(step, search_range + 1, step):
            for signed_offset in (offset, -offset):
                if self.imgstitch_stop_event.is_set():
                    return best_z
                target_z = center_z + signed_offset
                if target_z in sampled_positions:
                    continue
                score, current_z = self._autofocus_move_to_z(target_z, current_z, metric, stage="plane_af")
                sampled_positions.add(current_z)
                if score > best_score:
                    best_score = score
                    best_z = current_z
        refine_step = max(initial_step // 2, min_step)
        while refine_step >= min_step and not self.imgstitch_stop_event.is_set():
            for target_z in (best_z + refine_step, best_z - refine_step):
                if target_z in sampled_positions:
                    continue
                score, current_z = self._autofocus_move_to_z(target_z, current_z, metric, stage="plane_af")
                sampled_positions.add(current_z)
                if score > best_score:
                    best_score = score
                    best_z = current_z
            refine_step //= 2
        if current_z != best_z:
            _, current_z = self._autofocus_move_to_z(best_z, current_z, metric, stage="plane_af_final")
        return best_z

    def _move_absolute_stage(self, x_value: int, y_value: int, z_value: int):
        assert self.serial_client is not None
        entries = self.serial_client.read_stable_xyz_positions()
        current_x = self._axis_from_position_entries(entries, Axis.X)
        current_y = self._axis_from_position_entries(entries, Axis.Y)
        current_z = self._axis_from_position_entries(entries, Axis.Z)
        for axis, target, current in ((Axis.X, x_value, current_x), (Axis.Y, y_value, current_y), (Axis.Z, z_value, current_z)):
            delta = target - current
            if not delta:
                continue
            self.serial_client.move_relative(axis=axis, reverse=delta < 0, pulses=abs(delta), speed_percent=100)
            self.serial_client.wait_axis_reached(axis, timeout=max(5.0, abs(delta) / 100.0))
        entries = self.serial_client.read_stable_xyz_positions()
        self.result_queue.put(("read_positions", entries, "imgstitch"))
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

        self.motion_busy = True
        targets = {axis: self.current_position_values[axis] + steps[axis] for axis in ("X", "Y", "Z") if steps[axis]}
        self.status_var.set(f"Running CC multi-axis relative move: X={steps['X']} Y={steps['Y']} Z={steps['Z']}.")
        self._show_target_positions(targets)
        threading.Thread(target=self._move_xyz_cc_worker, args=(steps, targets), daemon=True).start()

    def emergency_stop(self) -> None:
        self.realtime_stop_event.set()
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
            if mode == "Absolute":
                command = self.serial_client.move_absolute(axis=controller_axis, target_position=pulses, speed_percent=100)
                action = "absolute"
            else:
                command = self.serial_client.move_relative(axis=controller_axis, reverse=reverse, pulses=pulses, speed_percent=100)
                action = "reverse" if reverse else "forward"
            self.result_queue.put(("motor_command", axis, action, command, source))
            reached = self.serial_client.wait_axis_reached(controller_axis, timeout=max(5.0, pulses / 100.0))
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
            if len(axes) == 1:
                axis_name = axes[0]
                controller_axis = self._controller_axis(axis_name)
                if controller_axis is None:
                    return
                value = values[axis_name]
                if modes[axis_name] == "Absolute":
                    command = self.serial_client.move_absolute(axis=controller_axis, target_position=value, speed_percent=100)
                    action = "absolute"
                else:
                    if value == 0:
                        raise ValueError("Relative move value must be non-zero.")
                    command = self.serial_client.move_relative(axis=controller_axis, reverse=value < 0, pulses=abs(value), speed_percent=100)
                    action = "relative"
                self.result_queue.put(("motor_command", axis_name, action, command, "button"))
                wait_pulses = abs(value) if modes[axis_name] == "Relative" else abs(value - self.current_position_values[axis_name])
                reached = self.serial_client.wait_axis_reached(controller_axis, timeout=max(5.0, wait_pulses / 100.0))
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
    def _cc_move_timeout(axis_params: dict[Axis, tuple[bool, int, int, int]]) -> float:
        max_pulses = max((pulses for _reverse, pulses, _speed, _acceleration in axis_params.values()), default=0)
        return max(5.0, max_pulses / 100.0)

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
                    threading.Thread(target=self._read_current_position_worker, args=("comm_test", False), daemon=True).start()
                else:
                    logger.warning("Communication test did not pass. %s %s Detail=%s", colorize_hex_frame(result.request_hex, "TX"), colorize_hex_frame(result.response_hex or "-", "RX"), result.message)
        except queue.Empty:
            pass
        next_interval = 1 if not self.result_queue.empty() else RESULT_POLL_INTERVAL_MS
        self.after(next_interval, self._poll_result_queue)

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
            elif source == "imgstitch":
                self.imgstitch_status_var.set("Stage moved")
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

        if event_type == "moving":
            self.status_var.set("Moving")
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

        if event_type == "autofocus_status":
            _, message = event
            self.autofocus_status_var.set(str(message))
            self.status_var.set(str(message))
            logger.info("AutoFocus: %s", message)
            return

        if event_type == "autofocus_sample":
            _, z_position, score, _direction, ppm_bytes = event
            if ppm_bytes and hasattr(self, "autofocus_video_label"):
                self.autofocus_camera_image = tk.PhotoImage(data=ppm_bytes, format="PPM")
                self.autofocus_video_label.configure(image=self.autofocus_camera_image, text="")
            self.autofocus_status_var.set(f"Sample Z={z_position}, score={score:.2f}")
            self._draw_autofocus_z_score()
            return

        if event_type == "autofocus_error":
            _, exc = event
            self.autofocus_status_var.set(f"Failed: {exc}")
            self.status_var.set(f"AutoFocus failed: {exc}")
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
            logger.info("AutoFocus stopped.")
            return

        if event_type == "imgstitch_status":
            _, message = event
            self.imgstitch_status_var.set(str(message))
            self.status_var.set(str(message))
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
            self._show_imgstitch_preview(mosaic)
            return

        if event_type == "imgstitch_done":
            _, output_path = event
            self.imgstitch_status_var.set(f"Saved {output_path.name}")
            self.status_var.set(f"ImgStitch saved: {output_path}")
            logger.info("ImgStitch saved to %s.", output_path)
            return

        if event_type == "imgstitch_error":
            _, exc = event
            self.imgstitch_status_var.set(f"Failed: {exc}")
            self.status_var.set(f"ImgStitch failed: {exc}")
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
        self.camera = UsbCamera(index=index, width=800, height=450)
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
        return self.current_page == "AutoFocus" or self.autofocus_running or self.imgstitch_focus_sampling_required

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
            self.camera_image = tk.PhotoImage(data=self._ppm_with_scalebar(frame.image_bgr), format="PPM")
            if self.vision_panel:
                self.vision_panel.set_image(self.camera_image)
            with self.focus_lock:
                self.latest_focus_frame_ppm = frame.ppm_bytes
            with self.camera_lock:
                self.latest_stitch_frame = frame.image_bgr
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
                self._update_focus_scores(frame.focus_scores)

        self.after(15, self._update_camera_frame)

    def destroy(self) -> None:
        self.autofocus_stop_event.set()
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
