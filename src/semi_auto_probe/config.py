from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


DEFAULT_CONFIG_FILENAME = "probe_config.local.json"
OBJECTIVE_OPTIONS = (20, 10, 5)
EYEPIECE_OPTIONS = (1.0, 1.5, 2.0, 2.5, 3.0, 3.5, 4.0)
CALIBRATION_REFERENCE_OBJECTIVE = 20
CALIBRATION_REFERENCE_EYEPIECE = 1.5


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
    eyepiece: float = 1.5
    microstep: int = 2
    lead_xy_mm: float = 1.0
    lead_z_mm: float = 0.5
    base_angle_deg: float = 0.72
    cc_speed_percent: int = 100
    cc_accel_time_s: float = 0.1
    calibrations: dict[str, float] = field(default_factory=dict)

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
            raise ValueError("CC speed percent must be in range 0..100.")
        if self.cc_accel_time_s < 0 or self.cc_accel_time_s > 2.55:
            raise ValueError("CC acceleration time must be in range 0..2.55 seconds.")
        for value in self.calibrations.values():
            if value <= 0:
                raise ValueError("Calibration values must be positive.")

    def cc_acceleration_units(self) -> int:
        return int(round(self.cc_accel_time_s * 100.0))

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
            "cc_accel_time_s": self.cc_accel_time_s,
            "calibrations": dict(sorted(self.calibrations.items())),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProbeConfig":
        config = cls(
            objective=int(data.get("objective", cls.objective)),
            eyepiece=float(data.get("eyepiece", cls.eyepiece)),
            microstep=int(data.get("microstep", cls.microstep)),
            lead_xy_mm=float(data.get("lead_xy_mm", cls.lead_xy_mm)),
            lead_z_mm=float(data.get("lead_z_mm", cls.lead_z_mm)),
            base_angle_deg=float(data.get("base_angle_deg", cls.base_angle_deg)),
            cc_speed_percent=int(data.get("cc_speed_percent", cls.cc_speed_percent)),
            cc_accel_time_s=float(data.get("cc_accel_time_s", cls.cc_accel_time_s)),
            calibrations={str(key): float(value) for key, value in data.get("calibrations", {}).items()},
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
