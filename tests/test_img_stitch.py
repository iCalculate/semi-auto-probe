import unittest

import cv2
import numpy as np

from semi_auto_probe.img_stitch import compose_mosaic, estimate_overlap_shift, fit_plane, flat_field_correct, serpentine_indices


class ImgStitchTest(unittest.TestCase):
    def test_serpentine_indices(self) -> None:
        self.assertEqual(
            serpentine_indices(2, 3),
            [(0, 0), (0, 1), (0, 2), (1, 2), (1, 1), (1, 0)],
        )

    def test_fit_plane(self) -> None:
        plane = fit_plane([(0, 0, 10), (10, 0, 20), (0, 10, 30), (10, 10, 40)])
        self.assertAlmostEqual(plane.z_at(5, 5), 25.0)

    def test_flat_field_correction_reduces_gradient(self) -> None:
        height, width = 80, 120
        gradient = np.linspace(0.5, 1.5, width, dtype=np.float32)
        base = np.full((height, width, 3), 120, dtype=np.float32) * gradient[None, :, None]
        corrected = flat_field_correct(base.astype(np.uint8), blur_kernel=31)
        before_span = float(np.ptp(base.mean(axis=(0, 2))))
        after_span = float(np.ptp(corrected.mean(axis=(0, 2))))
        self.assertLess(after_span, before_span)

    def test_phase_correlation_horizontal_shift(self) -> None:
        image = np.zeros((80, 120, 3), dtype=np.uint8)
        cv2.circle(image, (70, 40), 12, (255, 255, 255), -1)
        previous = image[:, :80]
        current = image[:, 40:120]
        dx, dy, response = estimate_overlap_shift(previous, current, "right", overlap_x=40, overlap_y=20)
        self.assertAlmostEqual(dx, 40.0, delta=1.0)
        self.assertAlmostEqual(dy, 0.0, delta=1.0)
        self.assertGreater(response, 0.1)

    def test_compose_mosaic(self) -> None:
        left = np.full((10, 10, 3), 50, dtype=np.uint8)
        right = np.full((10, 10, 3), 150, dtype=np.uint8)
        mosaic = compose_mosaic({(0, 0): left, (0, 1): right}, {(0, 0): (0, 0), (0, 1): (8, 0)})
        self.assertEqual(mosaic.shape[:2], (10, 18))
        self.assertEqual(int(mosaic[0, 0, 0]), 50)
        self.assertEqual(int(mosaic[0, -1, 0]), 150)


if __name__ == "__main__":
    unittest.main()
