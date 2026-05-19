import unittest
import tempfile
from pathlib import Path

try:
    import cv2
except ImportError:
    cv2 = None
import numpy as np

import semi_auto_probe.img_stitch as img_stitch_module
from semi_auto_probe.img_stitch import (
    StitchSession,
    StitchSettings,
    TileRecord,
    compose_mosaic,
    estimate_overlap_shift,
    fit_plane,
    focus_sharpness_map,
    flat_field_correct,
    fuse_t_stack,
    fuse_z_stack,
    recompose_session,
    serpentine_indices,
    stage_positions_from_um,
    z_stack_positions,
)
from semi_auto_probe.config import ProbeConfig


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

    def test_t_stack_average_preserves_dtype_and_averages(self) -> None:
        frames = [
            np.full((4, 5, 3), 10, dtype=np.uint8),
            np.full((4, 5, 3), 20, dtype=np.uint8),
            np.full((4, 5, 3), 30, dtype=np.uint8),
        ]

        fused = fuse_t_stack(frames, "average")

        self.assertEqual(fused.dtype, np.uint8)
        self.assertEqual(fused.shape, frames[0].shape)
        self.assertEqual(int(fused[0, 0, 0]), 20)

    def test_t_stack_registered_average_accepts_shifted_frames(self) -> None:
        base = np.zeros((80, 100, 3), dtype=np.uint8)
        cv2.circle(base, (50, 40), 10, (240, 240, 240), -1)
        shifted = np.roll(base, shift=3, axis=1)

        fused = fuse_t_stack([base, shifted], "registered_average")

        self.assertEqual(fused.dtype, np.uint8)
        self.assertGreater(int(fused[40, 50, 0]), 180)

    def test_t_stack_sharpness_fusion_uses_locally_sharp_regions(self) -> None:
        pattern = np.zeros((80, 100, 3), dtype=np.uint8)
        for x in range(0, 100, 8):
            color = (255, 255, 255) if (x // 8) % 2 == 0 else (40, 40, 40)
            cv2.rectangle(pattern, (x, 0), (x + 3, 79), color, -1)
        for y in range(0, 80, 8):
            color = (230, 230, 230) if (y // 8) % 2 == 0 else (20, 20, 20)
            cv2.line(pattern, (0, y), (99, y), color, 1)
        blurred = cv2.GaussianBlur(pattern, (13, 13), 0)
        frame_a = pattern.copy()
        frame_a[:, 50:] = blurred[:, 50:]
        frame_b = pattern.copy()
        frame_b[:, :50] = blurred[:, :50]

        average = fuse_t_stack([frame_a, frame_b], "average")
        fused = fuse_t_stack([frame_a, frame_b], "sharpness_fusion")
        avg_score = focus_sharpness_map(average, "tenengrad").mean()
        fused_score = focus_sharpness_map(fused, "tenengrad").mean()

        self.assertEqual(fused.dtype, np.uint8)
        self.assertGreater(fused_score, avg_score * 1.15)

    def test_focus_sharpness_map_and_z_stack_select_sharp_slice(self) -> None:
        sharp_left = np.zeros((40, 40, 3), dtype=np.uint8)
        sharp_right = np.zeros((40, 40, 3), dtype=np.uint8)
        cv2.rectangle(sharp_left, (4, 10), (16, 30), (255, 255, 255), -1)
        cv2.rectangle(sharp_right, (24, 10), (36, 30), (255, 255, 255), -1)
        blur_left = cv2.GaussianBlur(sharp_left, (9, 9), 0)
        blur_right = cv2.GaussianBlur(sharp_right, (9, 9), 0)
        frame_a = blur_right.copy()
        frame_a[:, :20] = sharp_left[:, :20]
        frame_b = blur_left.copy()
        frame_b[:, 20:] = sharp_right[:, 20:]

        sharpness = focus_sharpness_map(frame_a, "laplacian")
        fused = fuse_z_stack([frame_a, frame_b], "laplacian")

        self.assertEqual(sharpness.shape, frame_a.shape[:2])
        self.assertGreater(int(fused[20, 10, 0]), 200)
        self.assertGreater(int(fused[20, 30, 0]), 200)

    def test_z_stack_positions_swing_from_center(self) -> None:
        config = ProbeConfig()

        positions = z_stack_positions(center_z=100, z_range_um=2.0, z_step_um=0.5, config=config)

        self.assertEqual(positions, [100, 101, 99, 102, 98, 103, 97, 104, 96])

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
        self.assertEqual(positions[(1, 1)], (10.0, -20.0))

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

    def test_recompose_session_reports_all_internal_grid_edges(self) -> None:
        tiles = {
            (0, 0): np.full((20, 20, 3), 30, dtype=np.uint8),
            (0, 1): np.full((20, 20, 3), 60, dtype=np.uint8),
            (1, 0): np.full((20, 20, 3), 90, dtype=np.uint8),
            (1, 1): np.full((20, 20, 3), 120, dtype=np.uint8),
        }
        session = StitchSession(
            rows=2,
            cols=2,
            tile_width=20,
            tile_height=20,
            um_per_px=1.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=15.0,
            step_y_um=15.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(overlap_x=5, overlap_y=5, max_correction_um=0.0, registration_weight=0.0),
            tiles=(
                TileRecord(0, 0, 1, "a.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "b.png", 15, 0, 0, 15.0, 0.0),
                TileRecord(1, 1, 3, "d.png", 15, 15, 0, 15.0, 15.0),
                TileRecord(1, 0, 4, "c.png", 0, 15, 0, 0.0, 15.0),
            ),
        )

        _mosaic, _positions, edges = recompose_session(session, tile_images=tiles)

        self.assertEqual(len(edges), 4)
        self.assertEqual({edge.current for edge in edges}, {(0, 1), (1, 0), (1, 1)})

    def test_green_edge_correction_replaces_bad_path_shift(self) -> None:
        tiles = {(0, col): np.full((20, 20, 3), col * 40, dtype=np.uint8) for col in range(4)}
        session = StitchSession(
            rows=1,
            cols=4,
            tile_width=20,
            tile_height=20,
            um_per_px=1.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=10.0,
            step_y_um=10.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(
                overlap_x=10,
                overlap_y=10,
                max_correction_um=100.0,
                registration_weight=1.0,
                seam_response_yellow=0.2,
                seam_response_green=0.5,
                use_green_edge_correction=True,
            ),
            tiles=(
                TileRecord(0, 0, 1, "a.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "b.png", 10, 0, 0, 10.0, 0.0),
                TileRecord(0, 2, 3, "c.png", 20, 0, 0, 20.0, 0.0),
                TileRecord(0, 3, 4, "d.png", 30, 0, 0, 30.0, 0.0),
            ),
        )
        shifts = {
            ((0, 0), (0, 1)): (10.0, 0.0, 0.9),
            ((0, 1), (0, 2)): (12.0, 0.0, 0.9),
            ((0, 2), (0, 3)): (40.0, 0.0, 0.0),
        }
        original = img_stitch_module.estimate_overlap_shift

        def fake_estimate(_previous, _current, _direction, _overlap_x, _overlap_y):
            del _previous, _current, _direction, _overlap_x, _overlap_y
            key = fake_estimate.calls.pop(0)
            return shifts[key]

        try:
            fake_estimate.calls = [((0, 0), (0, 1)), ((0, 1), (0, 2)), ((0, 2), (0, 3))] * 4  # type: ignore[attr-defined]
            img_stitch_module.estimate_overlap_shift = fake_estimate  # type: ignore[assignment]
            _mosaic, positions, edges = recompose_session(session, tile_images=tiles)
        finally:
            img_stitch_module.estimate_overlap_shift = original  # type: ignore[assignment]

        bad_edge = next(edge for edge in edges if edge.current == (0, 3))
        self.assertTrue(bad_edge.was_corrected)
        self.assertEqual(bad_edge.quality, "bad")
        self.assertAlmostEqual(bad_edge.corrected_shift_px[0], 11.0)
        self.assertAlmostEqual(positions[(0, 3)][0], 33.0)

    def test_green_edge_correction_reverses_right_center_for_left_path(self) -> None:
        tiles = {
            (0, 0): np.full((20, 20, 3), 10, dtype=np.uint8),
            (0, 1): np.full((20, 20, 3), 20, dtype=np.uint8),
            (0, 2): np.full((20, 20, 3), 30, dtype=np.uint8),
            (1, 2): np.full((20, 20, 3), 40, dtype=np.uint8),
            (1, 1): np.full((20, 20, 3), 50, dtype=np.uint8),
        }
        session = StitchSession(
            rows=2,
            cols=3,
            tile_width=20,
            tile_height=20,
            um_per_px=1.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=10.0,
            step_y_um=10.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(
                overlap_x=10,
                overlap_y=10,
                max_correction_um=100.0,
                registration_weight=0.0,
                seam_response_yellow=0.2,
                seam_response_green=0.5,
                use_green_edge_correction=True,
            ),
            tiles=(
                TileRecord(0, 0, 1, "a.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "b.png", 10, 0, 0, 10.0, 0.0),
                TileRecord(0, 2, 3, "c.png", 20, 0, 0, 20.0, 0.0),
                TileRecord(1, 2, 4, "d.png", 20, 10, 0, 20.0, 10.0),
                TileRecord(1, 1, 5, "e.png", 10, 10, 0, 10.0, 10.0),
            ),
        )
        calls = {
            ((0, 0), (0, 1)): (10.0, 0.0, 0.9),
            ((0, 1), (0, 2)): (12.0, 0.0, 0.9),
            ((0, 2), (1, 2)): (0.0, -10.0, 0.9),
            ((1, 2), (1, 1)): (-40.0, 0.0, 0.0),
        }
        original = img_stitch_module.estimate_overlap_shift

        def fake_estimate(_previous, _current, direction, _overlap_x, _overlap_y):
            del _previous, _current, _overlap_x, _overlap_y
            if direction == "right":
                value = fake_estimate.right_values.pop(0)
            elif direction == "up":
                value = (0.0, -10.0, 0.9)
            elif direction == "left":
                value = (-40.0, 0.0, 0.0)
            else:
                value = calls[((0, 0), (0, 1))]
            return value

        try:
            fake_estimate.right_values = [(10.0, 0.0, 0.9), (12.0, 0.0, 0.9)] * 4  # type: ignore[attr-defined]
            img_stitch_module.estimate_overlap_shift = fake_estimate  # type: ignore[assignment]
            _mosaic, positions, edges = recompose_session(session, tile_images=tiles)
        finally:
            img_stitch_module.estimate_overlap_shift = original  # type: ignore[assignment]

        self.assertTrue(any(edge.was_corrected for edge in edges))
        self.assertAlmostEqual(positions[(1, 1)][0], 10.0)

    def test_green_edge_correction_can_be_disabled(self) -> None:
        tiles = {(0, col): np.full((20, 20, 3), col * 40, dtype=np.uint8) for col in range(4)}
        session = StitchSession(
            rows=1,
            cols=4,
            tile_width=20,
            tile_height=20,
            um_per_px=1.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=10.0,
            step_y_um=10.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(
                overlap_x=10,
                overlap_y=10,
                max_correction_um=100.0,
                registration_weight=1.0,
                seam_response_yellow=0.2,
                seam_response_green=0.5,
                use_green_edge_correction=False,
            ),
            tiles=(
                TileRecord(0, 0, 1, "a.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "b.png", 10, 0, 0, 10.0, 0.0),
                TileRecord(0, 2, 3, "c.png", 20, 0, 0, 20.0, 0.0),
                TileRecord(0, 3, 4, "d.png", 30, 0, 0, 30.0, 0.0),
            ),
        )
        responses = [(10.0, 0.0, 0.9), (12.0, 0.0, 0.9), (40.0, 0.0, 0.0)] * 4
        original = img_stitch_module.estimate_overlap_shift

        def fake_estimate(_previous, _current, _direction, _overlap_x, _overlap_y):
            del _previous, _current, _direction, _overlap_x, _overlap_y
            return responses.pop(0)

        try:
            img_stitch_module.estimate_overlap_shift = fake_estimate  # type: ignore[assignment]
            _mosaic, positions, edges = recompose_session(session, tile_images=tiles)
        finally:
            img_stitch_module.estimate_overlap_shift = original  # type: ignore[assignment]

        self.assertFalse(any(edge.was_corrected for edge in edges))
        self.assertAlmostEqual(positions[(0, 3)][0], 62.0)

    def test_green_edge_correction_skips_when_too_few_green_edges(self) -> None:
        tiles = {(0, col): np.full((20, 20, 3), col * 40, dtype=np.uint8) for col in range(3)}
        session = StitchSession(
            rows=1,
            cols=3,
            tile_width=20,
            tile_height=20,
            um_per_px=1.0,
            objective=20,
            eyepiece=1.5,
            range_mode="array",
            step_x_um=10.0,
            step_y_um=10.0,
            origin_stage_x=0,
            origin_stage_y=0,
            origin_stage_z=0,
            settings=StitchSettings(
                overlap_x=10,
                overlap_y=10,
                max_correction_um=100.0,
                registration_weight=1.0,
                seam_response_yellow=0.2,
                seam_response_green=0.5,
                use_green_edge_correction=True,
            ),
            tiles=(
                TileRecord(0, 0, 1, "a.png", 0, 0, 0, 0.0, 0.0),
                TileRecord(0, 1, 2, "b.png", 10, 0, 0, 10.0, 0.0),
                TileRecord(0, 2, 3, "c.png", 20, 0, 0, 20.0, 0.0),
            ),
        )
        responses = [(10.0, 0.0, 0.9), (40.0, 0.0, 0.0)] * 4
        original = img_stitch_module.estimate_overlap_shift

        def fake_estimate(_previous, _current, _direction, _overlap_x, _overlap_y):
            del _previous, _current, _direction, _overlap_x, _overlap_y
            return responses.pop(0)

        try:
            img_stitch_module.estimate_overlap_shift = fake_estimate  # type: ignore[assignment]
            _mosaic, _positions, edges = recompose_session(session, tile_images=tiles)
        finally:
            img_stitch_module.estimate_overlap_shift = original  # type: ignore[assignment]

        self.assertFalse(any(edge.was_corrected for edge in edges))


if __name__ == "__main__":
    unittest.main()
