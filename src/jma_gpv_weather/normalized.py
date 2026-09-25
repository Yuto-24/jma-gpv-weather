from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from .models import Bounds

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


def save_records(
    path: Path, surface, pressure, metadata: dict, *, pressure_levels: tuple[int, ...],
    surface_temperature_level: int | None = None,
) -> None:
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
                key = next((key for key in surface if key[0] == valid and key[2] == name
                            and (name != "tmp_surface" or surface_temperature_level is None
                                 or key[1] == surface_temperature_level)), None)
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
                for level in pressure_levels:
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
                "pressure_hpa": np.asarray(pressure_levels, dtype=int),
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


def prepare_records(cache_dir, run, prepared_bounds, valid_times, paths, hashes, *, pressure_levels,
                    verify_manifest=False, validate_records=None, surface_temperature_level=None):
    """Reuse the same atomic normalized cache for each caller-owned model root.

    Optional model validation raises ValueError for incomplete records. Validate
    loaded data before reuse and decoded data before publishing either artifact.
    """
    from .cache import file_lock, sha256_file
    from .grib import read_grib_records
    key = normalized_key(prepared_bounds, valid_times)
    normalized_path = (
        cache_dir
        / "normalized"
        / ("v1" if surface_temperature_level is None else f"v2-surface-{surface_temperature_level}")
        / str(run)
        / key
        / "weather.nc"
    )
    lock_path = cache_dir / "locks" / f"normalized-{run}-{key}.lock"
    manifest_path = normalized_path.parent / "manifest.json"
    with file_lock(lock_path):
        if normalized_path.exists():
            try:
                if verify_manifest:
                    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if (manifest.get("source_hashes") != hashes
                            or manifest.get("normalized_sha256") != sha256_file(normalized_path)):
                        raise ValueError("normalized cache hash/source mismatch")
                surface, pressure = load_records(normalized_path)
                if validate_records is not None:
                    validate_records(surface, pressure)
            except (OSError, ValueError):
                corrupt = normalized_path.with_suffix(".nc.corrupt")
                normalized_path.replace(corrupt)
                surface, pressure = {}, {}
        else:
            surface, pressure = {}, {}
        if not surface and not pressure:
            surface, pressure = read_grib_records(
                paths,
                None,
                prepared_bounds,
                valid_times=valid_times,
                pressure_levels=pressure_levels,
            )
            if validate_records is not None:
                validate_records(surface, pressure)
            save_records(
                normalized_path,
                surface,
                pressure,
                {
                    "initial_time_utc": run.initial_time_utc.isoformat(),
                    "source_hashes_json": json.dumps(hashes, sort_keys=True),
                },
                pressure_levels=pressure_levels,
                surface_temperature_level=surface_temperature_level,
            )
            manifest_temp = manifest_path.with_suffix(".json.tmp")
            manifest_temp.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "initial_time_utc": run.initial_time_utc.isoformat(),
                        "prepared_bounds": prepared_bounds.__dict__,
                        "valid_times": [value.isoformat() for value in valid_times],
                        "source_hashes": hashes,
                        "normalized_file": normalized_path.name,
                        **({"normalized_sha256": sha256_file(normalized_path)} if verify_manifest else {}),
                    },
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            manifest_temp.replace(manifest_path)
    return surface, pressure
