from __future__ import annotations

import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..agent import AgentContext, AgentPlan


class AgentPanel:
    def __init__(
        self,
        parent: ttk.Frame,
        colors: dict[str, str],
        *,
        get_context: Callable[[], AgentContext],
        get_microscope_preview: Callable[[], bytes | None],
        plan_instruction: Callable[[str], AgentPlan],
        execute_plan: Callable[[AgentPlan], str],
        cancel_plan: Callable[[], str],
        stop_task: Callable[[], str],
    ) -> None:
        self.parent = parent
        self.colors = colors
        self.get_context = get_context
        self.get_microscope_preview = get_microscope_preview
        self.plan_instruction = plan_instruction
        self.execute_plan = execute_plan
        self.cancel_plan = cancel_plan
        self.stop_task = stop_task
        self.pending_plan: AgentPlan | None = None
        self.planning_thread: threading.Thread | None = None
        self.microscope_image: tk.PhotoImage | None = None

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        shell = ttk.Frame(parent, style="App.TFrame", padding=10)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=2, minsize=300)
        shell.columnconfigure(1, weight=5, minsize=520)
        shell.columnconfigure(2, weight=2, minsize=280)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell, style="App.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="AI Agent", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="GDS-aware planning, local confirmation, controlled execution", style="Muted.TLabel").grid(row=0, column=1, sticky="e")

        left = ttk.Frame(shell, style="App.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=2)
        left.rowconfigure(1, weight=1)

        microscope_card = self._section(left, "Microscope")
        microscope_card.grid(row=0, column=0, sticky="nsew")
        microscope_card.rowconfigure(1, weight=1)
        self.microscope_label = tk.Label(
            microscope_card,
            text="No microscope frame",
            anchor="center",
            bg="#05070a",
            fg=colors["muted"],
            font=("Segoe UI Semibold", 12),
            bd=0,
            highlightthickness=1,
            highlightbackground=colors["border"],
        )
        self.microscope_label.grid(row=1, column=0, sticky="nsew")

        coordinate_card = self._section(left, "Coordinates")
        coordinate_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        coordinate_card.rowconfigure(1, weight=1)
        self.coordinate_text = self._readonly_text(coordinate_card, height=13, font_size=10, mono=True)
        self.coordinate_text.grid(row=1, column=0, sticky="nsew")

        center = ttk.Frame(shell, style="App.TFrame")
        center.grid(row=1, column=1, sticky="nsew", padx=(0, 10))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(0, weight=1)
        center.rowconfigure(1, weight=0)

        chat_card = self._section(center, "Conversation")
        chat_card.grid(row=0, column=0, sticky="nsew")
        chat_card.rowconfigure(1, weight=1)
        self.chat_text = self._readonly_text(chat_card, height=24, font_size=11)
        self.chat_text.grid(row=1, column=0, sticky="nsew")
        self._append_chat("Agent", "Ready. Send a natural-language command. I will generate a plan first; hardware actions still require confirmation.")

        composer = ttk.Frame(center, style="Panel.TFrame", padding=10)
        composer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        self.input_text = tk.Text(
            composer,
            height=3,
            wrap="word",
            bg=colors["surface_2"],
            fg=colors["text"],
            insertbackground=colors["accent"],
            bd=0,
            highlightthickness=1,
            highlightbackground=colors["border"],
            highlightcolor=colors["accent"],
            font=("Segoe UI", 11),
            padx=12,
            pady=9,
        )
        self.input_text.grid(row=0, column=0, columnspan=4, sticky="ew")
        self.input_text.insert("1.0", "\u79fb\u52a8\u5230\u5f53\u524d\u9009\u4e2d\u7684 GDS \u70b9")
        self.input_text.bind("<Control-Return>", lambda _event: self.generate_plan())

        self.generate_button = ttk.Button(composer, text="Plan", style="Accent.TButton", command=self.generate_plan)
        self.generate_button.grid(row=1, column=0, sticky="ew", pady=(8, 0), padx=(0, 6))
        self.confirm_button = ttk.Button(composer, text="Run", style="Accent.TButton", command=self.confirm_run, state="disabled")
        self.confirm_button.grid(row=1, column=1, sticky="ew", pady=(8, 0), padx=(0, 6))
        ttk.Button(composer, text="Cancel", command=self.cancel_pending_plan).grid(row=1, column=2, sticky="ew", pady=(8, 0), padx=(0, 6))
        ttk.Button(composer, text="Stop", style="Danger.TButton", command=self.stop_current_task).grid(row=1, column=3, sticky="ew", pady=(8, 0))

        right = ttk.Frame(shell, style="App.TFrame")
        right.grid(row=1, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=1)
        right.rowconfigure(1, weight=1)
        right.rowconfigure(2, weight=1)

        system_card = self._section(right, "System State")
        system_card.grid(row=0, column=0, sticky="nsew")
        system_card.rowconfigure(1, weight=1)
        self.state_text = self._readonly_text(system_card, height=10, font_size=10)
        self.state_text.grid(row=1, column=0, sticky="nsew")

        info_card = self._section(right, "Important Info")
        info_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        info_card.rowconfigure(1, weight=1)
        self.info_text = self._readonly_text(info_card, height=10, font_size=10)
        self.info_text.grid(row=1, column=0, sticky="nsew")

        status_card = self._section(right, "Status")
        status_card.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        status_card.rowconfigure(1, weight=1)
        self.status_text = self._readonly_text(status_card, height=7, font_size=10)
        self.status_text.grid(row=1, column=0, sticky="nsew")
        self.status_var = tk.StringVar(value="Idle")
        ttk.Label(status_card, textvariable=self.status_var, style="Status.TLabel", wraplength=260, padding=(8, 6)).grid(row=2, column=0, sticky="ew", pady=(8, 0))

        self.refresh_context()
        self._update_microscope_preview()

    def _section(self, parent: tk.Widget, title: str) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=title.upper(), style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        return frame

    def _readonly_text(self, parent: tk.Widget, *, height: int, font_size: int, mono: bool = False) -> tk.Text:
        widget = tk.Text(
            parent,
            height=height,
            wrap="word",
            bg=self.colors["surface_2"],
            fg=self.colors["text"],
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            font=("Cascadia Mono" if mono else "Segoe UI", font_size),
            padx=10,
            pady=8,
        )
        widget.configure(state="disabled")
        return widget

    @staticmethod
    def _replace_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _append_chat(self, role: str, text: str) -> None:
        if not hasattr(self, "chat_text"):
            return
        self.chat_text.configure(state="normal")
        if self.chat_text.index("end-1c") != "1.0":
            self.chat_text.insert("end", "\n\n")
        prefix = "You" if role == "User" else "AI Agent"
        self.chat_text.insert("end", f"{prefix}\n{text}")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def refresh_context(self) -> None:
        context = self.get_context()
        self._replace_text(self.coordinate_text, self._format_coordinates(context))
        self._replace_text(self.state_text, self._format_state(context))
        if self.pending_plan is None:
            self._replace_text(self.info_text, self._format_important_info(context, None))

    def _update_microscope_preview(self) -> None:
        payload = self.get_microscope_preview()
        if payload:
            self.microscope_image = tk.PhotoImage(data=payload, format="PPM")
            self.microscope_label.configure(image=self.microscope_image, text="")
        else:
            self.microscope_label.configure(image="", text="No microscope frame")
        self.parent.after(80, self._update_microscope_preview)

    def generate_plan(self) -> str:
        if self.planning_thread is not None and self.planning_thread.is_alive():
            self.status_var.set("Agent planning is already running.")
            return "break"
        instruction = self.input_text.get("1.0", "end").strip()
        if not instruction:
            self.status_var.set("Enter an instruction first.")
            return "break"
        self._append_chat("User", instruction)
        self.pending_plan = None
        self.confirm_button.configure(state="disabled")
        self.generate_button.configure(state="disabled")
        self.status_var.set("Generating plan...")
        self._replace_text(self.status_text, "Calling LLM planner or local fallback.")
        self.planning_thread = threading.Thread(target=self._generate_plan_worker, args=(instruction,), daemon=True)
        self.planning_thread.start()
        return "break"

    def _generate_plan_worker(self, instruction: str) -> None:
        try:
            plan = self.plan_instruction(instruction)
        except Exception as exc:
            self.parent.after(0, lambda: self._handle_plan_error(exc))
            return
        self.parent.after(0, lambda: self._apply_generated_plan(plan))

    def _handle_plan_error(self, exc: Exception) -> None:
        self.pending_plan = None
        self.confirm_button.configure(state="disabled")
        self.generate_button.configure(state="normal")
        message = f"Planning failed: {exc}"
        self.status_var.set(message)
        self._replace_text(self.status_text, message)
        self._append_chat("Agent", message)
        self.refresh_context()

    def _apply_generated_plan(self, plan: AgentPlan) -> None:
        self.pending_plan = plan if plan.executable and plan.requires_confirmation else None
        self._replace_text(self.info_text, self._format_important_info(self.get_context(), plan))
        self.confirm_button.configure(state="normal" if self.pending_plan is not None else "disabled")
        self.generate_button.configure(state="normal")
        status = "Plan ready for confirmation." if self.pending_plan is not None else "Plan is not executable."
        self.status_var.set(status)
        self._replace_text(self.status_text, self._format_agent_status(plan))
        self._append_chat("Agent", self._format_chat_plan(plan))
        self.refresh_context()

    def confirm_run(self) -> None:
        if self.pending_plan is None:
            self.status_var.set("No executable plan is waiting for confirmation.")
            return
        message = self.execute_plan(self.pending_plan)
        self.pending_plan = None
        self.confirm_button.configure(state="disabled")
        self.status_var.set(message)
        self._replace_text(self.status_text, message)
        self._append_chat("Agent", message)
        self.refresh_context()

    def cancel_pending_plan(self) -> None:
        self.pending_plan = None
        self.confirm_button.configure(state="disabled")
        message = self.cancel_plan()
        self.status_var.set(message)
        self._replace_text(self.status_text, message)
        self._append_chat("Agent", message)
        self.refresh_context()

    def stop_current_task(self) -> None:
        message = self.stop_task()
        self.pending_plan = None
        self.confirm_button.configure(state="disabled")
        self.status_var.set(message)
        self._replace_text(self.status_text, message)
        self._append_chat("Agent", message)
        self.refresh_context()

    def set_status(self, message: str) -> None:
        self.status_var.set(message)
        self._replace_text(self.status_text, message)
        self.refresh_context()

    @staticmethod
    def _format_bool(value: bool) -> str:
        return "ready" if value else "off"

    @staticmethod
    def _format_coordinates(context: AgentContext) -> str:
        stage_um = context.stage_position_um
        selected_gds = context.gds_selected_uv
        current_gds = context.current_mapped_gds_uv
        target_stage = context.gds_target_stage_um
        return "\n".join(
            [
                "Controller pulses",
                f"X {context.positions.get('X', 0):>10}",
                f"Y {context.positions.get('Y', 0):>10}",
                f"Z {context.positions.get('Z', 0):>10}",
                "",
                "Stage physical um",
                f"X {stage_um.get('X', 0.0):>10.3f}",
                f"Y {stage_um.get('Y', 0.0):>10.3f}",
                f"Z {stage_um.get('Z', 0.0):>10.3f}",
                "",
                "Current GDS u/v",
                "-" if current_gds is None else f"u {current_gds[0]:>10.3f}\nv {current_gds[1]:>10.3f}",
                "",
                "Selected GDS u/v",
                "-" if selected_gds is None else f"u {selected_gds[0]:>10.3f}\nv {selected_gds[1]:>10.3f}",
                "",
                "Selected target stage",
                "-" if target_stage is None else f"X {target_stage[0]:>10.3f}\nY {target_stage[1]:>10.3f}",
            ]
        )

    def _format_state(self, context: AgentContext) -> str:
        return "\n".join(
            [
                f"Serial        {self._format_bool(context.serial_connected)}",
                f"Camera        {self._format_bool(context.camera_running)}",
                f"Frame         {self._format_bool(context.camera_frame_available)}",
                f"Motion        {'busy' if context.motion_busy or context.keyboard_motion_busy else 'idle'}",
                f"Position read {'pending' if context.position_read_pending else 'idle'}",
                f"AutoFocus     {'running' if context.autofocus_running else 'idle'}",
                f"FocusMap      {'running' if context.focusmap_running else 'idle'}",
                f"ImgStitch     {'running' if context.imgstitch_running else 'idle'}",
                f"GDS binding   {'ready' if context.gds_mapping_ready else 'not bound'}",
                f"Page          {context.current_page or '-'}",
            ]
        )

    def _format_important_info(self, context: AgentContext, plan: AgentPlan | None) -> str:
        if plan is None:
            lines = [
                "No pending plan.",
                "",
                "GDS u/v exists only after a GDS point is selected.",
                "Current GDS u/v exists only after GDS-to-stage binding is fitted.",
                "Stage physical coordinates are derived from controller pulses and motor config.",
            ]
            if context.config_summary:
                lines.append("")
                lines.extend(f"{key}: {value}" for key, value in context.config_summary.items())
            return "\n".join(lines)
        return self._format_plan(plan)

    @staticmethod
    def _format_plan(plan: AgentPlan) -> str:
        lines = [
            plan.title,
            "",
            f"Understanding: {plan.understanding}",
            f"Planner: {plan.planner_source}",
            f"Needs confirmation: {'yes' if plan.requires_confirmation else 'no'}",
            f"Executable: {'yes' if plan.executable else 'no'}",
            "",
            "Steps:",
        ]
        for index, step in enumerate(plan.steps, start=1):
            flags = []
            if step.involves_motion:
                flags.append("motion")
            if step.involves_autofocus:
                flags.append("autofocus")
            if step.involves_capture:
                flags.append("capture")
            flag_text = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"{index}. {step.title} - {step.module}{flag_text}")
            lines.append(f"   {step.detail}")
        if plan.risks:
            lines.extend(("", "Risks:"))
            lines.extend(f"- {risk}" for risk in plan.risks)
        if plan.blockers:
            lines.extend(("", "Blockers:"))
            lines.extend(f"- {blocker}" for blocker in plan.blockers)
        if plan.recovery_suggestions:
            lines.extend(("", "Recovery:"))
            lines.extend(f"- {suggestion}" for suggestion in plan.recovery_suggestions)
        return "\n".join(lines)

    @staticmethod
    def _format_chat_plan(plan: AgentPlan) -> str:
        if plan.blockers:
            return f"{plan.title}\nBlocked: " + "; ".join(plan.blockers)
        return f"{plan.title}\n{plan.understanding}"

    @staticmethod
    def _format_agent_status(plan: AgentPlan) -> str:
        if plan.executable and plan.requires_confirmation:
            return "Waiting for user confirmation."
        if plan.blockers:
            return "Blocked:\n" + "\n".join(f"- {blocker}" for blocker in plan.blockers)
        return plan.understanding
