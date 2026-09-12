from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping


class WeatherVariable(str, Enum):
    ALOFT_WIND = "aloft_wind"
    ALOFT_TEMPERATURE = "aloft_temperature"
    SURFACE_WIND = "surface_wind"
    SURFACE_TEMPERATURE = "surface_temperature"
    ESTIMATED_QNH = "estimated_qnh"


class Availability(str, Enum):
    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, order=True)
class RunId:
    initial_time_utc: datetime

    def __post_init__(self):
        if self.initial_time_utc.tzinfo is None:
            raise ValueError("RunId requires a timezone-aware UTC datetime")

    def __str__(self) -> str:
        return self.initial_time_utc.strftime("%Y%m%d%H%M%S")


@dataclass(frozen=True)
class ForecastRequirements:
    valid_times: tuple[datetime, ...]
    variables: frozenset[WeatherVariable]

    def __post_init__(self):
        if not self.valid_times:
            raise ValueError("at least one valid time is required")
        if any(value.tzinfo is None for value in self.valid_times):
            raise ValueError("valid times must be timezone-aware")
        if not self.variables:
            raise ValueError("at least one weather variable is required")


@dataclass(frozen=True)
class ForecastRunStatus:
    selected_run: RunId | None
    latest_compatible_run: RunId | None
    update_available: bool
    selected_run_covers_request: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class AloftQuery:
    latitude: float
    longitude: float
    valid_time: datetime
    altitude_msl_m: float


@dataclass(frozen=True)
class SurfaceWindQuery:
    latitude: float
    longitude: float
    valid_time: datetime
    requested_agl_m: float = 0.0


@dataclass(frozen=True)
class SurfaceTemperatureQuery:
    latitude: float
    longitude: float
    valid_time: datetime


@dataclass(frozen=True)
class AloftTemperatureQuery(AloftQuery):
    """Temperature-only query; does not require wind fields."""


@dataclass(frozen=True)
class EstimatedQnhQuery:
    latitude: float
    longitude: float
    valid_time: datetime
    elevation_msl_m: float


@dataclass(frozen=True)
class Provenance:
    initial_time_utc: datetime | None = None
    source_urls: tuple[str, ...] = ()
    source_hashes: Mapping[str, str] = field(default_factory=dict)
    interpolation_method: str | None = None
    trace: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WeatherResult:
    availability: Availability
    kind: str
    values: Mapping[str, float | str | None] = field(default_factory=dict)
    reason_code: str | None = None
    warnings: tuple[str, ...] = ("NOT_FOR_OPERATIONAL_USE",)
    provenance: Provenance = field(default_factory=Provenance)


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
