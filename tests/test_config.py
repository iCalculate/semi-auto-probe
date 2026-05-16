import tempfile
import unittest
from pathlib import Path

from semi_auto_probe.config import (
    DEFAULT_CONFIG_FILENAME,
    ProbeConfig,
    calibration_distance_px,
    calibration_key,
    load_probe_config,
    pulses_from_um,
    save_probe_config,
)


class ProbeConfigTest(unittest.TestCase):
    def test_default_motor_conversion(self) -> None:
        config = ProbeConfig()

        self.assertAlmostEqual(config.um_per_pulse("X"), 1.0)
        self.assertAlmostEqual(config.um_per_pulse("Y"), 1.0)
        self.assertAlmostEqual(config.um_per_pulse("Z"), 0.5)
        self.assertEqual(pulses_from_um(1000.0, config, "X"), 1000)
        self.assertEqual(pulses_from_um(1000.0, config, "Y"), 1000)
        self.assertEqual(pulses_from_um(1000.0, config, "Z"), 2000)
        self.assertEqual(config.cc_speed_percent, 100)
        self.assertAlmostEqual(config.cc_accel_time_s, 0.1)
        self.assertEqual(config.cc_acceleration_units(), 10)

    def test_calibration_lookup_is_per_lens_combination(self) -> None:
        config = ProbeConfig()
        config.set_calibration(20, 1.5, 0.42)
        config.set_calibration(10, 1.5, 0.84)

        self.assertEqual(calibration_key(20, 1.5), "objective_20__eyepiece_1.5")
        self.assertAlmostEqual(config.current_um_per_px(), 0.42)
        config.objective = 10
        self.assertAlmostEqual(config.current_um_per_px(), 0.84)
        config.eyepiece = 2.0
        self.assertIsNone(config.current_um_per_px())

    def test_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(objective=5, eyepiece=2.5, microstep=4, lead_xy_mm=2.0, lead_z_mm=1.0, cc_speed_percent=80, cc_accel_time_s=0.2)
            config.set_calibration(5, 2.5, 1.25)

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertEqual(loaded.objective, 5)
            self.assertEqual(loaded.eyepiece, 2.5)
            self.assertEqual(loaded.microstep, 4)
            self.assertEqual(loaded.lead_xy_mm, 2.0)
            self.assertEqual(loaded.lead_z_mm, 1.0)
            self.assertEqual(loaded.cc_speed_percent, 80)
            self.assertAlmostEqual(loaded.cc_accel_time_s, 0.2)
            self.assertAlmostEqual(loaded.current_um_per_px(), 1.25)

    def test_three_point_distance(self) -> None:
        distance = calibration_distance_px((0, 0), (10, 0), (5, 4))
        self.assertAlmostEqual(distance, 4.0)

        with self.assertRaises(ValueError):
            calibration_distance_px((1, 1), (1, 1), (2, 2))


if __name__ == "__main__":
    unittest.main()
