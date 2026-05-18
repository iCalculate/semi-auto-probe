from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import numpy as np

try:
    import cv2
except ImportError:  # pragma: no cover - exercised in environments without OpenCV.
    cv2 = None


GridIndex = tuple[int, int]


@dataclass(frozen=True)
class PlaneModel:
    a: float
    b: float
    c: float

    def z_at(self, x: float, y: float) -> float:
        return self.a * x + self.b * y + self.c


@dataclass(frozen=True)
class StitchSettings:
    overlap_x: int
    overlap_y: int
    max_correction_um: float = 20.0
    registration_weight: float = 0.0
    show_seams: bool = True
    seam_response_yellow: float = 0.10
    seam_response_green: float = 0.25

    def normalized(self) -> "StitchSettings":
        yellow = max(0.0, float(self.seam_response_yellow))
        green = max(yellow, float(self.seam_response_green))
        return StitchSettings(
            overlap_x=max(1, int(self.overlap_x)),
            overlap_y=max(1, int(self.overlap_y)),
            max_correction_um=max(0.0, float(self.max_correction_um)),
            registration_weight=min(1.0, max(0.0, float(self.registration_weight))),
            show_seams=bool(self.show_seams),
            seam_response_yellow=yellow,
            seam_response_green=green,
        )

    def to_dict(self) -> dict[str, object]:
        settings = self.normalized()
        return {
            "overlap_x": settings.overlap_x,
            "overlap_y": settings.overlap_y,
            "max_correction_um": settings.max_correction_um,
            "registration_weight": settings.registration_weight,
            "show_seams": settings.show_seams,
            "seam_response_yellow": settings.seam_response_yellow,
            "seam_response_green": settings.seam_response_green,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StitchSettings":
        return cls(
            overlap_x=int(data.get("overlap_x", 1)),
            overlap_y=int(data.get("overlap_y", 1)),
            max_correction_um=float(data.get("max_correction_um", 20.0)),
            registration_weight=float(data.get("registration_weight", 0.0)),
            show_seams=bool(data.get("show_seams", True)),
            seam_response_yellow=float(data.get("seam_response_yellow", 0.10)),
            seam_response_green=float(data.get("seam_response_green", 0.25)),
        ).normalized()


@dataclass(frozen=True)
class TileRecord:
    row: int
    col: int
    order: int
    image_path: str
    stage_x: int
    stage_y: int
    stage_z: int
    stage_x_um: float
    stage_y_um: float

    @property
    def key(self) -> GridIndex:
        return (self.row, self.col)

    def to_dict(self) -> dict[str, object]:
        return {
            "row": self.row,
            "col": self.col,
            "order": self.order,
            "image_path": self.image_path,
            "stage_x": self.stage_x,
            "stage_y": self.stage_y,
            "stage_z": self.stage_z,
            "stage_x_um": self.stage_x_um,
            "stage_y_um": self.stage_y_um,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "TileRecord":
        return cls(
            row=int(data["row"]),
            col=int(data["col"]),
            order=int(data["order"]),
            image_path=str(data["image_path"]),
            stage_x=int(data["stage_x"]),
            stage_y=int(data["stage_y"]),
            stage_z=int(data["stage_z"]),
            stage_x_um=float(data["stage_x_um"]),
            stage_y_um=float(data["stage_y_um"]),
        )


@dataclass(frozen=True)
class StitchEdgeQuality:
    previous: GridIndex
    current: GridIndex
    direction: str
    expected_shift_px: tuple[float, float]
    measured_shift_px: tuple[float, float]
    applied_shift_px: tuple[float, float]
    response: float
    correction_um: float
    quality: str

    def to_dict(self) -> dict[str, object]:
        return {
            "previous": list(self.previous),
            "current": list(self.current),
            "direction": self.direction,
            "expected_shift_px": list(self.expected_shift_px),
            "measured_shift_px": list(self.measured_shift_px),
            "applied_shift_px": list(self.applied_shift_px),
            "response": self.response,
            "correction_um": self.correction_um,
            "quality": self.quality,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StitchEdgeQuality":
        return cls(
            previous=tuple(data["previous"]),  # type: ignore[arg-type]
            current=tuple(data["current"]),  # type: ignore[arg-type]
            direction=str(data["direction"]),
            expected_shift_px=tuple(data["expected_shift_px"]),  # type: ignore[arg-type]
            measured_shift_px=tuple(data["measured_shift_px"]),  # type: ignore[arg-type]
            applied_shift_px=tuple(data["applied_shift_px"]),  # type: ignore[arg-type]
            response=float(data["response"]),
            correction_um=float(data["correction_um"]),
            quality=str(data["quality"]),
        )


@dataclass(frozen=True)
class StitchSession:
    rows: int
    cols: int
    tile_width: int
    tile_height: int
    um_per_px: float
    objective: int
    eyepiece: float
    range_mode: str
    step_x_um: float
    step_y_um: float
    origin_stage_x: int
    origin_stage_y: int
    origin_stage_z: int
    settings: StitchSettings
    tiles: tuple[TileRecord, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "rows": self.rows,
            "cols": self.cols,
            "tile_width": self.tile_width,
            "tile_height": self.tile_height,
            "um_per_px": self.um_per_px,
            "objective": self.objective,
            "eyepiece": self.eyepiece,
            "range_mode": self.range_mode,
            "step_x_um": self.step_x_um,
            "step_y_um": self.step_y_um,
            "origin_stage_x": self.origin_stage_x,
            "origin_stage_y": self.origin_stage_y,
            "origin_stage_z": self.origin_stage_z,
            "settings": self.settings.to_dict(),
            "tiles": [tile.to_dict() for tile in self.tiles],
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> "StitchSession":
        return cls(
            rows=int(data["rows"]),
            cols=int(data["cols"]),
            tile_width=int(data["tile_width"]),
            tile_height=int(data["tile_height"]),
            um_per_px=float(data["um_per_px"]),
            objective=int(data["objective"]),
            eyepiece=float(data["eyepiece"]),
            range_mode=str(data["range_mode"]),
            step_x_um=float(data["step_x_um"]),
            step_y_um=float(data["step_y_um"]),
            origin_stage_x=int(data["origin_stage_x"]),
            origin_stage_y=int(data["origin_stage_y"]),
            origin_stage_z=int(data["origin_stage_z"]),
            settings=StitchSettings.from_dict(data["settings"]),  # type: ignore[arg-type]
            tiles=tuple(TileRecord.from_dict(tile) for tile in data["tiles"]),  # type: ignore[arg-type]
        )

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "StitchSession":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))


def require_cv2():
    if cv2 is None:
        raise ImportError("OpenCV is required for image stitching operations. Install opencv-python.")
    return cv2


def serpentine_indices(rows: int, cols: int) -> list[GridIndex]:
    if rows <= 0 or cols <= 0:
        raise ValueError("Rows and columns must be positive.")
    path: list[GridIndex] = []
    for row in range(rows):
        column_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        path.extend((row, col) for col in column_range)
    return path


def flat_field_correct(image_bgr: np.ndarray, blur_kernel: int = 0) -> np.ndarray:
    cv2_module = require_cv2()

    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError("Expected a BGR image with three channels.")

    height, width = image_bgr.shape[:2]
    if blur_kernel <= 0:
        blur_kernel = max(31, ((min(height, width) // 8) | 1))
    if blur_kernel % 2 == 0:
        blur_kernel += 1

    image = image_bgr.astype(np.float32)
    illumination = cv2_module.GaussianBlur(image, (blur_kernel, blur_kernel), 0)
    target = np.median(illumination.reshape(-1, 3), axis=0)
    corrected = image * (target / np.maximum(illumination, 1.0))
    return np.clip(corrected, 0, 255).astype(np.uint8)


def estimate_overlap_shift(
    previous_bgr: np.ndarray,
    current_bgr: np.ndarray,
    direction: str,
    overlap_x: int,
    overlap_y: int,
) -> tuple[float, float, float]:
    cv2_module = require_cv2()

    previous_gray = cv2_module.cvtColor(previous_bgr, cv2_module.COLOR_BGR2GRAY).astype(np.float32)
    current_gray = cv2_module.cvtColor(current_bgr, cv2_module.COLOR_BGR2GRAY).astype(np.float32)

    if direction == "right":
        previous_roi = previous_gray[:, -overlap_x:]
        current_roi = current_gray[:, :overlap_x]
        expected_dx, expected_dy = previous_gray.shape[1] - overlap_x, 0.0
    elif direction == "left":
        previous_roi = previous_gray[:, :overlap_x]
        current_roi = current_gray[:, -overlap_x:]
        expected_dx, expected_dy = -(previous_gray.shape[1] - overlap_x), 0.0
    elif direction == "down":
        previous_roi = previous_gray[-overlap_y:, :]
        current_roi = current_gray[:overlap_y, :]
        expected_dx, expected_dy = 0.0, previous_gray.shape[0] - overlap_y
    elif direction == "up":
        previous_roi = previous_gray[:overlap_y, :]
        current_roi = current_gray[-overlap_y:, :]
        expected_dx, expected_dy = 0.0, -(previous_gray.shape[0] - overlap_y)
    else:
        raise ValueError(f"Unsupported stitch direction: {direction}")

    if min(previous_roi.shape[:2]) < 2 or min(current_roi.shape[:2]) < 2:
        raise ValueError("Overlap is too small for phase correlation.")

    window = cv2_module.createHanningWindow((previous_roi.shape[1], previous_roi.shape[0]), cv2_module.CV_32F)
    (shift_x, shift_y), response = cv2_module.phaseCorrelate(previous_roi, current_roi, window)
    return expected_dx - shift_x, expected_dy - shift_y, float(response)


def compose_mosaic(
    tiles: dict[GridIndex, np.ndarray],
    positions: dict[GridIndex, tuple[float, float]],
) -> np.ndarray:
    if not tiles:
        raise ValueError("At least one tile is required.")

    sample = next(iter(tiles.values()))
    tile_height, tile_width = sample.shape[:2]
    xs = [position[0] for position in positions.values()]
    ys = [position[1] for position in positions.values()]
    min_x, min_y = min(xs), min(ys)
    max_x = max(x + tile_width for x in xs)
    max_y = max(y + tile_height for y in ys)
    width = int(np.ceil(max_x - min_x))
    height = int(np.ceil(max_y - min_y))

    accum = np.zeros((height, width, 3), dtype=np.float32)
    weights = np.zeros((height, width, 1), dtype=np.float32)
    for key, image in tiles.items():
        x, y = positions[key]
        x0 = int(round(x - min_x))
        y0 = int(round(y - min_y))
        tile = image.astype(np.float32)
        accum[y0 : y0 + tile_height, x0 : x0 + tile_width] += tile
        weights[y0 : y0 + tile_height, x0 : x0 + tile_width] += 1.0

    mosaic = accum / np.maximum(weights, 1.0)
    return np.clip(mosaic, 0, 255).astype(np.uint8)


def stage_positions_from_um(tiles: Iterable[TileRecord], um_per_px: float) -> dict[GridIndex, tuple[float, float]]:
    records = list(tiles)
    if not records:
        return {}
    if um_per_px <= 0:
        raise ValueError("um_per_px must be positive.")
    origin_x_um = records[0].stage_x_um
    origin_y_um = records[0].stage_y_um
    return {
        tile.key: (
            (tile.stage_x_um - origin_x_um) / um_per_px,
            -(tile.stage_y_um - origin_y_um) / um_per_px,
        )
        for tile in records
    }


def _direction_between(previous: GridIndex, current: GridIndex) -> str:
    previous_row, previous_col = previous
    row, col = current
    if row == previous_row and col > previous_col:
        return "right"
    if row == previous_row and col < previous_col:
        return "left"
    if row > previous_row:
        return "up"
    raise ValueError(f"Unsupported tile transition: {previous} -> {current}")


def _clamp_vector(dx: float, dy: float, max_length: float) -> tuple[float, float]:
    if max_length <= 0:
        return 0.0, 0.0
    length = float(np.hypot(dx, dy))
    if length <= max_length or length == 0.0:
        return dx, dy
    scale = max_length / length
    return dx * scale, dy * scale


def _quality_from_metrics(response: float, correction_um: float, settings: StitchSettings) -> str:
    if response >= settings.seam_response_green and correction_um <= settings.max_correction_um * 0.5:
        return "good"
    if response >= settings.seam_response_yellow and correction_um <= settings.max_correction_um:
        return "warning"
    return "bad"


def compose_mosaic_from_stage_positions(
    tiles: dict[GridIndex, np.ndarray],
    records: Iterable[TileRecord],
    um_per_px: float,
) -> tuple[np.ndarray, dict[GridIndex, tuple[float, float]]]:
    positions = stage_positions_from_um(records, um_per_px)
    return compose_mosaic(tiles, positions), positions


def _edge_quality_between(
    previous_key: GridIndex,
    current_key: GridIndex,
    settings: StitchSettings,
    session: StitchSession,
    tile_images: dict[GridIndex, np.ndarray],
    base_positions: dict[GridIndex, tuple[float, float]],
    positions: dict[GridIndex, tuple[float, float]],
) -> StitchEdgeQuality:
    direction = _direction_between(previous_key, current_key)
    expected_shift = (
        base_positions[current_key][0] - base_positions[previous_key][0],
        base_positions[current_key][1] - base_positions[previous_key][1],
    )
    measured_shift = expected_shift
    response = 0.0
    try:
        measured_dx, measured_dy, response = estimate_overlap_shift(
            tile_images[previous_key],
            tile_images[current_key],
            direction,
            settings.overlap_x,
            settings.overlap_y,
        )
        measured_shift = (measured_dx, measured_dy)
    except Exception:
        measured_shift = expected_shift
        response = 0.0
    applied_shift = (
        positions[current_key][0] - positions[previous_key][0],
        positions[current_key][1] - positions[previous_key][1],
    )
    correction_um = float(np.hypot(applied_shift[0] - expected_shift[0], applied_shift[1] - expected_shift[1]) * session.um_per_px)
    return StitchEdgeQuality(
        previous=previous_key,
        current=current_key,
        direction=direction,
        expected_shift_px=expected_shift,
        measured_shift_px=measured_shift,
        applied_shift_px=applied_shift,
        response=float(response),
        correction_um=correction_um,
        quality=_quality_from_metrics(float(response), correction_um, settings),
    )


def _all_adjacent_edges(
    session: StitchSession,
    settings: StitchSettings,
    tile_images: dict[GridIndex, np.ndarray],
    base_positions: dict[GridIndex, tuple[float, float]],
    positions: dict[GridIndex, tuple[float, float]],
) -> list[StitchEdgeQuality]:
    available = {tile.key for tile in session.tiles if tile.key in tile_images and tile.key in positions}
    edges: list[StitchEdgeQuality] = []
    for row, col in sorted(available):
        key = (row, col)
        right_key = (row, col + 1)
        lower_key = (row + 1, col)
        if right_key in available:
            edges.append(_edge_quality_between(key, right_key, settings, session, tile_images, base_positions, positions))
        if lower_key in available:
            edges.append(_edge_quality_between(key, lower_key, settings, session, tile_images, base_positions, positions))
    return edges


def recompose_session(
    session: StitchSession,
    settings: StitchSettings | None = None,
    tile_images: dict[GridIndex, np.ndarray] | None = None,
) -> tuple[np.ndarray, dict[GridIndex, tuple[float, float]], list[StitchEdgeQuality]]:
    settings = (settings or session.settings).normalized()
    if tile_images is None:
        cv2_module = require_cv2()
        tile_images = {}
        for tile in session.tiles:
            image = cv2_module.imread(tile.image_path)
            if image is None:
                raise FileNotFoundError(tile.image_path)
            tile_images[tile.key] = image

    base_positions = stage_positions_from_um(session.tiles, session.um_per_px)
    positions: dict[GridIndex, tuple[float, float]] = {}
    max_correction_px = settings.max_correction_um / session.um_per_px if session.um_per_px > 0 else 0.0
    ordered_keys = [tile.key for tile in sorted(session.tiles, key=lambda tile: tile.order)]
    previous_key: GridIndex | None = None

    for key in ordered_keys:
        if key not in tile_images:
            raise KeyError(f"Missing image for tile {key}.")
        if previous_key is None:
            positions[key] = base_positions[key]
            previous_key = key
            continue

        direction = _direction_between(previous_key, key)
        expected_shift = (
            base_positions[key][0] - base_positions[previous_key][0],
            base_positions[key][1] - base_positions[previous_key][1],
        )
        measured_shift = expected_shift
        response = 0.0
        try:
            measured_dx, measured_dy, response = estimate_overlap_shift(
                tile_images[previous_key],
                tile_images[key],
                direction,
                settings.overlap_x,
                settings.overlap_y,
            )
            measured_shift = (measured_dx, measured_dy)
        except Exception:
            measured_shift = expected_shift
            response = 0.0

        measured_position = (
            positions[previous_key][0] + measured_shift[0],
            positions[previous_key][1] + measured_shift[1],
        )
        raw_correction = (
            measured_position[0] - base_positions[key][0],
            measured_position[1] - base_positions[key][1],
        )
        clamped_correction = _clamp_vector(raw_correction[0], raw_correction[1], max_correction_px)
        applied_correction = (
            clamped_correction[0] * settings.registration_weight,
            clamped_correction[1] * settings.registration_weight,
        )
        positions[key] = (
            base_positions[key][0] + applied_correction[0],
            base_positions[key][1] + applied_correction[1],
        )
        previous_key = key

    edges = _all_adjacent_edges(session, settings, tile_images, base_positions, positions)
    return compose_mosaic(tile_images, positions), positions, edges


def build_seam_quality_overlay(
    mosaic_bgr: np.ndarray,
    positions: dict[GridIndex, tuple[float, float]],
    edges: Iterable[StitchEdgeQuality],
    tile_size: tuple[int, int],
    alpha: float = 0.28,
) -> np.ndarray:
    cv2_module = require_cv2()
    if mosaic_bgr.ndim != 3 or mosaic_bgr.shape[2] != 3:
        raise ValueError("Expected a BGR mosaic with three channels.")

    tile_width, tile_height = tile_size
    if not positions:
        return mosaic_bgr.copy()
    min_x = min(position[0] for position in positions.values())
    min_y = min(position[1] for position in positions.values())
    overlay = mosaic_bgr.copy()
    colors = {
        "good": (80, 220, 120),
        "warning": (30, 190, 245),
        "bad": (80, 80, 255),
    }

    edges = list(edges)
    for edge in edges:
        if edge.current not in positions:
            continue
        x, y = positions[edge.current]
        x0 = int(round(x - min_x))
        y0 = int(round(y - min_y))
        color = colors.get(edge.quality, colors["warning"])
        shift_x = abs(edge.applied_shift_px[0])
        shift_y = abs(edge.applied_shift_px[1])
        if edge.direction in ("right", "left"):
            overlap_width = max(2, min(tile_width, int(round(tile_width - shift_x))))
            x1 = x0 if edge.direction == "right" else x0 + tile_width - overlap_width
            x2 = x1 + overlap_width
            y1 = y0
            y2 = y0 + tile_height
        elif edge.direction in ("down", "up"):
            overlap_height = max(2, min(tile_height, int(round(tile_height - shift_y))))
            y1 = y0 if edge.direction == "down" else y0 + tile_height - overlap_height
            y2 = y1 + overlap_height
            x1 = x0
            x2 = x0 + tile_width
        else:
            continue
        x1 = max(0, min(overlay.shape[1] - 1, x1))
        x2 = max(0, min(overlay.shape[1], x2))
        y1 = max(0, min(overlay.shape[0] - 1, y1))
        y2 = max(0, min(overlay.shape[0], y2))
        if x2 > x1 and y2 > y1:
            cv2_module.rectangle(overlay, (x1, y1), (x2, y2), color, -1)
            cv2_module.rectangle(overlay, (x1, y1), (x2, y2), colors.get(edge.quality, colors["warning"]), 1)

    result = cv2_module.addWeighted(overlay, alpha, mosaic_bgr, 1.0 - alpha, 0)
    bar_width = min(180, max(90, result.shape[1] // 5))
    bar_height = 12
    margin = 12
    x0 = max(0, result.shape[1] - bar_width - margin)
    y0 = margin
    segment_width = max(1, bar_width // 3)
    for index, quality in enumerate(("bad", "warning", "good")):
        x1 = x0 + index * segment_width
        x2 = x0 + bar_width if index == 2 else x0 + (index + 1) * segment_width
        cv2_module.rectangle(result, (x1, y0), (x2, y0 + bar_height), colors[quality], -1)
    cv2_module.rectangle(result, (x0, y0), (x0 + bar_width, y0 + bar_height), (230, 230, 230), 1)
    cv2_module.putText(result, "bad", (x0, y0 + bar_height + 14), cv2_module.FONT_HERSHEY_SIMPLEX, 0.38, (230, 230, 230), 1, cv2_module.LINE_AA)
    cv2_module.putText(result, "warn", (x0 + segment_width - 8, y0 + bar_height + 14), cv2_module.FONT_HERSHEY_SIMPLEX, 0.38, (230, 230, 230), 1, cv2_module.LINE_AA)
    cv2_module.putText(result, "good", (x0 + bar_width - 36, y0 + bar_height + 14), cv2_module.FONT_HERSHEY_SIMPLEX, 0.38, (230, 230, 230), 1, cv2_module.LINE_AA)
    return result


def fit_plane(samples: Iterable[tuple[float, float, float]]) -> PlaneModel:
    points = list(samples)
    if len(points) < 3:
        raise ValueError("At least three plane samples are required.")
    matrix = np.array([[x, y, 1.0] for x, y, _z in points], dtype=np.float64)
    values = np.array([z for _x, _y, z in points], dtype=np.float64)
    coefficients, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    return PlaneModel(a=float(coefficients[0]), b=float(coefficients[1]), c=float(coefficients[2]))
