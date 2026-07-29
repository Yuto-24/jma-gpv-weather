from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .core import Bounds, RISH_BASE, RemoteFile, RunSelection, parse_listing, read_listing
from .errors import NoCompatibleRunError
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
                files.extend(parse_listing(read_listing(directory), directory))
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
