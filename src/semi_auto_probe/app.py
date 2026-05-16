from __future__ import annotations

import queue
import re
import threading
import time
import tkinter as tk
from pathlib import Path
from tkinter import ttk

from .camera import UsbCamera
from .img_stitch import compose_mosaic, estimate_overlap_shift, fit_plane, flat_field_correct, serpentine_indices
from .logging_utils import colorize_hex_frame, configure_logging, print_startup_banner
from .protocol import COMM_TEST_COMMAND, FUNCTION_READ_POSITION, RESPONSE_HEAD, Axis, AxisPosition, hex_bytes, parse_axis_position_response
from .serial_client import ControllerSerialClient, CommunicationTestResult, list_serial_ports


logger = configure_logging()


class ProbeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Semi Auto Probe")
        self.geometry("1303x818")
        self.minsize(1040, 600)
        self.configure(bg="#0b0f14")

        self.serial_client: ControllerSerialClient | None = None
        self.camera: UsbCamera | None = None
        self.camera_running = False
        self.camera_rendering = False
        self.camera_image: tk.PhotoImage | None = None
        self.latest_camera_frame = None
        self.camera_lock = threading.Lock()
        self.focus_lock = threading.Lock()
        self.camera_thread: threading.Thread | None = None
        self.camera_session_id = 0
        self.result_queue: queue.Queue[object] = queue.Queue()
        self.realtime_stop_event = threading.Event()
        self.realtime_thread: threading.Thread | None = None
        self.autofocus_stop_event = threading.Event()
        self.autofocus_thread: threading.Thread | None = None
        self.autofocus_running = False
        self.autofocus_restore_realtime = False
        self.imgstitch_stop_event = threading.Event()
        self.imgstitch_thread: threading.Thread | None = None
        self.imgstitch_running = False
        self.imgstitch_restore_realtime = False
        self.latest_stitch_frame = None

        self.port_var = tk.StringVar()
        self.camera_index_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="Ready")
        self.rx_var = tk.StringVar(value="-")
        self.tx_var = tk.StringVar(value="-")
        self.comm_input_mode_var = tk.StringVar(value="Hex")
        self.comm_read_length_var = tk.StringVar(value="12")
        self.comm_note_var = tk.StringVar(value="Default: communication test. Expected RX starts with A3 AA.")
        self.focus_metric_var = tk.StringVar(value="Laplacian")
        self.focus_score_var = tk.StringVar(value="-")
        self.autofocus_step_var = tk.StringVar(value="50")
        self.autofocus_min_step_var = tk.StringVar(value="2")
        self.autofocus_max_moves_var = tk.StringVar(value="500")
        self.focus_threshold_vars = {
            "Laplacian": tk.StringVar(value="1000"),
            "Tenengrad": tk.StringVar(value="20000"),
            "Brenner": tk.StringVar(value="1000"),
        }
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
        self.imgstitch_plane_af_var = tk.BooleanVar(value=False)
        self.imgstitch_status_var = tk.StringVar(value="Idle")

        self._configure_theme()
        self._build_ui()
        self._bind_keyboard_controls()
        self.bind("<Configure>", self._on_window_configure)
        self.refresh_ports()
        self.start_camera()
        self.after(100, self._poll_result_queue)

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
        ttk.Button(toolbar, text="Connect", style="Accent.TButton", command=self.connect_serial).grid(row=0, column=3, padx=(0, 6))
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
        for col, name in enumerate(("Main", "Communication", "AutoFocus", "ImgStitch")):
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
        self.pages = {"Main": main_page, "Communication": communication_page, "AutoFocus": autofocus_page, "ImgStitch": imgstitch_page}
        for page in self.pages.values():
            page.grid(row=0, column=0, sticky="nsew")

        self._build_main_page(main_page)
        self._build_communication_page(communication_page)
        self._build_autofocus_page(autofocus_page)
        self._build_imgstitch_page(imgstitch_page)
        self.show_page("Main")

    def show_page(self, name: str) -> None:
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

        self.video_label = ttk.Label(camera_panel, anchor="center", text="Camera preview", style="Video.TLabel")
        self.video_label.grid(row=1, column=0, sticky="nsew")

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

    def _axis_control_row(self, parent: ttk.Frame, row_index: int, axis: str, label: str, color: str) -> None:
        row = ttk.Frame(parent, style="Panel.TFrame", padding=(8, 6))
        row.grid(row=row_index, column=0, sticky="ew", padx=8, pady=(8 if row_index == 0 else 3, 4))
        row.columnconfigure(1, weight=1)

        marker = tk.Canvas(row, width=10, height=10, bg=self.colors["surface"], highlightthickness=0)
        marker.create_oval(1, 1, 9, 9, fill=color, outline=color)
        marker.grid(row=0, column=0, rowspan=2, padx=(0, 8))

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

        thresholds = ttk.Frame(control_panel, style="Panel.TFrame")
        thresholds.grid(row=10, column=0, sticky="ew", pady=(14, 0))
        thresholds.columnconfigure(1, weight=1)
        ttk.Label(thresholds, text="THRESHOLD", style="Section.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 6))
        for row_index, metric_name in enumerate(("Laplacian", "Tenengrad", "Brenner"), start=1):
            ttk.Label(thresholds, text=metric_name, style="Muted.TLabel").grid(row=row_index, column=0, sticky="w", pady=2)
            ttk.Spinbox(thresholds, from_=0, to=1_000_000_000, increment=10, textvariable=self.focus_threshold_vars[metric_name], width=9).grid(row=row_index, column=1, sticky="ew", padx=(8, 0), pady=2)

        manual = ttk.Frame(control_panel, style="Panel.TFrame")
        manual.grid(row=11, column=0, sticky="ew", pady=(16, 0))
        manual.columnconfigure((0, 1), weight=1, uniform="af_manual")
        ttk.Button(manual, text="Z-", command=lambda: self.autofocus_manual_z(reverse=True)).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(manual, text="Z+", style="Accent.TButton", command=lambda: self.autofocus_manual_z(reverse=False)).grid(row=0, column=1, sticky="ew", padx=(4, 0))

        ttk.Button(control_panel, text="Start Auto", style="Accent.TButton", command=self.start_autofocus).grid(row=12, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(control_panel, text="Set Z=0", command=self.set_autofocus_z_zero).grid(row=13, column=0, sticky="ew", pady=(8, 0))
        ttk.Button(control_panel, text="Stop", style="Danger.TButton", command=self.stop_autofocus).grid(row=14, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(control_panel, textvariable=self.autofocus_status_var, style="Status.TLabel", wraplength=190, padding=10).grid(row=15, column=0, sticky="ew", pady=(16, 0))

    def _build_imgstitch_page(self, parent: ttk.Frame) -> None:
        parent.columnconfigure(0, weight=1)
        parent.columnconfigure(1, weight=0)
        parent.rowconfigure(0, weight=1)

        preview_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        preview_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 12))
        preview_panel.columnconfigure(0, weight=1)
        preview_panel.rowconfigure(1, weight=1)
        preview_panel.rowconfigure(3, weight=1)

        ttk.Label(preview_panel, text="IMG STITCH", style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 10))
        self.imgstitch_live_label = ttk.Label(preview_panel, anchor="center", text="Camera preview", style="Video.TLabel")
        self.imgstitch_live_label.grid(row=1, column=0, sticky="nsew")
        ttk.Label(preview_panel, text="MOSAIC PREVIEW", style="Section.TLabel").grid(row=2, column=0, sticky="w", pady=(12, 10))
        self.imgstitch_mosaic_label = ttk.Label(preview_panel, anchor="center", text="No mosaic yet", style="Video.TLabel")
        self.imgstitch_mosaic_label.grid(row=3, column=0, sticky="nsew")

        control_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        control_panel.grid(row=0, column=1, sticky="ns")
        control_panel.columnconfigure(0, weight=1)
        ttk.Label(control_panel, text="GRID", style="Section.TLabel").grid(row=0, column=0, sticky="w")

        fields = (
            ("Rows", self.imgstitch_rows_var),
            ("Cols", self.imgstitch_cols_var),
            ("Overlap X (px)", self.imgstitch_overlap_x_var),
            ("Overlap Y (px)", self.imgstitch_overlap_y_var),
            ("Step X (pulse)", self.imgstitch_step_x_var),
            ("Step Y (pulse)", self.imgstitch_step_y_var),
        )
        row = 1
        for label, variable in fields:
            ttk.Label(control_panel, text=label, style="Muted.TLabel").grid(row=row, column=0, sticky="w", pady=(12, 4))
            ttk.Spinbox(control_panel, from_=1, to=1_000_000, increment=1, textvariable=variable, width=14).grid(row=row + 1, column=0, sticky="ew")
            row += 2

        ttk.Checkbutton(control_panel, text="Four-corner plane AF", variable=self.imgstitch_plane_af_var).grid(row=row, column=0, sticky="w", pady=(16, 0))
        ttk.Button(control_panel, text="Start Stitch", style="Accent.TButton", command=self.start_imgstitch).grid(row=row + 1, column=0, sticky="ew", pady=(16, 0))
        ttk.Button(control_panel, text="Stop", style="Danger.TButton", command=self.stop_imgstitch).grid(row=row + 2, column=0, sticky="ew", pady=(8, 0))
        ttk.Label(control_panel, textvariable=self.imgstitch_status_var, style="Status.TLabel", wraplength=190, padding=10).grid(row=row + 3, column=0, sticky="ew", pady=(16, 0))

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
            self.after_cancel(self.resize_log_job)
        self.resize_log_job = self.after(250, self._log_window_layout)

    def _log_window_layout(self) -> None:
        self.resize_log_job = None
        window_size = (self.winfo_width(), self.winfo_height())
        control_width = getattr(self, "controls_panel", None).winfo_width() if hasattr(self, "controls_panel") else None

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
        if isinstance(event.widget, tk.Entry):
            return None
        state = self.held_keys.pop(event.keysym, None)
        if state is None:
            return None

        job = state.get("job")
        if job is not None:
            self.after_cancel(str(job))
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
        try:
            return float(self.focus_threshold_vars[metric].get())
        except (KeyError, ValueError):
            return 0.0

    def _draw_focus_history(self) -> None:
        if not hasattr(self, "focus_canvas"):
            return
        metric = self.focus_metric_var.get()
        now = time.monotonic()
        window_seconds = self._focus_window_seconds()
        threshold = self._focus_threshold(metric)
        start_time = now - window_seconds
        end_time = now
        canvas = self.focus_canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill=self.colors["surface_2"], outline="")
        canvas.create_text(12, 12, text=f"{metric} | last {window_seconds} s | threshold {threshold:g}", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))

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
            color = self.colors["accent"] if value >= threshold else self.colors["danger"]
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
        min_score, max_score = 0.0, max(score_values)
        z_span = max(max_z - min_z, 1)
        score_span = max(max_score - min_score, 1.0)
        left, top, right, bottom = 50, 28, width - 18, height - 34
        canvas.create_rectangle(left, top, right, bottom, outline=self.colors["border"])
        points: list[float] = []
        for z_value, score, direction in samples:
            x = left + (z_value - min_z) / z_span * (right - left)
            y = bottom - (score - min_score) / score_span * (bottom - top)
            points.extend((x, y))
        if len(points) >= 4:
            canvas.create_line(*points, fill="#94a3b8", width=1, dash=(3, 3))
        for z_value, score, direction in samples:
            x = left + (z_value - min_z) / z_span * (right - left)
            y = bottom - (score - min_score) / score_span * (bottom - top)
            color = "#60a5fa" if direction >= 0 else "#fbbf24"
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
        threading.Thread(target=self._move_axis_worker, args=("Z", Axis.Z, reverse, pulses, "autofocus manual", "Relative"), daemon=True).start()

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
            threshold = float(self.focus_threshold_vars[metric].get())
        except ValueError:
            self.autofocus_status_var.set("AutoFocus settings must be integers.")
            return
        if initial_step <= 0 or min_step <= 0 or search_range <= 0 or threshold < 0:
            self.autofocus_status_var.set("AutoFocus settings must be positive.")
            return

        self.autofocus_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()

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
        self.autofocus_thread = threading.Thread(target=self._autofocus_worker, args=(metric, initial_step, min_step, search_range, threshold), daemon=True)
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

    def _sample_focus_scores(self, after_time: float | None = None, settle_delay: float = 0.1, duration: float = 0.36) -> dict[str, float]:
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

    def _autofocus_worker(self, metric: str, initial_step: int, min_step: int, search_range: int, threshold: float) -> None:
        assert self.serial_client is not None
        best_score = -1.0
        best_z = self.current_position_values["Z"]
        reached_threshold = False
        try:
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "autofocus"))
            initial_sample_after = time.monotonic()
            center_z = self._z_from_position_entries(entries)
            lower_bound = center_z - search_range
            upper_bound = center_z + search_range
            current_z = center_z
            step = initial_step
            sampled_positions: set[int] = set()
            coarse_scores: dict[int, float] = {}
            center_scores = self._sample_focus_scores(after_time=initial_sample_after, settle_delay=0.1, duration=0.36)
            best_score = center_scores.get(metric, 0.0)
            best_z = center_z
            sampled_positions.add(center_z)
            coarse_scores[center_z] = best_score
            self._record_autofocus_sample(metric, center_z, best_score, 0, stage="center", scores=center_scores)
            if best_score >= threshold:
                reached_threshold = True

            self.result_queue.put(("autofocus_status", f"Center {center_z}, range +/-{search_range}, threshold {threshold:g}"))
            coarse_offsets: list[int] = []
            coarse_start = max(min_step, initial_step // 2)
            for distance in range(coarse_start, search_range + 1, initial_step):
                coarse_offsets.extend((distance, -distance))
            if search_range not in {abs(offset) for offset in coarse_offsets}:
                coarse_offsets.extend((search_range, -search_range))

            for offset in coarse_offsets:
                if self.autofocus_stop_event.is_set():
                    break
                target_z = center_z + offset
                if target_z < lower_bound or target_z > upper_bound or target_z in sampled_positions:
                    continue
                score, current_z = self._autofocus_move_to_z(target_z, current_z, metric)
                sampled_positions.add(current_z)
                if score > best_score:
                    best_score = score
                    best_z = current_z
                coarse_scores[current_z] = score
                if score >= threshold:
                    reached_threshold = True
                self.result_queue.put(("autofocus_status", f"Coarse Z {current_z}, {metric} {score:.2f}, best {best_score:.2f}"))
                if self._coarse_peak_is_confirmed(coarse_scores, best_z, initial_step):
                    self.result_queue.put(("autofocus_status", f"Coarse peak confirmed near Z={best_z}; stop expanding range."))
                    break

            refine_step = max(initial_step // 2, min_step)
            while refine_step >= min_step and not self.autofocus_stop_event.is_set():
                self.result_queue.put(("autofocus_status", f"Refine around Z={best_z}, step {refine_step}"))
                for offset in self._wobble_offsets(refine_step, initial_step):
                    target_z = best_z + offset
                    if target_z < lower_bound or target_z > upper_bound:
                        continue
                    if target_z in sampled_positions or self.autofocus_stop_event.is_set():
                        continue
                    score, current_z = self._autofocus_move_to_z(target_z, current_z, metric)
                    sampled_positions.add(current_z)
                    if score > best_score:
                        best_score = score
                        best_z = current_z
                    if score >= threshold:
                        reached_threshold = True
                    self.result_queue.put(("autofocus_status", f"Refine Z {current_z}, {metric} {score:.2f}, best {best_score:.2f}"))
                refine_step //= 2

            boundary_margin = max(min_step, initial_step)
            best_near_edge = best_z <= center_z - search_range + boundary_margin or best_z >= center_z + search_range - boundary_margin
            result_is_usable = reached_threshold and not best_near_edge

            if result_is_usable:
                if best_score >= 0 and current_z != best_z and not self.autofocus_stop_event.is_set():
                    _, current_z = self._autofocus_move_to_z(best_z, current_z, metric, stage="final")
                self.result_queue.put(("autofocus_status", f"Done. Best Z={best_z}, {metric}={best_score:.2f}"))
            else:
                if current_z != center_z and not self.autofocus_stop_event.is_set():
                    _, current_z = self._autofocus_move_to_z(center_z, current_z, metric, stage="return_center")
                if best_near_edge:
                    self.result_queue.put(("autofocus_status", f"Best near range edge: Z={best_z}. Returned to {center_z}; increase range or recenter."))
                else:
                    self.result_queue.put(("autofocus_status", f"No {metric} >= {threshold:g} in +/-{search_range}. Returned to {center_z}; increase range."))
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
            time.sleep(0.1)
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "autofocus"))
            current_z = self._z_from_position_entries(entries)
        sample_after = time.monotonic()
        scores = self._sample_focus_scores(after_time=sample_after, settle_delay=0.1, duration=0.36)
        score = scores.get(metric, 0.0)
        self._record_autofocus_sample(metric, current_z, score, 1 if delta >= 0 else -1, command_hex=command_hex, reached_hex=reached_hex, stage=stage, scores=scores)
        return score, current_z

    def start_imgstitch(self) -> None:
        if self.imgstitch_running or self.motion_busy:
            return
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return
        try:
            rows = int(self.imgstitch_rows_var.get())
            cols = int(self.imgstitch_cols_var.get())
            overlap_x = int(self.imgstitch_overlap_x_var.get())
            overlap_y = int(self.imgstitch_overlap_y_var.get())
            step_x = int(self.imgstitch_step_x_var.get())
            step_y = int(self.imgstitch_step_y_var.get())
        except ValueError:
            self.imgstitch_status_var.set("Stitch settings must be integers.")
            return
        if min(rows, cols, overlap_x, overlap_y, step_x, step_y) <= 0:
            self.imgstitch_status_var.set("Stitch settings must be positive.")
            return
        if self.imgstitch_plane_af_var.get() and (rows < 2 or cols < 2):
            self.imgstitch_status_var.set("Plane AF requires at least a 2x2 grid.")
            return

        self.imgstitch_restore_realtime = self.realtime_enabled
        if self.realtime_enabled:
            self.disable_realtime_position()
        self.autofocus_stop_event.clear()
        self.imgstitch_running = True
        self.motion_busy = True
        self.imgstitch_stop_event.clear()
        self.imgstitch_status_var.set("Running")
        self.status_var.set("ImgStitch running.")
        self.imgstitch_thread = threading.Thread(
            target=self._imgstitch_worker,
            args=(rows, cols, overlap_x, overlap_y, step_x, step_y, self.imgstitch_plane_af_var.get()),
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
        overlap_x: int,
        overlap_y: int,
        step_x: int,
        step_y: int,
        use_plane_af: bool,
    ) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_stable_xyz_positions()
            origin_x = self._axis_from_position_entries(entries, Axis.X)
            origin_y = self._axis_from_position_entries(entries, Axis.Y)
            origin_z = self._axis_from_position_entries(entries, Axis.Z)
            plane = None
            if use_plane_af:
                self.result_queue.put(("imgstitch_status", "Running four-corner AF"))
                plane = self._fit_imgstitch_plane(origin_x, origin_y, origin_z, rows, cols, step_x, step_y)
                self._move_absolute_stage(origin_x, origin_y, origin_z)

            tiles: dict[tuple[int, int], object] = {}
            positions: dict[tuple[int, int], tuple[float, float]] = {}
            previous_key: tuple[int, int] | None = None
            for index, key in enumerate(serpentine_indices(rows, cols), start=1):
                if self.imgstitch_stop_event.is_set():
                    break
                row, col = key
                target_x = origin_x + col * step_x
                target_y = origin_y + row * step_y
                target_z = round(plane.z_at(target_x, target_y)) if plane else origin_z
                self._move_absolute_stage(target_x, target_y, target_z)
                image = self._capture_stitch_frame()
                corrected = flat_field_correct(image)
                tiles[key] = corrected
                if previous_key is None:
                    positions[key] = (0.0, 0.0)
                else:
                    previous_row, previous_col = previous_key
                    if row == previous_row and col > previous_col:
                        direction = "right"
                    elif row == previous_row and col < previous_col:
                        direction = "left"
                    else:
                        direction = "down"
                    dx, dy, response = estimate_overlap_shift(tiles[previous_key], corrected, direction, overlap_x, overlap_y)
                    previous_x, previous_y = positions[previous_key]
                    positions[key] = (previous_x + dx, previous_y + dy)
                    self.result_queue.put(("imgstitch_status", f"Tile {index}/{rows * cols}, phase response {response:.3f}"))
                previous_key = key
                mosaic = compose_mosaic(tiles, positions)
                self.result_queue.put(("imgstitch_preview", mosaic))
            if tiles and not self.imgstitch_stop_event.is_set():
                mosaic = compose_mosaic(tiles, positions)
                output_path = Path.cwd() / "last_imgstitch.png"
                import cv2

                cv2.imwrite(str(output_path), mosaic)
                self.result_queue.put(("imgstitch_done", output_path))
            elif self.imgstitch_stop_event.is_set():
                self.result_queue.put(("imgstitch_status", "Stopped"))
        except Exception as exc:
            self.result_queue.put(("imgstitch_error", exc))
        finally:
            self.result_queue.put(("imgstitch_finished",))

    def _fit_imgstitch_plane(self, origin_x: int, origin_y: int, origin_z: int, rows: int, cols: int, step_x: int, step_y: int):
        corners = (
            (origin_x, origin_y),
            (origin_x + (cols - 1) * step_x, origin_y),
            (origin_x, origin_y + (rows - 1) * step_y),
            (origin_x + (cols - 1) * step_x, origin_y + (rows - 1) * step_y),
        )
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
        entries = self.serial_client.read_stable_xyz_positions()
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

    def _move_absolute_stage(self, x_value: int, y_value: int, z_value: int) -> None:
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
        import cv2

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        max_width = 700
        max_height = 280
        scale = min(max_width / rgb.shape[1], max_height / rgb.shape[0], 1.0)
        if scale < 1.0:
            rgb = cv2.resize(rgb, (int(rgb.shape[1] * scale), int(rgb.shape[0] * scale)), interpolation=cv2.INTER_AREA)
        height, width = rgb.shape[:2]
        header = f"P6 {width} {height} 255\n".encode("ascii")
        self.imgstitch_preview_image = tk.PhotoImage(data=header + rgb.tobytes(), format="PPM")
        self.imgstitch_mosaic_label.configure(image=self.imgstitch_preview_image, text="")

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

    def _realtime_position_worker(self) -> None:
        assert self.serial_client is not None
        while not self.realtime_stop_event.is_set():
            try:
                response = self.serial_client.read_frame()
                if not response:
                    continue
                if len(response) != 12:
                    self.result_queue.put(("realtime_raw", response, f"incomplete frame: {len(response)} byte(s)"))
                    continue
                if response[0] != RESPONSE_HEAD or response[1] != FUNCTION_READ_POSITION:
                    self.result_queue.put(("realtime_raw", response, "non-position frame ignored"))
                    continue
                position = parse_axis_position_response(response)
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
        threading.Thread(target=self._read_current_position_worker, daemon=True).start()

    def _read_current_position_worker(self) -> None:
        assert self.serial_client is not None
        try:
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button"))
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
        threading.Thread(target=self._move_edited_positions_worker, args=(axes, modes, values), daemon=True).start()

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
        self.status_var.set(f"Running CC multi-axis relative move: X={steps['X']} Y={steps['Y']} Z={steps['Z']}.")
        self._show_target_positions({axis: self.current_position_values[axis] + steps[axis] for axis in ("X", "Y", "Z")})
        threading.Thread(target=self._move_xyz_cc_worker, args=(steps,), daemon=True).start()

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
        if source == "keyboard" and self.keyboard_motion_busy:
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
        if source == "keyboard":
            self.keyboard_motion_busy = True
        else:
            self.motion_busy = True
        self.status_var.set(f"Moving {axis} {action_text}.")
        threading.Thread(target=self._move_axis_worker, args=(axis, controller_axis, reverse, pulses, source, normalized_mode), daemon=True).start()

    def _move_axis_worker(self, axis: str, controller_axis: Axis, reverse: bool, pulses: int, source: str, mode: str) -> None:
        assert self.serial_client is not None
        try:
            if mode == "Absolute":
                command = self.serial_client.move_absolute(axis=controller_axis, target_position=pulses, speed_percent=100)
                action = "absolute"
            else:
                command = self.serial_client.move_relative(axis=controller_axis, reverse=reverse, pulses=pulses, speed_percent=100)
                action = "reverse" if reverse else "forward"
            self.result_queue.put(("motor_command", axis, action, command, source))
            if source == "keyboard":
                self.result_queue.put(("schedule_position_read", source))
            else:
                self.result_queue.put(("moving",))
                entries = self.serial_client.read_stable_xyz_positions()
                self.result_queue.put(("read_positions", entries, source))
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
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button"))
        except Exception as exc:
            self.result_queue.put(("motor_error", axis, exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _move_edited_positions_worker(self, axes: tuple[str, ...], modes: dict[str, str], values: dict[str, int]) -> None:
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
                    axis_params[controller_axis] = (delta < 0, abs(delta), 100 if delta else 0, 0)
                if not any(params[1] for params in axis_params.values()):
                    raise ValueError("CC move requires at least one non-zero relative delta.")
                command = self.serial_client.move_multi_axis_relative(axis_params)
                self.result_queue.put(("motor_command", "XYZ", "cc relative", command, "button"))

            self.result_queue.put(("moving",))
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button"))
        except Exception as exc:
            self.result_queue.put(("motor_error", "MOVE", exc))
        finally:
            self.result_queue.put(("motor_done",))

    def _move_xyz_cc_worker(self, steps: dict[str, int]) -> None:
        assert self.serial_client is not None
        try:
            command = self.serial_client.move_multi_axis_relative(
                {
                    Axis.X: (False, steps["X"], 100, 0),
                    Axis.Y: (False, steps["Y"], 100, 0),
                    Axis.Z: (False, steps["Z"], 100, 0),
                }
            )
            self.result_queue.put(("motor_command", "XYZ", "cc relative", command, "button"))
            self.result_queue.put(("moving",))
            entries = self.serial_client.read_stable_xyz_positions()
            self.result_queue.put(("read_positions", entries, "button"))
        except Exception as exc:
            self.result_queue.put(("motor_error", "XYZ", exc))
        finally:
            self.result_queue.put(("motor_done",))

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

    def connect_serial(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            self.status_var.set("Select a serial port first.")
            logger.warning("Serial connection skipped because no port is selected.")
            return

        try:
            self.serial_client = ControllerSerialClient(port)
            self.serial_client.open()
        except Exception as exc:
            self.status_var.set(f"Serial connection failed: {exc}")
            logger.error("Serial connection failed on %s: %s", port, exc)
            return

        self.status_var.set(f"Connected to {port} at 115200,N,8,1.")
        logger.info("Connected to %s at 115200,N,8,1.", port)

    def disconnect_serial(self) -> None:
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
        try:
            while True:
                result = self.result_queue.get_nowait()
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
                else:
                    logger.warning("Communication test did not pass. %s %s Detail=%s", colorize_hex_frame(result.request_hex, "TX"), colorize_hex_frame(result.response_hex or "-", "RX"), result.message)
        except queue.Empty:
            pass
        self.after(100, self._poll_result_queue)

    def _handle_worker_event(self, event: tuple) -> None:
        event_type = event[0]
        if event_type == "read_positions":
            _, entries, source = event
            self.position_read_pending = False
            positions: dict[str, int] = {}
            for command, response, position in entries:
                command_hex = hex_bytes(command)
                response_hex = hex_bytes(response)
                self.tx_var.set(command_hex)
                self.rx_var.set(response_hex)
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
            logger.info(
                "Position read: X=%s Y=%s Z=%s.",
                positions.get("X", "-"),
                positions.get("Y", "-"),
                positions.get("Z", "-"),
                extra={"repeat_key": "keyboard_motion"} if source == "keyboard" else None,
            )
            return

        if event_type == "zero_z_command":
            _, command = event
            command_hex = hex_bytes(command)
            self.tx_var.set(command_hex)
            self._append_hex_history("TX", command_hex)
            self.autofocus_status_var.set("Set Z=0 command sent")
            logger.info("Set Z=0 command sent: %s No response expected.", colorize_hex_frame(command_hex, "TX"))
            return

        if event_type == "read_position_error":
            _, exc = event
            self.position_read_pending = False
            self.status_var.set(f"Read current position failed: {exc}")
            logger.error("Read current position failed: %s", exc)
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
            self.video_label.configure(text=f"Camera unavailable: {exc}", image="")
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
            self.rx_var.set(hex_bytes(response))
            self._append_hex_history("RX", hex_bytes(response))
            self._update_axis_position(position)
            self.status_var.set("Realtime position updated.")
            return

        if event_type == "realtime_raw":
            _, response, detail = event
            self.rx_var.set(hex_bytes(response))
            self._append_hex_history("RX", hex_bytes(response))
            self.status_var.set(f"Realtime position frame ignored: {detail}")
            logger.warning("Realtime frame ignored: %s", detail)
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
            logger.info("AutoFocus stopped.")
            return

        if event_type == "imgstitch_status":
            _, message = event
            self.imgstitch_status_var.set(str(message))
            self.status_var.set(str(message))
            logger.info("ImgStitch: %s", message)
            return

        if event_type == "imgstitch_preview":
            _, mosaic = event
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
            self.motion_busy = False
            if self.imgstitch_status_var.get() == "Stopping":
                self.imgstitch_status_var.set("Stopped")
            if self.imgstitch_restore_realtime and not self.realtime_enabled and self.serial_client:
                self.imgstitch_restore_realtime = False
                self.toggle_realtime_position()
            return

        if event_type == "motor_command":
            _, axis, action, command, source = event
            command_hex = hex_bytes(command)
            self.tx_var.set(command_hex)
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
                extra={"repeat_key": "keyboard_motion"} if source == "keyboard" else None,
            )
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
        while self.camera_running and session_id == self.camera_session_id:
            try:
                frame = camera.read()
            except Exception as exc:
                self.result_queue.put(("camera_error", session_id, exc))
                return
            if frame:
                if not reported_ready:
                    self.result_queue.put(("camera_ready", session_id))
                    reported_ready = True
                with self.camera_lock:
                    self.latest_camera_frame = frame
        camera.close()

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
            self.camera_image = tk.PhotoImage(data=frame.ppm_bytes, format="PPM")
            self.video_label.configure(image=self.camera_image, text="")
            with self.focus_lock:
                self.latest_focus_frame_ppm = frame.ppm_bytes
            with self.camera_lock:
                self.latest_stitch_frame = frame.image_bgr
            if hasattr(self, "autofocus_video_label") and not self.autofocus_running:
                self.autofocus_camera_image = tk.PhotoImage(data=frame.ppm_bytes, format="PPM")
                self.autofocus_video_label.configure(image=self.autofocus_camera_image, text="")
            if hasattr(self, "imgstitch_live_label") and not self.imgstitch_running:
                self.imgstitch_camera_image = tk.PhotoImage(data=frame.ppm_bytes, format="PPM")
                self.imgstitch_live_label.configure(image=self.imgstitch_camera_image, text="")
            self._update_focus_scores(frame.focus_scores)

        self.after(15, self._update_camera_frame)

    def destroy(self) -> None:
        self.autofocus_stop_event.set()
        self.imgstitch_stop_event.set()
        self.realtime_stop_event.set()
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
