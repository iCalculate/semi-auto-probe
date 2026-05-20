import tempfile
import unittest
from pathlib import Path

from semi_auto_probe.config import (
    AUTOFOCUS_PEAK_MODEL_LORENTZIAN,
    DEFAULT_AGENT_BASE_URL,
    DEFAULT_AGENT_MODEL,
    DEFAULT_CONFIG_FILENAME,
    KEYBOARD_MOTION_SCHEME_ARROW_PAGE,
    KEYBOARD_MOTION_SCHEME_WASD_QE,
    MOTOR_SPEED_PROFILE_FAST,
    MOTOR_SPEED_PROFILE_SAFE,
    ProbeConfig,
    calibration_distance_px,
    calibration_key,
    derive_missing_calibrations,
    format_jog_step_levels,
    load_probe_config,
    parse_jog_step_levels_text,
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
        self.assertEqual(config.fine_speed_percent, 40)
        self.assertEqual(config.safe_speed_percent, 15)
        self.assertEqual(config.active_motor_speed_profile, MOTOR_SPEED_PROFILE_FAST)
        self.assertEqual(config.motor_speed_percent(), 100)
        self.assertEqual(
            config.controller_motion_parameters,
            {
                "X": {"minimum_speed": 0, "work_speed": 0, "acceleration": 0},
                "Y": {"minimum_speed": 0, "work_speed": 0, "acceleration": 0},
                "Z": {"minimum_speed": 0, "work_speed": 0, "acceleration": 0},
            },
        )
        self.assertAlmostEqual(config.cc_accel_time_s, 0.1)
        self.assertEqual(config.cc_acceleration_units(), 10)

    def test_calibration_lookup_is_per_lens_combination(self) -> None:
        config = ProbeConfig()
        config.eyepiece = 1.5
        config.set_calibration(20, 1.5, 0.42)
        config.set_calibration(10, 1.5, 0.84)

        self.assertEqual(calibration_key(20, 1.5), "objective_20__eyepiece_1.5")
        self.assertAlmostEqual(config.current_um_per_px(), 0.42)
        config.objective = 10
        self.assertAlmostEqual(config.current_um_per_px(), 0.84)
        config.eyepiece = 2.0
        self.assertIsNone(config.current_um_per_px())

    def test_derives_missing_calibrations_from_20x_1_5x_without_overwrite(self) -> None:
        config = ProbeConfig()
        config.set_calibration(20, 1.5, 0.42)
        config.set_calibration(10, 1.5, 99.0)

        added = derive_missing_calibrations(config)

        self.assertGreater(added, 0)
        self.assertAlmostEqual(config.calibrations[calibration_key(10, 1.5)], 99.0)
        self.assertAlmostEqual(config.calibrations[calibration_key(5, 3.0)], 0.42 * (20 * 1.5) / (5 * 3.0))

    def test_json_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(
                objective=5,
                eyepiece=2.5,
                microstep=4,
                lead_xy_mm=2.0,
                lead_z_mm=1.0,
                cc_speed_percent=80,
                fine_speed_percent=35,
                safe_speed_percent=12,
                active_motor_speed_profile=MOTOR_SPEED_PROFILE_SAFE,
                controller_motion_parameters={
                    "X": {"minimum_speed": 5, "work_speed": 100, "acceleration": 10},
                    "Y": {"minimum_speed": 6, "work_speed": 90, "acceleration": 11},
                    "Z": {"minimum_speed": 7, "work_speed": 80, "acceleration": 12},
                },
                cc_accel_time_s=0.2,
            )
            config.set_calibration(5, 2.5, 1.25)

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertEqual(loaded.objective, 5)
            self.assertEqual(loaded.eyepiece, 2.5)
            self.assertEqual(loaded.microstep, 4)
            self.assertEqual(loaded.lead_xy_mm, 2.0)
            self.assertEqual(loaded.lead_z_mm, 1.0)
            self.assertEqual(loaded.cc_speed_percent, 80)
            self.assertEqual(loaded.fine_speed_percent, 35)
            self.assertEqual(loaded.safe_speed_percent, 12)
            self.assertEqual(loaded.active_motor_speed_profile, MOTOR_SPEED_PROFILE_SAFE)
            self.assertEqual(loaded.motor_speed_percent(), 12)
            self.assertEqual(loaded.controller_motion_parameters["X"], {"minimum_speed": 5, "work_speed": 100, "acceleration": 10})
            self.assertEqual(loaded.controller_motion_parameters["Y"], {"minimum_speed": 6, "work_speed": 90, "acceleration": 11})
            self.assertEqual(loaded.controller_motion_parameters["Z"], {"minimum_speed": 7, "work_speed": 80, "acceleration": 12})
            self.assertAlmostEqual(loaded.cc_accel_time_s, 0.2)
            self.assertAlmostEqual(loaded.current_um_per_px(), 1.25)

    def test_imgstitch_seam_thresholds_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(imgstitch_seam_response_yellow=0.2, imgstitch_seam_response_green=0.45)

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertAlmostEqual(loaded.imgstitch_seam_response_yellow, 0.2)
            self.assertAlmostEqual(loaded.imgstitch_seam_response_green, 0.45)

    def test_autofocus_config_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(
                autofocus_settle_ms=150,
                autofocus_sample_count=7,
                autofocus_peak_model=AUTOFOCUS_PEAK_MODEL_LORENTZIAN,
                imgstitch_settle_ms=175,
                focus_threshold_yellow={"Laplacian": 700.0, "Tenengrad": 800.0, "Brenner": 900.0},
                focus_threshold_green={"Laplacian": 1700.0, "Tenengrad": 1800.0, "Brenner": 1900.0},
            )

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertEqual(loaded.autofocus_settle_ms, 150)
            self.assertEqual(loaded.autofocus_sample_count, 7)
            self.assertEqual(loaded.autofocus_peak_model, AUTOFOCUS_PEAK_MODEL_LORENTZIAN)
            self.assertEqual(loaded.imgstitch_settle_ms, 175)
            self.assertEqual(loaded.focus_threshold_yellow["Laplacian"], 700.0)
            self.assertEqual(loaded.focus_threshold_green["Brenner"], 1900.0)

    def test_keyboard_motion_scheme_defaults_and_round_trips(self) -> None:
        self.assertEqual(ProbeConfig().keyboard_motion_scheme, KEYBOARD_MOTION_SCHEME_ARROW_PAGE)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(keyboard_motion_scheme=KEYBOARD_MOTION_SCHEME_WASD_QE)

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertEqual(loaded.keyboard_motion_scheme, KEYBOARD_MOTION_SCHEME_WASD_QE)

    def test_agent_api_defaults_and_round_trip(self) -> None:
        self.assertEqual(ProbeConfig().agent_base_url, DEFAULT_AGENT_BASE_URL)
        self.assertEqual(ProbeConfig().agent_model, DEFAULT_AGENT_MODEL)
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(
                agent_api_key="sk-test",
                agent_base_url="https://api.deepseek.com",
                agent_model="deepseek-chat",
                agent_timeout_seconds=12.5,
            )

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertEqual(loaded.agent_api_key, "sk-test")
            self.assertEqual(loaded.agent_base_url, "https://api.deepseek.com")
            self.assertEqual(loaded.agent_model, "deepseek-chat")
            self.assertAlmostEqual(loaded.agent_timeout_seconds, 12.5)

    def test_jog_step_levels_parse_and_format_text(self) -> None:
        levels = parse_jog_step_levels_text("100, 1, 10, 10; 1000")

        self.assertEqual(levels, (1, 10, 100, 1000))
        self.assertEqual(format_jog_step_levels(levels), "1, 10, 100, 1000")

    def test_jog_step_levels_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / DEFAULT_CONFIG_FILENAME
            config = ProbeConfig(
                jog_step_levels={
                    "X": (1, 5, 25),
                    "Y": (2, 10, 50),
                    "Z": (3, 15, 75),
                }
            )

            save_probe_config(config, path)
            loaded = load_probe_config(path)

            self.assertEqual(loaded.jog_step_levels["X"], (1, 5, 25))
            self.assertEqual(loaded.jog_step_levels["Y"], (2, 10, 50))
            self.assertEqual(loaded.jog_step_levels["Z"], (3, 15, 75))

    def test_jog_step_levels_must_be_positive_integers(self) -> None:
        with self.assertRaises(ValueError):
            parse_jog_step_levels_text("1, 0, 10")
        with self.assertRaises(ValueError):
            parse_jog_step_levels_text("1, 2.5")

    def test_green_threshold_must_not_be_below_yellow(self) -> None:
        config = ProbeConfig(
            focus_threshold_yellow={"Laplacian": 100.0, "Tenengrad": 100.0, "Brenner": 100.0},
            focus_threshold_green={"Laplacian": 99.0, "Tenengrad": 100.0, "Brenner": 100.0},
        )

        with self.assertRaises(ValueError):
            config.validate()

    def test_three_point_distance(self) -> None:
        distance = calibration_distance_px((0, 0), (10, 0), (5, 4))
        self.assertAlmostEqual(distance, 4.0)

        with self.assertRaises(ValueError):
            calibration_distance_px((1, 1), (1, 1), (2, 2))


if __name__ == "__main__":
    unittest.main()
