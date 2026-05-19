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
    use_green_edge_correction: bool = True
    white_balance_correction: bool = True

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
            use_green_edge_correction=bool(self.use_green_edge_correction),
            white_balance_correction=bool(self.white_balance_correction),
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
            "use_green_edge_correction": settings.use_green_edge_correction,
            "white_balance_correction": settings.white_balance_correction,
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
            use_green_edge_correction=bool(data.get("use_green_edge_correction", True)),
            white_balance_correction=bool(data.get("white_balance_correction", True)),
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
    raw_shift_px: tuple[float, float] | None = None
    corrected_shift_px: tuple[float, float] | None = None
    was_corrected: bool = False

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
            "raw_shift_px": list(self.raw_shift_px or self.measured_shift_px),
            "corrected_shift_px": list(self.corrected_shift_px or self.measured_shift_px),
            "was_corrected": self.was_corrected,
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
            raw_shift_px=tuple(data.get("raw_shift_px", data["measured_shift_px"])),  # type: ignore[arg-type]
            corrected_shift_px=tuple(data.get("corrected_shift_px", data["measured_shift_px"])),  # type: ignore[arg-type]
            was_corrected=bool(data.get("was_corrected", False)),
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


def white_balance_tile_set(
    tile_images: dict[GridIndex, np.ndarray],
    max_gain: float = 2.5,
) -> dict[GridIndex, np.ndarray]:
    if not tile_images:
        return {}
    channel_means: dict[GridIndex, np.ndarray] = {}
    for key, image in tile_images.items():
        if image.ndim != 3 or image.shape[2] != 3:
            channel_means[key] = np.ones(1, dtype=np.float32)
            continue
        channel_means[key] = image.astype(np.float32).reshape(-1, 3).mean(axis=0)
    valid_means = [mean for mean in channel_means.values() if mean.shape == (3,) and np.all(mean > 1.0)]
    if not valid_means:
        return dict(tile_images)
    target = np.median(np.stack(valid_means, axis=0), axis=0)
    corrected: dict[GridIndex, np.ndarray] = {}
    for key, image in tile_images.items():
        mean = channel_means[key]
        if mean.shape != (3,):
            corrected[key] = image
            continue
        gains = np.clip(target / np.maximum(mean, 1.0), 1.0 / max_gain, max_gain)
        balanced = image.astype(np.float32) * gains.reshape(1, 1, 3)
        corrected[key] = _restore_dtype(balanced, image.dtype)
    return corrected


def _restore_dtype(image: np.ndarray, dtype: np.dtype) -> np.ndarray:
    if np.issubdtype(dtype, np.integer):
        info = np.iinfo(dtype)
        return np.clip(image, info.min, info.max).astype(dtype)
    return image.astype(dtype)


def _validate_frames(frames: Iterable[np.ndarray]) -> list[np.ndarray]:
    frame_list = [np.asarray(frame) for frame in frames]
    if not frame_list:
        raise ValueError("At least one frame is required.")
    first_shape = frame_list[0].shape
    for frame in frame_list:
        if frame.shape != first_shape:
            raise ValueError("All frames must have the same shape.")
    return frame_list


def _gray_float(image: np.ndarray) -> np.ndarray:
    cv2_module = require_cv2()
    if image.ndim == 2:
        return image.astype(np.float32)
    if image.ndim == 3 and image.shape[2] == 3:
        return cv2_module.cvtColor(image, cv2_module.COLOR_BGR2GRAY).astype(np.float32)
    raise ValueError("Expected grayscale or BGR image.")


def _translate_image(image: np.ndarray, shift_x: float, shift_y: float) -> np.ndarray:
    cv2_module = require_cv2()
    matrix = np.array([[1.0, 0.0, shift_x], [0.0, 1.0, shift_y]], dtype=np.float32)
    height, width = image.shape[:2]
    return cv2_module.warpAffine(
        image,
        matrix,
        (width, height),
        flags=cv2_module.INTER_LINEAR,
        borderMode=cv2_module.BORDER_REFLECT,
    )


def _registered_t_stack_frames(frame_list: list[np.ndarray]) -> list[np.ndarray]:
    cv2_module = require_cv2()
    reference = frame_list[0]
    reference_gray = _gray_float(reference)
    window = cv2_module.createHanningWindow((reference_gray.shape[1], reference_gray.shape[0]), cv2_module.CV_32F)
    aligned = [reference]
    for frame in frame_list[1:]:
        try:
            shift, response = cv2_module.phaseCorrelate(reference_gray, _gray_float(frame), window)
            if response <= 0:
                aligned.append(frame)
                continue
            shift_x, shift_y = shift
            aligned.append(_translate_image(frame, -shift_x, -shift_y))
        except Exception:
            aligned.append(frame)
    return aligned


def fuse_t_stack(frames: Iterable[np.ndarray], method: str = "average") -> np.ndarray:
    frame_list = _validate_frames(frames)
    dtype = frame_list[0].dtype
    method_name = method.lower()
    if method_name == "average":
        fused = np.mean([frame.astype(np.float32) for frame in frame_list], axis=0)
        return _restore_dtype(fused, dtype)
    if method_name not in ("registered_average", "sharpness_fusion"):
        raise ValueError(f"Unsupported T-stack fusion method: {method}")

    aligned = _registered_t_stack_frames(frame_list)
    if method_name == "registered_average":
        return _restore_dtype(np.mean([frame.astype(np.float32) for frame in aligned], axis=0), dtype)

    sharpness = np.stack([focus_sharpness_map(frame, "tenengrad", blur_kernel=9) for frame in aligned], axis=0)
    weights = sharpness + 1e-6
    weights /= np.maximum(weights.sum(axis=0, keepdims=True), 1e-6)
    stack = np.stack([frame.astype(np.float32) for frame in aligned], axis=0)
    if stack.ndim == 4:
        weights = weights[:, :, :, None]
    fused = (stack * weights).sum(axis=0)
    return _restore_dtype(fused, dtype)


def focus_sharpness_map(image: np.ndarray, method: str = "laplacian", blur_kernel: int = 5) -> np.ndarray:
    cv2_module = require_cv2()
    gray = _gray_float(image)
    method_name = method.lower()
    if method_name == "laplacian":
        score = np.abs(cv2_module.Laplacian(gray, cv2_module.CV_32F, ksize=3))
    elif method_name == "tenengrad":
        sobel_x = cv2_module.Sobel(gray, cv2_module.CV_32F, 1, 0, ksize=3)
        sobel_y = cv2_module.Sobel(gray, cv2_module.CV_32F, 0, 1, ksize=3)
        score = sobel_x * sobel_x + sobel_y * sobel_y
    else:
        raise ValueError(f"Unsupported focus fusion method: {method}")
    if blur_kernel > 1:
        if blur_kernel % 2 == 0:
            blur_kernel += 1
        score = cv2_module.GaussianBlur(score, (blur_kernel, blur_kernel), 0)
    return score.astype(np.float32)


def fuse_z_stack(frames: Iterable[np.ndarray], method: str = "laplacian") -> np.ndarray:
    frame_list = _validate_frames(frames)
    dtype = frame_list[0].dtype
    sharpness = np.stack([focus_sharpness_map(frame, method) for frame in frame_list], axis=0)
    indices = np.argmax(sharpness, axis=0)
    stack = np.stack(frame_list, axis=0)
    if stack.ndim == 3:
        fused = np.take_along_axis(stack, indices[None, :, :], axis=0)[0]
    else:
        expanded_indices = indices[None, :, :, None]
        fused = np.take_along_axis(stack, expanded_indices, axis=0)[0]
    return _restore_dtype(fused.astype(np.float32), dtype)


def z_stack_positions(center_z: int, z_range_um: float, z_step_um: float, config) -> list[int]:
    from .config import pulses_from_um

    if z_step_um <= 0:
        raise ValueError("Z step must be positive.")
    if z_range_um < 0:
        raise ValueError("Z range must be non-negative.")
    step_pulses = abs(pulses_from_um(z_step_um, config, "Z"))
    range_pulses = abs(pulses_from_um(z_range_um, config, "Z"))
    if step_pulses <= 0:
        raise ValueError("Z step is too small for the current Z calibration.")
    if range_pulses == 0:
        return [center_z]
    offsets = [0]
    distance = step_pulses
    while distance <= range_pulses:
        offsets.extend((distance, -distance))
        distance += step_pulses
    if len(offsets) == 1 or offsets[-2] != range_pulses:
        offsets.extend((range_pulses, -range_pulses))
    return [center_z + offset for offset in offsets]


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


def _edge_direction_group(direction: str) -> str:
    return "horizontal" if direction in ("right", "left") else "vertical"


def _measure_edge_shift(
    previous_key: GridIndex,
    current_key: GridIndex,
    settings: StitchSettings,
    tile_images: dict[GridIndex, np.ndarray],
    base_positions: dict[GridIndex, tuple[float, float]],
) -> tuple[str, tuple[float, float], tuple[float, float], float]:
    direction = _direction_between(previous_key, current_key)
    expected_shift = (
        base_positions[current_key][0] - base_positions[previous_key][0],
        base_positions[current_key][1] - base_positions[previous_key][1],
    )
    try:
        measured_dx, measured_dy, response = estimate_overlap_shift(
            tile_images[previous_key],
            tile_images[current_key],
            direction,
            settings.overlap_x,
            settings.overlap_y,
        )
        return direction, expected_shift, (measured_dx, measured_dy), float(response)
    except Exception:
        return direction, expected_shift, expected_shift, 0.0


def _position_from_shift(
    previous_position: tuple[float, float],
    base_position: tuple[float, float],
    shift: tuple[float, float],
    max_correction_px: float,
    registration_weight: float,
) -> tuple[float, float]:
    measured_position = (
        previous_position[0] + shift[0],
        previous_position[1] + shift[1],
    )
    raw_correction = (
        measured_position[0] - base_position[0],
        measured_position[1] - base_position[1],
    )
    clamped_correction = _clamp_vector(raw_correction[0], raw_correction[1], max_correction_px)
    return (
        base_position[0] + clamped_correction[0] * registration_weight,
        base_position[1] + clamped_correction[1] * registration_weight,
    )


def _green_edge_centers(edges: Iterable[StitchEdgeQuality], minimum_edges: int = 2) -> dict[str, tuple[float, float, float, int]]:
    # Keep direction signs separate. Serpentine scans can move left, which must
    # use the opposite of a right-edge center rather than the same horizontal vector.
    centers: dict[str, tuple[float, float, float, int]] = {}
    grouped: dict[str, list[tuple[float, float]]] = {}
    for edge in edges:
        if edge.quality != "good":
            continue
        grouped.setdefault(edge.direction, []).append(edge.raw_shift_px or edge.measured_shift_px)
    for direction, shifts in grouped.items():
        if len(shifts) < minimum_edges:
            continue
        values = np.array(shifts, dtype=np.float64)
        center = np.median(values, axis=0)
        mad = np.median(np.abs(values - center), axis=0)
        centers[direction] = (float(center[0]), float(center[1]), float(np.hypot(mad[0], mad[1])), len(shifts))
    return centers


def _center_for_direction(
    direction: str,
    centers: dict[str, tuple[float, float, float, int]],
) -> tuple[float, float, float, int] | None:
    center = centers.get(direction)
    if center is not None:
        return center
    opposite = {
        "left": "right",
        "right": "left",
        "down": "up",
        "up": "down",
    }.get(direction)
    if opposite is None or opposite not in centers:
        return None
    dx, dy, mad, count = centers[opposite]
    return -dx, -dy, mad, count


def stitch_displacement_diagnostics(edges: Iterable[StitchEdgeQuality]) -> dict[str, object]:
    edge_list = list(edges)
    counts = {
        "good": sum(1 for edge in edge_list if edge.quality == "good"),
        "warning": sum(1 for edge in edge_list if edge.quality == "warning"),
        "bad": sum(1 for edge in edge_list if edge.quality == "bad"),
    }
    centers = _green_edge_centers(edge_list)
    corrected = sum(1 for edge in edge_list if edge.was_corrected)
    return {
        "counts": counts,
        "centers": centers,
        "corrected": corrected,
        "applied": corrected > 0,
    }


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
    correction_centers: dict[str, tuple[float, float, float, int]] | None = None,
) -> StitchEdgeQuality:
    direction, expected_shift, measured_shift, response = _measure_edge_shift(previous_key, current_key, settings, tile_images, base_positions)
    applied_shift = (
        positions[current_key][0] - positions[previous_key][0],
        positions[current_key][1] - positions[previous_key][1],
    )
    correction_um = float(np.hypot(applied_shift[0] - expected_shift[0], applied_shift[1] - expected_shift[1]) * session.um_per_px)
    quality = _quality_from_metrics(float(response), correction_um, settings)
    corrected_shift = measured_shift
    was_corrected = False
    if settings.use_green_edge_correction and correction_centers and quality != "good":
        center = _center_for_direction(direction, correction_centers)
        if center is not None:
            corrected_shift = (center[0], center[1])
            was_corrected = True
    return StitchEdgeQuality(
        previous=previous_key,
        current=current_key,
        direction=direction,
        expected_shift_px=expected_shift,
        measured_shift_px=measured_shift,
        applied_shift_px=applied_shift,
        response=float(response),
        correction_um=correction_um,
        quality=quality,
        raw_shift_px=measured_shift,
        corrected_shift_px=corrected_shift,
        was_corrected=was_corrected,
    )


def _all_adjacent_edges(
    session: StitchSession,
    settings: StitchSettings,
    tile_images: dict[GridIndex, np.ndarray],
    base_positions: dict[GridIndex, tuple[float, float]],
    positions: dict[GridIndex, tuple[float, float]],
    correction_centers: dict[str, tuple[float, float, float, int]] | None = None,
) -> list[StitchEdgeQuality]:
    available = {tile.key for tile in session.tiles if tile.key in tile_images and tile.key in positions}
    edges: list[StitchEdgeQuality] = []
    for row, col in sorted(available):
        key = (row, col)
        right_key = (row, col + 1)
        lower_key = (row + 1, col)
        if right_key in available:
            edges.append(_edge_quality_between(key, right_key, settings, session, tile_images, base_positions, positions, correction_centers))
        if lower_key in available:
            edges.append(_edge_quality_between(key, lower_key, settings, session, tile_images, base_positions, positions, correction_centers))
    return edges


def _path_positions_and_edges(
    session: StitchSession,
    settings: StitchSettings,
    tile_images: dict[GridIndex, np.ndarray],
    base_positions: dict[GridIndex, tuple[float, float]],
    correction_centers: dict[str, tuple[float, float, float, int]] | None = None,
) -> tuple[dict[GridIndex, tuple[float, float]], list[StitchEdgeQuality]]:
    positions: dict[GridIndex, tuple[float, float]] = {}
    path_edges: list[StitchEdgeQuality] = []
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

        direction, expected_shift, measured_shift, response = _measure_edge_shift(previous_key, key, settings, tile_images, base_positions)
        provisional_position = _position_from_shift(
            positions[previous_key],
            base_positions[key],
            measured_shift,
            max_correction_px,
            settings.registration_weight,
        )
        provisional_shift = (
            provisional_position[0] - positions[previous_key][0],
            provisional_position[1] - positions[previous_key][1],
        )
        provisional_correction_um = float(
            np.hypot(provisional_shift[0] - expected_shift[0], provisional_shift[1] - expected_shift[1]) * session.um_per_px
        )
        quality = _quality_from_metrics(float(response), provisional_correction_um, settings)
        corrected_shift = measured_shift
        was_corrected = False
        # Low-confidence path edges borrow the robust green-edge displacement
        # only when the user enables the conservative correction mode.
        if settings.use_green_edge_correction and correction_centers and quality != "good":
            center = _center_for_direction(direction, correction_centers)
            if center is not None:
                corrected_shift = (center[0], center[1])
                was_corrected = True

        layout_weight = 1.0 if was_corrected else settings.registration_weight
        final_position = _position_from_shift(
            positions[previous_key],
            base_positions[key],
            corrected_shift,
            max_correction_px,
            layout_weight,
        )
        applied_shift = (
            final_position[0] - positions[previous_key][0],
            final_position[1] - positions[previous_key][1],
        )
        correction_um = float(np.hypot(applied_shift[0] - expected_shift[0], applied_shift[1] - expected_shift[1]) * session.um_per_px)
        positions[key] = final_position
        path_edges.append(
            StitchEdgeQuality(
                previous=previous_key,
                current=key,
                direction=direction,
                expected_shift_px=expected_shift,
                measured_shift_px=measured_shift,
                applied_shift_px=applied_shift,
                response=float(response),
                correction_um=correction_um,
                quality=quality,
                raw_shift_px=measured_shift,
                corrected_shift_px=corrected_shift,
                was_corrected=was_corrected,
            )
        )
        previous_key = key
    return positions, path_edges


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

    if settings.white_balance_correction:
        tile_images = white_balance_tile_set(tile_images)

    base_positions = stage_positions_from_um(session.tiles, session.um_per_px)
    initial_positions, _initial_path_edges = _path_positions_and_edges(session, settings, tile_images, base_positions)
    initial_edges = _all_adjacent_edges(session, settings, tile_images, base_positions, initial_positions)
    correction_centers = _green_edge_centers(initial_edges) if settings.use_green_edge_correction else {}
    positions, _path_edges = _path_positions_and_edges(session, settings, tile_images, base_positions, correction_centers)
    edges = _all_adjacent_edges(session, settings, tile_images, base_positions, positions, correction_centers)
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
