from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_FILENAME = "probe_config.local.json"
OBJECTIVE_OPTIONS = (20, 10, 5)
EYEPIECE_OPTIONS = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
CALIBRATION_REFERENCE_OBJECTIVE = 20
CALIBRATION_REFERENCE_EYEPIECE = 1.5
KEYBOARD_MOTION_SCHEME_ARROW_PAGE = "arrow_page"
KEYBOARD_MOTION_SCHEME_WASD_QE = "wasd_qe"
KEYBOARD_MOTION_SCHEMES = (
    KEYBOARD_MOTION_SCHEME_ARROW_PAGE,
    KEYBOARD_MOTION_SCHEME_WASD_QE,
)
KEYBOARD_MOTION_SCHEME_LABELS = {
    KEYBOARD_MOTION_SCHEME_ARROW_PAGE: "Arrow keys + PageUp/PageDown",
    KEYBOARD_MOTION_SCHEME_WASD_QE: "WASD + Q/E",
}
MOTOR_SPEED_PROFILE_FAST = "fast"
MOTOR_SPEED_PROFILE_FINE = "fine"
MOTOR_SPEED_PROFILE_SAFE = "safe"
MOTOR_SPEED_PROFILES = (
    MOTOR_SPEED_PROFILE_FAST,
    MOTOR_SPEED_PROFILE_FINE,
    MOTOR_SPEED_PROFILE_SAFE,
)
MOTOR_SPEED_PROFILE_LABELS = {
    MOTOR_SPEED_PROFILE_FAST: "Fast Move",
    MOTOR_SPEED_PROFILE_FINE: "Fine Position",
    MOTOR_SPEED_PROFILE_SAFE: "Safe Debug",
}


def normalize_motor_speed_profile(value: Any) -> str:
    normalized = str(value or MOTOR_SPEED_PROFILE_FAST).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        MOTOR_SPEED_PROFILE_FAST: MOTOR_SPEED_PROFILE_FAST,
        MOTOR_SPEED_PROFILE_FINE: MOTOR_SPEED_PROFILE_FINE,
        MOTOR_SPEED_PROFILE_SAFE: MOTOR_SPEED_PROFILE_SAFE,
        "fast_move": MOTOR_SPEED_PROFILE_FAST,
        "fine_position": MOTOR_SPEED_PROFILE_FINE,
        "safe_debug": MOTOR_SPEED_PROFILE_SAFE,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in MOTOR_SPEED_PROFILES:
        raise ValueError(f"Motor speed profile must be one of {MOTOR_SPEED_PROFILES}.")
    return normalized


AUTOFOCUS_PEAK_MODEL_GAUSSIAN = "gaussian"
AUTOFOCUS_PEAK_MODEL_LORENTZIAN = "lorentzian"
AUTOFOCUS_PEAK_MODEL_PARABOLIC = "parabolic"
AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT = "pseudo_voigt"
AUTOFOCUS_PEAK_MODELS = (
    AUTOFOCUS_PEAK_MODEL_GAUSSIAN,
    AUTOFOCUS_PEAK_MODEL_LORENTZIAN,
    AUTOFOCUS_PEAK_MODEL_PARABOLIC,
    AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT,
)
AUTOFOCUS_PEAK_MODEL_LABELS = {
    AUTOFOCUS_PEAK_MODEL_GAUSSIAN: "Gaussian",
    AUTOFOCUS_PEAK_MODEL_LORENTZIAN: "Lorentzian",
    AUTOFOCUS_PEAK_MODEL_PARABOLIC: "Parabolic",
    AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT: "Pseudo-Voigt",
}
JOG_STEP_AXES = ("X", "Y", "Z")
DEFAULT_JOG_STEP_LEVELS = {axis: (1, 10, 100, 1000) for axis in JOG_STEP_AXES}
CONTROLLER_MOTION_PARAMETER_FIELDS = ("minimum_speed", "work_speed", "acceleration")
DEFAULT_CONTROLLER_MOTION_PARAMETERS = {
    axis: {field_name: 0 for field_name in CONTROLLER_MOTION_PARAMETER_FIELDS}
    for axis in JOG_STEP_AXES
}
DEFAULT_AGENT_BASE_URL = "https://api.deepseek.com"
DEFAULT_AGENT_MODEL = "deepseek-chat"
DEFAULT_AGENT_TIMEOUT_SECONDS = 30.0


def parse_jog_step_levels_text(text: str) -> tuple[int, ...]:
    parts = [part for part in re.split(r"[\s,;]+", text.strip()) if part]
    if not parts:
        raise ValueError("Jog step levels cannot be empty.")
    values: list[int] = []
    for part in parts:
        try:
            value = int(part)
        except ValueError as exc:
            raise ValueError(f"Jog step level must be an integer: {part!r}.") from exc
        if value <= 0:
            raise ValueError("Jog step levels must be positive integers.")
        values.append(value)
    return tuple(sorted(set(values)))


def format_jog_step_levels(levels: tuple[int, ...] | list[int]) -> str:
    return ", ".join(str(value) for value in levels)


def normalize_autofocus_peak_model(value: Any) -> str:
    normalized = str(value or AUTOFOCUS_PEAK_MODEL_GAUSSIAN).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "normal": AUTOFOCUS_PEAK_MODEL_GAUSSIAN,
        "normal_distribution": AUTOFOCUS_PEAK_MODEL_GAUSSIAN,
        "cauchy": AUTOFOCUS_PEAK_MODEL_LORENTZIAN,
        "quadratic": AUTOFOCUS_PEAK_MODEL_PARABOLIC,
        "pseudo_voigt": AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT,
        "pseudovoigt": AUTOFOCUS_PEAK_MODEL_PSEUDO_VOIGT,
    }
    normalized = aliases.get(normalized, normalized)
    if normalized not in AUTOFOCUS_PEAK_MODELS:
        raise ValueError(f"AutoFocus peak model must be one of {AUTOFOCUS_PEAK_MODELS}.")
    return normalized


def normalize_jog_step_levels_map(data: Any) -> dict[str, tuple[int, ...]]:
    raw_map = data if isinstance(data, dict) else {}
    normalized: dict[str, tuple[int, ...]] = {}
    for axis in JOG_STEP_AXES:
        raw_value = raw_map.get(axis, raw_map.get(axis.lower(), DEFAULT_JOG_STEP_LEVELS[axis]))
        if isinstance(raw_value, str):
            levels = parse_jog_step_levels_text(raw_value)
        else:
            try:
                levels = parse_jog_step_levels_text(", ".join(str(value) for value in raw_value))
            except TypeError as exc:
                raise ValueError(f"{axis} jog step levels must be a list or comma-separated text.") from exc
        normalized[axis] = levels
    return normalized


def normalize_controller_motion_parameters_map(data: Any) -> dict[str, dict[str, int]]:
    raw_map = data if isinstance(data, dict) else {}
    aliases = {
        "minimum_speed": ("minimum_speed", "min_speed", "min", "minimum"),
        "work_speed": ("work_speed", "working_speed", "work", "speed"),
        "acceleration": ("acceleration", "accel"),
    }
    normalized: dict[str, dict[str, int]] = {}
    for axis in JOG_STEP_AXES:
        raw_axis = raw_map.get(axis, raw_map.get(axis.lower(), {}))
        raw_axis = raw_axis if isinstance(raw_axis, dict) else {}
        values: dict[str, int] = {}
        for field_name, field_aliases in aliases.items():
            raw_value = next((raw_axis[alias] for alias in field_aliases if alias in raw_axis), 0)
            value = int(raw_value)
            if value < 0 or value > 0xFFFF:
                raise ValueError(f"{axis} controller {field_name.replace('_', ' ')} must be in range 0..65535.")
            values[field_name] = value
        normalized[axis] = values
    return normalized


def calibration_key(objective: int, eyepiece: float) -> str:
    return f"objective_{objective:g}__eyepiece_{eyepiece:g}"


def derive_missing_calibrations(config: "ProbeConfig") -> int:
    base_key = calibration_key(CALIBRATION_REFERENCE_OBJECTIVE, CALIBRATION_REFERENCE_EYEPIECE)
    base_um_per_px = config.calibrations.get(base_key)
    if base_um_per_px is None:
        return 0

    added = 0
    reference_magnification = CALIBRATION_REFERENCE_OBJECTIVE * CALIBRATION_REFERENCE_EYEPIECE
    for objective in OBJECTIVE_OPTIONS:
        for eyepiece in EYEPIECE_OPTIONS:
            key = calibration_key(objective, eyepiece)
            if key in config.calibrations:
                continue
            config.calibrations[key] = float(base_um_per_px) * reference_magnification / (objective * eyepiece)
            added += 1
    return added


def calibration_distance_px(p1: tuple[float, float], p2: tuple[float, float], p3: tuple[float, float]) -> float:
    x1, y1 = p1
    x2, y2 = p2
    x3, y3 = p3
    dx = x2 - x1
    dy = y2 - y1
    baseline = math.hypot(dx, dy)
    if baseline <= 0:
        raise ValueError("First two points must define a non-zero line.")
    return abs(dx * (y1 - y3) - (x1 - x3) * dy) / baseline


def pulses_from_um(distance_um: float, config: "ProbeConfig", axis: str) -> int:
    um_per_pulse = config.um_per_pulse(axis)
    if um_per_pulse <= 0:
        raise ValueError(f"{axis} um-per-pulse must be positive.")
    pulses = int(round(distance_um / um_per_pulse))
    if distance_um > 0 and pulses <= 0:
        return 1
    return pulses


@dataclass
class ProbeConfig:
    objective: int = 20
    eyepiece: float = 2.0
    microstep: int = 2
    lead_xy_mm: float = 1.0
    lead_z_mm: float = 0.5
    base_angle_deg: float = 0.72
    cc_speed_percent: int = 100
    fine_speed_percent: int = 40
    safe_speed_percent: int = 15
    active_motor_speed_profile: str = MOTOR_SPEED_PROFILE_FAST
    controller_motion_parameters: dict[str, dict[str, int]] = field(default_factory=lambda: {
        axis: dict(DEFAULT_CONTROLLER_MOTION_PARAMETERS[axis])
        for axis in JOG_STEP_AXES
    })
    cc_accel_time_s: float = 0.1
    autofocus_settle_ms: int = 100
    autofocus_sample_count: int = 5
    autofocus_peak_model: str = AUTOFOCUS_PEAK_MODEL_GAUSSIAN
    imgstitch_settle_ms: int = 100
    imgstitch_seam_response_yellow: float = 0.10
    imgstitch_seam_response_green: float = 0.25
    keyboard_motion_scheme: str = KEYBOARD_MOTION_SCHEME_ARROW_PAGE
    jog_step_levels: dict[str, tuple[int, ...]] = field(default_factory=lambda: dict(DEFAULT_JOG_STEP_LEVELS))
    focus_threshold_yellow: dict[str, float] = field(default_factory=lambda: {"Laplacian": 1000.0, "Tenengrad": 20000.0, "Brenner": 1000.0})
    focus_threshold_green: dict[str, float] = field(default_factory=lambda: {"Laplacian": 2000.0, "Tenengrad": 40000.0, "Brenner": 2000.0})
    calibrations: dict[str, float] = field(default_factory=dict)
    agent_api_key: str = ""
    agent_base_url: str = DEFAULT_AGENT_BASE_URL
    agent_model: str = DEFAULT_AGENT_MODEL
    agent_timeout_seconds: float = DEFAULT_AGENT_TIMEOUT_SECONDS

    def validate(self) -> None:
        if self.objective not in OBJECTIVE_OPTIONS:
            raise ValueError(f"Objective must be one of {OBJECTIVE_OPTIONS}.")
        if self.eyepiece not in EYEPIECE_OPTIONS:
            raise ValueError(f"Eyepiece must be one of {EYEPIECE_OPTIONS}.")
        if self.microstep <= 0:
            raise ValueError("Microstep must be positive.")
        if self.lead_xy_mm <= 0 or self.lead_z_mm <= 0:
            raise ValueError("Lead values must be positive.")
        if self.base_angle_deg <= 0:
            raise ValueError("Base angle must be positive.")
        if self.cc_speed_percent < 0 or self.cc_speed_percent > 100:
            raise ValueError("Fast motor speed percent must be in range 0..100.")
        if self.fine_speed_percent < 0 or self.fine_speed_percent > 100:
            raise ValueError("Fine motor speed percent must be in range 0..100.")
        if self.safe_speed_percent < 0 or self.safe_speed_percent > 100:
            raise ValueError("Safe motor speed percent must be in range 0..100.")
        self.active_motor_speed_profile = normalize_motor_speed_profile(self.active_motor_speed_profile)
        self.controller_motion_parameters = normalize_controller_motion_parameters_map(self.controller_motion_parameters)
        if self.cc_accel_time_s < 0 or self.cc_accel_time_s > 2.55:
            raise ValueError("CC acceleration time must be in range 0..2.55 seconds.")
        if self.autofocus_settle_ms < 0 or self.autofocus_settle_ms > 10000:
            raise ValueError("AutoFocus settle time must be in range 0..10000 ms.")
        if self.autofocus_sample_count <= 0 or self.autofocus_sample_count > 1000:
            raise ValueError("AutoFocus sample count must be in range 1..1000.")
        self.autofocus_peak_model = normalize_autofocus_peak_model(self.autofocus_peak_model)
        if self.imgstitch_settle_ms < 0 or self.imgstitch_settle_ms > 10000:
            raise ValueError("ImgStitch settle time must be in range 0..10000 ms.")
        if self.imgstitch_seam_response_yellow < 0 or self.imgstitch_seam_response_green < 0:
            raise ValueError("ImgStitch seam response thresholds must be non-negative.")
        if self.imgstitch_seam_response_green < self.imgstitch_seam_response_yellow:
            raise ValueError("ImgStitch green seam response threshold must be greater than or equal to yellow threshold.")
        if self.keyboard_motion_scheme not in KEYBOARD_MOTION_SCHEMES:
            raise ValueError(f"Keyboard motion scheme must be one of {KEYBOARD_MOTION_SCHEMES}.")
        self.jog_step_levels = normalize_jog_step_levels_map(self.jog_step_levels)
        for metric in ("Laplacian", "Tenengrad", "Brenner"):
            yellow = float(self.focus_threshold_yellow.get(metric, 0.0))
            green = float(self.focus_threshold_green.get(metric, 0.0))
            if yellow < 0 or green < 0:
                raise ValueError("Focus thresholds must be non-negative.")
            if green < yellow:
                raise ValueError("Green focus threshold must be greater than or equal to yellow threshold.")
        for value in self.calibrations.values():
            if value <= 0:
                raise ValueError("Calibration values must be positive.")
        self.agent_api_key = str(self.agent_api_key or "").strip()
        self.agent_base_url = str(self.agent_base_url or DEFAULT_AGENT_BASE_URL).strip().rstrip("/")
        self.agent_model = str(self.agent_model or DEFAULT_AGENT_MODEL).strip()
        if not self.agent_base_url.startswith(("http://", "https://")):
            raise ValueError("Agent API base URL must start with http:// or https://.")
        if not self.agent_model:
            raise ValueError("Agent model cannot be empty.")
        if self.agent_timeout_seconds <= 0 or self.agent_timeout_seconds > 300:
            raise ValueError("Agent API timeout must be in range 0..300 seconds.")

    def cc_acceleration_units(self) -> int:
        return int(round(self.cc_accel_time_s * 100.0))

    def motor_speed_percent(self, profile: str | None = None) -> int:
        selected_profile = normalize_motor_speed_profile(profile or self.active_motor_speed_profile)
        if selected_profile == MOTOR_SPEED_PROFILE_FINE:
            return int(self.fine_speed_percent)
        if selected_profile == MOTOR_SPEED_PROFILE_SAFE:
            return int(self.safe_speed_percent)
        return int(self.cc_speed_percent)

    @property
    def steps_per_revolution(self) -> float:
        return 360.0 / (self.base_angle_deg / self.microstep)

    def um_per_pulse(self, axis: str) -> float:
        axis_name = axis.upper()
        if axis_name in ("X", "Y"):
            lead_mm = self.lead_xy_mm
        elif axis_name == "Z":
            lead_mm = self.lead_z_mm
        else:
            raise ValueError(f"Unsupported axis: {axis}.")
        return lead_mm * 1000.0 / self.steps_per_revolution

    def pulses_per_um(self, axis: str) -> float:
        return 1.0 / self.um_per_pulse(axis)

    def current_calibration_key(self) -> str:
        return calibration_key(self.objective, self.eyepiece)

    def current_um_per_px(self) -> float | None:
        return self.calibrations.get(self.current_calibration_key())

    def set_calibration(self, objective: int, eyepiece: float, um_per_px: float) -> None:
        if objective not in OBJECTIVE_OPTIONS:
            raise ValueError(f"Objective must be one of {OBJECTIVE_OPTIONS}.")
        if eyepiece not in EYEPIECE_OPTIONS:
            raise ValueError(f"Eyepiece must be one of {EYEPIECE_OPTIONS}.")
        if um_per_px <= 0:
            raise ValueError("Calibration value must be positive.")
        self.calibrations[calibration_key(objective, eyepiece)] = float(um_per_px)

    def to_dict(self) -> dict[str, Any]:
        derive_missing_calibrations(self)
        self.validate()
        return {
            "objective": self.objective,
            "eyepiece": self.eyepiece,
            "microstep": self.microstep,
            "lead_xy_mm": self.lead_xy_mm,
            "lead_z_mm": self.lead_z_mm,
            "base_angle_deg": self.base_angle_deg,
            "cc_speed_percent": self.cc_speed_percent,
            "fine_speed_percent": self.fine_speed_percent,
            "safe_speed_percent": self.safe_speed_percent,
            "active_motor_speed_profile": self.active_motor_speed_profile,
            "controller_motion_parameters": {
                axis: dict(self.controller_motion_parameters[axis])
                for axis in JOG_STEP_AXES
            },
            "cc_accel_time_s": self.cc_accel_time_s,
            "autofocus_settle_ms": self.autofocus_settle_ms,
            "autofocus_sample_count": self.autofocus_sample_count,
            "autofocus_peak_model": self.autofocus_peak_model,
            "imgstitch_settle_ms": self.imgstitch_settle_ms,
            "imgstitch_seam_response_yellow": self.imgstitch_seam_response_yellow,
            "imgstitch_seam_response_green": self.imgstitch_seam_response_green,
            "keyboard_motion_scheme": self.keyboard_motion_scheme,
            "jog_step_levels": {
                axis: list(self.jog_step_levels[axis])
                for axis in JOG_STEP_AXES
            },
            "focus_threshold_yellow": dict(sorted(self.focus_threshold_yellow.items())),
            "focus_threshold_green": dict(sorted(self.focus_threshold_green.items())),
            "calibrations": dict(sorted(self.calibrations.items())),
            "agent_api_key": self.agent_api_key,
            "agent_base_url": self.agent_base_url,
            "agent_model": self.agent_model,
            "agent_timeout_seconds": self.agent_timeout_seconds,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProbeConfig":
        controller_motion_parameters = data.get("controller_motion_parameters")
        if controller_motion_parameters is None:
            controller_motion_parameters = {
                axis: {
                    "minimum_speed": data.get("controller_min_speed", 0),
                    "work_speed": data.get("controller_work_speed", 0),
                    "acceleration": data.get("controller_acceleration", 0),
                }
                for axis in JOG_STEP_AXES
            }
        config = cls(
            objective=int(data.get("objective", cls.objective)),
            eyepiece=float(data.get("eyepiece", cls.eyepiece)),
            microstep=int(data.get("microstep", cls.microstep)),
            lead_xy_mm=float(data.get("lead_xy_mm", cls.lead_xy_mm)),
            lead_z_mm=float(data.get("lead_z_mm", cls.lead_z_mm)),
            base_angle_deg=float(data.get("base_angle_deg", cls.base_angle_deg)),
            cc_speed_percent=int(data.get("cc_speed_percent", cls.cc_speed_percent)),
            fine_speed_percent=int(data.get("fine_speed_percent", cls.fine_speed_percent)),
            safe_speed_percent=int(data.get("safe_speed_percent", cls.safe_speed_percent)),
            active_motor_speed_profile=normalize_motor_speed_profile(data.get("active_motor_speed_profile", MOTOR_SPEED_PROFILE_FAST)),
            controller_motion_parameters=normalize_controller_motion_parameters_map(controller_motion_parameters),
            cc_accel_time_s=float(data.get("cc_accel_time_s", cls.cc_accel_time_s)),
            autofocus_settle_ms=int(data.get("autofocus_settle_ms", cls.autofocus_settle_ms)),
            autofocus_sample_count=int(data.get("autofocus_sample_count", cls.autofocus_sample_count)),
            autofocus_peak_model=normalize_autofocus_peak_model(data.get("autofocus_peak_model", AUTOFOCUS_PEAK_MODEL_GAUSSIAN)),
            imgstitch_settle_ms=int(data.get("imgstitch_settle_ms", cls.imgstitch_settle_ms)),
            imgstitch_seam_response_yellow=float(data.get("imgstitch_seam_response_yellow", cls.imgstitch_seam_response_yellow)),
            imgstitch_seam_response_green=float(data.get("imgstitch_seam_response_green", cls.imgstitch_seam_response_green)),
            keyboard_motion_scheme=str(data.get("keyboard_motion_scheme", KEYBOARD_MOTION_SCHEME_ARROW_PAGE)),
            jog_step_levels=normalize_jog_step_levels_map(data.get("jog_step_levels")),
            focus_threshold_yellow={
                **cls().focus_threshold_yellow,
                **{str(key): float(value) for key, value in data.get("focus_threshold_yellow", {}).items()},
            },
            focus_threshold_green={
                **cls().focus_threshold_green,
                **{str(key): float(value) for key, value in data.get("focus_threshold_green", {}).items()},
            },
            calibrations={str(key): float(value) for key, value in data.get("calibrations", {}).items()},
            agent_api_key=str(data.get("agent_api_key", "")),
            agent_base_url=str(data.get("agent_base_url", DEFAULT_AGENT_BASE_URL)),
            agent_model=str(data.get("agent_model", DEFAULT_AGENT_MODEL)),
            agent_timeout_seconds=float(data.get("agent_timeout_seconds", DEFAULT_AGENT_TIMEOUT_SECONDS)),
        )
        derive_missing_calibrations(config)
        config.validate()
        return config


def load_probe_config(path: Path | None = None) -> ProbeConfig:
    config_path = path or Path.cwd() / DEFAULT_CONFIG_FILENAME
    if not config_path.exists():
        config = ProbeConfig()
        derive_missing_calibrations(config)
        return config
    with config_path.open("r", encoding="utf-8") as file:
        data = json.load(file)
    return ProbeConfig.from_dict(data)


def save_probe_config(config: ProbeConfig, path: Path | None = None) -> None:
    config_path = path or Path.cwd() / DEFAULT_CONFIG_FILENAME
    derive_missing_calibrations(config)
    config_path.write_text(json.dumps(config.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
