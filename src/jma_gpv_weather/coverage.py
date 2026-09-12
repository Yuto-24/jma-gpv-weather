"""Offline specification coverage; no source, filesystem or network access."""
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
import math

from .models import RunId
from .time_utils import UTC


class CoverageState(str, Enum):
    COVERED = "covered"
    OUTSIDE_SPEC = "outside_spec"
    REQUIRES_HGT = "requires_hgt"


@dataclass(frozen=True)
class CoveragePoint:
    latitude: float
    longitude: float
    altitude_msl_m: float | None = None
    pressure_bracket_hpa: tuple[int, int] | None = None

    def __post_init__(self):
        values = [self.latitude, self.longitude]
        if self.altitude_msl_m is not None:
            values.append(self.altitude_msl_m)
        if not all(math.isfinite(v) for v in values):
            raise ValueError("coverage coordinates and altitude must be finite")
        if self.pressure_bracket_hpa is not None:
            if len(self.pressure_bracket_hpa) != 2:
                raise ValueError("pressure bracket requires two endpoints")
            lower, upper = self.pressure_bracket_hpa
            if not all(math.isfinite(v) and v > 0 for v in (lower, upper)) or lower < upper:
                raise ValueError("pressure bracket must be positive, descending hPa")


@dataclass(frozen=True)
class SpecCoverage:
    state: CoverageState
    reason_codes: tuple[str, ...]
    candidate_runs: tuple[RunId, ...]
    pressure_levels_hpa: tuple[int, ...]

    @property
    def outside_spec(self):
        """The only affirmative specification-exclusion signal."""
        return self.state == CoverageState.OUTSIDE_SPEC


def candidate_initial_times(requirements, as_of, hours, max_horizon):
    if as_of.tzinfo is None:
        raise ValueError("as_of must be timezone-aware")
    earliest = min(requirements.valid_times).astimezone(UTC)
    latest = max(requirements.valid_times).astimezone(UTC)
    end = min(earliest, as_of.astimezone(UTC))
    day = (latest - timedelta(hours=max_horizon)).replace(hour=0, minute=0, second=0, microsecond=0)
    result = []
    while day <= end:
        for hour in hours:
            run = day.replace(hour=hour)
            if latest - timedelta(hours=max_horizon) <= run <= end:
                result.append(run)
        day += timedelta(days=1)
    return tuple(sorted(result, reverse=True))


def evaluate_coverage(requirements, points, domain, levels, variables, candidates):
    reasons = []
    if not requirements.variables <= variables:
        reasons.append("UNSUPPORTED_VARIABLE")
    if any(not (domain.lat_min <= p.latitude <= domain.lat_max
                and domain.lon_min <= p.longitude <= domain.lon_max) for p in points):
        reasons.append("OUTSIDE_DOMAIN")
    if any(p.pressure_bracket_hpa is not None
           and any(level not in levels for level in p.pressure_bracket_hpa) for p in points):
        reasons.append("PRESSURE_LEVEL_UNSUPPORTED")
    if not candidates:
        reasons.append("NO_SPEC_COMPATIBLE_RUN")
    needs_hgt = any(p.altitude_msl_m is not None for p in points)
    state = (CoverageState.OUTSIDE_SPEC if reasons else
             CoverageState.REQUIRES_HGT if needs_hgt else CoverageState.COVERED)
    if needs_hgt:
        reasons.append("MSL_ALTITUDE_REQUIRES_HGT")
    return SpecCoverage(state, tuple(reasons), tuple(RunId(r) for r in candidates), levels)
