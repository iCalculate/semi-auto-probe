from __future__ import annotations

import threading
import unittest
import queue

import numpy as np

from semi_auto_probe.app import ProbeApp
from semi_auto_probe.config import ProbeConfig


class DummySerial:
    def __init__(self) -> None:
        self.position = {"X": 0, "Y": 0, "Z": 100}

    def read_stable_xyz_positions(self):
        return []


class ImgStackAcquisitionTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(ProbeApp)
        app.probe_config = ProbeConfig()
        app.imgstitch_stop_event = threading.Event()
        app.result_queue = queue.Queue()
        app.imgstitch_session_dir = None
        app.serial_client = DummySerial()
        app.current_position_values = {"X": 0, "Y": 0, "Z": 100}
        return app

    def test_acquire_tile_single_frame_returns_one_capture(self) -> None:
        app = self.make_app()
        frame = np.full((5, 6, 3), 17, dtype=np.uint8)
        app._capture_stitch_frame = lambda: frame.copy()

        tile = ProbeApp.acquire_tile(app, "Single Frame", {})

        self.assertTrue(np.array_equal(tile, frame))

    def test_acquire_t_stack_tile_returns_fused_tile(self) -> None:
        app = self.make_app()
        frames = [
            np.full((5, 6, 3), 10, dtype=np.uint8),
            np.full((5, 6, 3), 20, dtype=np.uint8),
        ]
        app._capture_stitch_frame = lambda: frames.pop(0)

        tile = ProbeApp.acquire_t_stack_tile(app, 2, "average")

        self.assertEqual(int(tile[0, 0, 0]), 15)

    def test_acquire_z_stack_tile_moves_z_and_returns_to_center(self) -> None:
        app = self.make_app()
        captured = []
        moved = []

        def move_absolute(x_value, y_value, z_value):
            moved.append(z_value)
            app.current_position_values["Z"] = z_value
            return []

        def capture():
            value = 255 if app.current_position_values["Z"] == 100 else 0
            captured.append(value)
            return np.full((8, 8, 3), value, dtype=np.uint8)

        app._axis_from_position_entries = lambda _entries, axis: app.current_position_values[axis.name]
        app._move_absolute_stage = move_absolute
        app._wait_after_imgstitch_motion = lambda: None
        app._capture_stitch_frame = capture

        tile = ProbeApp.acquire_z_stack_tile(app, 0.5, 0.5, "laplacian", return_to_original_z=True)

        self.assertEqual(moved, [100, 101, 99, 100])
        self.assertEqual(len(captured), 3)
        self.assertEqual(tile.shape, (8, 8, 3))


if __name__ == "__main__":
    unittest.main()
