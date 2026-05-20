from __future__ import annotations

import re
import math
import threading
import tkinter as tk
from collections.abc import Callable
from tkinter import ttk

from ..agent import (
    AgentContext,
    AgentPlan,
    VISUALIZATION_AUTOFOCUS,
    VISUALIZATION_DEFAULT,
    VISUALIZATION_FOCUSMAP,
    VISUALIZATION_IMGSTITCH,
    VISUALIZATION_LAYOUT,
)


class AgentPanel:
    MODE_CHAT = "Conversation Only"
    MODE_STEP = "Authorized Step"
    MODE_AUTO = "Auto Step"

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
        draw_autofocus_plot: Callable[[tk.Canvas], None] | None = None,
    ) -> None:
        self.parent = parent
        self.colors = colors
        self.get_context = get_context
        self.get_microscope_preview = get_microscope_preview
        self.draw_autofocus_plot = draw_autofocus_plot
        self.plan_instruction = plan_instruction
        self.execute_plan = execute_plan
        self.cancel_plan = cancel_plan
        self.stop_task = stop_task
        self.pending_plan: AgentPlan | None = None
        self.planning_thread: threading.Thread | None = None
        self.microscope_image: tk.PhotoImage | None = None
        self.phase_var = tk.StringVar(value="Idle")
        self.status_var = tk.StringVar(value="Ready")
        self.history_keep_var = tk.StringVar(value="12")
        self.permission_mode_var = tk.StringVar(value=self.MODE_STEP)
        self.total_token_usage = 0
        self.archived_message_count = 0
        self.chat_messages: list[tuple[str, str, bool]] = []
        self.task_hint = VISUALIZATION_DEFAULT

        parent.columnconfigure(0, weight=1)
        parent.rowconfigure(0, weight=1)

        shell = ttk.Frame(parent, style="App.TFrame", padding=10)
        shell.grid(row=0, column=0, sticky="nsew")
        shell.columnconfigure(0, weight=0, minsize=350)
        shell.columnconfigure(1, weight=1, minsize=560)
        shell.columnconfigure(2, weight=0, minsize=300)
        shell.rowconfigure(1, weight=1)

        header = ttk.Frame(shell, style="App.TFrame")
        header.grid(row=0, column=0, columnspan=3, sticky="ew", pady=(0, 10))
        header.columnconfigure(0, weight=1)
        ttk.Label(header, text="AI Agent", style="Title.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(header, text="Natural-language planning with step-by-step confirmation", style="Subtitle.TLabel").grid(row=0, column=1, sticky="e")

        self._build_left_column(shell)
        self._build_center_column(shell)
        self._build_right_column(shell)

        self._append_chat(
            "Agent",
            "Ready. Describe an experiment workflow in natural language. I will plan with the current software state and ask before each hardware or state-changing step.",
        )
        self.refresh_context()
        self._update_microscope_preview()

    def _build_left_column(self, shell: ttk.Frame) -> None:
        left = ttk.Frame(shell, style="App.TFrame", width=350)
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 10))
        left.grid_propagate(False)
        left.columnconfigure(0, weight=1)
        left.rowconfigure(0, weight=3)
        left.rowconfigure(1, weight=2)
        left.rowconfigure(2, weight=2)

        microscope_card = self._section(left, "Microscope")
        microscope_card.grid(row=0, column=0, sticky="nsew")
        microscope_card.rowconfigure(1, weight=1)
        self.microscope_label = tk.Label(
            microscope_card,
            text="No microscope frame",
            anchor="center",
            bg="#05070a",
            fg=self.colors["muted"],
            font=("Segoe UI Semibold", 12),
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
        )
        self.microscope_label.grid(row=1, column=0, sticky="nsew")

        coordinate_card = self._section(left, "Stage XYZ / Layout UV")
        coordinate_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        coordinate_card.rowconfigure(1, weight=1)
        self.coord_value_labels = self._build_metric_grid(
            coordinate_card,
            (
                ("stage_x", "Stage X"),
                ("stage_y", "Stage Y"),
                ("stage_z", "Stage Z"),
                ("gds_u", "GDS U"),
                ("gds_v", "GDS V"),
            ),
            row=1,
        )

        status_card = self._section(left, "Agent Status")
        status_card.grid(row=2, column=0, sticky="nsew", pady=(10, 0))
        status_card.rowconfigure(1, weight=1)
        self.agent_state_labels = self._build_metric_grid(
            status_card,
            (
                ("phase", "Phase"),
                ("permission", "Mode"),
                ("serial", "Serial"),
                ("camera", "Camera"),
                ("motion", "Motion"),
                ("api", "API"),
                ("last_tokens", "Last Tokens"),
                ("total_tokens", "Total Tokens"),
            ),
            row=1,
        )

    def _build_center_column(self, shell: ttk.Frame) -> None:
        center = ttk.Frame(shell, style="App.TFrame")
        center.grid(row=1, column=1, sticky="nsew", padx=(0, 10))
        center.columnconfigure(0, weight=1)
        center.rowconfigure(0, weight=1)
        center.rowconfigure(1, weight=0)

        chat_card = self._section(center, "Conversation")
        chat_card.grid(row=0, column=0, sticky="nsew")
        chat_card.rowconfigure(1, weight=1)
        chat_card.rowconfigure(2, weight=0)
        self.chat_text = tk.Text(
            chat_card,
            wrap="word",
            bg=self.colors["bg"],
            fg=self.colors["text"],
            bd=0,
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            font=("Segoe UI", 10),
            padx=10,
            pady=10,
        )
        self.chat_text.grid(row=1, column=0, sticky="nsew")
        chat_scrollbar = ttk.Scrollbar(chat_card, orient=tk.VERTICAL, command=self.chat_text.yview)
        chat_scrollbar.grid(row=1, column=1, sticky="ns")
        self.chat_text.configure(yscrollcommand=chat_scrollbar.set, state="disabled")
        self._configure_chat_tags()

        self.chat_action_frame = ttk.Frame(chat_card, style="Panel.TFrame")
        self.chat_action_frame.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(8, 0))
        self.chat_action_frame.columnconfigure(0, weight=1)
        self.permission_mode_combo = ttk.Combobox(
            self.chat_action_frame,
            textvariable=self.permission_mode_var,
            values=(self.MODE_CHAT, self.MODE_STEP, self.MODE_AUTO),
            state="readonly",
            width=18,
        )
        self.permission_mode_combo.grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.permission_mode_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_permission_mode_changed())
        self.confirm_button = ttk.Button(self.chat_action_frame, text="Run Next Step", style="Accent.TButton", command=self.confirm_next_step, state="disabled")
        self.confirm_button.grid(row=0, column=1, sticky="e", padx=(0, 6))
        ttk.Button(self.chat_action_frame, text="Cancel Plan", command=self.cancel_pending_plan).grid(row=0, column=2, sticky="e")

        composer = ttk.Frame(center, style="Panel.TFrame", padding=10)
        composer.grid(row=1, column=0, sticky="ew", pady=(10, 0))
        composer.columnconfigure(0, weight=1)
        composer.columnconfigure(1, weight=0)
        self.input_text = tk.Text(
            composer,
            height=3,
            wrap="word",
            bg=self.colors.get("input", self.colors["surface_2"]),
            fg=self.colors["text"],
            insertbackground=self.colors["accent"],
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            bd=0,
            highlightthickness=2,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors.get("border_focus", self.colors["accent"]),
            font=("Segoe UI", 11),
            padx=12,
            pady=9,
        )
        self.input_text.grid(row=0, column=0, columnspan=6, sticky="ew")
        self.input_text.insert("1.0", "Run autofocus, then capture and save one image")
        self.input_text.bind("<Control-Return>", lambda _event: self.generate_plan())

        self.generate_button = ttk.Button(composer, text="Send", style="Accent.TButton", command=self.generate_plan)
        self.generate_button.grid(row=1, column=0, sticky="ew", pady=(8, 0), padx=(0, 6))
        ttk.Button(composer, text="Reset Context", command=self.reset_context_marker).grid(row=1, column=1, sticky="ew", pady=(8, 0), padx=(0, 6))
        ttk.Button(composer, text="Stop", style="Danger.TButton", command=self.stop_current_task).grid(row=1, column=2, sticky="ew", pady=(8, 0), padx=(0, 6))
        keep_frame = ttk.Frame(composer, style="Panel.TFrame")
        keep_frame.grid(row=1, column=5, sticky="e", pady=(8, 0))
        ttk.Label(keep_frame, text="Keep", style="Muted.TLabel").grid(row=0, column=0, sticky="e", padx=(0, 4))
        self._small_entry(keep_frame, self.history_keep_var, width=4).grid(row=0, column=1, sticky="e")

    def _build_right_column(self, shell: ttk.Frame) -> None:
        right = ttk.Frame(shell, style="App.TFrame")
        right.grid(row=1, column=2, sticky="nsew")
        right.columnconfigure(0, weight=1)
        right.rowconfigure(0, weight=3)
        right.rowconfigure(1, weight=2)

        task_card = self._section(right, "Task View")
        task_card.grid(row=0, column=0, sticky="nsew")
        task_card.rowconfigure(1, weight=1)
        self.task_canvas = tk.Canvas(task_card, bg="#05070a", highlightthickness=1, highlightbackground=self.colors["border"])
        self.task_canvas.grid(row=1, column=0, sticky="nsew")
        self.task_canvas.bind("<Configure>", lambda _event: self._draw_task_view())

        plan_card = self._section(right, "Plan / Results")
        plan_card.grid(row=1, column=0, sticky="nsew", pady=(10, 0))
        plan_card.rowconfigure(1, weight=1)
        self.plan_text = self._readonly_text(plan_card, height=12, font_size=10)
        self.plan_text.grid(row=1, column=0, sticky="nsew")
        ttk.Label(plan_card, textvariable=self.status_var, style="Status.TLabel", wraplength=260, padding=(8, 6)).grid(row=2, column=0, sticky="ew", pady=(8, 0))

    def _section(self, parent: tk.Widget, title: str) -> ttk.Frame:
        frame = ttk.Frame(parent, style="Panel.TFrame", padding=10)
        frame.columnconfigure(0, weight=1)
        ttk.Label(frame, text=title.upper(), style="Section.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        return frame

    def _build_metric_grid(self, parent: tk.Widget, fields: tuple[tuple[str, str], ...], *, row: int) -> dict[str, tk.Label]:
        frame = ttk.Frame(parent, style="Panel.TFrame")
        frame.grid(row=row, column=0, sticky="nsew")
        frame.columnconfigure(0, weight=1)
        frame.columnconfigure(1, weight=1)
        labels: dict[str, tk.Label] = {}
        for index, (key, label_text) in enumerate(fields):
            label = tk.Label(
                frame,
                text=label_text,
                anchor="w",
                padx=7,
                pady=4,
                bg=self.colors["surface"],
                fg=self.colors["muted"],
                font=("Segoe UI", 8),
            )
            label.grid(row=index, column=0, sticky="ew", pady=(0, 4), padx=(0, 4))
            value = tk.Label(
                frame,
                text="-",
                anchor="e",
                padx=7,
                pady=4,
                bg=self.colors["surface_2"],
                fg=self.colors["accent"],
                font=("Cascadia Mono", 8),
            )
            value.grid(row=index, column=1, sticky="ew", pady=(0, 4))
            labels[key] = value
        return labels

    def _set_metric(self, labels: dict[str, tk.Label], key: str, value: str, *, available: bool = True, tone: str | None = None) -> None:
        label = labels.get(key)
        if label is None:
            return
        color = self.colors["muted"] if not available else self.colors["accent"]
        if tone == "warning":
            color = self.colors["warning"]
        elif tone == "danger":
            color = self.colors["danger"]
        elif tone == "blue":
            color = self.colors["blue"]
        label.configure(text=value, fg=color)

    def _small_entry(self, parent: tk.Widget, variable: tk.StringVar, width: int) -> ttk.Entry:
        return ttk.Entry(parent, textvariable=variable, width=width, justify="center")

    def _readonly_text(self, parent: tk.Widget, *, height: int, font_size: int, mono: bool = False) -> tk.Text:
        widget = tk.Text(
            parent,
            height=height,
            wrap="word",
            bg=self.colors.get("input", self.colors["surface_2"]),
            fg=self.colors["text"],
            bd=0,
            selectbackground="#0e7490",
            selectforeground="#f8fafc",
            highlightthickness=1,
            highlightbackground=self.colors["border"],
            highlightcolor=self.colors.get("border_focus", self.colors["accent"]),
            font=("Cascadia Mono" if mono else "Segoe UI", font_size),
            padx=10,
            pady=8,
        )
        widget.configure(state="disabled")
        return widget

    def _configure_chat_tags(self) -> None:
        self.chat_text.tag_configure("role_agent", foreground=self.colors["accent"], font=("Segoe UI Semibold", 9), spacing1=8)
        self.chat_text.tag_configure("role_user", foreground=self.colors["blue"], font=("Segoe UI Semibold", 9), justify="right", spacing1=8)
        self.chat_text.tag_configure("agent_bubble", background=self.colors["surface"], lmargin1=12, lmargin2=12, rmargin=72, spacing1=2, spacing3=8)
        self.chat_text.tag_configure("user_bubble", background=self.colors["surface_3"], lmargin1=72, lmargin2=72, rmargin=12, justify="right", spacing1=2, spacing3=8)
        self.chat_text.tag_configure("divider", foreground=self.colors["muted"], justify="center", spacing1=10, spacing3=10)
        self.chat_text.tag_configure("md_h1", foreground=self.colors["text"], font=("Segoe UI Semibold", 15))
        self.chat_text.tag_configure("md_h2", foreground=self.colors["text"], font=("Segoe UI Semibold", 13))
        self.chat_text.tag_configure("md_bold", foreground="#f8fafc", font=("Segoe UI Semibold", 10))
        self.chat_text.tag_configure("md_code", background="#071018", foreground="#c4b5fd", font=("Cascadia Mono", 9), lmargin1=18, lmargin2=18)
        self.chat_text.tag_configure("md_table", font=("Cascadia Mono", 9), foreground="#cbd5e1")
        self.chat_text.tag_configure("md_list", lmargin1=22, lmargin2=36)

    @staticmethod
    def _replace_text(widget: tk.Text, text: str) -> None:
        widget.configure(state="normal")
        widget.delete("1.0", "end")
        widget.insert("1.0", text)
        widget.configure(state="disabled")

    def _append_chat(self, role: str, text: str, *, archived: bool = False) -> None:
        if archived:
            self.chat_messages.append(("divider", text, True))
        else:
            self.chat_messages.append((role, text, False))
        self._append_chat_render(role, text, archived=archived)

    def _append_chat_render(self, role: str, text: str, *, archived: bool = False) -> None:
        self.chat_text.configure(state="normal")
        if self.chat_text.index("end-1c") != "1.0":
            self.chat_text.insert("end", "\n")
        if archived:
            self.chat_text.insert("end", f"\n{text}\n", ("divider",))
        elif role == "User":
            self.chat_text.insert("end", "You\n", ("role_user",))
            self._insert_markdown(text, base_tag="user_bubble")
        else:
            self.chat_text.insert("end", "AI Agent\n", ("role_agent",))
            self._insert_markdown(text, base_tag="agent_bubble")
        self.chat_text.see("end")
        self.chat_text.configure(state="disabled")

    def _insert_markdown(self, text: str, *, base_tag: str) -> None:
        in_code = False
        for raw_line in text.splitlines() or [""]:
            line = raw_line.rstrip()
            if line.strip().startswith("```"):
                in_code = not in_code
                continue
            tags: tuple[str, ...] = (base_tag,)
            render = line
            if in_code:
                tags = (base_tag, "md_code")
            elif line.startswith("## "):
                tags = (base_tag, "md_h2")
                render = line[3:]
            elif line.startswith("# "):
                tags = (base_tag, "md_h1")
                render = line[2:]
            elif line.startswith(("- ", "* ")):
                tags = (base_tag, "md_list")
                render = "- " + line[2:]
            elif line.startswith("|"):
                tags = (base_tag, "md_table")
            if "**" in render and not in_code:
                self._insert_bold_line(render, base_tag, tags)
            else:
                self.chat_text.insert("end", render + "\n", tags)

    def _insert_bold_line(self, line: str, base_tag: str, tags: tuple[str, ...]) -> None:
        parts = re.split(r"(\*\*[^*]+\*\*)", line)
        for part in parts:
            if part.startswith("**") and part.endswith("**"):
                self.chat_text.insert("end", part[2:-2], tuple(dict.fromkeys((*tags, "md_bold"))))
            else:
                self.chat_text.insert("end", part, tags)
        self.chat_text.insert("end", "\n", (base_tag,))

    def reset_context_marker(self) -> None:
        keep = self._history_keep_count()
        self.archived_message_count = max(0, len([item for item in self.chat_messages if item[0] != "divider"]) - keep)
        self._append_chat("divider", f"History archived here. Keeping the last {keep} message(s) for future planning.", archived=True)
        self.status_var.set(f"Context reset marker inserted; keeping last {keep} messages.")

    def _history_keep_count(self) -> int:
        try:
            return max(0, min(50, int(float(self.history_keep_var.get()))))
        except (TypeError, ValueError, tk.TclError):
            self.history_keep_var.set("12")
            return 12

    def refresh_context(self) -> None:
        context = self.get_context()
        self._update_coordinate_metrics(context)
        self._update_agent_state_metrics(context)
        if self.pending_plan is None:
            self._replace_text(self.plan_text, self._format_idle_plan(context))
        self._draw_task_view()

    def _update_coordinate_metrics(self, context: AgentContext) -> None:
        stage_um = context.stage_position_um
        current_gds = context.current_mapped_gds_uv
        self._set_metric(self.coord_value_labels, "stage_x", f"{stage_um.get('X', 0.0):.3f}")
        self._set_metric(self.coord_value_labels, "stage_y", f"{stage_um.get('Y', 0.0):.3f}")
        self._set_metric(self.coord_value_labels, "stage_z", f"{stage_um.get('Z', 0.0):.3f}")
        self._set_metric(self.coord_value_labels, "gds_u", "-" if current_gds is None else f"{current_gds[0]:.3f}", available=current_gds is not None)
        self._set_metric(self.coord_value_labels, "gds_v", "-" if current_gds is None else f"{current_gds[1]:.3f}", available=current_gds is not None)

    def _update_agent_state_metrics(self, context: AgentContext) -> None:
        tokens = self.pending_plan.token_usage if self.pending_plan is not None else {}
        self._set_metric(self.agent_state_labels, "phase", self.phase_var.get(), tone="blue")
        self._set_metric(self.agent_state_labels, "permission", self._short_permission_mode())
        self._set_metric(self.agent_state_labels, "serial", "ready" if context.serial_connected else "off", tone=None if context.serial_connected else "warning")
        self._set_metric(
            self.agent_state_labels,
            "camera",
            "frame" if context.camera_frame_available else ("on" if context.camera_running else "off"),
            tone=None if context.camera_frame_available else "warning",
        )
        self._set_metric(self.agent_state_labels, "motion", "busy" if context.motion_busy or context.keyboard_motion_busy else "idle", tone="warning" if context.motion_busy or context.keyboard_motion_busy else None)
        self._set_metric(self.agent_state_labels, "api", "configured" if context.agent_api_configured else "fallback", tone=None if context.agent_api_configured else "warning")
        self._set_metric(self.agent_state_labels, "last_tokens", f"{tokens.get('total_tokens', '-')}" if tokens else "-", available=bool(tokens))
        self._set_metric(self.agent_state_labels, "total_tokens", str(self.total_token_usage), available=self.total_token_usage > 0)

    def _short_permission_mode(self) -> str:
        mode = self.permission_mode_var.get()
        if mode == self.MODE_CHAT:
            return "chat"
        if mode == self.MODE_AUTO:
            return "auto"
        return "step"

    def _update_microscope_preview(self) -> None:
        try:
            payload = self.get_microscope_preview()
            if payload:
                self.microscope_image = tk.PhotoImage(data=payload, format="PPM")
                self.microscope_label.configure(image=self.microscope_image, text="")
            else:
                self.microscope_label.configure(image="", text="No microscope frame")
        except tk.TclError:
            return
        self.parent.after(120, self._update_microscope_preview)

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
        self.phase_var.set("Planning")
        self.status_var.set("Collecting state and calling planner...")
        self._replace_text(self.plan_text, "Preparing capability brief and current software state.")
        planning_instruction = self._planning_instruction_with_history(instruction)
        self.planning_thread = threading.Thread(target=self._generate_plan_worker, args=(planning_instruction,), daemon=True)
        self.planning_thread.start()
        return "break"

    def _planning_instruction_with_history(self, instruction: str) -> str:
        keep = self._history_keep_count()
        retained = [(role, text) for role, text, archived in self.chat_messages if not archived and role in {"User", "Agent"}]
        retained = retained[-keep:] if keep else []
        if not retained:
            return instruction
        lines = ["## Retained conversation context"]
        for role, text in retained:
            compact = " ".join(text.strip().split())
            if compact:
                lines.append(f"- {role}: {compact[:800]}")
        lines.extend(["", "## Current request", instruction])
        return "\n".join(lines)

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
        self.phase_var.set("Blocked")
        self.status_var.set(message)
        self._replace_text(self.plan_text, message)
        self._append_chat("Agent", message)
        self.refresh_context()

    def _apply_generated_plan(self, plan: AgentPlan) -> None:
        self.pending_plan = plan if plan.executable else None
        self.task_hint = plan.visualization_hint
        self.total_token_usage += int(plan.token_usage.get("total_tokens", 0))
        self._replace_text(self.plan_text, self._format_plan(plan))
        self.confirm_button.configure(state="normal" if self._can_confirm_next_step(plan) else "disabled")
        self.generate_button.configure(state="normal")
        mode = self.permission_mode_var.get()
        if mode == self.MODE_CHAT:
            self.phase_var.set("Conversation only")
            self.status_var.set("Conversation-only mode: Agent will plan and explain, but not execute.")
        elif self.pending_plan is not None and mode == self.MODE_AUTO:
            self.phase_var.set("Auto stepping")
            self.status_var.set(self._status_for_plan(plan))
        else:
            self.phase_var.set("Waiting for confirmation" if self._can_confirm_next_step(plan) else ("Blocked" if plan.blockers or plan.needs_clarification else "Idle"))
            self.status_var.set(self._status_for_plan(plan))
        self._append_chat("Agent", plan.reply_markdown or self._format_chat_plan(plan))
        self.refresh_context()
        self._schedule_auto_advance()

    def _can_confirm_next_step(self, plan: AgentPlan | None) -> bool:
        if self.permission_mode_var.get() != self.MODE_STEP:
            return False
        if plan is None:
            return False
        index = plan.next_pending_step_index()
        if index is None:
            return False
        step = plan.steps[index]
        return step.requires_confirmation and not step.blockers

    def confirm_next_step(self) -> None:
        if self.permission_mode_var.get() == self.MODE_CHAT:
            self.status_var.set("Conversation-only mode: execution is disabled.")
            return
        if self.permission_mode_var.get() != self.MODE_STEP:
            self.status_var.set("Switch to authorized step mode for manual confirmation.")
            return
        self._run_next_step(auto=False)

    def _run_next_step(self, *, auto: bool) -> None:
        if auto and self.permission_mode_var.get() != self.MODE_AUTO:
            return
        if not auto and self.permission_mode_var.get() != self.MODE_STEP:
            self.status_var.set("Execution is not enabled in the current permission mode.")
            return
        if self.pending_plan is None:
            self.status_var.set("No executable Agent step is waiting for confirmation.")
            return
        step_index = self.pending_plan.next_pending_step_index()
        if step_index is None:
            self.status_var.set("No pending executable step remains.")
            self.confirm_button.configure(state="disabled")
            return
        step = self.pending_plan.steps[step_index]
        message = self.execute_plan(self.pending_plan)
        failed = "blocked" in message.lower() or "failed" in message.lower() or "not executable" in message.lower()
        immediate_done = any(token in message.lower() for token in ("captured", "associated", "summary refreshed", "skipped", "already at"))
        if failed:
            self.pending_plan = self.pending_plan.with_step_status(step_index, "blocked", message)
            self.phase_var.set("Blocked")
        elif immediate_done:
            self.pending_plan = self.pending_plan.with_step_status(step_index, "done", message)
            self.phase_var.set("Waiting for confirmation" if self.pending_plan.next_pending_step_index() is not None else "Done")
        else:
            self.pending_plan = self.pending_plan.with_step_status(step_index, "running", message)
            self.phase_var.set("Executing")
        self.task_hint = step.visualization_hint
        self.confirm_button.configure(state="normal" if self._can_confirm_next_step(self.pending_plan) else "disabled")
        self.status_var.set(message)
        self._replace_text(self.plan_text, self._format_plan(self.pending_plan))
        self._append_chat("Agent", message)
        self.refresh_context()
        if auto:
            self._schedule_auto_advance()

    def _schedule_auto_advance(self) -> None:
        if self.permission_mode_var.get() != self.MODE_AUTO or self.pending_plan is None:
            return
        running = any(step.status == "running" for step in self.pending_plan.steps)
        if running or self.pending_plan.next_pending_step_index() is None:
            return
        self.confirm_button.configure(state="disabled")
        self.parent.after(150, lambda: self._run_next_step(auto=True))

    def _on_permission_mode_changed(self) -> None:
        mode = self.permission_mode_var.get()
        if mode == self.MODE_CHAT:
            self.confirm_button.configure(state="disabled")
            self.phase_var.set("Conversation only")
            self.status_var.set("Conversation-only mode: Agent will plan and explain, but not execute.")
        elif mode == self.MODE_AUTO:
            self.confirm_button.configure(state="disabled")
            self.phase_var.set("Auto stepping")
            self.status_var.set("Auto step mode: each step still runs through local safety checks.")
            self._schedule_auto_advance()
        else:
            self.confirm_button.configure(state="normal" if self._can_confirm_next_step(self.pending_plan) else "disabled")
            self.phase_var.set("Waiting for confirmation" if self._can_confirm_next_step(self.pending_plan) else "Idle")
            self.status_var.set("Authorized step mode: confirm each executable step manually.")
        self.refresh_context()

    def cancel_pending_plan(self) -> None:
        self.pending_plan = None
        self.confirm_button.configure(state="disabled")
        message = self.cancel_plan()
        self.phase_var.set("Idle")
        self.status_var.set(message)
        self._replace_text(self.plan_text, message)
        self._append_chat("Agent", message)
        self.refresh_context()

    def stop_current_task(self) -> None:
        message = self.stop_task()
        if self.pending_plan is not None:
            running_index = next((index for index, step in enumerate(self.pending_plan.steps) if step.status == "running"), None)
            if running_index is not None:
                self.pending_plan = self.pending_plan.with_step_status(running_index, "blocked", message)
        self.confirm_button.configure(state="disabled")
        self.phase_var.set("Blocked")
        self.status_var.set(message)
        self._replace_text(self.plan_text, message if self.pending_plan is None else self._format_plan(self.pending_plan))
        self._append_chat("Agent", message)
        self.refresh_context()

    def set_status(self, message: str) -> None:
        self.status_var.set(message)
        lower = message.lower()
        if self.pending_plan is not None:
            running_index = next((index for index, step in enumerate(self.pending_plan.steps) if step.status == "running"), None)
            if running_index is not None:
                if any(token in lower for token in ("failed", "blocked", "stopped", "unavailable")):
                    self.pending_plan = self.pending_plan.with_step_status(running_index, "blocked", message)
                    self.phase_var.set("Blocked")
                elif any(token in lower for token in ("completed", "saved", "done", "arrived")):
                    self.pending_plan = self.pending_plan.with_step_status(running_index, "done", message)
                    self.phase_var.set("Waiting for confirmation" if self.pending_plan.next_pending_step_index() is not None else "Done")
                self.confirm_button.configure(state="normal" if self._can_confirm_next_step(self.pending_plan) else "disabled")
                self._replace_text(self.plan_text, self._format_plan(self.pending_plan))
                self._schedule_auto_advance()
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
                "Stage XYZ um",
                f"X {stage_um.get('X', 0.0):>12.3f}",
                f"Y {stage_um.get('Y', 0.0):>12.3f}",
                f"Z {stage_um.get('Z', 0.0):>12.3f}",
                "",
                "Controller pulses",
                f"X {context.positions.get('X', 0):>12}",
                f"Y {context.positions.get('Y', 0):>12}",
                f"Z {context.positions.get('Z', 0):>12}",
                "",
                "Layout UV current",
                "unavailable" if current_gds is None else f"U {current_gds[0]:>12.3f}\nV {current_gds[1]:>12.3f}",
                "",
                "Selected GDS UV",
                "unavailable" if selected_gds is None else f"U {selected_gds[0]:>12.3f}\nV {selected_gds[1]:>12.3f}",
                "",
                "Selected stage target",
                "unavailable" if target_stage is None else f"X {target_stage[0]:>12.3f}\nY {target_stage[1]:>12.3f}",
            ]
        )

    def _format_agent_status(self, context: AgentContext) -> str:
        tokens = self.pending_plan.token_usage if self.pending_plan is not None else {}
        token_text = f"{tokens.get('total_tokens', '-')}" if tokens else "-"
        return "\n".join(
            [
                f"Phase          {self.phase_var.get()}",
                f"Serial         {self._format_bool(context.serial_connected)}",
                f"Camera         {self._format_bool(context.camera_running)}",
                f"Frame          {self._format_bool(context.camera_frame_available)}",
                f"Motion         {'busy' if context.motion_busy or context.keyboard_motion_busy else 'idle'}",
                f"AutoFocus      {'running' if context.autofocus_running else 'idle'}",
                f"FocusMap       {'running' if context.focusmap_running else 'idle'}",
                f"ImgStitch      {'running' if context.imgstitch_running else 'idle'}",
                f"Model          {context.agent_model}",
                f"API            {'configured' if context.agent_api_configured else 'fallback'}",
                f"Tokens         {token_text}",
            ]
        )

    @staticmethod
    def _format_idle_plan(context: AgentContext) -> str:
        return "\n".join(
            [
                "No pending plan.",
                "",
                f"Current page: {context.current_page or '-'}",
                f"GDS binding: {'ready' if context.gds_mapping_ready else 'not bound'}",
                f"Last image: {context.last_stitch_path or '-'}",
                f"ImgStitch: {context.imgstitch_mode or '-'} / {context.imgstitch_tile_mode or '-'}",
                f"FocusMap: {context.focusmap_valid_points}/{context.focusmap_points} valid point(s)",
            ]
        )

    @staticmethod
    def _format_plan(plan: AgentPlan) -> str:
        next_index = plan.next_pending_step_index()
        next_step = plan.steps[next_index] if next_index is not None else None
        lines = [
            "Planning Results",
            "",
            f"Title: {plan.title}",
            f"Understanding: {plan.understanding}",
            f"Planner: {plan.planner_source}",
            f"Executable: {'yes' if plan.executable else 'no'}",
            f"Step confirmation: {'yes' if plan.requires_confirmation else 'no'}",
            f"Next step: {next_step.title if next_step is not None else '-'}",
            f"Visualization: {plan.visualization_hint}",
            "",
            "Step List:",
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
            lines.append(f"{index}. {step.status.upper()} | {step.title}{flag_text}")
            lines.append(f"   module: {step.module}")
            lines.append(f"   action: {step.action_id}")
            lines.append(f"   confirm: {'yes' if step.requires_confirmation else 'no'}")
            lines.append(f"   detail: {step.detail}")
            if step.blockers:
                lines.append("   blockers: " + "; ".join(step.blockers))
        if plan.blockers:
            lines.extend(("", "Plan blockers:"))
            lines.extend(f"- {blocker}" for blocker in plan.blockers)
        if plan.risks:
            lines.extend(("", "Risks:"))
            lines.extend(f"- {risk}" for risk in plan.risks)
        if plan.recovery_suggestions:
            lines.extend(("", "Recovery:"))
            lines.extend(f"- {suggestion}" for suggestion in plan.recovery_suggestions)
        if plan.token_usage:
            lines.extend(("", "Token usage:"))
            lines.extend(f"- {key}: {value}" for key, value in sorted(plan.token_usage.items()))
        return "\n".join(lines)

    @staticmethod
    def _format_chat_plan(plan: AgentPlan) -> str:
        if plan.blockers:
            return f"{plan.title}\nBlocked: " + "; ".join(plan.blockers)
        return plan.reply_markdown or f"{plan.title}\n{plan.understanding}"

    @staticmethod
    def _status_for_plan(plan: AgentPlan) -> str:
        if plan.executable and plan.next_pending_step_index() is not None:
            step = plan.steps[plan.next_pending_step_index() or 0]
            return f"Waiting to confirm: {step.title}"
        if plan.blockers:
            return "Blocked: " + "; ".join(plan.blockers)
        if plan.needs_clarification:
            return "Clarification needed."
        return "No executable step."

    def _draw_task_view(self) -> None:
        if not hasattr(self, "task_canvas"):
            return
        context = self.get_context()
        hint = self.task_hint
        if self.pending_plan is not None:
            running = next((step for step in self.pending_plan.steps if step.status == "running"), None)
            pending = self.pending_plan.steps[self.pending_plan.next_pending_step_index() or 0] if self.pending_plan.next_pending_step_index() is not None else None
            if running is not None:
                hint = running.visualization_hint
            elif pending is not None:
                hint = pending.visualization_hint
        canvas = self.task_canvas
        width = max(canvas.winfo_width(), 10)
        height = max(canvas.winfo_height(), 10)
        canvas.delete("all")
        canvas.create_rectangle(0, 0, width, height, fill="#05070a", outline="")
        if hint == VISUALIZATION_AUTOFOCUS:
            self._draw_autofocus_task(canvas, width, height, context)
        elif hint == VISUALIZATION_IMGSTITCH:
            self._draw_imgstitch_task(canvas, width, height, context)
        elif hint == VISUALIZATION_FOCUSMAP:
            self._draw_focusmap_task(canvas, width, height, context)
        elif hint == VISUALIZATION_LAYOUT:
            self._draw_layout_task(canvas, width, height, context)
        else:
            self._draw_summary_task(canvas, width, height, context)

    def _draw_panel_title(self, canvas: tk.Canvas, title: str, subtitle: str = "") -> None:
        canvas.create_text(14, 12, text=title, anchor="nw", fill=self.colors["text"], font=("Segoe UI Semibold", 12))
        if subtitle:
            canvas.create_text(14, 34, text=subtitle, anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))

    def _draw_autofocus_task(self, canvas: tk.Canvas, width: int, height: int, context: AgentContext) -> None:
        if self.draw_autofocus_plot is not None:
            try:
                self.draw_autofocus_plot(canvas)
            except tk.TclError:
                return
            return
        self._draw_panel_title(canvas, "AutoFocus", f"Metric {context.focus_metric or '-'} | samples {context.focus_sample_count or '-'}")
        canvas.create_text(width // 2, height // 2, text="No AF-Z samples available", fill=self.colors["muted"], font=("Segoe UI Semibold", 11))

    def _draw_imgstitch_task(self, canvas: tk.Canvas, width: int, height: int, context: AgentContext) -> None:
        self._draw_panel_title(canvas, "ImgStitch / Capture", f"{context.imgstitch_mode or '-'} | {context.imgstitch_tile_mode or '-'}")
        grid_left, grid_top = 30, 76
        cell = max(22, min((width - 70) // 4, (height - 130) // 3))
        rows = max(1, min(context.imgstitch_rows or 3, 4))
        cols = max(1, min(context.imgstitch_cols or 3, 4))
        for row in range(rows):
            column_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
            for col in column_range:
                x = grid_left + col * (cell + 8)
                y = grid_top + row * (cell + 8)
                fill = "#12352d" if (row + col) % 2 == 0 else self.colors["surface_2"]
                canvas.create_rectangle(x, y, x + cell, y + cell, fill=fill, outline=self.colors["border"])
        canvas.create_text(14, height - 42, text=f"Last output: {context.last_stitch_path or '-'}", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9), width=width - 28)

    def _draw_focusmap_task(self, canvas: tk.Canvas, width: int, height: int, context: AgentContext) -> None:
        self._draw_panel_title(canvas, "FocusMap", f"{context.focusmap_valid_points}/{context.focusmap_points} measured | plane {'stored' if context.focusmap_has_plane else 'not stored'}")
        cx, cy = width / 2, height / 2 + 16
        radius = max(34, min(width, height) * 0.28)
        count = max(context.focusmap_points, 9)
        for index in range(min(count, 25)):
            angle = index / max(count, 1) * 6.283
            ring = 0.38 + (index % 3) * 0.22
            x = cx + radius * ring * math.cos(angle)
            y = cy + radius * ring * math.sin(angle)
            color = self.colors["accent"] if index < context.focusmap_valid_points else self.colors["muted"]
            canvas.create_oval(x - 4, y - 4, x + 4, y + 4, fill=color, outline="")
        canvas.create_polygon(cx - radius, cy + radius * 0.5, cx + radius * 0.88, cy + radius * 0.2, cx + radius * 0.35, cy - radius * 0.62, fill="#082f49", outline="#38bdf8", stipple="gray25")

    def _draw_layout_task(self, canvas: tk.Canvas, width: int, height: int, context: AgentContext) -> None:
        self._draw_panel_title(canvas, "LayoutBond", "GDS target and mapped field of view")
        center_x, center_y = width / 2, height / 2 + 8
        canvas.create_rectangle(24, 70, width - 24, height - 56, fill="#071018", outline=self.colors["border"])
        for offset in range(-3, 4):
            x = center_x + offset * 28
            canvas.create_line(x, 78, x, height - 64, fill="#123044")
            y = center_y + offset * 22
            canvas.create_line(32, y, width - 32, y, fill="#123044")
        canvas.create_rectangle(center_x - 42, center_y - 30, center_x + 42, center_y + 30, outline=self.colors["accent"], width=2)
        if context.gds_target_selected:
            canvas.create_oval(center_x - 5, center_y - 5, center_x + 5, center_y + 5, fill=self.colors["warning"], outline="")
        canvas.create_text(14, height - 42, text=f"Binding: {'ready' if context.gds_mapping_ready else 'not ready'} | Target: {self._pair_text(context.gds_target_stage_um)}", anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9), width=width - 28)

    def _draw_summary_task(self, canvas: tk.Canvas, width: int, height: int, context: AgentContext) -> None:
        self._draw_panel_title(canvas, "Workflow Summary", "Idle task visualization")
        lines = [
            ("Serial", "connected" if context.serial_connected else "not connected", self.colors["accent"] if context.serial_connected else self.colors["warning"]),
            ("Camera", "frame ready" if context.camera_frame_available else "waiting", self.colors["accent"] if context.camera_frame_available else self.colors["warning"]),
            ("GDS", "bound" if context.gds_mapping_ready else "not bound", self.colors["accent"] if context.gds_mapping_ready else self.colors["muted"]),
            ("Image", context.last_stitch_path or "no recent output", self.colors["muted"]),
        ]
        y = 72
        for label, value, color in lines:
            canvas.create_text(18, y, text=label, anchor="nw", fill=self.colors["muted"], font=("Segoe UI", 9))
            canvas.create_text(94, y, text=value, anchor="nw", fill=color, font=("Segoe UI Semibold", 9), width=width - 108)
            y += 34

    @staticmethod
    def _pair_text(value: tuple[float, float] | None) -> str:
        if value is None:
            return "-"
        return f"{value[0]:.4g}, {value[1]:.4g}"
