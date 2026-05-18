from __future__ import annotations

import unittest

from semi_auto_probe.app import ProbeApp


class AutoFocusLogicTests(unittest.TestCase):
    def test_coarse_wobble_uses_initial_step(self) -> None:
        self.assertEqual(
            ProbeApp._coarse_wobble_offsets(initial_step=50, search_range=120),
            [50, -50, 100, -100, 120, -120],
        )

    def test_fine_scan_is_single_direction_min_step(self) -> None:
        self.assertEqual(
            ProbeApp._fine_scan_positions(start_z=90, end_z=106, step=5),
            [90, 95, 100, 105, 106],
        )

    def test_fine_scan_bounds_stay_near_coarse_peak_center(self) -> None:
        self.assertEqual(
            ProbeApp._fine_scan_bounds(
                best_z=1000,
                initial_step=50,
                min_step=2,
                lower_bound=800,
                upper_bound=1200,
            ),
            (950, 1050),
        )

    def test_fine_scan_bounds_keep_minimum_min_step_window(self) -> None:
        self.assertEqual(
            ProbeApp._fine_scan_bounds(
                best_z=1000,
                initial_step=10,
                min_step=3,
                lower_bound=990,
                upper_bound=1008,
            ),
            (990, 1008),
        )

    def test_fine_scan_bounds_cover_at_least_eight_min_steps_each_side(self) -> None:
        self.assertEqual(
            ProbeApp._fine_scan_bounds(
                best_z=1000,
                initial_step=10,
                min_step=3,
                lower_bound=900,
                upper_bound=1100,
            ),
            (976, 1024),
        )

    def test_gaussian_fit_returns_peak_center(self) -> None:
        scores = {
            90: 20.0,
            95: 73.0,
            100: 100.0,
            105: 73.0,
            110: 20.0,
        }

        self.assertAlmostEqual(ProbeApp._fit_gaussian_focus_peak(scores), 100.0, delta=0.25)

    def test_gaussian_fit_model_exposes_mu_and_sigma(self) -> None:
        model = ProbeApp._fit_gaussian_focus_model(
            {
                90: 20.0,
                95: 73.0,
                100: 100.0,
                105: 73.0,
                110: 20.0,
            }
        )

        self.assertIsNotNone(model)
        assert model is not None
        self.assertAlmostEqual(model["mu"], 100.0, delta=0.25)
        self.assertEqual(model["baseline"], 0.0)
        self.assertGreater(model["sigma"], 0.0)
        self.assertGreater(ProbeApp._gaussian_score_at(model["mu"], model), 90.0)

    def test_gaussian_fit_falls_back_for_flat_scores(self) -> None:
        self.assertEqual(ProbeApp._fit_gaussian_focus_peak({1: 10.0, 2: 10.0, 3: 10.0}), 1.0)


if __name__ == "__main__":
    unittest.main()
