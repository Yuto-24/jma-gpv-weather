"""Current GSM Japan GPV: JMA specification No.12501 and corrected TI 601.

Only Rjp_Gll0p1deg products. FD is days/hours, not forecast hours.
"""
from bisect import bisect_left
from collections import defaultdict
from datetime import datetime, timedelta
import re

from ..coverage import candidate_initial_times, evaluate_coverage
from ..models import Bounds, RunSelection, RemoteFile, WeatherVariable
from ..time_utils import UTC

DOMAIN = Bounds(20.0, 50.0, 120.0, 150.0)
LEVELS_HPA = (1000, 975, 950, 925, 900, 850, 800, 700, 600, 500, 400, 300, 250, 200, 150, 100)
RUN_HOURS = (0, 6, 12, 18)
SUPPORTED_VARIABLES = frozenset({WeatherVariable.ALOFT_WIND,
                               WeatherVariable.ALOFT_TEMPERATURE,
                               WeatherVariable.SURFACE_TEMPERATURE})
FILE_RE = re.compile(
    r"Z__C_RJTD_(?P<run>\d{14})_GSM_GPV_Rjp_Gll0p1deg_(?P<kind>Lsurf|L-pall)_"
    r"FD(?P<first>\d{4})-(?P<last>\d{4})_grib2\.bin"
)
# TI 601 (2024-01-24 correction), table 3; No.12501 appendix 4.
FILE_INTERVALS = {
    "Lsurf": ((0, 24), (25, 48), (49, 72), (73, 96), (97, 120), (121, 132),
              (135, 168), (171, 216), (219, 264)),
    "L-pall": ((0, 24), (27, 48), (51, 72), (75, 96), (99, 120), (123, 132),
               (138, 168), (174, 216), (222, 264)),
}


def horizon(run):
    run = run.astimezone(UTC)
    return 264 if run.hour in (0, 12) else 132


def valid_initial_time(run):
    if run.tzinfo is None:
        return False
    run = run.astimezone(UTC)
    return run.hour in RUN_HOURS and not (run.minute or run.second or run.microsecond)


def forecast_hours(run, kind):
    step = 1 if kind == "Lsurf" else 3
    extension = 3 if kind == "Lsurf" else 6
    return tuple(range(0, 133, step)) + tuple(range(132 + extension, horizon(run) + 1, extension))


def required_valid_times(requirements, run):
    if not valid_initial_time(run) or not requirements.variables <= SUPPORTED_VARIABLES:
        return None
    run = run.astimezone(UTC)
    kinds = []
    if WeatherVariable.SURFACE_TEMPERATURE in requirements.variables:
        kinds.append("Lsurf")
    if requirements.variables & {WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE}:
        kinds.append("L-pall")
    result = {}
    for kind in kinds:
        times = tuple(run + timedelta(hours=h) for h in forecast_hours(run, kind))
        needed = set()
        for valid in requirements.valid_times:
            i = bisect_left(times, valid)
            if i < len(times) and times[i] == valid:
                needed.add(times[i])
            elif i == 0 or i == len(times):
                return None
            else:
                needed.update(times[i-1:i+1])
        result[kind] = tuple(sorted(needed))
    return result


def check_coverage(requirements, *, points=(), run=None, as_of=None):
    as_of = datetime.now(UTC) if as_of is None else as_of
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    candidates = ((run.initial_time_utc.astimezone(UTC),) if run is not None else
                  candidate_initial_times(requirements, as_of, RUN_HOURS, 264))
    candidates = tuple(r for r in candidates if r <= as_of and required_valid_times(requirements, r))
    return evaluate_coverage(requirements, points, DOMAIN, LEVELS_HPA, SUPPORTED_VARIABLES, candidates)


def parse_listing(html, directory_url):
    result = {}
    for match in FILE_RE.finditer(html):
        try:
            run = datetime.strptime(match['run'], '%Y%m%d%H%M%S').replace(tzinfo=UTC)
        except ValueError:
            continue
        def hour(fd):
            return int(fd[:2]) * 24 + int(fd[2:])
        first, last = hour(match['first']), hour(match['last'])
        kind = match['kind']
        if (not valid_initial_time(run) or (first, last) not in FILE_INTERVALS[kind]
                or last > horizon(run) or int(match['first'][2:]) >= 24
                or int(match['last'][2:]) >= 24):
            continue
        name = match.group(0)
        result[name] = RemoteFile(name, f"{directory_url.rstrip('/')}/{name}", run, kind, first, last)
    return sorted(result.values(), key=lambda f: f.name)


def select_compatible_runs(files, requirements):
    grouped = defaultdict(list)
    for item in files:
        # Check even injected RunSelection objects against the actual product contract.
        parsed = parse_listing(item.name, item.url.rsplit('/', 1)[0])
        if parsed and parsed[0] == item:
            grouped[item.run_utc].append(item)
    selections = []
    for run in sorted(grouped, reverse=True):
        needed = required_valid_times(requirements, run)
        if needed is None:
            continue
        chosen = {}
        for kind, times in needed.items():
            for valid in times:
                hour = (valid - run).total_seconds() / 3600
                found = next((f for f in grouped[run] if f.kind == kind
                              and f.first_hour <= hour <= f.last_hour), None)
                if found is None:
                    break
                chosen[found.name] = found
            else:
                continue
            break
        else:
            selections.append(RunSelection(run, tuple(sorted(chosen.values(), key=lambda f: f.name))))
    return tuple(selections)


def interpolation_bounds(bounds):
    return Bounds(max(DOMAIN.lat_min, bounds.lat_min - .11),
                  min(DOMAIN.lat_max, bounds.lat_max + .11),
                  max(DOMAIN.lon_min, bounds.lon_min - .13),
                  min(DOMAIN.lon_max, bounds.lon_max + .13))
