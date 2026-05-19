from __future__ import annotations

import unittest
import queue

from semi_auto_probe.app import ProbeApp
from semi_auto_probe.config import ProbeConfig
from semi_auto_probe.gds_stage_mapper import (
    AffineCoordinateMapper,
    CalibrationPoint,
    CanvasTransform,
    stage_move_plan_from_um,
)
from semi_auto_probe.protocol import Axis, AxisPosition


class AffineCoordinateMapperTests(unittest.TestCase):
    def point(self, name: str, u: float, v: float, dx: float = 0.0, dy: float = 0.0) -> CalibrationPoint:
        return CalibrationPoint(
            name=name,
            u=u,
            v=v,
            x_um=10.0 + 2.0 * u + 0.5 * v + dx,
            y_um=-4.0 - 0.25 * u + 3.0 * v + dy,
        )

    def test_affine_fit_recovers_known_transform_and_inverse(self) -> None:
        mapper = AffineCoordinateMapper.fit(
            [
                self.point("P1", 0.0, 0.0),
                self.point("P2", 100.0, 0.0),
                self.point("P3", 0.0, 50.0),
                self.point("P4", 100.0, 50.0),
            ]
        )

        x_um, y_um = mapper.gds_to_stage(20.0, 10.0)
        self.assertAlmostEqual(x_um, 55.0)
        self.assertAlmostEqual(y_um, 21.0)

        u, v = mapper.stage_to_gds(x_um, y_um)
        self.assertAlmostEqual(u, 20.0)
        self.assertAlmostEqual(v, 10.0)
        self.assertAlmostEqual(mapper.rms_error_um, 0.0)

    def test_residuals_are_reported_for_overdetermined_fit(self) -> None:
        mapper = AffineCoordinateMapper.fit(
            [
                self.point("P1", 0.0, 0.0),
                self.point("P2", 100.0, 0.0),
                self.point("P3", 0.0, 50.0),
                self.point("P4", 100.0, 50.0, dx=8.0),
            ]
        )

        self.assertGreater(mapper.rms_error_um, 0.0)
        self.assertEqual(set(mapper.residuals_um), {"P1", "P2", "P3", "P4"})
        self.assertTrue(any(value > 0 for value in mapper.residuals_um.values()))

    def test_fit_rejects_missing_points(self) -> None:
        with self.assertRaisesRegex(ValueError, "Four complete"):
            AffineCoordinateMapper.fit(
                [
                    self.point("P1", 0.0, 0.0),
                    self.point("P2", 1.0, 0.0),
                    self.point("P3", 0.0, 1.0),
                    CalibrationPoint(name="P4", u=1.0, v=1.0, x_um=1.0, y_um=None),
                ]
            )

    def test_fit_rejects_duplicate_gds_points(self) -> None:
        with self.assertRaisesRegex(ValueError, "distinct"):
            AffineCoordinateMapper.fit(
                [
                    CalibrationPoint("P1", 0.0, 0.0, 0.0, 0.0),
                    CalibrationPoint("P2", 0.0, 0.0, 1.0, 0.0),
                    CalibrationPoint("P3", 0.0, 1.0, 0.0, 1.0),
                    CalibrationPoint("P4", 1.0, 1.0, 1.0, 1.0),
                ]
            )

    def test_fit_rejects_collinear_gds_points(self) -> None:
        with self.assertRaisesRegex(ValueError, "collinear"):
            AffineCoordinateMapper.fit(
                [
                    CalibrationPoint("P1", 0.0, 0.0, 0.0, 0.0),
                    CalibrationPoint("P2", 1.0, 1.0, 1.0, 0.0),
                    CalibrationPoint("P3", 2.0, 2.0, 2.0, 0.0),
                    CalibrationPoint("P4", 3.0, 3.0, 3.0, 0.0),
                ]
            )

    def test_fit_rejects_singular_stage_transform(self) -> None:
        with self.assertRaisesRegex(ValueError, "singular"):
            AffineCoordinateMapper.fit(
                [
                    CalibrationPoint("P1", 0.0, 0.0, 0.0, 0.0),
                    CalibrationPoint("P2", 1.0, 0.0, 1.0, 2.0),
                    CalibrationPoint("P3", 0.0, 1.0, 2.0, 4.0),
                    CalibrationPoint("P4", 1.0, 1.0, 3.0, 6.0),
                ]
            )


class CanvasTransformTests(unittest.TestCase):
    def test_canvas_gds_round_trip_and_y_orientation(self) -> None:
        transform = CanvasTransform()
        transform.fit_to_bounds((0.0, 0.0, 100.0, 50.0), width=800, height=400, padding=20)

        canvas_x, canvas_y = transform.gds_to_canvas(25.0, 10.0)
        u, v = transform.canvas_to_gds(canvas_x, canvas_y)

        self.assertAlmostEqual(u, 25.0)
        self.assertAlmostEqual(v, 10.0)

        _low_x, low_canvas_y = transform.gds_to_canvas(50.0, 5.0)
        _high_x, high_canvas_y = transform.gds_to_canvas(50.0, 45.0)
        self.assertLess(high_canvas_y, low_canvas_y)


class StageMovePlanTests(unittest.TestCase):
    def test_stage_um_target_converts_to_pulse_deltas(self) -> None:
        plan = stage_move_plan_from_um(
            {"X": 10, "Y": -4},
            target_x_um=6.0,
            target_y_um=-1.5,
            um_per_pulse_x=0.5,
            um_per_pulse_y=0.25,
        )

        self.assertEqual(plan.target_pulses, {"X": 12, "Y": -6})
        self.assertEqual(plan.deltas, {"X": 2, "Y": -2})
        self.assertTrue(plan.has_motion)

    def test_stage_um_target_rejects_invalid_scale(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            stage_move_plan_from_um({"X": 0, "Y": 0}, 0.0, 0.0, 0.0, 1.0)


class DummyMapperSerial:
    def __init__(self) -> None:
        self.positions = {"X": 10, "Y": -4, "Z": 0}
        self.axis_params = None

    def _entries(self):
        return [
            (b"", b"", AxisPosition(Axis.X, False, self.positions["X"], b"")),
            (b"", b"", AxisPosition(Axis.Y, False, self.positions["Y"], b"")),
            (b"", b"", AxisPosition(Axis.Z, False, self.positions["Z"], b"")),
        ]

    def read_stable_xyz_positions(self):
        return self._entries()

    def move_multi_axis_relative_and_wait(self, axis_params, timeout=10.0):
        self.axis_params = axis_params
        for axis, (reverse, pulses, _speed, _acceleration) in axis_params.items():
            if axis in (Axis.X, Axis.Y):
                self.positions[axis.name] += -pulses if reverse else pulses
        return b"command", b"done"

    def read_xyz_positions(self):
        return self._entries()


class GDSMapperMotionBridgeTests(unittest.TestCase):
    def test_worker_uses_existing_serial_move_api_with_um_target(self) -> None:
        app = ProbeApp.__new__(ProbeApp)
        app.probe_config = ProbeConfig()
        app.current_position_values = {"X": 10, "Y": -4, "Z": 0}
        app.result_queue = queue.Queue()
        app.serial_client = DummyMapperSerial()

        ProbeApp._gds_mapper_move_worker(app, target_x_um=12.0, target_y_um=-6.0)

        self.assertEqual(
            app.serial_client.axis_params,
            {
                Axis.X: (False, 2, 100, 10),
                Axis.Y: (True, 2, 100, 10),
            },
        )
        events = []
        while not app.result_queue.empty():
            events.append(app.result_queue.get_nowait())
        self.assertEqual(events[0][:4], ("motor_command", "XY", "cc GDS mapper", b"command"))
        self.assertEqual(events[1], ("cc_done", b"done", "gds_mapper"))
        self.assertEqual(events[-1], ("motor_done",))


if __name__ == "__main__":
    unittest.main()
