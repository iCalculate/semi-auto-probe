from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AGENT_ACTION_MOVE_GDS = "move_gds_target"
AGENT_ACTION_STAGE_MOVE = "stage_move"
AGENT_ACTION_AUTOFOCUS = "autofocus_current_position"
AGENT_ACTION_IMAGE_CAPTURE = "image_capture_sequence"
AGENT_ACTION_SINGLE_CAPTURE = "capture_single_frame"
AGENT_ACTION_FOCUSMAP = "focusmap_current_settings"
AGENT_ACTION_LAYOUT_OVERLAY = "layout_image_overlay"
AGENT_ACTION_STATUS = "status_summary"
AGENT_ACTION_CLARIFY = "clarify"

AGENT_ACTIONS = {
    AGENT_ACTION_MOVE_GDS,
    AGENT_ACTION_STAGE_MOVE,
    AGENT_ACTION_AUTOFOCUS,
    AGENT_ACTION_IMAGE_CAPTURE,
    AGENT_ACTION_SINGLE_CAPTURE,
    AGENT_ACTION_FOCUSMAP,
    AGENT_ACTION_LAYOUT_OVERLAY,
    AGENT_ACTION_STATUS,
    AGENT_ACTION_CLARIFY,
}

AGENT_API_KEY_ENV = "SEMI_AUTO_PROBE_AGENT_API_KEY"
AGENT_BASE_URL_ENV = "SEMI_AUTO_PROBE_AGENT_BASE_URL"
AGENT_MODEL_ENV = "SEMI_AUTO_PROBE_AGENT_MODEL"
AGENT_TIMEOUT_ENV = "SEMI_AUTO_PROBE_AGENT_TIMEOUT_SECONDS"
DEFAULT_AGENT_BASE_URL = "https://api.deepseek.com"
DEFAULT_AGENT_MODEL = "deepseek-chat"

VISUALIZATION_DEFAULT = "summary"
VISUALIZATION_AUTOFOCUS = "autofocus"
VISUALIZATION_IMGSTITCH = "imgstitch"
VISUALIZATION_FOCUSMAP = "focusmap"
VISUALIZATION_LAYOUT = "layoutbond"


@dataclass(frozen=True)
class AgentContext:
    positions: dict[str, int]
    serial_connected: bool
    motion_busy: bool
    keyboard_motion_busy: bool
    position_read_pending: bool
    camera_running: bool
    camera_frame_available: bool
    autofocus_running: bool
    focusmap_running: bool
    imgstitch_running: bool
    gds_target_selected: bool
    gds_mapping_ready: bool
    gds_target_stage_um: tuple[float, float] | None
    stage_position_um: dict[str, float] = field(default_factory=dict)
    gds_selected_uv: tuple[float, float] | None = None
    current_mapped_gds_uv: tuple[float, float] | None = None
    last_stitch_path: str | None = None
    current_page: str = ""
    config_summary: dict[str, str] = field(default_factory=dict)
    agent_model: str = DEFAULT_AGENT_MODEL
    agent_base_url: str = DEFAULT_AGENT_BASE_URL
    agent_api_configured: bool = False
    focus_metric: str = ""
    focus_sample_count: int = 0
    imgstitch_mode: str = ""
    imgstitch_tile_mode: str = ""
    imgstitch_rows: int | None = None
    imgstitch_cols: int | None = None
    focusmap_points: int = 0
    focusmap_valid_points: int = 0
    focusmap_has_plane: bool = False

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "positions": self.positions,
            "serial_connected": self.serial_connected,
            "motion_busy": self.motion_busy,
            "keyboard_motion_busy": self.keyboard_motion_busy,
            "position_read_pending": self.position_read_pending,
            "camera_running": self.camera_running,
            "camera_frame_available": self.camera_frame_available,
            "autofocus_running": self.autofocus_running,
            "focusmap_running": self.focusmap_running,
            "imgstitch_running": self.imgstitch_running,
            "gds_target_selected": self.gds_target_selected,
            "gds_mapping_ready": self.gds_mapping_ready,
            "stage_position_um": self.stage_position_um,
            "gds_selected_uv": self.gds_selected_uv,
            "current_mapped_gds_uv": self.current_mapped_gds_uv,
            "gds_target_stage_um": self.gds_target_stage_um,
            "last_stitch_path": self.last_stitch_path,
            "current_page": self.current_page,
            "config_summary": self.config_summary,
            "agent_model": self.agent_model,
            "agent_base_url": self.agent_base_url,
            "agent_api_configured": self.agent_api_configured,
            "focus_metric": self.focus_metric,
            "focus_sample_count": self.focus_sample_count,
            "imgstitch_mode": self.imgstitch_mode,
            "imgstitch_tile_mode": self.imgstitch_tile_mode,
            "imgstitch_rows": self.imgstitch_rows,
            "imgstitch_cols": self.imgstitch_cols,
            "focusmap_points": self.focusmap_points,
            "focusmap_valid_points": self.focusmap_valid_points,
            "focusmap_has_plane": self.focusmap_has_plane,
        }


@dataclass(frozen=True)
class AgentCapability:
    action_id: str
    title: str
    module: str
    purpose: str
    prerequisites: tuple[str, ...]
    hardware_effects: tuple[str, ...] = ()
    requires_confirmation: bool = True
    visualization_hint: str = VISUALIZATION_DEFAULT
    available: bool = True
    blockers: tuple[str, ...] = ()

    def to_prompt_dict(self) -> dict[str, object]:
        return {
            "action_id": self.action_id,
            "title": self.title,
            "module": self.module,
            "purpose": self.purpose,
            "prerequisites": list(self.prerequisites),
            "hardware_effects": list(self.hardware_effects),
            "requires_confirmation": self.requires_confirmation,
            "visualization_hint": self.visualization_hint,
            "available": self.available,
            "blockers": list(self.blockers),
        }


@dataclass(frozen=True)
class AgentWorkflowStep:
    action_id: str
    title: str
    module: str
    detail: str
    parameters: dict[str, object] = field(default_factory=dict)
    requires_confirmation: bool = True
    involves_motion: bool = False
    involves_autofocus: bool = False
    involves_capture: bool = False
    changes_experiment_state: bool = False
    visualization_hint: str = VISUALIZATION_DEFAULT
    risks: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    recovery_suggestions: tuple[str, ...] = ()
    status: str = "pending"

    @property
    def executable(self) -> bool:
        return self.action_id not in {AGENT_ACTION_CLARIFY} and not self.blockers


@dataclass(frozen=True)
class AgentPlan:
    title: str
    understanding: str
    reply_markdown: str
    steps: tuple[AgentWorkflowStep, ...]
    needs_clarification: bool = False
    blockers: tuple[str, ...] = ()
    recovery_suggestions: tuple[str, ...] = ()
    planner_source: str = "rules"
    token_usage: dict[str, int] = field(default_factory=dict)
    visualization_hint: str = VISUALIZATION_DEFAULT
    capabilities_markdown: str = ""

    @property
    def action(self) -> str:
        for step in self.steps:
            if step.action_id != AGENT_ACTION_STATUS:
                return step.action_id
        if self.steps:
            return self.steps[0].action_id
        return AGENT_ACTION_CLARIFY

    @property
    def requires_confirmation(self) -> bool:
        return any(step.requires_confirmation for step in self.steps)

    @property
    def involves_motion(self) -> bool:
        return any(step.involves_motion for step in self.steps)

    @property
    def involves_autofocus(self) -> bool:
        return any(step.involves_autofocus for step in self.steps)

    @property
    def involves_capture(self) -> bool:
        return any(step.involves_capture for step in self.steps)

    @property
    def risks(self) -> tuple[str, ...]:
        values: list[str] = []
        for step in self.steps:
            for risk in step.risks:
                if risk not in values:
                    values.append(risk)
        return tuple(values)

    @property
    def executable(self) -> bool:
        return not self.needs_clarification and not self.blockers and self.next_pending_step_index() is not None

    def next_pending_step_index(self) -> int | None:
        for index, step in enumerate(self.steps):
            if step.status == "pending":
                return index if step.executable else None
        return None

    def with_step_status(self, index: int, status: str, message: str | None = None) -> "AgentPlan":
        steps = list(self.steps)
        step = steps[index]
        detail = step.detail if message is None else f"{step.detail}\n{message}"
        steps[index] = replace(step, status=status, detail=detail)
        return replace(self, steps=tuple(steps))


class LLMPlanner(Protocol):
    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        ...


def build_agent_planner_from_environment(spec_path: Path | None = None) -> "AgentPlanner":
    llm_planner = OpenAICompatibleLLMPlanner.from_environment(spec_path=spec_path)
    return AgentPlanner(llm_planner=llm_planner)


def build_agent_planner_from_config(
    *,
    api_key: str,
    model: str,
    base_url: str,
    timeout_seconds: float,
    spec_path: Path | None = None,
) -> "AgentPlanner":
    if api_key.strip():
        return AgentPlanner(
            llm_planner=OpenAICompatibleLLMPlanner(
                api_key=api_key.strip(),
                model=model.strip() or DEFAULT_AGENT_MODEL,
                base_url=(base_url.strip() or DEFAULT_AGENT_BASE_URL),
                timeout_seconds=timeout_seconds,
                spec_path=spec_path,
            )
        )
    return build_agent_planner_from_environment(spec_path=spec_path)


def context_blockers_for_action(action: str, context: AgentContext) -> tuple[str, ...]:
    blockers: list[str] = []
    motion_busy = context.motion_busy or context.keyboard_motion_busy
    needs_idle_hardware = {
        AGENT_ACTION_MOVE_GDS,
        AGENT_ACTION_STAGE_MOVE,
        AGENT_ACTION_AUTOFOCUS,
        AGENT_ACTION_IMAGE_CAPTURE,
        AGENT_ACTION_SINGLE_CAPTURE,
        AGENT_ACTION_FOCUSMAP,
    }
    if action in needs_idle_hardware:
        if motion_busy:
            blockers.append("Motion is busy.")
        if context.position_read_pending:
            blockers.append("Position read is pending.")
        if context.autofocus_running:
            blockers.append("AutoFocus is already running.")
        if context.focusmap_running:
            blockers.append("FocusMap is already running.")
        if context.imgstitch_running:
            blockers.append("ImgStitch is already running.")
        if not context.serial_connected and action != AGENT_ACTION_SINGLE_CAPTURE:
            blockers.append("Serial port is not connected.")
    elif action == AGENT_ACTION_LAYOUT_OVERLAY:
        if motion_busy or context.position_read_pending:
            blockers.append("Motion or position read is busy.")
        if context.autofocus_running:
            blockers.append("AutoFocus is already running.")
        if context.focusmap_running:
            blockers.append("FocusMap is already running.")
        if context.imgstitch_running:
            blockers.append("ImgStitch is already running.")

    if action == AGENT_ACTION_MOVE_GDS:
        if not context.gds_target_selected or context.gds_target_stage_um is None:
            blockers.append("No GDS target is selected.")
        if not context.gds_mapping_ready:
            blockers.append("GDS-to-stage mapping is not ready.")
    elif action == AGENT_ACTION_STAGE_MOVE:
        pass
    elif action in {AGENT_ACTION_AUTOFOCUS, AGENT_ACTION_IMAGE_CAPTURE, AGENT_ACTION_SINGLE_CAPTURE}:
        if not context.camera_running or not context.camera_frame_available:
            blockers.append("Camera frame is not available.")
    elif action == AGENT_ACTION_FOCUSMAP:
        if context.focusmap_points < 1:
            blockers.append("No FocusMap mesh is generated.")
    elif action == AGENT_ACTION_LAYOUT_OVERLAY:
        if not context.gds_mapping_ready:
            blockers.append("GDS-to-stage mapping is not ready.")
        if not context.last_stitch_path or not Path(context.last_stitch_path).exists():
            blockers.append("No recent stitched image is available.")
    elif action in {AGENT_ACTION_STATUS, AGENT_ACTION_CLARIFY}:
        pass
    else:
        blockers.append("Unsupported Agent action.")
    return tuple(blockers)


def build_agent_capabilities(context: AgentContext) -> tuple[AgentCapability, ...]:
    catalog = (
        AgentCapability(
            action_id=AGENT_ACTION_STATUS,
            title="Summarize current software state",
            module="Agent Panel",
            purpose="Explain current stage, camera, API, GDS, and workflow state without changing hardware.",
            prerequisites=("Application is running.",),
            hardware_effects=(),
            requires_confirmation=False,
            visualization_hint=VISUALIZATION_DEFAULT,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_MOVE_GDS,
            title="Move to selected GDS target",
            module="LayoutBond",
            purpose="Move XY stage to the currently selected GDS/LayoutBond target using fitted GDS-to-stage mapping.",
            prerequisites=("Serial connected.", "Motion idle.", "GDS target selected.", "GDS-to-stage mapping fitted."),
            hardware_effects=("Moves XY stage.", "May move Z when FocusMap Z lock is enabled."),
            visualization_hint=VISUALIZATION_LAYOUT,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_STAGE_MOVE,
            title="Move stage by coordinates",
            module="Stage Control",
            purpose="Move stage axes with the existing coordinate movement workflow. Supports zero/origin moves, absolute pulse targets, and relative pulse deltas without requiring GDS or FocusMap binding.",
            prerequisites=("Serial connected.", "Motion idle.", "No active workflow.", "Parameters specify mode and axes."),
            hardware_effects=("Moves selected X/Y/Z stage axes.",),
            visualization_hint=VISUALIZATION_DEFAULT,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_AUTOFOCUS,
            title="AutoFocus at current position",
            module="AutoFocus",
            purpose="Run the existing Z-axis autofocus workflow with current AutoFocus UI settings.",
            prerequisites=("Serial connected.", "Camera running.", "Current camera frame available.", "No active workflow."),
            hardware_effects=("Moves Z axis during focus search.",),
            visualization_hint=VISUALIZATION_AUTOFOCUS,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_SINGLE_CAPTURE,
            title="Capture and save one frame",
            module="ImgStitch",
            purpose="Capture the current microscope frame, save it in the ImgStitch session, and update ImgStitch preview.",
            prerequisites=("Camera running.", "Current camera frame available.", "No active workflow."),
            hardware_effects=("Captures an image frame.",),
            requires_confirmation=True,
            visualization_hint=VISUALIZATION_IMGSTITCH,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_IMAGE_CAPTURE,
            title="Run current image acquisition sequence",
            module="ImgStitch",
            purpose="Run the current ImgStitch, T-stack, or Z-stack acquisition settings.",
            prerequisites=("Serial connected for motion acquisition.", "Camera running.", "Current camera frame available.", "No active workflow.", "Current ImgStitch settings are valid."),
            hardware_effects=("May move XY and Z axes.", "May run focus sampling.", "Captures one or more image frames."),
            visualization_hint=VISUALIZATION_IMGSTITCH,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_FOCUSMAP,
            title="Run FocusMap with current settings",
            module="FocusMap",
            purpose="Run the existing AF Plane / FocusMap workflow using the currently generated mesh and UI settings.",
            prerequisites=("Serial connected.", "Camera running.", "FocusMap mesh generated.", "No active workflow."),
            hardware_effects=("Moves XY and Z axes.", "Runs autofocus at sampled points."),
            visualization_hint=VISUALIZATION_FOCUSMAP,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_LAYOUT_OVERLAY,
            title="Associate latest image with LayoutBond",
            module="LayoutBond",
            purpose="Attach the latest stitched or captured image path to the current LayoutBond context for inspection.",
            prerequisites=("GDS-to-stage mapping fitted.", "Recent image output exists.", "No active workflow."),
            hardware_effects=("Changes inspection context only.",),
            visualization_hint=VISUALIZATION_LAYOUT,
        ),
        AgentCapability(
            action_id=AGENT_ACTION_CLARIFY,
            title="Ask for clarification",
            module="Agent Panel",
            purpose="Ask the user to clarify requests that cannot be mapped safely to supported high-level workflows.",
            prerequisites=("User request is ambiguous or unsupported.",),
            hardware_effects=(),
            requires_confirmation=False,
            visualization_hint=VISUALIZATION_DEFAULT,
        ),
    )
    capabilities: list[AgentCapability] = []
    for capability in catalog:
        blockers = context_blockers_for_action(capability.action_id, context)
        capabilities.append(replace(capability, available=not blockers, blockers=blockers))
    return tuple(capabilities)


def capabilities_to_markdown(context: AgentContext, capabilities: tuple[AgentCapability, ...] | None = None) -> str:
    capabilities = capabilities or build_agent_capabilities(context)
    lines = [
        "# Semi Auto Probe capability brief",
        "",
        "## Current software state",
        f"- Serial: {'connected' if context.serial_connected else 'not connected'}",
        f"- Camera: {'running' if context.camera_running else 'not running'}; frame: {'available' if context.camera_frame_available else 'missing'}",
        f"- Motion: {'busy' if context.motion_busy or context.keyboard_motion_busy else 'idle'}; position read: {'pending' if context.position_read_pending else 'idle'}",
        f"- Workflows: AutoFocus {'running' if context.autofocus_running else 'idle'}, FocusMap {'running' if context.focusmap_running else 'idle'}, ImgStitch {'running' if context.imgstitch_running else 'idle'}",
        f"- Stage XYZ um: X={context.stage_position_um.get('X', 0.0):.6g}, Y={context.stage_position_um.get('Y', 0.0):.6g}, Z={context.stage_position_um.get('Z', 0.0):.6g}",
        f"- Current GDS UV: {_format_pair(context.current_mapped_gds_uv)}",
        f"- Selected GDS UV: {_format_pair(context.gds_selected_uv)}",
        f"- Selected target stage: {_format_pair(context.gds_target_stage_um)}",
        f"- GDS binding: {'ready' if context.gds_mapping_ready else 'not ready'}",
        f"- Last image: {context.last_stitch_path or '-'}",
        f"- Agent model/API: {context.agent_model} at {context.agent_base_url} ({'configured' if context.agent_api_configured else 'fallback/no key'})",
        "",
        "## High-level capabilities",
    ]
    for capability in capabilities:
        lines.extend(
            [
                f"### {capability.action_id}",
                f"- Title: {capability.title}",
                f"- Module: {capability.module}",
                f"- Purpose: {capability.purpose}",
                f"- Requires confirmation: {'yes' if capability.requires_confirmation else 'no'}",
                f"- Visualization: {capability.visualization_hint}",
                f"- Available now: {'yes' if capability.available else 'no'}",
            ]
        )
        if capability.prerequisites:
            lines.append("- Prerequisites: " + "; ".join(capability.prerequisites))
        if capability.hardware_effects:
            lines.append("- Hardware effects: " + "; ".join(capability.hardware_effects))
        if capability.blockers:
            lines.append("- Current blockers: " + "; ".join(capability.blockers))
        lines.append("")
    lines.extend(
        [
            "## Hard boundaries",
            "- Never output raw serial/controller commands or protocol frames.",
            "- Use only action_id values listed in this brief.",
            "- For stage_move parameters use one of: {'mode':'zero','axes':['X','Y']}; {'mode':'absolute','targets':{'X':0,'Y':0}}; {'mode':'relative','deltas':{'X':100,'Y':-50}}. Values are controller pulses unless the application explicitly adds a unit converter.",
            "- Hardware or experiment-state changes require confirmation.",
            "- The application will re-check current state before every confirmed step.",
        ]
    )
    return "\n".join(lines)


def _format_pair(value: tuple[float, float] | None) -> str:
    if value is None:
        return "-"
    return f"{value[0]:.6g}, {value[1]:.6g}"


def merge_context_blockers(plan: AgentPlan, context: AgentContext) -> AgentPlan:
    plan_blockers = list(plan.blockers)
    steps: list[AgentWorkflowStep] = []
    for step in plan.steps:
        blockers = list(step.blockers)
        if step.action_id not in AGENT_ACTIONS:
            blockers.append("Unsupported Agent action.")
        if step.action_id == AGENT_ACTION_STAGE_MOVE:
            for blocker in stage_move_parameter_blockers(step.parameters):
                if blocker not in blockers:
                    blockers.append(blocker)
        for blocker in context_blockers_for_action(step.action_id, context):
            if blocker not in blockers:
                blockers.append(blocker)
        steps.append(replace(step, blockers=tuple(blockers)))
    if not steps:
        plan_blockers.append("No executable Agent steps were returned.")
    return replace(plan, steps=tuple(steps), blockers=tuple(dict.fromkeys(plan_blockers)))


def stage_move_parameter_blockers(parameters: dict[str, object]) -> tuple[str, ...]:
    blockers: list[str] = []
    mode = str(parameters.get("mode", "")).lower()
    if mode not in {"zero", "absolute", "relative"}:
        blockers.append("Stage move mode must be zero, absolute, or relative.")
        return tuple(blockers)
    axes = stage_move_axes(parameters)
    if not axes:
        blockers.append("Stage move must specify at least one axis.")
    if mode == "zero":
        return tuple(blockers)
    values_key = "targets" if mode == "absolute" else "deltas"
    values = parameters.get(values_key)
    if not isinstance(values, dict):
        blockers.append(f"Stage move {mode} mode requires {values_key}.")
        return tuple(blockers)
    for axis in axes:
        try:
            int(float(values[axis]))
        except (KeyError, TypeError, ValueError):
            blockers.append(f"Stage move {values_key} must include numeric {axis}.")
    return tuple(blockers)


def stage_move_axes(parameters: dict[str, object]) -> tuple[str, ...]:
    raw_axes = parameters.get("axes")
    axes: list[str] = []
    if isinstance(raw_axes, list):
        candidates = raw_axes
    elif isinstance(raw_axes, str):
        candidates = [raw_axes]
    else:
        candidates = []
    for candidate in candidates:
        text = str(candidate).upper()
        if text == "XY":
            for axis in ("X", "Y"):
                if axis not in axes:
                    axes.append(axis)
            continue
        if text == "XYZ":
            for axis in ("X", "Y", "Z"):
                if axis not in axes:
                    axes.append(axis)
            continue
        if text in {"X", "Y", "Z"} and text not in axes:
            axes.append(text)
    if axes:
        return tuple(axes)
    for key in ("targets", "deltas"):
        values = parameters.get(key)
        if isinstance(values, dict):
            for axis in ("X", "Y", "Z"):
                if axis in values and axis not in axes:
                    axes.append(axis)
    return tuple(axes)


class OpenAICompatibleLLMPlanner:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        base_url: str = DEFAULT_AGENT_BASE_URL,
        timeout_seconds: float = 30.0,
        spec_path: Path | None = None,
    ) -> None:
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.spec_path = spec_path
        self.function_spec = self._read_spec(spec_path)
        self.rule_planner = RuleBasedAgentPlanner()

    @classmethod
    def from_environment(cls, spec_path: Path | None = None) -> "OpenAICompatibleLLMPlanner | None":
        api_key = os.environ.get(AGENT_API_KEY_ENV, "").strip()
        if not api_key:
            return None
        model = os.environ.get(AGENT_MODEL_ENV, DEFAULT_AGENT_MODEL).strip() or DEFAULT_AGENT_MODEL
        base_url = os.environ.get(AGENT_BASE_URL_ENV, DEFAULT_AGENT_BASE_URL).strip() or DEFAULT_AGENT_BASE_URL
        timeout_text = os.environ.get(AGENT_TIMEOUT_ENV, "30").strip()
        try:
            timeout_seconds = max(1.0, float(timeout_text))
        except ValueError:
            timeout_seconds = 30.0
        return cls(
            api_key=api_key,
            model=model,
            base_url=base_url,
            timeout_seconds=timeout_seconds,
            spec_path=spec_path,
        )

    @staticmethod
    def _read_spec(spec_path: Path | None) -> str:
        if spec_path is None or not spec_path.exists():
            return ""
        return spec_path.read_text(encoding="utf-8")

    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        try:
            payload = self._request_plan(instruction, context)
            plan = self._plan_from_payload(payload, context)
            usage = self._token_usage(payload)
            return replace(plan, token_usage=usage)
        except Exception as exc:
            fallback = self.rule_planner.plan(instruction, context)
            return replace(
                fallback,
                planner_source="rules-fallback",
                reply_markdown=f"{fallback.reply_markdown}\n\nLLM planning failed; using local fallback: `{exc}`",
            )

    def _request_plan(self, instruction: str, context: AgentContext) -> dict[str, Any]:
        capabilities = build_agent_capabilities(context)
        capability_brief = capabilities_to_markdown(context, capabilities)
        request_payload = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": self._system_prompt()},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "instruction": instruction,
                            "context": context.to_prompt_dict(),
                            "capability_brief_markdown": capability_brief,
                            "function_spec_template": self.function_spec,
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }
        request = Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(request_payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM API HTTP {exc.code}: {detail[:300]}") from exc
        except URLError as exc:
            raise RuntimeError(f"LLM API request failed: {exc.reason}") from exc

        content = response_payload["choices"][0]["message"]["content"]
        payload = self._extract_json_object(str(content))
        payload["_usage"] = response_payload.get("usage", {})
        return payload

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are the planning layer for a semi-auto probe station. "
            "Return one JSON object only. No markdown fences. "
            "Write user-visible reply_markdown in English by default unless the user explicitly requests another language. "
            "Use only high-level action_id values from the capability brief. "
            "Never invent raw serial commands, protocol frames, direct controller operations, or arbitrary file writes. "
            "Every hardware-changing or experiment-state-changing step must set requires_confirmation true. "
            "JSON schema: {"
            '"title": string, "understanding": string, "reply_markdown": string, '
            '"needs_clarification": boolean, "visualization_hint": string, '
            '"plan": {"steps": [{"action_id": string, "title": string, "module": string, '
            '"detail": string, "parameters": object, "requires_confirmation": boolean, '
            '"involves_motion": boolean, "involves_autofocus": boolean, "involves_capture": boolean, '
            '"changes_experiment_state": boolean, "visualization_hint": string, '
            '"risks": [string], "blockers": [string], "recovery_suggestions": [string]}]}, '
            '"blockers": [string], "recovery_suggestions": [string]}'
        )

    @staticmethod
    def _extract_json_object(text: str) -> dict[str, Any]:
        stripped = text.strip()
        if stripped.startswith("```"):
            stripped = stripped.strip("`")
            if stripped.startswith("json"):
                stripped = stripped[4:].strip()
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start < 0 or end < start:
            raise ValueError("LLM response did not contain a JSON object.")
        return json.loads(stripped[start : end + 1])

    @staticmethod
    def _strings(value: object) -> tuple[str, ...]:
        if not isinstance(value, list):
            return ()
        return tuple(str(item) for item in value if str(item).strip())

    @staticmethod
    def _token_usage(payload: dict[str, Any]) -> dict[str, int]:
        usage = payload.get("_usage", {})
        if not isinstance(usage, dict):
            return {}
        result = {}
        for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
            try:
                result[key] = int(usage[key])
            except (KeyError, TypeError, ValueError):
                pass
        return result

    def _plan_from_payload(self, payload: dict[str, Any], context: AgentContext) -> AgentPlan:
        steps_payload = payload.get("plan", {}).get("steps", []) if isinstance(payload.get("plan"), dict) else payload.get("steps", [])
        steps = []
        for item in steps_payload if isinstance(steps_payload, list) else []:
            if not isinstance(item, dict):
                continue
            action_id = str(item.get("action_id", item.get("action", AGENT_ACTION_CLARIFY)))
            if action_id not in AGENT_ACTIONS:
                action_id = AGENT_ACTION_CLARIFY
            steps.append(
                AgentWorkflowStep(
                    action_id=action_id,
                    title=str(item.get("title", "Step")),
                    module=str(item.get("module", "Agent Panel")),
                    detail=str(item.get("detail", "")),
                    parameters=dict(item.get("parameters", {})) if isinstance(item.get("parameters"), dict) else {},
                    requires_confirmation=bool(item.get("requires_confirmation", action_id not in {AGENT_ACTION_STATUS, AGENT_ACTION_CLARIFY})),
                    involves_motion=bool(item.get("involves_motion", False)),
                    involves_autofocus=bool(item.get("involves_autofocus", False)),
                    involves_capture=bool(item.get("involves_capture", False)),
                    changes_experiment_state=bool(item.get("changes_experiment_state", action_id not in {AGENT_ACTION_STATUS, AGENT_ACTION_CLARIFY})),
                    visualization_hint=str(item.get("visualization_hint", VISUALIZATION_DEFAULT)),
                    risks=self._strings(item.get("risks", [])),
                    blockers=self._strings(item.get("blockers", [])),
                    recovery_suggestions=self._strings(item.get("recovery_suggestions", [])),
                )
            )
        if not steps:
            steps.append(self._clarify_step("No detailed steps were returned by the LLM."))
        return AgentPlan(
            title=str(payload.get("title", "AI Agent plan")),
            understanding=str(payload.get("understanding", "")),
            reply_markdown=str(payload.get("reply_markdown", payload.get("understanding", ""))),
            steps=tuple(steps),
            needs_clarification=bool(payload.get("needs_clarification", False)),
            blockers=self._strings(payload.get("blockers", [])),
            recovery_suggestions=self._strings(payload.get("recovery_suggestions", [])),
            planner_source="llm",
            visualization_hint=str(payload.get("visualization_hint", steps[0].visualization_hint if steps else VISUALIZATION_DEFAULT)),
            capabilities_markdown=capabilities_to_markdown(context),
        )

    @staticmethod
    def _clarify_step(detail: str) -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_CLARIFY,
            title="Ask for clarification",
            module="Agent Panel",
            detail=detail,
            requires_confirmation=False,
        )


class RuleBasedAgentPlanner:
    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        current_instruction = self._current_request_text(instruction)
        normalized = self._normalize(current_instruction)
        steps: list[AgentWorkflowStep] = []
        is_layout_overlay = self._is_layout_overlay(normalized)
        if self._is_gds_move(normalized):
            steps.append(self._gds_move_step(context))
        stage_move_step = self._stage_move_step_from_text(current_instruction, normalized)
        if stage_move_step is not None:
            steps.append(stage_move_step)
        if self._is_autofocus(normalized):
            steps.append(self._autofocus_step())
        if not is_layout_overlay and self._is_single_capture(normalized):
            steps.append(self._single_capture_step())
        elif not is_layout_overlay and self._is_image_capture(normalized):
            steps.append(self._image_capture_step())
        if self._is_focusmap(normalized):
            steps.append(self._focusmap_step())
        if is_layout_overlay:
            steps.append(self._layout_overlay_step())
        if not steps:
            steps.append(self._clarify_step())
            return self._plan(
                "Need clarification",
                "The instruction does not match a supported Agent task.",
                "I need a clearer supported task. I can help with stage movement, GDS movement, autofocus, image capture, FocusMap, ImgStitch, or LayoutBond image association.",
                steps,
                context,
                needs_clarification=True,
            )

        title = self._title_for_steps(steps)
        reply = self._reply_for_steps(title, steps)
        return self._plan(title, title, reply, steps, context)

    @staticmethod
    def _normalize(text: str) -> str:
        return "".join(text.lower().split())

    @staticmethod
    def _current_request_text(text: str) -> str:
        marker = "## Current request"
        if marker not in text:
            return text
        return text.split(marker, 1)[1].strip()

    @staticmethod
    def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    def _is_gds_move(self, text: str) -> bool:
        if self._has_any(text, ("zero", "origin", "home", "\u539f\u70b9", "\u56de\u96f6", "\u5f52\u96f6")):
            return False
        return self._has_any(text, ("\u79fb\u52a8", "\u53bb", "move", "goto", "go")) and self._has_any(
            text,
            ("gds", "layout", "layoutbond", "\u7248\u56fe", "\u9009\u4e2d", "selected", "target", "\u70b9", "\u4f4d\u7f6e"),
        )

    def _stage_move_step_from_text(self, original_text: str, normalized: str) -> AgentWorkflowStep | None:
        if self._is_gds_move(normalized):
            return None
        if not self._has_any(normalized, ("\u79fb\u52a8", "\u53bb", "move", "goto", "go", "return")):
            return None
        if self._has_any(normalized, ("zero", "origin", "home", "\u539f\u70b9", "\u56de\u96f6", "\u5f52\u96f6")):
            axes = self._axes_from_text(normalized, default=("X", "Y"))
            return self._stage_move_step("zero", axes, title=f"Move {'/'.join(axes)} stage to zero")
        parsed = self._parse_axis_values(original_text)
        if not parsed:
            return None
        mode = "relative" if self._has_any(normalized, ("relative", "\u76f8\u5bf9", "delta")) or any(value < 0 for value in parsed.values()) else "absolute"
        if self._has_any(normalized, ("absolute", "\u7edd\u5bf9", "to", "\u5230")):
            mode = "absolute"
        parameters = {"mode": mode, "axes": list(parsed.keys())}
        if mode == "absolute":
            parameters["targets"] = parsed
            title = "Move stage to absolute coordinates"
        else:
            parameters["deltas"] = parsed
            title = "Move stage by relative deltas"
        return self._stage_move_step(mode, tuple(parsed.keys()), title=title, parameters=parameters)

    @staticmethod
    def _axes_from_text(text: str, default: tuple[str, ...]) -> tuple[str, ...]:
        if "xyz" in text or "\u5168\u90e8" in text or "all" in text:
            return ("X", "Y", "Z")
        if "xy" in text:
            return ("X", "Y")
        axes = []
        for axis in ("x", "y", "z"):
            if (f"{axis}axis" in text or f"{axis}\u8f74" in text) and axis.upper() not in axes:
                axes.append(axis.upper())
        return tuple(axes or default)

    @staticmethod
    def _parse_axis_values(text: str) -> dict[str, int]:
        values: dict[str, int] = {}
        for match in re.finditer(r"\b([xyzXYZ])\s*(?:=|to|:)?\s*([+-]?\d+(?:\.\d+)?)", text):
            values[match.group(1).upper()] = int(round(float(match.group(2))))
        return values

    def _is_autofocus(self, text: str) -> bool:
        return self._has_any(text, ("\u81ea\u52a8\u5bf9\u7126", "\u81ea\u52a8\u805a\u7126", "autofocus", "auto-focus", "focus"))

    def _is_single_capture(self, text: str) -> bool:
        if self._has_any(text, ("stitch", "\u62fc\u56fe", "\u5e8f\u5217", "sequence", "mosaic", "stack")):
            return False
        return self._has_any(text, ("\u62cd\u4e00\u5f20", "\u62cd\u5f20", "\u62cd\u56fe", "\u62cd\u7167", "singleframe", "oneimage", "onephoto", "captureone", "takeaphoto"))

    def _is_image_capture(self, text: str) -> bool:
        return self._has_any(text, ("\u62cd\u7167", "\u91c7\u96c6", "capture", "acquire", "image", "stitch", "\u62fc\u56fe", "\u5e8f\u5217", "\u4fdd\u5b58"))

    def _is_focusmap(self, text: str) -> bool:
        return self._has_any(text, ("focusmap", "afplane", "planemapping", "\u5e73\u9762\u62df\u5408", "\u7126\u5e73\u9762"))

    def _is_layout_overlay(self, text: str) -> bool:
        return self._has_any(text, ("\u53e0\u52a0", "overlay", "\u5173\u8054", "associate", "layoutbond")) and self._has_any(
            text,
            ("\u56fe\u50cf", "image", "\u521a\u62cd", "\u6700\u8fd1", "last", "stitch"),
        )

    def _gds_move_step(self, context: AgentContext) -> AgentWorkflowStep:
        target = context.gds_target_stage_um
        target_text = f"stage X {target[0]:.6g} um, Y {target[1]:.6g} um" if target else "the selected GDS target"
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_MOVE_GDS,
            title="Move to selected GDS target",
            module="LayoutBond",
            detail=f"Call the existing LayoutBond move workflow for {target_text}.",
            requires_confirmation=True,
            involves_motion=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_LAYOUT,
            risks=("Moves the XY stage. Confirm sample clearance before running.",),
            recovery_suggestions=("Connect serial and select a GDS target after fitting LayoutBond mapping.",),
        )

    @staticmethod
    def _stage_move_step(
        mode: str,
        axes: tuple[str, ...],
        *,
        title: str,
        parameters: dict[str, object] | None = None,
    ) -> AgentWorkflowStep:
        parameters = parameters or {"mode": mode, "axes": list(axes)}
        detail = f"Use the existing stage coordinate movement workflow in {mode} mode for axes {', '.join(axes)}."
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_STAGE_MOVE,
            title=title,
            module="Stage Control",
            detail=detail,
            parameters=parameters,
            requires_confirmation=True,
            involves_motion=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_DEFAULT,
            risks=("Moves the selected stage axes. Confirm probe/sample clearance before running.",),
            recovery_suggestions=("Connect the serial controller and wait for active workflows to finish before retrying.",),
        )

    @staticmethod
    def _autofocus_step() -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_AUTOFOCUS,
            title="AutoFocus at current position",
            module="AutoFocus",
            detail="Run the existing Z-axis autofocus workflow using current AutoFocus UI settings.",
            requires_confirmation=True,
            involves_motion=True,
            involves_autofocus=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_AUTOFOCUS,
            risks=("Moves the Z axis during focus search.",),
            recovery_suggestions=("Connect serial, start the camera, and wait for active workflows to finish.",),
        )

    @staticmethod
    def _single_capture_step() -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_SINGLE_CAPTURE,
            title="Capture and save one frame",
            module="ImgStitch",
            detail="Capture the current microscope frame and save it to the ImgStitch session outputs.",
            requires_confirmation=True,
            involves_capture=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_IMGSTITCH,
            risks=("Captures and saves the current camera frame.",),
            recovery_suggestions=("Start the camera and wait for a current frame before retrying.",),
        )

    @staticmethod
    def _image_capture_step() -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_IMAGE_CAPTURE,
            title="Run current image acquisition sequence",
            module="ImgStitch",
            detail="Run current ImgStitch, T-stack, or Z-stack acquisition settings.",
            requires_confirmation=True,
            involves_motion=True,
            involves_autofocus=True,
            involves_capture=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_IMGSTITCH,
            risks=("May move XY/Z axes and capture multiple camera frames.",),
            recovery_suggestions=("Connect serial, confirm camera preview, and check ImgStitch settings before retrying.",),
        )

    @staticmethod
    def _focusmap_step() -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_FOCUSMAP,
            title="Run FocusMap with current settings",
            module="FocusMap",
            detail="Run the existing FocusMap workflow using the current generated mesh and settings.",
            requires_confirmation=True,
            involves_motion=True,
            involves_autofocus=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_FOCUSMAP,
            risks=("Moves XY/Z and samples focus across the current mesh.",),
            recovery_suggestions=("Generate a FocusMap mesh and confirm camera/serial readiness before retrying.",),
        )

    @staticmethod
    def _layout_overlay_step() -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_LAYOUT_OVERLAY,
            title="Associate latest image with GDS layout",
            module="LayoutBond",
            detail="Reference the latest stitched or captured output in the LayoutBond status area.",
            requires_confirmation=True,
            changes_experiment_state=True,
            visualization_hint=VISUALIZATION_LAYOUT,
            risks=("This changes inspection context but does not perform pixel-level registration.",),
            recovery_suggestions=("Load or fit a LayoutBond mapping and run image acquisition before overlay association.",),
        )

    @staticmethod
    def _clarify_step() -> AgentWorkflowStep:
        return AgentWorkflowStep(
            action_id=AGENT_ACTION_CLARIFY,
            title="Ask for a supported task",
            module="Agent Panel",
            detail="Try GDS movement, autofocus, image acquisition, FocusMap, or layout image association.",
            requires_confirmation=False,
        )

    def _plan(
        self,
        title: str,
        understanding: str,
        reply_markdown: str,
        steps: list[AgentWorkflowStep],
        context: AgentContext,
        *,
        needs_clarification: bool = False,
    ) -> AgentPlan:
        plan = AgentPlan(
            title=title,
            understanding=understanding,
            reply_markdown=reply_markdown,
            steps=tuple(steps),
            needs_clarification=needs_clarification,
            planner_source="rules",
            visualization_hint=steps[0].visualization_hint if steps else VISUALIZATION_DEFAULT,
            capabilities_markdown=capabilities_to_markdown(context),
        )
        return plan

    @staticmethod
    def _title_for_steps(steps: list[AgentWorkflowStep]) -> str:
        if len(steps) == 1:
            return steps[0].title
        return " -> ".join(step.title for step in steps)

    @staticmethod
    def _reply_for_steps(title: str, steps: list[AgentWorkflowStep]) -> str:
        lines = [f"## {title}", "", "I will run this as a step-by-step workflow. Each hardware or state-changing step waits for confirmation before it starts.", "", "| Step | Module | Action |", "| --- | --- | --- |"]
        for index, step in enumerate(steps, start=1):
            lines.append(f"| {index} | {step.module} | {step.title} |")
        return "\n".join(lines)


class AgentPlanner:
    def __init__(self, llm_planner: LLMPlanner | None = None) -> None:
        self.rule_planner = RuleBasedAgentPlanner()
        self.llm_planner = llm_planner

    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        if not instruction.strip():
            plan = self.rule_planner.plan(instruction, context)
        else:
            plan = self.llm_planner.plan(instruction, context) if self.llm_planner is not None else self.rule_planner.plan(instruction, context)
        return merge_context_blockers(plan, context)
