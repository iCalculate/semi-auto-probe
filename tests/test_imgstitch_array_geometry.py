from __future__ import annotations

import unittest

from semi_auto_probe.app import ProbeApp


class ImgStitchArrayGeometryTests(unittest.TestCase):
    def test_array_targets_are_centered_on_current_position_for_3x3(self) -> None:
        targets = {
            (row, col): ProbeApp._imgstitch_tile_target(
                origin_x=1000,
                origin_y=2000,
                row=row,
                col=col,
                rows=3,
                cols=3,
                step_x=100,
                step_y=200,
                range_mode="Array",
            )
            for row in range(3)
            for col in range(3)
        }

        self.assertEqual(targets[(1, 1)], (1000, 2000))
        self.assertEqual(targets[(0, 0)], (900, 1800))
        self.assertEqual(targets[(2, 2)], (1100, 2200))

    def test_non_array_targets_still_start_from_origin_corner(self) -> None:
        self.assertEqual(
            ProbeApp._imgstitch_tile_target(
                origin_x=1000,
                origin_y=2000,
                row=1,
                col=2,
                rows=3,
                cols=3,
                step_x=100,
                step_y=200,
                range_mode="Space",
            ),
            (1200, 2200),
        )


if __name__ == "__main__":
    unittest.main()
