from __future__ import annotations
from typing import Callable
from .spec import LEVELS_HPA
from ..models import RunSelection
from ..errors import InvalidQueryError
from ..interpolation import wind_metrics
from ..models import (
    AloftQuery, AloftTemperatureQuery, Availability, EstimatedQnhQuery,
    SurfaceWindQuery, SurfaceTemperatureQuery, WeatherResult,
)
from ..weather import WeatherDataset, RecordMap
from .qnh import estimate_qnh

TerrainProvider = Callable[[float, float], float | None]

class PreparedForecast(WeatherDataset):
    def __init__(self, selection, surface, pressure, source_hashes, terrain_provider=None):
        super().__init__(selection, surface, pressure, source_hashes, pressure_levels=LEVELS_HPA)
        self.terrain_provider = terrain_provider

    def query(self, query):
        if query.valid_time.tzinfo is None:
            raise InvalidQueryError("valid_time must be timezone-aware")
        if isinstance(query, AloftTemperatureQuery):
            return self.query_aloft_temperature(query)
        if isinstance(query, SurfaceTemperatureQuery):
            return self.query_surface_temperature(query)
        if isinstance(query, AloftQuery):
            return self.query_aloft(query)
        if isinstance(query, SurfaceWindQuery):
            return self.query_surface_wind(query)
        if isinstance(query, EstimatedQnhQuery):
            return self.query_estimated_qnh(query)
        raise InvalidQueryError(f"unsupported query type: {type(query).__name__}")

    def query_many(self, queries):
        return tuple(self.query(query) for query in queries)

    def query_surface_wind(self, query: SurfaceWindQuery):
        if query.requested_agl_m != 0:
            raise InvalidQueryError("surface wind accepts only requested_agl_m=0")
        u = self._surface_scalar("u", query.latitude, query.longitude, query.valid_time)
        v = self._surface_scalar("v", query.latitude, query.longitude, query.valid_time)
        if u is None or v is None:
            return WeatherResult(Availability.UNAVAILABLE, "surface_wind", reason_code="MISSING_SOURCE_VALUE")
        speed_ms, speed_kt, direction = wind_metrics(u[0], v[0])
        return WeatherResult(
            Availability.AVAILABLE,
            "surface_wind",
            {
                "requested_height_agl_m": 0.0,
                "representative_height_agl_m": 10.0,
                "u_ms": u[0],
                "v_ms": v[0],
                "wind_speed_ms": speed_ms,
                "wind_speed_kt": speed_kt,
                "wind_direction_deg_from": direction,
            },
            provenance=self._provenance("bilinear,time-linear", {"u": u[1], "v": v[1]}),
        )

    def query_estimated_qnh(self, query: EstimatedQnhQuery):
        if self.terrain_provider is None:
            return WeatherResult(
                Availability.UNAVAILABLE,
                "estimated_qnh",
                reason_code="MODEL_TERRAIN_UNAVAILABLE",
                warnings=("ESTIMATED_QNH_NOT_OFFICIAL", "NOT_FOR_OPERATIONAL_USE"),
            )
        terrain = self.terrain_provider(query.latitude, query.longitude)
        fields = [
            self._surface_scalar(name, query.latitude, query.longitude, query.valid_time)
            for name in ("sp", "tmp_surface", "rh")
        ]
        if terrain is None or any(field is None for field in fields):
            return WeatherResult(
                Availability.UNAVAILABLE,
                "estimated_qnh",
                reason_code="QNH_INPUT_UNAVAILABLE",
                warnings=("ESTIMATED_QNH_NOT_OFFICIAL", "NOT_FOR_OPERATIONAL_USE"),
            )
        estimate = estimate_qnh(fields[0][0], fields[1][0], fields[2][0], terrain, query.elevation_msl_m)
        mslp = self._surface_scalar("mslp", query.latitude, query.longitude, query.valid_time)
        warnings = ["ESTIMATED_QNH_NOT_OFFICIAL", "NOT_FOR_OPERATIONAL_USE"]
        if abs(estimate.terrain_difference_m) > 100:
            warnings.append("MODEL_TERRAIN_DIFFERENCE")
        return WeatherResult(
            Availability.AVAILABLE,
            "estimated_qnh",
            {
                "label": "MSM-derived estimated QNH",
                "qnh_pa": estimate.qnh_pa,
                "qnh_hpa": estimate.qnh_pa / 100,
                "station_pressure_pa": estimate.station_pressure_pa,
                "model_terrain_height_m": terrain,
                "requested_elevation_msl_m": query.elevation_msl_m,
                "terrain_difference_m": estimate.terrain_difference_m,
                "diagnostic_mslp_pa": None if mslp is None else mslp[0],
                "method_version": estimate.method_version,
            },
            warnings=tuple(warnings),
            provenance=self._provenance(
                "bilinear,time-linear,hypsometric-isa-v1",
                {
                    "terrain_source": getattr(self.terrain_provider, "source", None),
                    "terrain_source_sha256": getattr(
                        self.terrain_provider, "source_sha256", None
                    ),
                },
            ),
        )
