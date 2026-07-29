from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Sequence

import numpy as np


@dataclass(frozen=True)
class VerticalValue:
    value: float
    lower_height_m: float
    upper_height_m: float
    weight: float


def linear_value(x0: float, x1: float, y0: float, y1: float, x: float) -> float:
    if x1 == x0:
        if x == x0:
            return float(y0)
        raise ValueError("zero-width interpolation interval")
    return float(y0 + (x - x0) / (x1 - x0) * (y1 - y0))


def vertical_at_height(
    heights: Sequence[float], values: Sequence[float], target_m: float
) -> VerticalValue | None:
    points = sorted(
        (float(height), float(value))
        for height, value in zip(heights, values)
        if math.isfinite(height) and math.isfinite(value)
    )
    for height, value in points:
        if target_m == height:
            return VerticalValue(value, height, height, 0.0)
    for (z0, v0), (z1, v1) in zip(points, points[1:]):
        if z0 < target_m < z1 and z1 > z0:
            weight = (target_m - z0) / (z1 - z0)
            return VerticalValue(v0 + weight * (v1 - v0), z0, z1, weight)
    return None


def bilinear(
    x: float, y: float, x0: float, x1: float, y0: float, y1: float, values
) -> float:
    grid = np.asarray(values, dtype=float)
    if grid.shape != (2, 2) or not np.isfinite(grid).all():
        raise ValueError("bilinear interpolation requires four finite values")
    if x0 == x1 and y0 == y1:
        return float(grid[0, 0])
    if x0 == x1:
        return linear_value(y0, y1, grid[0, 0], grid[1, 0], y)
    if y0 == y1:
        return linear_value(x0, x1, grid[0, 0], grid[0, 1], x)
    tx = (x - x0) / (x1 - x0)
    ty = (y - y0) / (y1 - y0)
    return float(
        grid[0, 0] * (1 - tx) * (1 - ty)
        + grid[0, 1] * tx * (1 - ty)
        + grid[1, 0] * (1 - tx) * ty
        + grid[1, 1] * tx * ty
    )


def temporal(
    before: datetime, after: datetime, before_value: float, after_value: float, target: datetime
) -> float:
    if any(value.tzinfo is None for value in (before, after, target)):
        raise ValueError("temporal interpolation requires timezone-aware datetimes")
    return linear_value(
        before.timestamp(), after.timestamp(), before_value, after_value, target.timestamp()
    )


def wind_metrics(u_ms: float, v_ms: float) -> tuple[float, float, float | None]:
    speed_ms = math.hypot(u_ms, v_ms)
    speed_kt = speed_ms * 1.9438444924406
    direction = None if speed_ms < 0.1 else (270.0 - math.degrees(math.atan2(v_ms, u_ms))) % 360
    return speed_ms, speed_kt, direction
