from __future__ import annotations

import math
from bisect import bisect_left
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


def _time_bracket(times: Sequence[datetime], target: datetime):
    ordered = sorted(set(times))
    index = bisect_left(ordered, target)
    if index < len(ordered) and ordered[index] == target:
        return ordered[index], ordered[index]
    if index == 0 or index == len(ordered):
        return None
    return ordered[index - 1], ordered[index]

def _grid_bracket(lat: np.ndarray, lon: np.ndarray, latitude: float, longitude: float):
    lat_axis = np.asarray(lat[:, 0], dtype=float)
    lon_axis = np.asarray(lon[0, :], dtype=float)
    if lat_axis[0] > lat_axis[-1]:
        lat_axis = lat_axis[::-1]
        reverse_lat = True
    else:
        reverse_lat = False
    if lon_axis[0] > lon_axis[-1]:
        lon_axis = lon_axis[::-1]
        reverse_lon = True
    else:
        reverse_lon = False

    def bracket(axis, value):
        position = bisect_left(axis.tolist(), value)
        if position < len(axis) and axis[position] == value:
            return position, position
        if position == 0 or position == len(axis):
            return None
        return position - 1, position

    lat_indices = bracket(lat_axis, latitude)
    lon_indices = bracket(lon_axis, longitude)
    if lat_indices is None or lon_indices is None:
        return None

    def original(index, length, reverse):
        return length - 1 - index if reverse else index

    yi = tuple(original(i, len(lat_axis), reverse_lat) for i in lat_indices)
    xi = tuple(original(i, len(lon_axis), reverse_lon) for i in lon_indices)
    return yi, xi, (lat_axis[lat_indices[0]], lat_axis[lat_indices[1]]), (
        lon_axis[lon_indices[0]],
        lon_axis[lon_indices[1]],
    )
