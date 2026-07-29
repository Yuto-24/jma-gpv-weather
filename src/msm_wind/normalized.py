from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .core import Bounds, LEVELS_HPA

SURFACE_VARIABLES = ("u", "v", "sp", "mslp", "tmp_surface", "rh")
PRESSURE_VARIABLES = ("hgt", "u", "v", "tmp")


def normalized_key(bounds: Bounds, valid_times) -> str:
    payload = {
        "schema": 1,
        "bounds": bounds.__dict__,
        "valid_times": [value.isoformat() for value in sorted(valid_times)],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _coordinates(records):
    _, lat, lon = next(iter(records.values()))
    return lat[:, 0], lon[0, :]


def _netcdf_times(values):
    return np.asarray(
        [np.datetime64(value.astimezone(timezone.utc).replace(tzinfo=None), "ns") for value in values]
    )


def save_records(path: Path, surface, pressure, metadata: dict) -> None:
    import xarray as xr

    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".nc.tmp")
    surface_times = sorted({key[0] for key in surface})
    pressure_times = sorted({key[0] for key in pressure})
    datasets = {}
    if surface:
        lat, lon = _coordinates(surface)
        variables = {}
        for name in SURFACE_VARIABLES:
            arrays = []
            for valid in surface_times:
                key = next((key for key in surface if key[0] == valid and key[2] == name), None)
                arrays.append(
                    np.full((len(lat), len(lon)), np.nan)
                    if key is None
                    else surface[key][0]
                )
            variables[name] = (("valid_time", "latitude", "longitude"), np.stack(arrays))
        datasets["surface"] = xr.Dataset(
            variables,
            coords={"valid_time": _netcdf_times(surface_times), "latitude": lat, "longitude": lon},
        )
    if pressure:
        lat, lon = _coordinates(pressure)
        variables = {}
        for name in PRESSURE_VARIABLES:
            time_arrays = []
            for valid in pressure_times:
                level_arrays = []
                for level in LEVELS_HPA:
                    key = (valid, level, name)
                    level_arrays.append(
                        np.full((len(lat), len(lon)), np.nan)
                        if key not in pressure
                        else pressure[key][0]
                    )
                time_arrays.append(np.stack(level_arrays))
            variables[name] = (
                ("valid_time", "pressure_hpa", "latitude", "longitude"),
                np.stack(time_arrays),
            )
        datasets["pressure"] = xr.Dataset(
            variables,
            coords={
                "valid_time": _netcdf_times(pressure_times),
                "pressure_hpa": np.asarray(LEVELS_HPA, dtype=int),
                "latitude": lat,
                "longitude": lon,
            },
        )
    for index, (group, dataset) in enumerate(datasets.items()):
        dataset.attrs.update({"schema_version": 1, **metadata})
        mode = "w" if index == 0 else "a"
        dataset.to_netcdf(temporary, engine="h5netcdf", group=group, mode=mode)
    temporary.replace(path)


def load_records(path: Path):
    import xarray as xr

    surface, pressure = {}, {}
    try:
        dataset = xr.open_dataset(path, engine="h5netcdf", group="surface")
        lat, lon = np.meshgrid(dataset.latitude.values, dataset.longitude.values, indexing="ij")
        for valid_value in dataset.valid_time.values:
            valid = _to_datetime(valid_value)
            for name in SURFACE_VARIABLES:
                values = dataset[name].sel(valid_time=valid_value).values
                if np.isfinite(values).any():
                    level = 10 if name in ("u", "v") else 2 if name in ("tmp_surface", "rh") else 0
                    surface[valid, level, name] = (values, lat, lon)
        dataset.close()
    except (OSError, KeyError):
        pass
    try:
        dataset = xr.open_dataset(path, engine="h5netcdf", group="pressure")
        lat, lon = np.meshgrid(dataset.latitude.values, dataset.longitude.values, indexing="ij")
        for valid_value in dataset.valid_time.values:
            valid = _to_datetime(valid_value)
            for level in dataset.pressure_hpa.values:
                for name in PRESSURE_VARIABLES:
                    values = dataset[name].sel(
                        valid_time=valid_value, pressure_hpa=level
                    ).values
                    if np.isfinite(values).any():
                        pressure[valid, int(level), name] = (values, lat, lon)
        dataset.close()
    except (OSError, KeyError):
        pass
    return surface, pressure


def _to_datetime(value) -> datetime:
    timestamp_ns = np.datetime64(value, "ns").astype("int64")
    return datetime.fromtimestamp(timestamp_ns / 1_000_000_000, tz=timezone.utc)
