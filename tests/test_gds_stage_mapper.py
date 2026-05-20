from __future__ import annotations

import unittest
import queue
import os
import tempfile

from semi_auto_probe.config import ProbeConfig
from semi_auto_probe.gds_stage_mapper import (
    AffineCoordinateMapper,
    CalibrationPoint,
    CanvasTransform,
    GDSCanvasViewer,
    GDSLayoutModel,
    GDSShape,
    GDSStageMapperPanel,
    LAYOUTBOND_AUTOSAVE_FILENAME,
    layer_grid_position,
    render_gds_preview_ppm,
    snap_gds_point,
    stage_move_plan_from_um,
    stage_xyz_move_plan_from_um,
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

    def test_snap_gds_point_uses_grid_in_micrometers(self) -> None:
        self.assertEqual(snap_gds_point((1.26, -2.24), 0.1), (1.3, -2.2))
        self.assertEqual(snap_gds_point((12.6, 17.4), 5.0), (15.0, 15.0))
        self.assertEqual(snap_gds_point((12.6, 17.4), 10.0), (10.0, 20.0))

    def test_layer_grid_position_is_horizontal_first_with_default_five_columns(self) -> None:
        self.assertEqual([layer_grid_position(index) for index in range(7)], [
            (0, 0),
            (0, 1),
            (0, 2),
            (0, 3),
            (0, 4),
            (1, 0),
            (1, 1),
        ])


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

    def test_stage_xyz_um_target_converts_to_pulse_deltas(self) -> None:
        plan = stage_xyz_move_plan_from_um(
            {"X": 10, "Y": -4, "Z": 7},
            {"X": 6.0, "Y": -1.5, "Z": 4.0},
            {"X": 0.5, "Y": 0.25, "Z": 2.0},
        )

        self.assertEqual(plan.target_pulses, {"X": 12, "Y": -6, "Z": 2})
        self.assertEqual(plan.deltas, {"X": 2, "Y": -2, "Z": -5})


class GDSCanvasViewerModelTests(unittest.TestCase):
    def test_new_model_defaults_all_layers_hidden(self) -> None:
        viewer = GDSCanvasViewer.__new__(GDSCanvasViewer)
        viewer.layer_order = []
        viewer.stage_center_gds = (1.0, 2.0)
        viewer.fov_polygon_gds = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0)]
        viewer.fit_to_view = lambda: None

        model = GDSLayoutModel(
            path=__file__,
            top_cell_name="TOP",
            top_cell_names=("TOP",),
            shapes=[
                GDSShape(points=((0.0, 0.0), (1.0, 0.0), (1.0, 1.0)), layer=1, datatype=0, bbox=(0.0, 0.0, 1.0, 1.0)),
                GDSShape(points=((2.0, 2.0), (3.0, 2.0), (3.0, 3.0)), layer=2, datatype=0, bbox=(2.0, 2.0, 3.0, 3.0)),
            ],
            labels=[],
            bounds=(0.0, 0.0, 3.0, 3.0),
        )

        GDSCanvasViewer.set_model(viewer, model)

        self.assertEqual(viewer.layer_visibility, {(1, 0): False, (2, 0): False})
        self.assertIsNone(viewer.stage_center_gds)
        self.assertIsNone(viewer.fov_polygon_gds)

    def test_raster_preview_draws_only_visible_layers(self) -> None:
        model = GDSLayoutModel(
            path=__file__,
            top_cell_name="TOP",
            top_cell_names=("TOP",),
            shapes=[
                GDSShape(points=((1.0, 1.0), (5.0, 1.0), (5.0, 5.0), (1.0, 5.0)), layer=1, datatype=0, bbox=(1.0, 1.0, 5.0, 5.0)),
                GDSShape(points=((8.0, 8.0), (12.0, 8.0), (12.0, 12.0), (8.0, 12.0)), layer=2, datatype=0, bbox=(8.0, 8.0, 12.0, 12.0)),
            ],
            labels=[],
            bounds=(0.0, 0.0, 20.0, 20.0),
        )
        transform = CanvasTransform(scale=1.0, offset_x=0.0, offset_y=20.0)

        hidden = render_gds_preview_ppm(
            model,
            transform,
            width=24,
            height=24,
            layer_visibility={(1, 0): False, (2, 0): False},
            layer_colors={(1, 0): "#ff0000", (2, 0): "#00ff00"},
        )
        visible = render_gds_preview_ppm(
            model,
            transform,
            width=24,
            height=24,
            layer_visibility={(1, 0): True, (2, 0): False},
            layer_colors={(1, 0): "#ff0000", (2, 0): "#00ff00"},
        )

        if hidden is None or visible is None:
            self.skipTest("OpenCV raster renderer is not available.")

        self.assertEqual(hidden[1], 0)
        self.assertEqual(visible[1], 1)
        pixels = visible[0].split(b"\n", 1)[1]
        pixel_triplets = {pixels[index : index + 3] for index in range(0, len(pixels), 3)}
        self.assertIn(b"\xff\x00\x00", pixel_triplets)
        self.assertNotIn(b"\x00\xff\x00", pixel_triplets)


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
            if axis in (Axis.X, Axis.Y, Axis.Z):
                self.positions[axis.name] += -pulses if reverse else pulses
        return b"command", b"done"

    def read_xyz_positions(self):
        return self._entries()


class DummyVar:
    def __init__(self, value: str = "") -> None:
        self.value = value

    def get(self) -> str:
        return self.value

    def set(self, value: str) -> None:
        self.value = value


class GDSMapperMotionBridgeTests(unittest.TestCase):
    def panel_for_coordinate_tests(self) -> GDSStageMapperPanel:
        panel = GDSStageMapperPanel.__new__(GDSStageMapperPanel)
        panel.coord_vars = {axis: DummyVar("0") for axis in ("X", "Y", "Z", "U", "V")}
        panel.coord_edit_modes = {axis: None for axis in ("X", "Y", "Z", "U", "V")}
        panel.coord_inputs = {}
        panel.modified_coord_axes = set()
        panel.current_coord_edit_mode = None
        panel.use_focus_z_var = DummyVar(False)
        panel.motion_status_var = DummyVar()
        panel.layout_jog_step_uv_var = DummyVar("1")
        panel.layout_jog_buttons = []
        panel.mapper = None
        panel.get_stage_position_um = lambda: (10.0, 20.0, 3.0)
        return panel

    def test_coordinate_stage_target_uses_panel_move_callback(self) -> None:
        moves = []
        panel = self.panel_for_coordinate_tests()
        panel.coord_vars["X"].set("12.5")
        panel.coord_vars["Y"].set("-3.25")
        panel.modified_coord_axes = {"X", "Y"}
        panel.coord_edit_modes["X"] = "Absolute"
        panel.coord_edit_modes["Y"] = "Absolute"
        panel.move_to_stage_xyz_um = lambda x_um, y_um, z_um: moves.append((x_um, y_um, z_um))

        GDSStageMapperPanel.move_coordinate_target(panel)

        self.assertEqual(moves, [(12.5, -3.25, None)])
        self.assertIn("Coordinate move requested", panel.motion_status_var.get())

    def test_relative_stage_target_offsets_current_stage_um(self) -> None:
        panel = self.panel_for_coordinate_tests()
        panel.coord_vars["X"].set("1.5")
        panel.coord_vars["Y"].set("-2.0")
        panel.coord_edit_modes["X"] = "Relative"
        panel.coord_edit_modes["Y"] = "Relative"
        panel.modified_coord_axes = {"X", "Y"}

        self.assertEqual(GDSStageMapperPanel._coordinate_target_from_edits(panel), (11.5, 18.0, None))

    def test_uv_target_uses_affine_mapping_when_bound(self) -> None:
        panel = self.panel_for_coordinate_tests()
        panel.mapper = AffineCoordinateMapper.fit(
            [
                CalibrationPoint("P1", 0.0, 0.0, 10.0, 20.0),
                CalibrationPoint("P2", 10.0, 0.0, 20.0, 20.0),
                CalibrationPoint("P3", 0.0, 10.0, 10.0, 30.0),
                CalibrationPoint("P4", 10.0, 10.0, 20.0, 30.0),
            ]
        )
        panel.coord_vars["U"].set("3")
        panel.coord_vars["V"].set("4")
        panel.coord_edit_modes["U"] = "Absolute"
        panel.coord_edit_modes["V"] = "Absolute"
        panel.modified_coord_axes = {"U", "V"}

        target = GDSStageMapperPanel._coordinate_target_from_edits(panel)
        self.assertAlmostEqual(target[0], 13.0)
        self.assertAlmostEqual(target[1], 24.0)
        self.assertIsNone(target[2])

    def test_focus_z_overrides_z_target_from_focus_callback(self) -> None:
        panel = self.panel_for_coordinate_tests()
        panel.use_focus_z_var = DummyVar(True)
        panel.get_focus_z_um = lambda x_um, y_um: x_um + y_um
        panel.coord_vars["X"].set("12")
        panel.coord_vars["Y"].set("4")
        panel.coord_vars["Z"].set("99")
        panel.coord_edit_modes["X"] = "Absolute"
        panel.coord_edit_modes["Y"] = "Absolute"
        panel.coord_edit_modes["Z"] = "Absolute"
        panel.modified_coord_axes = {"X", "Y", "Z"}

        self.assertEqual(GDSStageMapperPanel._coordinate_target_from_edits(panel), (12.0, 4.0, 16.0))

    def test_selected_target_move_uses_focus_z_when_enabled(self) -> None:
        moves = []
        panel = self.panel_for_coordinate_tests()
        panel.selected_target_stage_um = (4.0, 5.0)
        panel.use_focus_z_var = DummyVar(True)
        panel.get_focus_z_um = lambda x_um, y_um: x_um + y_um
        panel.move_to_stage_um = lambda x_um, y_um: moves.append((x_um, y_um, None))
        panel.move_to_stage_xyz_um = lambda x_um, y_um, z_um: moves.append((x_um, y_um, z_um))

        GDSStageMapperPanel.move_selected_target(panel)

        self.assertEqual(moves, [(4.0, 5.0, 9.0)])
        self.assertIn("Z 9", panel.motion_status_var.get())

    def test_selected_target_can_populate_coordinate_fields(self) -> None:
        panel = self.panel_for_coordinate_tests()
        panel.selected_target_stage_um = (4.0, 5.5)
        panel.coord_inputs = {}

        GDSStageMapperPanel.copy_selected_target_to_coordinates(panel)

        self.assertEqual(panel.coord_vars["X"].get(), "4")
        self.assertEqual(panel.coord_vars["Y"].get(), "5.5")
        self.assertIn("copied", panel.motion_status_var.get())

    def test_layout_uv_jog_requires_mapping_and_moves_mapped_stage(self) -> None:
        moves = []
        panel = self.panel_for_coordinate_tests()
        panel.mapper = AffineCoordinateMapper.fit(
            [
                CalibrationPoint("P1", 0.0, 0.0, 10.0, 20.0),
                CalibrationPoint("P2", 10.0, 0.0, 20.0, 20.0),
                CalibrationPoint("P3", 0.0, 10.0, 10.0, 30.0),
                CalibrationPoint("P4", 10.0, 10.0, 20.0, 30.0),
            ]
        )
        panel.move_to_stage_xyz_um = lambda x_um, y_um, z_um: moves.append((x_um, y_um, z_um))

        GDSStageMapperPanel._move_layout_uv_jog(panel, 1.0, 0.0)

        self.assertEqual(moves, [(11.0, 20.0, None)])
        self.assertIn("Layout UV jog requested", panel.motion_status_var.get())

    def test_autosave_writes_default_ignored_layoutbond_file(self) -> None:
        panel = self.panel_for_coordinate_tests()
        panel.gds_path = None
        panel.model = None
        panel.top_cell_var = DummyVar("-")
        panel.point_vars = {
            name: {
                "u": DummyVar(str(u)),
                "v": DummyVar(str(v)),
                "x_um": DummyVar(str(x_um)),
                "y_um": DummyVar(str(y_um)),
            }
            for name, u, v, x_um, y_um in (
                ("P1", 0, 0, 10, 20),
                ("P2", 10, 0, 20, 20),
                ("P3", 0, 10, 10, 30),
                ("P4", 10, 10, 20, 30),
            )
        }
        panel.fov_width_var = DummyVar("200")
        panel.fov_height_var = DummyVar("150")
        panel.residual_threshold_var = DummyVar("5")
        panel.mapper = AffineCoordinateMapper.fit(GDSStageMapperPanel._points_from_entries(panel))
        cwd = os.getcwd()
        with tempfile.TemporaryDirectory() as tmpdir:
            try:
                os.chdir(tmpdir)
                output = GDSStageMapperPanel._autosave_calibration_result(panel)
                self.assertEqual(output.name, LAYOUTBOND_AUTOSAVE_FILENAME)
                self.assertTrue(output.exists())
            finally:
                os.chdir(cwd)

    def test_worker_uses_existing_serial_move_api_with_um_target(self) -> None:
        try:
            from semi_auto_probe.app import ProbeApp
        except ModuleNotFoundError as exc:
            self.skipTest(f"ProbeApp import unavailable: {exc}")

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
        self.assertEqual(events[0][:4], ("motor_command", "XY", "cc LayoutBond", b"command"))
        self.assertEqual(events[1], ("cc_done", b"done", "gds_mapper"))
        self.assertEqual(events[-1], ("motor_done",))

    def test_worker_can_include_focus_z_stage_target(self) -> None:
        try:
            from semi_auto_probe.app import ProbeApp
        except ModuleNotFoundError as exc:
            self.skipTest(f"ProbeApp import unavailable: {exc}")

        app = ProbeApp.__new__(ProbeApp)
        app.probe_config = ProbeConfig()
        app.current_position_values = {"X": 10, "Y": -4, "Z": 0}
        app.result_queue = queue.Queue()
        app.serial_client = DummyMapperSerial()

        ProbeApp._gds_mapper_move_worker(app, target_x_um=12.0, target_y_um=-6.0, target_z_um=4.0)

        self.assertEqual(
            app.serial_client.axis_params,
            {
                Axis.X: (False, 2, 100, 10),
                Axis.Z: (False, 8, 100, 10),
                Axis.Y: (True, 2, 100, 10),
            },
        )


if __name__ == "__main__":
    unittest.main()
