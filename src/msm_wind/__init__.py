"""JMA MSM weather download, interpolation, and provenance tools."""

from .client import DEFAULT_BOUNDS, MsmClient
from .core import Bounds
from .models import (
    AloftQuery,
    Availability,
    EstimatedQnhQuery,
    ForecastRequirements,
    ForecastRunStatus,
    RunId,
    SurfaceWindQuery,
    WeatherResult,
    WeatherVariable,
)

__all__ = [
    "DEFAULT_BOUNDS",
    "AloftQuery",
    "Availability",
    "Bounds",
    "EstimatedQnhQuery",
    "ForecastRequirements",
    "ForecastRunStatus",
    "MsmClient",
    "RunId",
    "SurfaceWindQuery",
    "WeatherResult",
    "WeatherVariable",
]

__version__ = "0.2.0"
