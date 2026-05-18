from __future__ import annotations

import unittest

from semi_auto_probe.app import ProbeApp
from semi_auto_probe.config import ProbeConfig


class VisionCenterMoveTests(unittest.TestCase):
    def make_app_shell(self) -> ProbeApp:
        app = ProbeApp.__new__(ProbeApp)
        app.probe_config = ProbeConfig()
        app.current_position_values = {"X": 100, "Y": 200, "Z": 300}
        return app

    def test_y_axis_is_reversed_from_image_down_direction(self) -> None:
        app = self.make_app_shell()

        move = app._image_centering_move(
            point_x=400,
            point_y=245,
            image_width=800,
            image_height=450,
            um_per_px=0.5,
        )

        self.assertEqual(move["Y"], (-10.0, 10, True))

    def test_image_offset_converts_to_axis_pulses(self) -> None:
        app = self.make_app_shell()

        move = app._image_centering_move(
            point_x=430,
            point_y=205,
            image_width=800,
            image_height=450,
            um_per_px=0.5,
        )

        self.assertEqual(move["X"], (15.0, 15, False))
        self.assertEqual(move["Y"], (10.0, 10, False))

    def test_sub_half_pulse_offset_does_not_force_motion(self) -> None:
        app = self.make_app_shell()

        move = app._image_centering_move(
            point_x=400.4,
            point_y=225,
            image_width=800,
            image_height=450,
            um_per_px=0.1,
        )

        stage_um, pulses, reverse = move["X"]
        self.assertAlmostEqual(stage_um, 0.04)
        self.assertEqual(pulses, 0)
        self.assertFalse(reverse)

    def test_preview_and_cc_plan_share_signed_pulses(self) -> None:
        app = self.make_app_shell()
        app.probe_config.set_calibration(20, 2.0, 0.5)

        plan = app._image_centering_cc_plan(
            point_x=430,
            point_y=245,
            image_width=800,
            image_height=450,
            um_per_px=0.5,
        )
        preview = app.image_centering_preview(
            point_x=430,
            point_y=245,
            image_width=800,
            image_height=450,
        )

        self.assertEqual(plan["axis_params"], {1: (False, 15, 100, 10), 2: (True, 10, 100, 10)})
        self.assertEqual(plan["signed_pulses"], {"X": 15, "Y": -10})
        self.assertEqual(plan["target_positions"], {"X": 115, "Y": 190})
        self.assertEqual(preview, plan["preview_text"])

    def test_cc_plan_uses_configured_speed_and_acceleration(self) -> None:
        app = self.make_app_shell()
        app.probe_config.cc_speed_percent = 60
        app.probe_config.cc_accel_time_s = 0.25

        plan = app._image_centering_cc_plan(
            point_x=430,
            point_y=225,
            image_width=800,
            image_height=450,
            um_per_px=0.5,
        )

        self.assertEqual(plan["axis_params"], {1: (False, 15, 60, 25), 2: (False, 0, 0, 25)})


if __name__ == "__main__":
    unittest.main()
