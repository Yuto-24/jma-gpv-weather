"""MSM product names, pressure levels and temporal/spatial coverage rules."""
import re
from collections import defaultdict
from datetime import date, datetime, timedelta
from typing import Iterable, Sequence

from ..errors import MsmError
from ..models import Bounds, RemoteFile, RunSelection, ForecastRequirements, WeatherVariable
from ..time_utils import UTC, expected_valid_times

LEVELS_HPA = (1000, 975, 950, 925, 900, 850, 800, 700, 600, 500)
FILE_RE = re.compile(
    r"Z__C_RJTD_(?P<run>\d{14})_MSM_GPV_Rjp_(?P<kind>Lsurf|L-pall)_"
    r"FH(?P<first>\d{2})-(?P<last>\d{2})_grib2\.bin"
)
MAX_FORECAST_HOURS = 78
DEFAULT_BOUNDS = Bounds()
SURFACE_VARIABLES = {WeatherVariable.SURFACE_WIND, WeatherVariable.ESTIMATED_QNH}
PRESSURE_VARIABLES = {WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE}

def parse_listing(html: str, directory_url: str) -> list[RemoteFile]:
    result = {}
    for match in FILE_RE.finditer(html):
        name = match.group(0)
        run = datetime.strptime(match.group("run"), "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        result[name] = RemoteFile(name, f"{directory_url.rstrip('/')}/{name}", run,
                                  match.group("kind"), int(match.group("first")), int(match.group("last")))
    return sorted(result.values(), key=lambda f: f.name)

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
