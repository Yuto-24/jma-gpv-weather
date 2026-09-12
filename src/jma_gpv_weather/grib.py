"""GRIB decoding via pygrib; callers supply their product's pressure levels."""
from __future__ import annotations
from datetime import date, datetime
from pathlib import Path
from typing import Iterable

import numpy as np
from .models import Bounds
from .errors import GpvError
from .time_utils import UTC, target_window

def _subset_message(message, bounds: Bounds):
    # Request a halo because nominal 29.7 can be encoded as 29.699999..., then
    # round coordinates and strictly remove every point outside the rectangle.
    halo = 1e-6
    values, lat, lon = message.data(lat1=bounds.lat_min-halo, lat2=bounds.lat_max+halo,
                                    lon1=bounds.lon_min-halo, lon2=bounds.lon_max+halo)
    values = np.asarray(np.ma.filled(values, np.nan), dtype=float)
    lat = np.round(np.asarray(lat, dtype=float), 10)
    lon = np.round(np.asarray(lon, dtype=float), 10)
    inside = ((lat >= bounds.lat_min) & (lat <= bounds.lat_max) &
              (lon >= bounds.lon_min) & (lon <= bounds.lon_max))
    rows, columns = np.any(inside, axis=1), np.any(inside, axis=0)
    if not rows.any() or not columns.any():
        raise GpvError("No grid points inside requested rectangle")
    return values[np.ix_(rows, columns)], lat[np.ix_(rows, columns)], lon[np.ix_(rows, columns)]

def _identity(message, pressure_levels):
    level_type, level = getattr(message, "typeOfLevel", ""), int(getattr(message, "level", -1))
    category, number = getattr(message, "parameterCategory", None), getattr(message, "parameterNumber", None)
    short_name = str(getattr(message, "shortName", "")).lower()
    if level_type == "heightAboveGround" and level == 10 and category == 2 and number in (2, 3):
        return ("u" if number == 2 else "v"), 10
    if level_type == "heightAboveGround" and level in (0, 2):
        if (category, number) == (0, 0) or short_name in ("2t", "t2m"):
            return "tmp_surface", level
        if (category, number) == (1, 1) or short_name in ("2r", "r2", "rh"):
            return "rh", level
    if level_type in ("surface", "groundOrWaterSurface"):
        if (category, number) == (3, 0) or short_name in ("sp", "pres"):
            return "sp", 0
    if level_type in ("meanSea", "meanSeaLevel"):
        if (category, number) == (3, 1) or short_name in ("msl", "prmsl"):
            return "mslp", 0
    if level_type == "isobaricInhPa" and level in pressure_levels:
        if (category, number) == (0, 0) or short_name == "t":
            return "tmp", level
        if category == 2 and number in (2, 3):
            return ("u" if number == 2 else "v"), level
        if category == 3 and number == 5:
            return "hgt", level
    return None

def read_grib_records(
    paths: Iterable[Path],
    target_date: date | None,
    bounds: Bounds,
    valid_times: Iterable[datetime] | None = None,
    *,
    pressure_levels: tuple[int, ...],
):
    try:
        import pygrib
    except ImportError as exc:
        raise GpvError("pygrib is required; run: python -m pip install -e .") from exc
    requested_times = None if valid_times is None else {
        value.astimezone(UTC) for value in valid_times
    }
    if target_date is not None:
        start, end = target_window(target_date)
    elif not requested_times:
        raise ValueError("target_date or valid_times is required")
    surface, pressure = {}, {}
    for path in paths:
        try:
            grib = pygrib.open(str(path))
        except Exception as exc:
            raise GpvError(f"Cannot open {path}: {exc}") from exc
        try:
            for message in grib:
                identity = _identity(message, pressure_levels)
                valid = message.validDate
                valid = valid.replace(tzinfo=UTC) if valid.tzinfo is None else valid.astimezone(UTC)
                if identity is None:
                    continue
                if requested_times is not None and valid not in requested_times:
                    continue
                if requested_times is None and not start <= valid < end:
                    continue
                variable, level = identity
                try:
                    record = _subset_message(message, bounds)
                except Exception as exc:
                    raise GpvError(f"Failed to subset {path.name}: {exc}") from exc
                surface_variables = {"u", "v", "tmp_surface", "rh", "sp", "mslp"}
                target = surface if variable in surface_variables and level in (0, 2, 10) else pressure
                target[valid, level, variable] = record
        finally:
            grib.close()
    return surface, pressure
