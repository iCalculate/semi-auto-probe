from __future__ import annotations

import tkinter as tk
from tkinter import messagebox, ttk

from ..config import calibration_distance_px


class PixelCalibrationDialog(tk.Toplevel):
    def __init__(self, parent: tk.Tk, image_bgr, colors: dict[str, str]) -> None:
        super().__init__(parent)
        self.title("Pixel Calibration")
        self.configure(bg=colors["bg"])
        self.transient(parent)
        self.grab_set()
        self.result_um_per_px: float | None = None
        self.colors = colors
        self.points_display: list[tuple[float, float]] = []
        self.points_original: list[tuple[float, float]] = []
        self.known_um_var = tk.StringVar(value="100")
        self.result_var = tk.StringVar(value="Click three points: first two define a line, third measures perpendicular distance.")

        import cv2

        rgb = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2RGB)
        original_height, original_width = rgb.shape[:2]
        max_width, max_height = 820, 520
        self.scale = min(max_width / original_width, max_height / original_height, 1.0)
        if self.scale < 1.0:
            display_width = max(1, int(original_width * self.scale))
            display_height = max(1, int(original_height * self.scale))
            rgb = cv2.resize(rgb, (display_width, display_height), interpolation=cv2.INTER_AREA)
        else:
            display_height, display_width = original_height, original_width
        header = f"P6 {display_width} {display_height} 255\n".encode("ascii")
        self.photo = tk.PhotoImage(data=header + rgb.tobytes(), format="PPM")

        container = ttk.Frame(self, style="Panel.TFrame", padding=14)
        container.grid(row=0, column=0, sticky="nsew")
        container.columnconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)
        self.rowconfigure(0, weight=1)

        toolbar = ttk.Frame(container, style="Panel.TFrame")
        toolbar.grid(row=0, column=0, sticky="ew", pady=(0, 10))
        toolbar.columnconfigure(4, weight=1)
        ttk.Label(toolbar, text="Known distance (um)", style="Muted.TLabel").grid(row=0, column=0, sticky="w", padx=(0, 8))
        known_um_validate = self.register(self._known_um_text_allowed)
        tk.Entry(
            toolbar,
            textvariable=self.known_um_var,
            width=12,
            validate="key",
            validatecommand=(known_um_validate, "%P"),
            relief="flat",
            bd=0,
            bg=colors.get("input", colors["surface_2"]),
            fg=colors["text"],
            insertbackground=colors.get("accent", colors["text"]),
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            highlightthickness=2,
            highlightbackground=colors["border"],
            highlightcolor=colors.get("border_focus", "#38bdf8"),
            font=("Segoe UI", 10),
        ).grid(row=0, column=1, sticky="w", padx=(0, 12), ipady=5)
        ttk.Button(toolbar, text="Reset", command=self.reset_points).grid(row=0, column=2, sticky="w", padx=(0, 8))
        self.save_button = ttk.Button(toolbar, text="Save Calibration", style="Accent.TButton", command=self.save_result, state="disabled")
        self.save_button.grid(row=0, column=3, sticky="w")

        self.canvas = tk.Canvas(container, width=display_width, height=display_height, bg="#05070a", highlightthickness=1, highlightbackground=colors["border"])
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self.canvas.create_image(0, 0, anchor="nw", image=self.photo)
        self.canvas.bind("<Button-1>", self.on_click)

        ttk.Label(container, textvariable=self.result_var, style="Status.TLabel", wraplength=820, padding=10).grid(row=2, column=0, sticky="ew", pady=(10, 0))
        ttk.Button(container, text="Cancel", command=self.destroy).grid(row=3, column=0, sticky="e", pady=(10, 0))

    def on_click(self, event: tk.Event) -> None:
        if len(self.points_display) >= 3:
            return
        x = float(event.x)
        y = float(event.y)
        self.points_display.append((x, y))
        self.points_original.append((x / self.scale, y / self.scale))
        self.draw_overlay()
        if len(self.points_display) == 3:
            self.update_result()
        else:
            self.result_var.set(f"Point {len(self.points_display)} recorded. Click {3 - len(self.points_display)} more.")

    def reset_points(self) -> None:
        self.points_display.clear()
        self.points_original.clear()
        self.save_button.configure(state="disabled")
        self.result_var.set("Click three points: first two define a line, third measures perpendicular distance.")
        self.draw_overlay()

    def draw_overlay(self) -> None:
        self.canvas.delete("overlay")
        if len(self.points_display) >= 2:
            self.canvas.create_line(*self.points_display[0], *self.points_display[1], fill="#34d399", width=2, tags="overlay")
        if len(self.points_display) >= 3:
            p1, p2, p3 = self.points_display
            foot = self._projection_foot(p1, p2, p3)
            self.canvas.create_line(*p3, *foot, fill="#fbbf24", width=2, dash=(4, 3), tags="overlay")
        for index, (x, y) in enumerate(self.points_display, start=1):
            self.canvas.create_oval(x - 5, y - 5, x + 5, y + 5, fill="#60a5fa", outline="#dbeafe", width=1, tags="overlay")
            self.canvas.create_text(x + 8, y - 8, text=str(index), fill="#e5edf5", anchor="sw", font=("Segoe UI Semibold", 10), tags="overlay")

    def update_result(self) -> None:
        try:
            known_um = float(self.known_um_var.get())
            if known_um <= 0:
                raise ValueError("Known distance must be positive.")
            distance_px = calibration_distance_px(*self.points_original)
            if distance_px <= 0:
                raise ValueError("Pixel distance must be positive.")
        except ValueError as exc:
            self.result_var.set(str(exc))
            self.save_button.configure(state="disabled")
            return

        self.result_um_per_px = known_um / distance_px
        self.result_var.set(f"Distance: {distance_px:.3f} px, calibration: {self.result_um_per_px:.6g} um/px")
        self.save_button.configure(state="normal")

    def save_result(self) -> None:
        if self.result_um_per_px is None:
            messagebox.showerror("Pixel Calibration", "Complete three-point calibration before saving.", parent=self)
            return
        self.destroy()

    @staticmethod
    def _known_um_text_allowed(proposed: str) -> bool:
        if proposed in {"", "."}:
            return True
        if proposed.count(".") > 1:
            return False
        digits = proposed.replace(".", "", 1)
        return digits.isdigit()

    @staticmethod
    def _projection_foot(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> tuple[float, float]:
        x1, y1 = p1
        x2, y2 = p2
        x3, y3 = p3
        dx = x2 - x1
        dy = y2 - y1
        length_sq = dx * dx + dy * dy
        if length_sq <= 0:
            return p1
        t = ((x3 - x1) * dx + (y3 - y1) * dy) / length_sq
        return x1 + t * dx, y1 + t * dy
