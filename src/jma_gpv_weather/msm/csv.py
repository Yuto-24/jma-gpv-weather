from __future__ import annotations
import csv
import json
import math
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Sequence

import numpy as np
from ..models import Bounds, RemoteFile, RunSelection
from ..errors import MsmError
from ..grib import read_grib_records
from ..sources import DataSource
from ..sources.rish import RISH_BASE, RishSource
from ..time_utils import UTC, JST, target_window, expected_valid_times
from .spec import LEVELS_HPA, parse_listing, select_latest_complete_run

TARGET_HEIGHT_M = 4572.0
HEADER = ["valid_time_utc", "valid_time_jst", "level", "pressure_hpa", "height_m",
          "latitude", "longitude", "u_ms", "v_ms", "wind_speed_ms", "wind_speed_kt",
          "wind_direction_deg_from", "lower_bracket_m", "upper_bracket_m"]

def discover_run(target_date: date, base_url: str = RISH_BASE, *, source: DataSource | None = None) -> RunSelection:
    source = source if source is not None else RishSource(base_url)
    start, _ = target_window(target_date)
    days = {target_date-timedelta(days=n) for n in range(3)} | {start.date()}
    files = []
    errors = []
    for day in sorted(days):
        directory = source.directory_url(day)
        try:
            files.extend(parse_listing(source.read_listing(directory), directory))
        except MsmError as exc:
            errors.append(str(exc))
    if not files:
        raise MsmError("Could not discover MSM files: " + " | ".join(errors))
    return select_latest_complete_run(files, target_date)

def wind_metrics(u, v):
    speed = np.hypot(u, v)
    knots = speed * 1.9438444924406
    direction = (270.0 - np.degrees(np.arctan2(v, u))) % 360.0
    return speed, knots, np.where(speed < 0.1, np.nan, direction)

def interpolate_at_height(heights, u_values, v_values, target_m: float = TARGET_HEIGHT_M):
    points = sorted((float(z), float(u), float(v)) for z, u, v in zip(heights, u_values, v_values)
                    if math.isfinite(z) and math.isfinite(u) and math.isfinite(v))
    for (z0, u0, v0), (z1, u1, v1) in zip(points, points[1:]):
        if z0 <= target_m <= z1 and z1 > z0:
            weight = (target_m-z0)/(z1-z0)
            return u0+weight*(u1-u0), v0+weight*(v1-v0), z0, z1
    return None

def _times(valid):
    return valid.astimezone(UTC).isoformat(), valid.astimezone(JST).isoformat()

def _summary(lat, lon, rows):
    return {"rows": rows, "latitude_min": float(lat.min()), "latitude_max": float(lat.max()),
            "longitude_min": float(lon.min()), "longitude_max": float(lon.max()),
            "grid_points_per_time": int(lat.size)}

def _check(lat, lon, bounds):
    if lat.min() < bounds.lat_min or lat.max() > bounds.lat_max or lon.min() < bounds.lon_min or lon.max() > bounds.lon_max:
        raise MsmError("Subset contains a coordinate outside requested rectangle")

def _write_surface(path, records, target_date, bounds):
    count, lat, lon = 0, None, None
    with path.open("w", newline="", encoding="utf-8-sig") as handle:
        writer = csv.writer(handle); writer.writerow(HEADER)
        for valid in expected_valid_times(target_date, 1):
            if (valid, 10, "u") not in records or (valid, 10, "v") not in records:
                raise MsmError(f"Missing surface U/V at {valid.isoformat()}")
            u, lat, lon = records[valid, 10, "u"]; v = records[valid, 10, "v"][0]
            _check(lat, lon, bounds); speed, knots, direction = wind_metrics(u, v)
            utc_text, jst_text = _times(valid)
            for values in zip(lat.ravel(), lon.ravel(), u.ravel(), v.ravel(), speed.ravel(), knots.ravel(), direction.ravel()):
                latitude, longitude, u_ms, v_ms, speed_ms, speed_kt, direction_deg = values
                writer.writerow([utc_text, jst_text, "10m_AGL", "", 10, latitude, longitude,
                                 u_ms, v_ms, speed_ms, speed_kt, direction_deg, "", ""]); count += 1
    return _summary(lat, lon, count)

def _cube(records, valid):
    arrays, lat, lon = [], None, None
    for level in LEVELS_HPA:
        keys = [(valid, level, name) for name in ("hgt", "u", "v")]
        if any(key not in records for key in keys):
            raise MsmError(f"Missing HGT/U/V at {valid.isoformat()}, {level} hPa")
        hgt, lat, lon = records[keys[0]]; u = records[keys[1]][0]; v = records[keys[2]][0]
        arrays.append((hgt, u, v))
    return *(np.stack([x[i] for x in arrays]) for i in range(3)), lat, lon

def _write_pressure(paths, records, target_date, bounds):
    pressure_path, bounded_path, interpolated_path = paths
    counts = [0, 0, 0]; lat = lon = None
    with pressure_path.open("w", newline="", encoding="utf-8-sig") as p_handle, \
         bounded_path.open("w", newline="", encoding="utf-8-sig") as b_handle, \
         interpolated_path.open("w", newline="", encoding="utf-8-sig") as i_handle:
        writers = [csv.writer(h) for h in (p_handle, b_handle, i_handle)]
        for writer in writers: writer.writerow(HEADER)
        for valid in expected_valid_times(target_date, 3):
            hgt, u, v, lat, lon = _cube(records, valid); _check(lat, lon, bounds)
            utc_text, jst_text = _times(valid)
            for index, level in enumerate(LEVELS_HPA):
                speed, knots, direction = wind_metrics(u[index], v[index])
                for vals in zip(hgt[index].ravel(), lat.ravel(), lon.ravel(), u[index].ravel(),
                                v[index].ravel(), speed.ravel(), knots.ravel(), direction.ravel()):
                    height, latitude, longitude, u_ms, v_ms, speed_ms, speed_kt, direction_deg = vals
                    row = [utc_text, jst_text, f"{level}hPa", level, height, latitude, longitude,
                           u_ms, v_ms, speed_ms, speed_kt, direction_deg, "", ""]
                    writers[0].writerow(row); counts[0] += 1
                    if math.isfinite(height) and height <= TARGET_HEIGHT_M:
                        writers[1].writerow(row); counts[1] += 1
            fh, fu, fv = (array.reshape(len(LEVELS_HPA), -1) for array in (hgt, u, v))
            for index, (latitude, longitude) in enumerate(zip(lat.ravel(), lon.ravel())):
                result = interpolate_at_height(fh[:, index], fu[:, index], fv[:, index])
                if result is None: continue
                u_ms, v_ms, lower, upper = result
                speed, knots, direction = wind_metrics(np.array(u_ms), np.array(v_ms))
                row = [utc_text, jst_text, "15000ft_MSL", "", TARGET_HEIGHT_M, latitude, longitude,
                       u_ms, v_ms, float(speed), float(knots), float(direction), lower, upper]
                writers[1].writerow(row); writers[2].writerow(row); counts[1] += 1; counts[2] += 1
    return tuple(_summary(lat, lon, count) for count in counts)

def write_outputs(output_dir: Path, target_date: date, bounds: Bounds,
                  selection: RunSelection, local_files: Sequence[Path]):
    output_dir.mkdir(parents=True, exist_ok=True)
    surface, pressure = read_grib_records(local_files, target_date, bounds, pressure_levels=LEVELS_HPA)
    prefix = f"msm_wind_{target_date:%Y%m%d}"
    surface_path = output_dir/f"{prefix}_surface.csv"
    pressure_path = output_dir/f"{prefix}_pressure_levels.csv"
    bounded_path = output_dir/f"{prefix}_to_15000ft.csv"
    interpolated_path = output_dir/f"{prefix}_15000ft.csv"
    surface_summary = _write_surface(surface_path, surface, target_date, bounds)
    pressure_summary, bounded_summary, interp_summary = _write_pressure(
        (pressure_path, bounded_path, interpolated_path), pressure, target_date, bounds)
    start, end = target_window(target_date)
    metadata = {
        "target_date_jst": target_date.isoformat(),
        "target_window_utc": {"start": start.isoformat(), "end_exclusive": end.isoformat()},
        "selected_initial_time_utc": selection.run_utc.isoformat(),
        "selected_initial_time_jst": selection.run_utc.astimezone(JST).isoformat(),
        "requested_bounds": asdict(bounds),
        "target_height": {"feet": 15000, "metres": TARGET_HEIGHT_M,
                          "datum": "geopotential height above mean sea level; linear U/V interpolation"},
        "pressure_levels_hpa": list(LEVELS_HPA),
        "source": {"provider": "Kyoto University RISH JMA data archive",
                   "originator": "Japan Meteorological Agency", "archive_url": RISH_BASE,
                   "files": [f.url for f in selection.files]},
        "outputs": {surface_path.name: surface_summary, pressure_path.name: pressure_summary,
                    bounded_path.name: bounded_summary, interpolated_path.name: interp_summary},
    }
    (output_dir/f"{prefix}_metadata.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    return metadata
