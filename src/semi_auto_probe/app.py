from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import ttk

from .camera import UsbCamera
from .serial_client import ControllerSerialClient, CommunicationTestResult, list_serial_ports


class ProbeApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Semi Auto Probe")
        self.geometry("1240x800")
        self.minsize(980, 660)
        self.configure(bg="#0b0f14")

        self.serial_client: ControllerSerialClient | None = None
        self.camera: UsbCamera | None = None
        self.camera_running = False
        self.camera_image: tk.PhotoImage | None = None
        self.result_queue: queue.Queue[CommunicationTestResult | Exception] = queue.Queue()

        self.port_var = tk.StringVar()
        self.camera_index_var = tk.StringVar(value="0")
        self.status_var = tk.StringVar(value="Ready")
        self.rx_var = tk.StringVar(value="-")
        self.tx_var = tk.StringVar(value="-")

        self._configure_theme()
        self._build_ui()
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
        style.configure("Status.TLabel", background=self.colors["surface_2"], foreground=self.colors["accent"], font=("Segoe UI Semibold", 10))
        style.configure("Video.TLabel", background="#05070a", foreground=self.colors["muted"], font=("Segoe UI Semibold", 14))
        style.configure("TButton", background=self.colors["surface_3"], foreground=self.colors["text"], bordercolor=self.colors["border"], focusthickness=0, padding=(12, 7))
        style.map("TButton", background=[("active", "#223144"), ("pressed", "#1d2a3a")])
        style.configure("Accent.TButton", background="#0f3b2d", foreground="#d1fae5", bordercolor="#1f7a5a", padding=(14, 7))
        style.map("Accent.TButton", background=[("active", "#14543f"), ("pressed", "#0f3b2d")])
        style.configure("Ghost.TButton", background=self.colors["surface"], foreground=self.colors["muted"], bordercolor=self.colors["border"], padding=(10, 7))
        style.map("Ghost.TButton", background=[("active", self.colors["surface_2"])], foreground=[("active", self.colors["text"])])
        style.configure("TCombobox", fieldbackground=self.colors["surface_2"], background=self.colors["surface_2"], foreground=self.colors["text"], bordercolor=self.colors["border"], arrowcolor=self.colors["muted"], padding=5)
        style.map("TCombobox", fieldbackground=[("readonly", self.colors["surface_2"])], foreground=[("readonly", self.colors["text"])])
        style.configure("TSpinbox", fieldbackground=self.colors["surface_2"], background=self.colors["surface_2"], foreground=self.colors["text"], bordercolor=self.colors["border"], arrowcolor=self.colors["muted"], padding=5)
        style.configure("TLabelframe", background=self.colors["surface"], bordercolor=self.colors["border"])
        style.configure("TLabelframe.Label", background=self.colors["surface"], foreground=self.colors["muted"], font=("Segoe UI Semibold", 9))
        self.option_add("*TCombobox*Listbox.background", self.colors["surface_2"])
        self.option_add("*TCombobox*Listbox.foreground", self.colors["text"])
        self.option_add("*TCombobox*Listbox.selectBackground", self.colors["surface_3"])
        self.option_add("*TCombobox*Listbox.selectForeground", self.colors["text"])

    def _build_ui(self) -> None:
        self.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=1)

        header = ttk.Frame(self, style="App.TFrame", padding=(20, 16, 20, 8))
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)
        ttk.Label(header, text="Semi Auto Probe", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="3-axis RS-232 motion control with synchronized USB vision", style="Subtitle.TLabel").grid(row=1, column=0, sticky="w", pady=(2, 0))

        toolbar = ttk.Frame(self, style="Toolbar.TFrame", padding=(16, 12))
        toolbar.grid(row=1, column=0, sticky="ew", padx=20, pady=(4, 14))
        toolbar.columnconfigure(11, weight=1)

        ttk.Label(toolbar, text="SERIAL", style="Muted.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.port_combo = ttk.Combobox(toolbar, textvariable=self.port_var, width=14, state="readonly")
        self.port_combo.grid(row=0, column=1, padx=(0, 8), ipady=2)
        ttk.Button(toolbar, text="Refresh", style="Ghost.TButton", command=self.refresh_ports).grid(row=0, column=2, padx=(0, 8))
        ttk.Button(toolbar, text="Connect", style="Accent.TButton", command=self.connect_serial).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(toolbar, text="Disconnect", command=self.disconnect_serial).grid(row=0, column=4, padx=(0, 14))
        ttk.Button(toolbar, text="Communication Test", command=self.run_comm_test).grid(row=0, column=5, padx=(0, 22))

        ttk.Label(toolbar, text="CAMERA", style="Muted.TLabel").grid(row=0, column=6, padx=(0, 8))
        ttk.Spinbox(toolbar, from_=0, to=8, textvariable=self.camera_index_var, width=4).grid(row=0, column=7, padx=(0, 8), ipady=2)
        ttk.Button(toolbar, text="Restart Camera", command=self.restart_camera).grid(row=0, column=8)

        body = ttk.Frame(self, style="App.TFrame")
        body.grid(row=2, column=0, sticky="nsew", padx=20, pady=(0, 20))
        body.columnconfigure(0, weight=4)
        body.columnconfigure(1, weight=1)
        body.rowconfigure(0, weight=1)

        camera_panel = ttk.Frame(body, style="Panel.TFrame", padding=14)
        camera_panel.grid(row=0, column=0, sticky="nsew", padx=(0, 14))
        camera_panel.columnconfigure(0, weight=1)
        camera_panel.rowconfigure(1, weight=1)

        camera_header = ttk.Frame(camera_panel, style="Panel.TFrame")
        camera_header.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        camera_header.columnconfigure(0, weight=1)
        ttk.Label(camera_header, text="LIVE VISION", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(camera_header, text="USB camera index 0", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        self.video_label = ttk.Label(camera_panel, anchor="center", text="Camera preview", style="Video.TLabel")
        self.video_label.grid(row=1, column=0, sticky="nsew")

        side = ttk.Frame(body, style="Panel.TFrame", padding=14)
        side.grid(row=0, column=1, sticky="nsew")
        side.columnconfigure(0, weight=1)

        ttk.Label(side, text="TELEMETRY", style="Section.TLabel").grid(row=0, column=0, sticky="w")
        self._telemetry_value(side, "TX", self.tx_var, 1)
        self._telemetry_value(side, "RX", self.rx_var, 3)
        self._status_value(side, 5)

        axes = ttk.LabelFrame(side, text="AXES")
        axes.grid(row=7, column=0, sticky="ew", pady=(18, 0))
        axes.columnconfigure(0, weight=1)
        for i, (name, color) in enumerate((("X / Axis 1", "#60a5fa"), ("Y / Axis 2", "#34d399"), ("Z / Axis 3", "#fbbf24"))):
            row = ttk.Frame(axes, style="Panel.TFrame")
            row.grid(row=i, column=0, sticky="ew", padx=10, pady=(10 if i == 0 else 4, 6))
            row.columnconfigure(1, weight=1)
            marker = tk.Canvas(row, width=10, height=10, bg=self.colors["surface"], highlightthickness=0)
            marker.create_oval(1, 1, 9, 9, fill=color, outline=color)
            marker.grid(row=0, column=0, padx=(0, 8))
            ttk.Label(row, text=name, style="Panel.TLabel").grid(row=0, column=1, sticky="w")
            ttk.Button(row, text="Standby", style="Ghost.TButton").grid(row=0, column=2, padx=(8, 0))

        footer = ttk.Frame(side, style="Panel.TFrame")
        footer.grid(row=8, column=0, sticky="sew", pady=(18, 0))
        ttk.Label(footer, text="Controller mode", style="Muted.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(footer, text="Manual observation", style="Panel.TLabel").grid(row=1, column=0, sticky="w", pady=(3, 0))

    def _telemetry_value(self, parent: ttk.Frame, title: str, variable: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=title, style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(16, 6))
        value = ttk.Label(parent, textvariable=variable, style="Value.TLabel", wraplength=300, padding=10)
        value.grid(row=row + 1, column=0, sticky="ew")

    def _status_value(self, parent: ttk.Frame, row: int) -> None:
        ttk.Label(parent, text="STATUS", style="Section.TLabel").grid(row=row, column=0, sticky="w", pady=(16, 6))
        value = ttk.Label(parent, textvariable=self.status_var, style="Status.TLabel", wraplength=300, padding=10)
        value.grid(row=row + 1, column=0, sticky="ew")

    def refresh_ports(self) -> None:
        ports = list_serial_ports()
        self.port_combo["values"] = ports
        if ports and not self.port_var.get():
            self.port_var.set(ports[0])
        if not ports:
            self.status_var.set("No serial ports found. Install pyserial and check the USB-RS232 adapter.")

    def connect_serial(self) -> None:
        port = self.port_var.get().strip()
        if not port:
            self.status_var.set("Select a serial port first.")
            return

        try:
            self.serial_client = ControllerSerialClient(port)
            self.serial_client.open()
        except Exception as exc:
            self.status_var.set(f"Serial connection failed: {exc}")
            return

        self.status_var.set(f"Connected to {port} at 115200,N,8,1.")

    def disconnect_serial(self) -> None:
        if self.serial_client:
            self.serial_client.close()
        self.serial_client = None
        self.status_var.set("Serial disconnected.")

    def run_comm_test(self) -> None:
        if not self.serial_client:
            self.connect_serial()
        if not self.serial_client:
            return

        self.status_var.set("Running communication test...")
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
                    continue
                self.tx_var.set(result.request_hex)
                self.rx_var.set(result.response_hex or "-")
                self.status_var.set(result.message)
        except queue.Empty:
            pass
        self.after(100, self._poll_result_queue)

    def start_camera(self) -> None:
        try:
            index = int(self.camera_index_var.get())
        except ValueError:
            self.status_var.set("Camera index must be an integer.")
            return

        self.camera = UsbCamera(index=index)
        self.camera_running = True
        self._update_camera_frame()

    def restart_camera(self) -> None:
        self.stop_camera()
        self.start_camera()

    def stop_camera(self) -> None:
        self.camera_running = False
        if self.camera:
            self.camera.close()
        self.camera = None

    def _update_camera_frame(self) -> None:
        if not self.camera_running or not self.camera:
            return

        try:
            frame = self.camera.read()
        except Exception as exc:
            self.video_label.configure(text=f"Camera unavailable: {exc}", image="")
            self.status_var.set(str(exc))
            self.camera_running = False
            return

        if frame:
            self.camera_image = tk.PhotoImage(data=frame.ppm_bytes, format="PPM")
            self.video_label.configure(image=self.camera_image, text="")

        self.after(30, self._update_camera_frame)

    def destroy(self) -> None:
        self.stop_camera()
        if self.serial_client:
            self.serial_client.close()
        super().destroy()


def main() -> None:
    app = ProbeApp()
    app.mainloop()
