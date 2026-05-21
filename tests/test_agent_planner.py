from __future__ import annotations

import tempfile
import threading
import os
import queue
import unittest
from pathlib import Path

import numpy as np

from semi_auto_probe.agent import (
    AGENT_ACTION_AUTOFOCUS,
    AGENT_ACTION_CLARIFY,
    AGENT_ACTION_IMAGE_CAPTURE,
    AGENT_ACTION_LAYOUT_OVERLAY,
    AGENT_ACTION_MOVE_GDS,
    AGENT_ACTION_SINGLE_CAPTURE,
    AGENT_ACTION_STAGE_MOVE,
    AgentContext,
    AgentPlan,
    AgentPlanner,
    AgentWorkflowStep,
    OpenAICompatibleLLMPlanner,
    build_agent_capabilities,
    capabilities_to_markdown,
)
from semi_auto_probe.app import ProbeApp
from semi_auto_probe.config import ProbeConfig


class FakeVar:
    def __init__(self, value: str) -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


def context(**overrides: object) -> AgentContext:
    values = {
        "positions": {"X": 0, "Y": 0, "Z": 0},
        "serial_connected": True,
        "motion_busy": False,
        "keyboard_motion_busy": False,
        "position_read_pending": False,
        "camera_running": True,
        "camera_frame_available": True,
        "autofocus_running": False,
        "focusmap_running": False,
        "imgstitch_running": False,
        "gds_target_selected": True,
        "gds_mapping_ready": True,
        "gds_target_stage_um": (12.5, -6.0),
        "last_stitch_path": None,
        "current_page": "Main",
        "config_summary": {},
        "focusmap_points": 4,
    }
    values.update(overrides)
    return AgentContext(**values)


class FakeLLMPlanner:
    def __init__(self, plan: AgentPlan) -> None:
        self._plan = plan

    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        return self._plan


class AgentPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = AgentPlanner()

    def test_move_to_selected_gds_generates_layoutbond_step(self) -> None:
        plan = self.planner.plan("move to the currently selected GDS point", context())

        self.assertEqual(plan.action, AGENT_ACTION_MOVE_GDS)
        self.assertTrue(plan.executable)
        self.assertTrue(plan.requires_confirmation)
        self.assertTrue(plan.involves_motion)
        self.assertIn("LayoutBond", {step.module for step in plan.steps})

    def test_chinese_move_instruction_is_supported_by_rule_fallback(self) -> None:
        plan = self.planner.plan("\u79fb\u52a8\u5230\u5f53\u524d\u9009\u4e2d\u7684 GDS \u70b9", context())

        self.assertEqual(plan.action, AGENT_ACTION_MOVE_GDS)
        self.assertTrue(plan.executable)

    def test_autofocus_then_single_capture_generates_two_steps(self) -> None:
        plan = self.planner.plan("帮我先自动聚焦，然后拍一张图并保存", context())

        self.assertEqual([step.action_id for step in plan.steps], [AGENT_ACTION_AUTOFOCUS, AGENT_ACTION_SINGLE_CAPTURE])
        self.assertTrue(plan.involves_autofocus)
        self.assertTrue(plan.involves_capture)

    def test_gds_selected_position_then_capture_generates_move_and_capture(self) -> None:
        plan = self.planner.plan("去当前 GDS 选中的位置并拍图", context())

        self.assertEqual([step.action_id for step in plan.steps], [AGENT_ACTION_MOVE_GDS, AGENT_ACTION_SINGLE_CAPTURE])

    def test_move_to_zero_xy_generates_stage_move_without_gds_dependency(self) -> None:
        plan = self.planner.plan("Move to zero XY", context(gds_target_selected=False, gds_mapping_ready=False, gds_target_stage_um=None))

        self.assertEqual([step.action_id for step in plan.steps], [AGENT_ACTION_STAGE_MOVE])
        self.assertTrue(plan.executable)
        self.assertEqual(plan.steps[0].parameters, {"mode": "zero", "axes": ["X", "Y"]})

    def test_chinese_stage_origin_generates_xy_zero_move(self) -> None:
        plan = self.planner.plan("移动回台子原点", context(gds_target_selected=False, gds_mapping_ready=False, gds_target_stage_um=None))

        self.assertEqual([step.action_id for step in plan.steps], [AGENT_ACTION_STAGE_MOVE])
        self.assertEqual(plan.steps[0].parameters, {"mode": "zero", "axes": ["X", "Y"]})

    def test_capture_sequence_generates_imgstitch_plan(self) -> None:
        plan = self.planner.plan("capture images using the current sequence", context())

        self.assertEqual(plan.action, AGENT_ACTION_IMAGE_CAPTURE)
        self.assertTrue(plan.executable)
        self.assertTrue(plan.involves_capture)
        self.assertIn("ImgStitch", {step.module for step in plan.steps})

    def test_overlay_latest_image_generates_layout_plan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            image_path = Path(directory) / "last_imgstitch.png"
            image_path.write_bytes(b"png")
            plan = self.planner.plan("overlay the latest image on the layout", context(last_stitch_path=str(image_path)))

        self.assertEqual(plan.action, AGENT_ACTION_LAYOUT_OVERLAY)
        self.assertTrue(plan.executable)
        self.assertFalse(plan.involves_motion)

    def test_unknown_instruction_returns_clarification_plan(self) -> None:
        plan = self.planner.plan("optimize my experiment results", context())

        self.assertEqual(plan.action, AGENT_ACTION_CLARIFY)
        self.assertFalse(plan.executable)
        self.assertFalse(plan.requires_confirmation)

    def test_motion_busy_blocks_motion_steps(self) -> None:
        plan = self.planner.plan("move to selected GDS point", context(motion_busy=True))

        self.assertFalse(plan.executable)
        self.assertIn("Motion is busy.", plan.steps[0].blockers)

    def test_missing_serial_blocks_hardware_step(self) -> None:
        plan = self.planner.plan("run autofocus", context(serial_connected=False))

        self.assertFalse(plan.executable)
        self.assertIn("Serial port is not connected.", plan.steps[0].blockers)

    def test_missing_gds_target_blocks_gds_move(self) -> None:
        plan = self.planner.plan(
            "move to selected GDS target",
            context(gds_target_selected=False, gds_target_stage_um=None),
        )

        self.assertFalse(plan.executable)
        self.assertIn("No GDS target is selected.", plan.steps[0].blockers)

    def test_running_imgstitch_blocks_new_hardware_plan(self) -> None:
        plan = self.planner.plan("run autofocus", context(imgstitch_running=True))

        self.assertFalse(plan.executable)
        self.assertIn("ImgStitch is already running.", plan.steps[0].blockers)

    def test_llm_plan_still_gets_local_safety_blockers(self) -> None:
        llm_plan = AgentPlan(
            title="Fake LLM plan",
            understanding="LLM interpreted focus",
            reply_markdown="Run autofocus.",
            steps=(
                AgentWorkflowStep(
                    action_id=AGENT_ACTION_AUTOFOCUS,
                    title="Run",
                    module="AutoFocus",
                    detail="Fake step",
                    involves_motion=True,
                    involves_autofocus=True,
                ),
            ),
            planner_source="llm",
        )
        planner = AgentPlanner(llm_planner=FakeLLMPlanner(llm_plan))

        plan = planner.plan("please focus", context(serial_connected=False))

        self.assertEqual(plan.planner_source, "llm")
        self.assertFalse(plan.executable)
        self.assertIn("Serial port is not connected.", plan.steps[0].blockers)

    def test_capability_catalog_exposes_only_high_level_actions(self) -> None:
        capabilities = build_agent_capabilities(context())
        action_ids = {capability.action_id for capability in capabilities}

        self.assertIn(AGENT_ACTION_AUTOFOCUS, action_ids)
        self.assertIn(AGENT_ACTION_MOVE_GDS, action_ids)
        self.assertIn(AGENT_ACTION_STAGE_MOVE, action_ids)
        self.assertNotIn("serial_write", action_ids)
        self.assertNotIn("controller_command", action_ids)

    def test_capability_markdown_contains_current_state(self) -> None:
        brief = capabilities_to_markdown(context(serial_connected=False))

        self.assertIn("Semi Auto Probe capability brief", brief)
        self.assertIn("Serial: not connected", brief)
        self.assertIn(AGENT_ACTION_AUTOFOCUS, brief)

    def test_llm_payload_parser_accepts_multi_step_contract(self) -> None:
        planner = OpenAICompatibleLLMPlanner(api_key="key", model="model")
        payload = {
            "title": "Focus and capture",
            "understanding": "Run AF then capture.",
            "reply_markdown": "## Plan\n- AF\n- Capture",
            "plan": {
                "steps": [
                    {"action_id": AGENT_ACTION_AUTOFOCUS, "title": "AF", "module": "AutoFocus", "detail": "Run AF", "involves_motion": True, "involves_autofocus": True},
                    {"action_id": AGENT_ACTION_SINGLE_CAPTURE, "title": "Capture", "module": "ImgStitch", "detail": "Save one frame", "involves_capture": True},
                ]
            },
        }

        plan = planner._plan_from_payload(payload, context())

        self.assertEqual(plan.reply_markdown, "## Plan\n- AF\n- Capture")
        self.assertEqual([step.action_id for step in plan.steps], [AGENT_ACTION_AUTOFOCUS, AGENT_ACTION_SINGLE_CAPTURE])

    def test_rule_fallback_uses_current_request_marker_not_retained_history(self) -> None:
        instruction = "\n".join(
            [
                "## Retained conversation context",
                "- User: run autofocus",
                "",
                "## Current request",
                "move to selected GDS point",
            ]
        )

        plan = self.planner.plan(instruction, context())

        self.assertEqual([step.action_id for step in plan.steps], [AGENT_ACTION_MOVE_GDS])

    def test_app_agent_context_uses_stable_stitch_frame_for_camera_availability(self) -> None:
        app = ProbeApp.__new__(ProbeApp)
        app.current_position_values = {"X": 0, "Y": 0, "Z": 0}
        app.probe_config = ProbeConfig()
        app.serial_client = None
        app.motion_busy = False
        app.keyboard_motion_busy = False
        app.position_read_pending = False
        app.camera_running = True
        app.camera_lock = threading.Lock()
        app.latest_camera_frame = None
        app.latest_stitch_frame = object()
        app.autofocus_running = False
        app.af_plane_running = False
        app.imgstitch_running = False
        app.gds_stage_mapper_panel = None
        app.current_page = "Agent"
        app.imgstitch_session_dir = Path("missing-imgstitch-session")
        app.focus_metric_var = FakeVar("laplacian")
        app.imgstack_mode_var = FakeVar("Single")
        app.imgstitch_tile_acquisition_var = FakeVar("Manual")
        app.imgstitch_rows_var = FakeVar("1")
        app.imgstitch_cols_var = FakeVar("1")
        app.af_plane_mesh_points = []
        app.af_plane_results = []

        agent_context = ProbeApp.agent_context(app)

        self.assertTrue(agent_context.camera_frame_available)

    def test_agent_single_capture_falls_back_to_current_stitch_frame(self) -> None:
        app = ProbeApp.__new__(ProbeApp)
        app.camera_running = True
        app.camera_lock = threading.Lock()
        app.latest_stitch_frame = np.zeros((8, 8, 3), dtype=np.uint8)
        app.status_var = FakeVar("")
        app.result_queue = queue.Queue()
        app.agent_panel = None

        def stale_frame_only(*, stop_event=None):
            raise RuntimeError("No camera frame available for stitching.")

        app._capture_stitch_frame = stale_frame_only
        old_cwd = Path.cwd()
        with tempfile.TemporaryDirectory() as directory:
            try:
                os.chdir(directory)
                app.imgstitch_session_dir = Path("imgstitch_session")

                message = ProbeApp.capture_agent_single_frame(app)
            finally:
                os.chdir(old_cwd)

        self.assertIn("Agent captured one frame", message)


if __name__ == "__main__":
    unittest.main()
