from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from semi_auto_probe.agent import (
    AGENT_ACTION_AUTOFOCUS,
    AGENT_ACTION_CLARIFY,
    AGENT_ACTION_IMAGE_CAPTURE,
    AGENT_ACTION_LAYOUT_OVERLAY,
    AGENT_ACTION_MOVE_GDS,
    AgentContext,
    AgentPlan,
    AgentPlanner,
    AgentStep,
)


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
    }
    values.update(overrides)
    return AgentContext(**values)


class FakeLLMPlanner:
    def __init__(self, action: str) -> None:
        self.action = action

    def plan(self, instruction: str, context: AgentContext) -> AgentPlan:
        return AgentPlan(
            action=self.action,
            title="Fake LLM plan",
            understanding=f"LLM interpreted: {instruction}",
            steps=(AgentStep("Run", "Fake", "Fake step"),),
            requires_confirmation=True,
            involves_motion=False,
            involves_autofocus=False,
            involves_capture=False,
            planner_source="llm",
        )


class AgentPlannerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.planner = AgentPlanner()

    def test_move_to_selected_gds_generates_layoutbond_plan(self) -> None:
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

    def test_autofocus_generates_autofocus_plan(self) -> None:
        plan = self.planner.plan("run autofocus at the current position", context())

        self.assertEqual(plan.action, AGENT_ACTION_AUTOFOCUS)
        self.assertTrue(plan.executable)
        self.assertTrue(plan.involves_autofocus)
        self.assertTrue(plan.involves_motion)

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

    def test_motion_busy_blocks_motion_plans(self) -> None:
        plan = self.planner.plan("move to selected GDS point", context(motion_busy=True))

        self.assertFalse(plan.executable)
        self.assertIn("Motion is busy.", plan.blockers)

    def test_missing_serial_blocks_hardware_plan(self) -> None:
        plan = self.planner.plan("run autofocus", context(serial_connected=False))

        self.assertFalse(plan.executable)
        self.assertIn("Serial port is not connected.", plan.blockers)

    def test_missing_gds_target_blocks_gds_move(self) -> None:
        plan = self.planner.plan(
            "move to selected GDS target",
            context(gds_target_selected=False, gds_target_stage_um=None),
        )

        self.assertFalse(plan.executable)
        self.assertIn("No GDS target is selected.", plan.blockers)

    def test_running_imgstitch_blocks_new_hardware_plan(self) -> None:
        plan = self.planner.plan("run autofocus", context(imgstitch_running=True))

        self.assertFalse(plan.executable)
        self.assertIn("ImgStitch is already running.", plan.blockers)

    def test_llm_plan_still_gets_local_safety_blockers(self) -> None:
        planner = AgentPlanner(llm_planner=FakeLLMPlanner(AGENT_ACTION_AUTOFOCUS))

        plan = planner.plan("please focus", context(serial_connected=False))

        self.assertEqual(plan.planner_source, "llm")
        self.assertFalse(plan.executable)
        self.assertIn("Serial port is not connected.", plan.blockers)


if __name__ == "__main__":
    unittest.main()
