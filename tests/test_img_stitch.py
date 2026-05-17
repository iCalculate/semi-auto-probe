import unittest
import tempfile
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None
import numpy as np

from semi_auto_probe.img_stitch import (
    StitchSession,
    StitchSettings,
    TileRecord,
    compose_mosaic,
    estimate_overlap_shift,
    fit_plane,
    flat_field_correct,
    recompose_session,
    serpentine_indices,
    stage_positions_from_um,
)


@unittest.skipIf(cv2 is None, "OpenCV is not installed.")
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

    def test_stage_positions_from_um(self) -> None:
        records = (
            TileRecord(0, 0, 1, "a.png", 100, 200, 0, 10.0, 20.0),
            TileRecord(0, 1, 2, "b.png", 110, 200, 0, 30.0, 20.0),
            TileRecord(1, 1, 3, "c.png", 110, 220, 0, 30.0, 60.0),
        )

        positions = stage_positions_from_um(records, 2.0)

        self.assertEqual(positions[(0, 0)], (0.0, 0.0))
        self.assertEqual(positions[(0, 1)], (10.0, 0.0))
        self.assertEqual(positions[(1, 1)], (10.0, 20.0))

    def test_recompose_session_zero_weight_uses_stage_positions(self) -> None:
        left = np.zeros((40, 50, 3), dtype=np.uint8)
        right = np.zeros((40, 50, 3), dtype=np.uint8)
        cv2.circle(left, (35, 20), 6, (255, 255, 255), -1)
        cv2.circle(right, (5, 20), 6, (255, 255, 255), -1)
        session = StitchSession(
            rows=1,
            cols=2,
            tile_width=50,
            tile_height=40,
            um_per_px=2.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=80.0,
            step_y_um=80.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(overlap_x=20, overlap_y=20, max_correction_um=10.0, registration_weight=0.0),
            tiles=(
                TileRecord(0, 0, 1, "left.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "right.png", 80, 0, 0, 80.0, 0.0),
            ),
        )

        _mosaic, positions, edges = recompose_session(session, tile_images={(0, 0): left, (0, 1): right})

        self.assertEqual(positions[(0, 1)], (40.0, 0.0))
        self.assertEqual(len(edges), 1)
        self.assertAlmostEqual(edges[0].correction_um, 0.0)

    def test_recompose_session_clamps_registration_correction(self) -> None:
        source = np.zeros((50, 120, 3), dtype=np.uint8)
        cv2.circle(source, (70, 25), 8, (255, 255, 255), -1)
        left = source[:, :80].copy()
        right = source[:, 40:120].copy()
        session = StitchSession(
            rows=1,
            cols=2,
            tile_width=80,
            tile_height=50,
            um_per_px=1.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=60.0,
            step_y_um=60.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(overlap_x=40, overlap_y=20, max_correction_um=5.0, registration_weight=1.0),
            tiles=(
                TileRecord(0, 0, 1, "left.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "right.png", 60, 0, 0, 60.0, 0.0),
            ),
        )

        _mosaic, positions, edges = recompose_session(session, tile_images={(0, 0): left, (0, 1): right})

        self.assertLessEqual(abs(positions[(0, 1)][0] - 60.0), 5.01)
        self.assertLessEqual(edges[0].correction_um, 5.01)

    def test_session_json_round_trip(self) -> None:
        session = StitchSession(
            rows=1,
            cols=1,
            tile_width=20,
            tile_height=10,
            um_per_px=0.5,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=100.0,
            step_y_um=100.0,
            origin_stage_x=1,
            origin_stage_y=2,
            origin_stage_z=3,
            settings=StitchSettings(overlap_x=5, overlap_y=4, max_correction_um=8.0, registration_weight=0.25),
            tiles=(TileRecord(0, 0, 1, "tile.png", 1, 2, 3, 0.0, 0.0),),
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "session.json"
            session.save(path)
            loaded = StitchSession.load(path)

        self.assertEqual(loaded, session)


if __name__ == "__main__":
    unittest.main()
