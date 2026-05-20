from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AGENT_ACTION_MOVE_GDS = "move_gds_target"
AGENT_ACTION_AUTOFOCUS = "autofocus_current_position"
AGENT_ACTION_IMAGE_CAPTURE = "image_capture_sequence"
AGENT_ACTION_LAYOUT_OVERLAY = "layout_image_overlay"
AGENT_ACTION_CLARIFY = "clarify"

AGENT_ACTIONS = {
    AGENT_ACTION_MOVE_GDS,
    AGENT_ACTION_AUTOFOCUS,
    AGENT_ACTION_IMAGE_CAPTURE,
    AGENT_ACTION_LAYOUT_OVERLAY,
    AGENT_ACTION_CLARIFY,
}

AGENT_API_KEY_ENV = "SEMI_AUTO_PROBE_AGENT_API_KEY"
AGENT_BASE_URL_ENV = "SEMI_AUTO_PROBE_AGENT_BASE_URL"
AGENT_MODEL_ENV = "SEMI_AUTO_PROBE_AGENT_MODEL"
AGENT_TIMEOUT_ENV = "SEMI_AUTO_PROBE_AGENT_TIMEOUT_SECONDS"
DEFAULT_AGENT_BASE_URL = "https://api.deepseek.com"
DEFAULT_AGENT_MODEL = "deepseek-chat"


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
        }


@dataclass(frozen=True)
class AgentStep:
    title: str
    module: str
    detail: str
    involves_motion: bool = False
    involves_autofocus: bool = False
    involves_capture: bool = False


@dataclass(frozen=True)
class AgentPlan:
    action: str
    title: str
    understanding: str
    steps: tuple[AgentStep, ...]
    requires_confirmation: bool
    involves_motion: bool
    involves_autofocus: bool
    involves_capture: bool
    risks: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    recovery_suggestions: tuple[str, ...] = ()
    planner_source: str = "rules"

    @property
    def executable(self) -> bool:
        return not self.blockers and self.action != AGENT_ACTION_CLARIFY


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
    if action in {AGENT_ACTION_MOVE_GDS, AGENT_ACTION_AUTOFOCUS, AGENT_ACTION_IMAGE_CAPTURE}:
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
        if not context.serial_connected:
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
    elif action == AGENT_ACTION_AUTOFOCUS:
        if not context.camera_running or not context.camera_frame_available:
            blockers.append("Camera frame is not available.")
    elif action == AGENT_ACTION_IMAGE_CAPTURE:
        if not context.camera_running or not context.camera_frame_available:
            blockers.append("Camera frame is not available.")
    elif action == AGENT_ACTION_LAYOUT_OVERLAY:
        if not context.gds_mapping_ready:
            blockers.append("GDS-to-stage mapping is not ready.")
        if not context.last_stitch_path or not Path(context.last_stitch_path).exists():
            blockers.append("No recent stitched image is available.")
    elif action != AGENT_ACTION_CLARIFY:
        blockers.append("Unsupported Agent action.")
    return tuple(blockers)


def merge_context_blockers(plan: AgentPlan, context: AgentContext) -> AgentPlan:
    blockers = list(plan.blockers)
    for blocker in context_blockers_for_action(plan.action, context):
        if blocker not in blockers:
            blockers.append(blocker)
    if plan.action not in AGENT_ACTIONS:
        blockers.append("Unsupported Agent action.")
    return replace(plan, blockers=tuple(blockers))


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
            return self._plan_from_payload(payload)
        except Exception as exc:
            fallback = self.rule_planner.plan(instruction, context)
            return replace(
                fallback,
                planner_source="rules-fallback",
                risks=fallback.risks + (f"LLM planning failed, using rule fallback: {exc}",),
            )

    def _request_plan(self, instruction: str, context: AgentContext) -> dict[str, Any]:
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
                            "function_spec": self.function_spec,
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
        return self._extract_json_object(str(content))

    @staticmethod
    def _system_prompt() -> str:
        return (
            "You are the planning layer for a semi-auto probe station. "
            "Return JSON only. Do not return markdown. "
            "You may choose only these actions: "
            f"{', '.join(sorted(AGENT_ACTIONS))}. "
            "Never invent low-level hardware commands. The app will execute only high-level existing workflows. "
            "Every hardware-changing action must require confirmation. "
            "JSON schema: {"
            '"action": string, "title": string, "understanding": string, '
            '"steps": [{"title": string, "module": string, "detail": string, '
            '"involves_motion": boolean, "involves_autofocus": boolean, "involves_capture": boolean}], '
            '"requires_confirmation": boolean, "involves_motion": boolean, '
            '"involves_autofocus": boolean, "involves_capture": boolean, '
            '"risks": [string], "blockers": [string], "recovery_suggestions": [string]}'
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

    def _plan_from_payload(self, payload: dict[str, Any]) -> AgentPlan:
        action = str(payload.get("action", AGENT_ACTION_CLARIFY))
        if action not in AGENT_ACTIONS:
            action = AGENT_ACTION_CLARIFY
        steps = []
        for item in payload.get("steps", []):
            if not isinstance(item, dict):
                continue
            steps.append(
                AgentStep(
                    title=str(item.get("title", "Step")),
                    module=str(item.get("module", "Agent Panel")),
                    detail=str(item.get("detail", "")),
                    involves_motion=bool(item.get("involves_motion", False)),
                    involves_autofocus=bool(item.get("involves_autofocus", False)),
                    involves_capture=bool(item.get("involves_capture", False)),
                )
            )
        if not steps:
            steps.append(AgentStep("Review request", "Agent Panel", "No detailed steps were returned by the LLM."))
        return AgentPlan(
            action=action,
            title=str(payload.get("title", "AI Agent plan")),
            understanding=str(payload.get("understanding", "")),
            steps=tuple(steps),
            requires_confirmation=bool(payload.get("requires_confirmation", action != AGENT_ACTION_CLARIFY)),
            involves_motion=bool(payload.get("involves_motion", False)),
            involves_autofocus=bool(payload.get("involves_autofocus", False)),
            involves_capture=bool(payload.get("involves_capture", False)),
            risks=self._strings(payload.get("risks", [])),
            blockers=self._strings(payload.get("blockers", [])),
            recovery_suggestions=self._strings(payload.get("recovery_suggestions", [])),
            planner_source="llm",
        )


class RuleBasedAgentPlanner:
    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        normalized = self._normalize(instruction)
        if self._is_gds_move(normalized):
            return self._gds_move_plan(context)
        if self._is_layout_overlay(normalized):
            return self._layout_overlay_plan()
        if self._is_autofocus(normalized):
            return self._autofocus_plan()
        if self._is_image_capture(normalized):
            return self._image_capture_plan()
        return self._clarify_plan()

    @staticmethod
    def _normalize(text: str) -> str:
        return "".join(text.lower().split())

    @staticmethod
    def _has_any(text: str, keywords: tuple[str, ...]) -> bool:
        return any(keyword in text for keyword in keywords)

    def _is_gds_move(self, text: str) -> bool:
        return self._has_any(text, ("\u79fb\u52a8", "move", "goto", "go")) and self._has_any(
            text,
            ("gds", "layout", "layoutbond", "\u7248\u56fe", "\u9009\u4e2d", "selected", "target", "\u70b9"),
        )

    def _is_autofocus(self, text: str) -> bool:
        return self._has_any(text, ("\u81ea\u52a8\u5bf9\u7126", "autofocus", "auto-focus", "focus"))

    def _is_image_capture(self, text: str) -> bool:
        return self._has_any(text, ("\u62cd\u7167", "\u91c7\u96c6", "capture", "acquire", "image", "stitch", "\u62fc\u56fe", "\u5e8f\u5217"))

    def _is_layout_overlay(self, text: str) -> bool:
        return self._has_any(text, ("\u53e0\u52a0", "overlay", "\u5173\u8054", "map", "layout", "\u7248\u56fe")) and self._has_any(
            text,
            ("\u56fe\u50cf", "image", "\u521a\u62cd", "\u6700\u8fd1", "last", "stitch"),
        )

    def _gds_move_plan(self, context: AgentContext) -> AgentPlan:
        target = context.gds_target_stage_um
        target_text = f"stage X {target[0]:.6g} um, Y {target[1]:.6g} um" if target else "the selected GDS target"
        return AgentPlan(
            action=AGENT_ACTION_MOVE_GDS,
            title="Move to selected GDS target",
            understanding=f"Move the stage XY position to {target_text}.",
            steps=(
                AgentStep("Check current state", "Agent Panel", "Verify serial connection, idle motion state, selected GDS u/v, and a valid GDS-to-stage mapping."),
                AgentStep("Move stage to target", "LayoutBond", f"Call the existing LayoutBond move workflow for {target_text}.", involves_motion=True),
            ),
            requires_confirmation=True,
            involves_motion=True,
            involves_autofocus=False,
            involves_capture=False,
            risks=("Moves the XY stage. Confirm sample clearance before running.",),
            recovery_suggestions=("Connect serial and select a GDS target after fitting LayoutBond mapping.",),
            planner_source="rules",
        )

    @staticmethod
    def _autofocus_plan() -> AgentPlan:
        return AgentPlan(
            action=AGENT_ACTION_AUTOFOCUS,
            title="AutoFocus at current position",
            understanding="Run the existing Z-axis autofocus workflow at the current XY position.",
            steps=(
                AgentStep("Check current state", "Agent Panel", "Verify serial connection, camera frame availability, and idle motion state."),
                AgentStep("Run autofocus", "AutoFocus", "Call the existing AutoFocus workflow using current settings.", involves_motion=True, involves_autofocus=True),
            ),
            requires_confirmation=True,
            involves_motion=True,
            involves_autofocus=True,
            involves_capture=False,
            risks=("Moves the Z axis during focus search.",),
            recovery_suggestions=("Connect serial, start the camera, and wait for active workflows to finish.",),
            planner_source="rules",
        )

    @staticmethod
    def _image_capture_plan() -> AgentPlan:
        return AgentPlan(
            action=AGENT_ACTION_IMAGE_CAPTURE,
            title="Run current image acquisition sequence",
            understanding="Run the current ImgStitch or stack acquisition settings.",
            steps=(
                AgentStep("Check acquisition settings", "ImgStitch", "Reuse current rows, columns, overlap, range, and tile acquisition settings."),
                AgentStep("Acquire images", "ImgStitch", "Call the existing acquisition workflow.", involves_motion=True, involves_autofocus=True, involves_capture=True),
            ),
            requires_confirmation=True,
            involves_motion=True,
            involves_autofocus=True,
            involves_capture=True,
            risks=("May move XY/Z axes and capture multiple camera frames.",),
            recovery_suggestions=("Connect serial, confirm camera preview, and check ImgStitch settings before retrying.",),
            planner_source="rules",
        )

    @staticmethod
    def _layout_overlay_plan() -> AgentPlan:
        return AgentPlan(
            action=AGENT_ACTION_LAYOUT_OVERLAY,
            title="Associate latest image with GDS layout",
            understanding="Attach the latest stitched image result to the current LayoutBond context.",
            steps=(
                AgentStep("Check layout context", "LayoutBond", "Verify a valid GDS-to-stage mapping is loaded."),
                AgentStep("Associate image", "LayoutBond", "Reference the latest stitched output in the LayoutBond status area."),
            ),
            requires_confirmation=True,
            involves_motion=False,
            involves_autofocus=False,
            involves_capture=False,
            risks=("This does not perform pixel-level image registration in v1.",),
            recovery_suggestions=("Load or fit a LayoutBond mapping and run image acquisition before overlay association.",),
            planner_source="rules",
        )

    @staticmethod
    def _clarify_plan() -> AgentPlan:
        return AgentPlan(
            action=AGENT_ACTION_CLARIFY,
            title="Need clarification",
            understanding="The instruction does not match a supported Agent task.",
            steps=(
                AgentStep("Ask for a supported task", "Agent Panel", "Try GDS movement, autofocus, image acquisition, or layout image association."),
            ),
            requires_confirmation=False,
            involves_motion=False,
            involves_autofocus=False,
            involves_capture=False,
            recovery_suggestions=("Use a supported command or enable LLM planning for broader language understanding.",),
            planner_source="rules",
        )


class AgentPlanner:
    def __init__(self, llm_planner: LLMPlanner | None = None) -> None:
        self.rule_planner = RuleBasedAgentPlanner()
        self.llm_planner = llm_planner

    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        if not instruction.strip():
            return self.rule_planner._clarify_plan()
        plan = self.llm_planner.plan(instruction, context) if self.llm_planner is not None else self.rule_planner.plan(instruction, context)
        return merge_context_blockers(plan, context)
