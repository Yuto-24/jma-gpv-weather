"""JMA MSM weather download, interpolation, and provenance tools."""

from .client import DEFAULT_BOUNDS, MsmClient
from .core import Bounds
from .dataset import PreparedForecast
from .interpolated_terrain import (
    InterpolatedMsmTopographyProvider,
    InterpolatedTerrainSourceManifest,
)
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
from .terrain import GridTerrainProvider

__all__ = [
    "DEFAULT_BOUNDS",
    "AloftQuery",
    "Availability",
    "Bounds",
    "EstimatedQnhQuery",
    "ForecastRequirements",
    "ForecastRunStatus",
    "GridTerrainProvider",
    "InterpolatedMsmTopographyProvider",
    "InterpolatedTerrainSourceManifest",
    "MsmClient",
    "PreparedForecast",
    "RunId",
    "SurfaceWindQuery",
    "WeatherResult",
    "WeatherVariable",
]

__version__ = "0.2.1"
