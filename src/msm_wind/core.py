from __future__ import annotations

import csv
import json
import math
import re
import time
import urllib.error
import urllib.request
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Iterable, Sequence
from zoneinfo import ZoneInfo

import numpy as np

JST = ZoneInfo("Asia/Tokyo")
UTC = timezone.utc
RISH_BASE = "http://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original"
LEVELS_HPA = (1000, 975, 950, 925, 900, 850, 800, 700, 600, 500)
TARGET_HEIGHT_M = 4572.0
FILE_RE = re.compile(
    r"Z__C_RJTD_(?P<run>\d{14})_MSM_GPV_Rjp_(?P<kind>Lsurf|L-pall)_"
    r"FH(?P<first>\d{2})-(?P<last>\d{2})_grib2\.bin"
)


from .errors import MsmError


@dataclass(frozen=True)
class Bounds:
    lat_min: float = 29.7
    lat_max: float = 35.2
    lon_min: float = 128.5
    lon_max: float = 134.8


@dataclass(frozen=True)
class RemoteFile:
    name: str
    url: str
    run_utc: datetime
    kind: str
    first_hour: int
    last_hour: int


@dataclass(frozen=True)
class RunSelection:
    run_utc: datetime
    files: tuple[RemoteFile, ...]


def target_window(target_date: date) -> tuple[datetime, datetime]:
    start = datetime.combine(target_date, dt_time.min, JST)
    return start.astimezone(UTC), (start + timedelta(days=1)).astimezone(UTC)


def expected_valid_times(target_date: date, step: int) -> tuple[datetime, ...]:
    start, end = target_window(target_date)
    return tuple(start + timedelta(hours=h) for h in range(0, int((end-start).total_seconds()/3600), step))


def parse_listing(html: str, directory_url: str) -> list[RemoteFile]:
    result = {}
    for match in FILE_RE.finditer(html):
        name = match.group(0)
        run = datetime.strptime(match.group("run"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        result[name] = RemoteFile(name, f"{directory_url.rstrip('/')}/{name}", run,
                                  match.group("kind"), int(match.group("first")), int(match.group("last")))
    return sorted(result.values(), key=lambda f: f.name)


def _urlopen(url: str, timeout: int = 30, headers: dict[str, str] | None = None):
    request = urllib.request.Request(url, headers={
        "User-Agent": "jma-msm-wind/0.1 (+educational-research)", **(headers or {})
    })
    return urllib.request.urlopen(request, timeout=timeout)


def read_listing(url: str, attempts: int = 3) -> str:
    last = None
    for attempt in range(attempts):
        try:
            with _urlopen(url.rstrip("/") + "/") as response:
                return response.read().decode("ascii", errors="ignore")
        except (OSError, urllib.error.URLError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise MsmError(f"RISH directory listing failed: {url}: {last}")


def _cover(files: Sequence[RemoteFile], kind: str, hour: int) -> RemoteFile | None:
    candidates = [f for f in files if f.kind == kind and f.first_hour <= hour <= f.last_hour]
    return min(candidates, key=lambda f: (f.last_hour-f.first_hour, f.name), default=None)


def select_latest_complete_run(files: Iterable[RemoteFile], target_date: date) -> RunSelection:
    grouped: dict[datetime, list[RemoteFile]] = defaultdict(list)
    for item in files:
        grouped[item.run_utc].append(item)
    failures = []
    for run in sorted(grouped, reverse=True):
        required = {}
        missing = []
        for kind, times in (("Lsurf", expected_valid_times(target_date, 1)),
                            ("L-pall", expected_valid_times(target_date, 3))):
            for valid in times:
                seconds = (valid-run).total_seconds()
                hour = int(seconds//3600)
                found = None if seconds < 0 or seconds % 3600 else _cover(grouped[run], kind, hour)
                if found is None:
                    missing.append(f"{kind}:FH{hour:02d}")
                else:
                    required[found.name] = found
        if not missing:
            return RunSelection(run, tuple(sorted(required.values(), key=lambda f: f.name)))
        failures.append(f"{run.isoformat()} missing {', '.join(missing[:4])}")
    raise MsmError(f"No run completely covers {target_date} JST ({'; '.join(failures[:5]) or 'no files'})")


def discover_run(target_date: date, base_url: str = RISH_BASE) -> RunSelection:
    start, _ = target_window(target_date)
    days = {target_date-timedelta(days=n) for n in range(3)} | {start.date()}
    files = []
    errors = []
    for day in sorted(days):
        directory = f"{base_url.rstrip('/')}/{day:%Y/%m/%d}"
        try:
            files.extend(parse_listing(read_listing(directory), directory))
        except MsmError as exc:
            errors.append(str(exc))
    if not files:
        raise MsmError("Could not discover MSM files: " + " | ".join(errors))
    return select_latest_complete_run(files, target_date)


def download(remote: RemoteFile, destination: Path, attempts: int = 4) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(destination.suffix + ".part")
    last = None
    for attempt in range(attempts):
        offset = partial.stat().st_size if partial.exists() else 0
        try:
            with _urlopen(remote.url, 60, {"Range": f"bytes={offset}-"} if offset else {}) as response:
                status = getattr(response, "status", 200)
                if offset and status != 206:
                    partial.unlink(missing_ok=True)
                    offset = 0
                with partial.open("ab" if offset and status == 206 else "wb") as output:
                    while chunk := response.read(1024*1024):
                        output.write(chunk)
            with partial.open("rb") as handle:
                if handle.read(4) != b"GRIB":
                    raise MsmError(f"Not a GRIB2 file: {remote.url}")
            partial.replace(destination)
            return destination
        except (OSError, urllib.error.URLError, MsmError) as exc:
            last = exc
            if attempt + 1 < attempts:
                time.sleep(2**attempt)
    raise MsmError(f"Download failed: {remote.url}: {last}")


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
        raise MsmError("No grid points inside requested rectangle")
    return values[np.ix_(rows, columns)], lat[np.ix_(rows, columns)], lon[np.ix_(rows, columns)]


def _identity(message):
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
    if level_type == "isobaricInhPa" and level in LEVELS_HPA:
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
):
    try:
        import pygrib
    except ImportError as exc:
        raise MsmError("pygrib is required; run: python -m pip install -e .") from exc
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
            raise MsmError(f"Cannot open {path}: {exc}") from exc
        try:
            for message in grib:
                identity = _identity(message)
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
                    raise MsmError(f"Failed to subset {path.name}: {exc}") from exc
                surface_variables = {"u", "v", "tmp_surface", "rh", "sp", "mslp"}
                target = surface if variable in surface_variables and level in (0, 2, 10) else pressure
                target[valid, level, variable] = record
        finally:
            grib.close()
    return surface, pressure


def _times(valid):
    return valid.astimezone(UTC).isoformat(), valid.astimezone(JST).isoformat()


def _summary(lat, lon, rows):
    return {"rows": rows, "latitude_min": float(lat.min()), "latitude_max": float(lat.max()),
            "longitude_min": float(lon.min()), "longitude_max": float(lon.max()),
            "grid_points_per_time": int(lat.size)}


def _check(lat, lon, bounds):
    if lat.min() < bounds.lat_min or lat.max() > bounds.lat_max or lon.min() < bounds.lon_min or lon.max() > bounds.lon_max:
        raise MsmError("Subset contains a coordinate outside requested rectangle")


HEADER = ["valid_time_utc", "valid_time_jst", "level", "pressure_hpa", "height_m",
          "latitude", "longitude", "u_ms", "v_ms", "wind_speed_ms", "wind_speed_kt",
          "wind_direction_deg_from", "lower_bracket_m", "upper_bracket_m"]


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
    surface, pressure = read_grib_records(local_files, target_date, bounds)
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
