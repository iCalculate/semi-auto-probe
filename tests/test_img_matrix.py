from __future__ import annotations

import unittest

from semi_auto_probe.gds_stage_mapper import AffineCoordinateMapper, CalibrationPoint
from semi_auto_probe.img_matrix import ImgMatrixSettings, generate_imgmatrix_points, imgmatrix_filename


class ImgMatrixTests(unittest.TestCase):
    def mapper(self) -> AffineCoordinateMapper:
        return AffineCoordinateMapper.fit(
            [
                CalibrationPoint("P1", 0.0, 0.0, 100.0, 200.0),
                CalibrationPoint("P2", 10.0, 0.0, 120.0, 200.0),
                CalibrationPoint("P3", 0.0, 10.0, 100.0, 230.0),
                CalibrationPoint("P4", 10.0, 10.0, 120.0, 230.0),
            ]
        )

    def test_generate_points_from_gds_basis_vectors(self) -> None:
        points = generate_imgmatrix_points(
            ImgMatrixSettings(
                origin_u=1.0,
                origin_v=2.0,
                u_vector_u=10.0,
                u_vector_v=0.5,
                v_vector_u=-1.0,
                v_vector_v=5.0,
                rows=2,
                cols=3,
                fov_width_um=20.0,
                fov_height_um=10.0,
            ),
            self.mapper(),
        )

        self.assertEqual([(point.row, point.col, point.order) for point in points], [(0, 0, 1), (0, 1, 2), (0, 2, 3), (1, 0, 4), (1, 1, 5), (1, 2, 6)])
        self.assertAlmostEqual(points[0].u, 1.0)
        self.assertAlmostEqual(points[0].v, 2.0)
        self.assertAlmostEqual(points[4].u, 10.0)
        self.assertAlmostEqual(points[4].v, 7.5)
        self.assertAlmostEqual(points[0].stage_x_um, 102.0)
        self.assertAlmostEqual(points[0].stage_y_um, 206.0)
        self.assertEqual(len(points[0].fov_polygon_gds), 4)

    def test_invalid_matrix_settings_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            ImgMatrixSettings(0, 0, 1, 0, 0, 1, 0, 1, 10, 10).normalized()
        with self.assertRaisesRegex(ValueError, "non-zero"):
            ImgMatrixSettings(0, 0, 0, 0, 0, 1, 1, 1, 10, 10).normalized()

    def test_filename_uses_layout_uv_coordinates(self) -> None:
        self.assertEqual(imgmatrix_filename(1, 2, -12.5, 3.25), "r001_c002_um12p5_v3p25.png")


if __name__ == "__main__":
    unittest.main()
