from __future__ import annotations

import unittest

from semi_auto_probe.af_plane import (
    SamplePlaneModel,
    clear_sample_plane_model,
    fit_sample_plane,
    generate_af_mesh,
    get_focus_z_at_xy,
    set_sample_plane_model,
)


class AFPlaneTests(unittest.TestCase):
    def tearDown(self) -> None:
        clear_sample_plane_model()

    def test_rectangular_mesh_uses_serpentine_order(self) -> None:
        points = generate_af_mesh(
            mesh_type="Rectangular",
            center_x=100,
            center_y=200,
            x_range=20,
            y_range=20,
            rows=3,
            cols=3,
        )

        self.assertEqual(
            [(point.x, point.y) for point in points],
            [
                (90, 190),
                (100, 190),
                (110, 190),
                (110, 200),
                (100, 200),
                (90, 200),
                (90, 210),
                (100, 210),
                (110, 210),
            ],
        )

    def test_hexagonal_mesh_staggers_every_other_row(self) -> None:
        points = generate_af_mesh(
            mesh_type="Hexagonal",
            center_x=0,
            center_y=0,
            x_range=20,
            y_range=10,
            rows=2,
            cols=3,
        )

        self.assertEqual([(point.x, point.y) for point in points], [(-10, -5), (0, -5), (10, -5), (5, 5), (-5, 5)])

    def test_fit_sample_plane_recovers_coefficients_and_metrics(self) -> None:
        samples = [
            (0.0, 0.0, 10.0),
            (10.0, 0.0, 12.0),
            (0.0, 10.0, 9.0),
            (10.0, 10.0, 11.0),
        ]

        model = fit_sample_plane(samples)

        self.assertAlmostEqual(model.a, 0.2)
        self.assertAlmostEqual(model.b, -0.1)
        self.assertAlmostEqual(model.c, 10.0)
        self.assertAlmostEqual(model.implicit_coefficients()[0], 0.2)
        self.assertAlmostEqual(model.implicit_coefficients()[1], -0.1)
        self.assertAlmostEqual(model.implicit_coefficients()[2], -1.0)
        self.assertAlmostEqual(model.implicit_coefficients()[3], 10.0)
        self.assertAlmostEqual(model.rms_residual, 0.0, places=8)
        self.assertEqual(model.valid_points, 4)

    def test_shared_plane_state_returns_focus_z(self) -> None:
        model = SamplePlaneModel(
            enabled=True,
            type="plane",
            a=0.2,
            b=-0.1,
            c=10.0,
            rms_residual=0.0,
            pv_residual=0.0,
            max_abs_residual=0.0,
            tilt_x_deg=0.0,
            tilt_y_deg=0.0,
            valid_points=3,
            failed_points=0,
        )

        set_sample_plane_model(model)

        self.assertAlmostEqual(get_focus_z_at_xy(10.0, 5.0), 11.5)


if __name__ == "__main__":
    unittest.main()
