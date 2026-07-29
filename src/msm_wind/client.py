from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Iterable

from .core import (
    LEVELS_HPA,
    Bounds,
    RISH_BASE,
    RemoteFile,
    RunSelection,
    parse_listing,
    read_listing,
)
from .errors import MissingVariableError, NoCompatibleRunError, SelectedRunCoverageError
from .models import (
    ForecastRequirements,
    ForecastRunStatus,
    RunId,
    WeatherVariable,
)

UTC = timezone.utc
DEFAULT_BOUNDS = Bounds()
SURFACE_VARIABLES = {
    WeatherVariable.SURFACE_WIND,
    WeatherVariable.ESTIMATED_QNH,
}
PRESSURE_VARIABLES = {
    WeatherVariable.ALOFT_WIND,
    WeatherVariable.ALOFT_TEMPERATURE,
}


def interpolation_bounds(bounds: Bounds) -> Bounds:
    return Bounds(
        max(22.4, bounds.lat_min - 0.11),
        min(47.6, bounds.lat_max + 0.11),
        max(120.0, bounds.lon_min - 0.13),
        min(150.0, bounds.lon_max + 0.13),
    )


def _bracket_hours(valid_time: datetime, step_hours: int) -> tuple[datetime, ...]:
    utc = valid_time.astimezone(UTC)
    epoch_hours = int(utc.timestamp() // 3600)
    lower_hours = epoch_hours - epoch_hours % step_hours
    lower = datetime.fromtimestamp(lower_hours * 3600, UTC)
    if utc == lower:
        return (lower,)
    return lower, lower + timedelta(hours=step_hours)


def required_valid_times(requirements: ForecastRequirements) -> dict[str, tuple[datetime, ...]]:
    result: dict[str, set[datetime]] = {"Lsurf": set(), "L-pall": set()}
    if requirements.variables & SURFACE_VARIABLES:
        for valid in requirements.valid_times:
            result["Lsurf"].update(_bracket_hours(valid, 1))
    if requirements.variables & PRESSURE_VARIABLES:
        for valid in requirements.valid_times:
            result["L-pall"].update(_bracket_hours(valid, 3))
    return {kind: tuple(sorted(values)) for kind, values in result.items() if values}


def select_compatible_runs(
    files: Iterable[RemoteFile], requirements: ForecastRequirements
) -> tuple[RunSelection, ...]:
    grouped: dict[datetime, list[RemoteFile]] = defaultdict(list)
    for item in files:
        grouped[item.run_utc].append(item)
    needed = required_valid_times(requirements)
    selections = []
    for run in sorted(grouped, reverse=True):
        chosen: dict[str, RemoteFile] = {}
        complete = True
        for kind, times in needed.items():
            for valid in times:
                seconds = (valid - run).total_seconds()
                if seconds < 0 or seconds % 3600:
                    complete = False
                    break
                hour = int(seconds // 3600)
                candidates = [
                    item
                    for item in grouped[run]
                    if item.kind == kind and item.first_hour <= hour <= item.last_hour
                ]
                if not candidates:
                    complete = False
                    break
                selected = min(candidates, key=lambda item: (item.last_hour - item.first_hour, item.name))
                chosen[selected.name] = selected
            if not complete:
                break
        if complete:
            selections.append(
                RunSelection(run, tuple(sorted(chosen.values(), key=lambda item: item.name)))
            )
    return tuple(selections)


class MsmClient:
    def __init__(
        self,
        cache_dir: str | Path = "data",
        bounds: Bounds = DEFAULT_BOUNDS,
        base_url: str = RISH_BASE,
    ):
        self.cache_dir = Path(cache_dir)
        self.bounds = bounds
        self.base_url = base_url

    def discover_runs(self, requirements: ForecastRequirements) -> tuple[RunSelection, ...]:
        from .cache import cached_listing

        needed = required_valid_times(requirements)
        earliest = min(time for values in needed.values() for time in values)
        latest = max(time for values in needed.values() for time in values)
        first_day = (earliest - timedelta(hours=78)).date()
        last_day = min(latest, datetime.now(UTC)).date()
        files: list[RemoteFile] = []
        day = first_day
        while day <= last_day:
            directory = f"{self.base_url.rstrip('/')}/{day:%Y/%m/%d}"
            try:
                listing = cached_listing(directory, self.cache_dir, read_listing)
                files.extend(parse_listing(listing, directory))
            except Exception:
                pass
            day += timedelta(days=1)
        runs = select_compatible_runs(files, requirements)
        if not runs:
            raise NoCompatibleRunError("No forecast run covers all required interpolation times")
        return runs

    def resolve_run(
        self,
        requirements: ForecastRequirements,
        selected_run: RunId | None = None,
        available_runs: tuple[RunSelection, ...] | None = None,
    ) -> ForecastRunStatus:
        runs = available_runs or self.discover_runs(requirements)
        latest = RunId(runs[0].run_utc)
        run_ids = {RunId(run.run_utc) for run in runs}
        if selected_run is None:
            return ForecastRunStatus(latest, latest, False, True)
        covers = selected_run in run_ids
        update = covers and latest.initial_time_utc > selected_run.initial_time_utc
        warnings = ("UPDATE_AVAILABLE",) if update else ()
        if not covers:
            warnings = ("SELECTED_RUN_OUT_OF_COVERAGE",)
        return ForecastRunStatus(selected_run, latest, update, covers, warnings)

    def prepare_run(
        self,
        run: RunId,
        requirements: ForecastRequirements,
        available_runs: tuple[RunSelection, ...] | None = None,
        terrain_provider=None,
    ):
        from .cache import acquire_files, file_lock
        from .core import read_grib_records
        from .dataset import PreparedForecast
        from .normalized import load_records, normalized_key, save_records

        runs = available_runs or self.discover_runs(requirements)
        selection = next(
            (candidate for candidate in runs if candidate.run_utc == run.initial_time_utc),
            None,
        )
        if selection is None:
            raise SelectedRunCoverageError(
                f"selected run {run} does not cover all required interpolation times"
            )
        paths, hashes = acquire_files(selection.files, self.cache_dir)
        needed = required_valid_times(requirements)
        valid_times = tuple(
            sorted({value for values in needed.values() for value in values})
        )
        prepared_bounds = interpolation_bounds(self.bounds)
        key = normalized_key(prepared_bounds, valid_times)
        normalized_path = (
            self.cache_dir
            / "normalized"
            / "v1"
            / str(run)
            / key
            / "weather.nc"
        )
        lock_path = self.cache_dir / "locks" / f"normalized-{run}-{key}.lock"
        with file_lock(lock_path):
            if normalized_path.exists():
                try:
                    surface, pressure = load_records(normalized_path)
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
                )
                save_records(
                    normalized_path,
                    surface,
                    pressure,
                    {
                        "initial_time_utc": run.initial_time_utc.isoformat(),
                        "source_hashes_json": json.dumps(hashes, sort_keys=True),
                    },
                )
                manifest_path = normalized_path.parent / "manifest.json"
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
                        },
                        indent=2,
                        sort_keys=True,
                    )
                    + "\n",
                    encoding="utf-8",
                )
                manifest_temp.replace(manifest_path)
        self._validate_prepared(requirements, needed, surface, pressure)
        return PreparedForecast(
            selection,
            surface,
            pressure,
            hashes,
            terrain_provider=terrain_provider,
        )

    @staticmethod
    def _validate_prepared(requirements, needed, surface, pressure):
        missing = []
        if WeatherVariable.SURFACE_WIND in requirements.variables:
            for valid in needed.get("Lsurf", ()):
                for name in ("u", "v"):
                    if not any(key[0] == valid and key[2] == name for key in surface):
                        missing.append(f"{valid.isoformat()}:surface:{name}")
        if WeatherVariable.ESTIMATED_QNH in requirements.variables:
            for valid in needed.get("Lsurf", ()):
                for name in ("sp", "tmp_surface", "rh"):
                    if not any(key[0] == valid and key[2] == name for key in surface):
                        missing.append(f"{valid.isoformat()}:surface:{name}")
        pressure_names = {"hgt"}
        if WeatherVariable.ALOFT_WIND in requirements.variables:
            pressure_names.update(("u", "v"))
        if WeatherVariable.ALOFT_TEMPERATURE in requirements.variables:
            pressure_names.add("tmp")
        if pressure_names != {"hgt"}:
            for valid in needed.get("L-pall", ()):
                for level in LEVELS_HPA:
                    for name in pressure_names:
                        if (valid, level, name) not in pressure:
                            missing.append(f"{valid.isoformat()}:{level}hPa:{name}")
        if missing:
            raise MissingVariableError(
                "required MSM fields are missing: " + ", ".join(missing[:12])
            )
