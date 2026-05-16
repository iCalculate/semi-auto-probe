from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable

import cv2
import numpy as np


GridIndex = tuple[int, int]


@dataclass(frozen=True)
class PlaneModel:
    a: float
    b: float
    c: float

    def z_at(self, x: float, y: float) -> float:
        return self.a * x + self.b * y + self.c


def serpentine_indices(rows: int, cols: int) -> list[GridIndex]:
    if rows <= 0 or cols <= 0:
        raise ValueError("Rows and columns must be positive.")
    path: list[GridIndex] = []
    for row in range(rows):
        column_range = range(cols) if row % 2 == 0 else range(cols - 1, -1, -1)
        path.extend((row, col) for col in column_range)
    return path


def flat_field_correct(image_bgr: np.ndarray, blur_kernel: int = 0) -> np.ndarray:
    if image_bgr.ndim != 3 or image_bgr.shape[2] != 3:
        raise ValueError("Expected a BGR image with three channels.")

    height, width = image_bgr.shape[:2]
    if blur_kernel <= 0:
        blur_kernel = max(31, ((min(height, width) // 8) | 1))
    if blur_kernel % 2 == 0:
        blur_kernel += 1

    image = image_bgr.astype(np.float32)
    illumination = cv2.GaussianBlur(image, (blur_kernel, blur_kernel), 0)
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
    previous_gray = cv2.cvtColor(previous_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)
    current_gray = cv2.cvtColor(current_bgr, cv2.COLOR_BGR2GRAY).astype(np.float32)

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
    else:
        raise ValueError(f"Unsupported stitch direction: {direction}")

    if min(previous_roi.shape[:2]) < 2 or min(current_roi.shape[:2]) < 2:
        raise ValueError("Overlap is too small for phase correlation.")

    window = cv2.createHanningWindow((previous_roi.shape[1], previous_roi.shape[0]), cv2.CV_32F)
    (shift_x, shift_y), response = cv2.phaseCorrelate(previous_roi, current_roi, window)
    return expected_dx + shift_x, expected_dy + shift_y, float(response)


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


def fit_plane(samples: Iterable[tuple[float, float, float]]) -> PlaneModel:
    points = list(samples)
    if len(points) < 3:
        raise ValueError("At least three plane samples are required.")
    matrix = np.array([[x, y, 1.0] for x, y, _z in points], dtype=np.float64)
    values = np.array([z for _x, _y, z in points], dtype=np.float64)
    coefficients, *_ = np.linalg.lstsq(matrix, values, rcond=None)
    return PlaneModel(a=float(coefficients[0]), b=float(coefficients[1]), c=float(coefficients[2]))
