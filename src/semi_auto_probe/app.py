from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from .camera import UsbCamera
from .logging_utils import colorize_hex_frame, configure_logging, print_startup_banner
from .protocol import FUNCTION_READ_POSITION, RESPONSE_HEAD, Axis, AxisPosition, hex_bytes, parse_axis_position_response
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
        self.camera_thread: threading.Thread | None = None
        self.result_queue: queue.Queue[object] = queue.Queue()
        self.realtime_stop_event = threading.Event()
        self.realtime_thread: threading.Thread | None = None

        self.port_var = tk.StringVar()
        self.camera_index_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="Ready")
        self.rx_var = tk.StringVar(value="-")
        self.tx_var = tk.StringVar(value="-")
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
        self.motion_mode_var = tk.StringVar(value="Relative")
        self.realtime_enabled = False
        self.realtime_button_var = tk.StringVar(value="Continue")
        self.motion_busy = False
        self.keyboard_motion_busy = False
        self.position_read_pending = False
        self.position_read_job: str | None = None
        self.held_keys: dict[str, dict[str, object]] = {}
        self.resize_log_job: str | None = None
        self.last_logged_window_size: tuple[int, int] | None = None
        self.last_logged_control_width: int | None = None

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
        style.configure("TNotebook", background=self.colors["bg"], borderwidth=0)
        style.configure("TNotebook.Tab", background=self.colors["surface"], foreground=self.colors["muted"], padding=(18, 9), borderwidth=0)
        style.map("TNotebook.Tab", background=[("selected", self.colors["surface_2"]), ("active", self.colors["surface_3"])], foreground=[("selected", self.colors["text"]), ("active", self.colors["text"])])
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
        ttk.Spinbox(toolbar, from_=0, to=8, textvariable=self.camera_index_var, width=3).grid(row=0, column=6, padx=(0, 6), ipady=1)
        ttk.Button(toolbar, text="Restart", command=self.restart_camera).grid(row=0, column=7, padx=(0, 10))
        ttk.Button(toolbar, text="EMERGENCY STOP", style="Danger.TButton", command=self.emergency_stop).grid(row=0, column=8)

        notebook = ttk.Notebook(self)
        notebook.grid(row=1, column=0, sticky="nsew", padx=18, pady=(0, 14))

        main_page = ttk.Frame(notebook, style="App.TFrame", padding=(0, 10, 0, 0))
        communication_page = ttk.Frame(notebook, style="App.TFrame", padding=(0, 10, 0, 0))
        notebook.add(main_page, text="Main")
        notebook.add(communication_page, text="Communication")

        self._build_main_page(main_page)
        self._build_communication_page(communication_page)

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
            entry.bind("<Button-1>", lambda _event, a=axis: self.begin_position_edit(a, "Relative"))
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
        parent.rowconfigure(1, weight=1)

        summary = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        summary.grid(row=0, column=0, sticky="ew", pady=(0, 14))
        summary.columnconfigure((0, 1), weight=1, uniform="comm_summary")
        ttk.Label(summary, text="LAST TX", style="Section.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        ttk.Label(summary, text="LAST RX", style="Section.TLabel").grid(row=0, column=1, sticky="w", padx=(8, 0))
        ttk.Label(summary, textvariable=self.tx_var, style="Value.TLabel", wraplength=480, padding=10).grid(row=1, column=0, sticky="ew", padx=(0, 8), pady=(6, 0))
        ttk.Label(summary, textvariable=self.rx_var, style="Value.TLabel", wraplength=480, padding=10).grid(row=1, column=1, sticky="ew", padx=(8, 0), pady=(6, 0))

        history_panel = ttk.Frame(parent, style="Panel.TFrame", padding=14)
        history_panel.grid(row=1, column=0, sticky="nsew")
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

    def begin_position_edit(self, axis: str, mode: str) -> str:
        self.current_position_edit_mode = mode
        if mode == "Relative":
            self.position_edit_modes[axis] = mode
            self.modified_position_axes.add(axis)
            self.position_inputs[axis].configure(state="normal")
            self.position_inputs[axis].focus_set()
            self.position_vars[axis].set("")
            self.position_inputs[axis].configure(fg=self.colors["warning"])
            self.status_var.set(f"{axis} relative input. Enter signed pulses, then press Move.")
        else:
            for target_axis in ("X", "Y", "Z"):
                self.position_edit_modes[target_axis] = "Absolute"
                self.position_inputs[target_axis].configure(state="normal")
                self.position_vars[target_axis].set(str(self.current_position_values[target_axis]))
                self.position_inputs[target_axis].configure(fg=self.colors["blue"])
            self.modified_position_axes.add(axis)
            self.position_inputs[axis].focus_set()
            self.status_var.set(f"{axis} absolute target input. Enter target position, then press Move.")
        self.position_inputs[axis].after_idle(lambda a=axis: self.position_inputs[a].icursor("end"))
        return "break"

    def focus_next_position_input(self, axis: str, _event: tk.Event) -> str:
        axes = ("X", "Y", "Z")
        next_axis = axes[(axes.index(axis) + 1) % len(axes)]
        self.position_inputs[next_axis].focus_set()
        self.begin_position_edit(next_axis, self.current_position_edit_mode or "Relative")
        return "break"

    def focus_previous_position_input(self, axis: str, _event: tk.Event) -> str:
        axes = ("X", "Y", "Z")
        previous_axis = axes[(axes.index(axis) - 1) % len(axes)]
        self.position_inputs[previous_axis].focus_set()
        self.begin_position_edit(previous_axis, self.current_position_edit_mode or "Relative")
        return "break"

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
            if not self._axis_is_actively_editing(axis_name):
                self.modified_position_axes.discard(axis_name)
                self.position_edit_modes[axis_name] = None
                if axis_name in self.position_inputs:
                    self.position_inputs[axis_name].configure(state="normal")
                    self.position_inputs[axis_name].configure(fg=self.colors["accent"])
                self.position_vars[axis_name].set(str(position.position))
                if axis_name in self.position_inputs:
                    self.position_inputs[axis_name].configure(state="readonly", readonlybackground=self.colors["surface_2"])

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
            values = {axis: int(self.position_vars[axis].get()) for axis in self.modified_position_axes}
        except ValueError:
            self.status_var.set("Move requires integer coordinate input values.")
            logger.warning("Move rejected because at least one coordinate input is not an integer.")
            return

        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        axes = tuple(axis for axis in ("X", "Y", "Z") if axis in self.modified_position_axes)
        modes = {axis: self.position_edit_modes[axis] or "Relative" for axis in axes}
        self.motion_busy = True
        self.status_var.set("Running coordinate move...")
        self.modified_position_axes.clear()
        for axis in ("X", "Y", "Z"):
            self.position_edit_modes[axis] = None
            self.position_inputs[axis].configure(fg=self.colors["accent"], state="readonly", readonlybackground=self.colors["surface_2"])
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
            logger.info(
                "Position read: X=%s Y=%s Z=%s.",
                positions.get("X", "-"),
                positions.get("Y", "-"),
                positions.get("Z", "-"),
                extra={"repeat_key": "keyboard_motion"} if source == "keyboard" else None,
            )
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
            _, exc = event
            self.video_label.configure(text=f"Camera unavailable: {exc}", image="")
            self.status_var.set(str(exc))
            self.camera_running = False
            self.camera_rendering = False
            logger.error("Camera unavailable: %s", exc)
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

        if event_type == "motor_command":
            _, axis, action, command, source = event
            command_hex = hex_bytes(command)
            self.tx_var.set(command_hex)
            self._append_hex_history("TX", command_hex)
            self.status_var.set(f"{axis} {action} command sent.")
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
            logger.warning("Camera start skipped because index is not an integer: %s", self.camera_index_var.get())
            return

        self.camera = UsbCamera(index=index, width=800, height=450)
        self.camera_running = True
        self.camera_rendering = True
        logger.info("Starting USB camera preview on index %s.", index)
        self.camera_thread = threading.Thread(target=self._camera_worker, daemon=True)
        self.camera_thread.start()
        self._update_camera_frame()

    def restart_camera(self) -> None:
        logger.info("Restarting USB camera preview.")
        self.stop_camera()
        self.start_camera()

    def stop_camera(self) -> None:
        self.camera_running = False
        self.camera_rendering = False
        if self.camera:
            self.camera.close()
        self.camera = None

    def _camera_worker(self) -> None:
        assert self.camera is not None
        while self.camera_running and self.camera:
            try:
                frame = self.camera.read()
            except Exception as exc:
                self.result_queue.put(("camera_error", exc))
                return
            if frame:
                with self.camera_lock:
                    self.latest_camera_frame = frame

    def _update_camera_frame(self) -> None:
        if not self.camera_rendering:
            return
        with self.camera_lock:
            frame = self.latest_camera_frame
            self.latest_camera_frame = None

        if frame:
            self.camera_image = tk.PhotoImage(data=frame.ppm_bytes, format="PPM")
            self.video_label.configure(image=self.camera_image, text="")

        self.after(15, self._update_camera_frame)

    def destroy(self) -> None:
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
