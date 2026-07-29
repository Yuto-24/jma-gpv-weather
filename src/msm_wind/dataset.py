from __future__ import annotations

from bisect import bisect_left
from datetime import datetime
from typing import Callable, Mapping, Sequence

import numpy as np

from .core import LEVELS_HPA, RunSelection
from .errors import InvalidQueryError
from .interpolation import bilinear, temporal, vertical_at_height, wind_metrics
from .models import (
    AloftQuery,
    Availability,
    EstimatedQnhQuery,
    Provenance,
    SurfaceWindQuery,
    WeatherResult,
)
from .qnh import estimate_qnh

RecordMap = Mapping[tuple[datetime, int, str], tuple[np.ndarray, np.ndarray, np.ndarray]]
TerrainProvider = Callable[[float, float], float | None]


def _time_bracket(times: Sequence[datetime], target: datetime):
    ordered = sorted(set(times))
    index = bisect_left(ordered, target)
    if index < len(ordered) and ordered[index] == target:
        return ordered[index], ordered[index]
    if index == 0 or index == len(ordered):
        return None
    return ordered[index - 1], ordered[index]


def _grid_bracket(lat: np.ndarray, lon: np.ndarray, latitude: float, longitude: float):
    lat_axis = np.asarray(lat[:, 0], dtype=float)
    lon_axis = np.asarray(lon[0, :], dtype=float)
    if lat_axis[0] > lat_axis[-1]:
        lat_axis = lat_axis[::-1]
        reverse_lat = True
    else:
        reverse_lat = False
    if lon_axis[0] > lon_axis[-1]:
        lon_axis = lon_axis[::-1]
        reverse_lon = True
    else:
        reverse_lon = False

    def bracket(axis, value):
        position = bisect_left(axis.tolist(), value)
        if position < len(axis) and axis[position] == value:
            return position, position
        if position == 0 or position == len(axis):
            return None
        return position - 1, position

    lat_indices = bracket(lat_axis, latitude)
    lon_indices = bracket(lon_axis, longitude)
    if lat_indices is None or lon_indices is None:
        return None

    def original(index, length, reverse):
        return length - 1 - index if reverse else index

    yi = tuple(original(i, len(lat_axis), reverse_lat) for i in lat_indices)
    xi = tuple(original(i, len(lon_axis), reverse_lon) for i in lon_indices)
    return yi, xi, (lat_axis[lat_indices[0]], lat_axis[lat_indices[1]]), (
        lon_axis[lon_indices[0]],
        lon_axis[lon_indices[1]],
    )


class PreparedForecast:
    def __init__(
        self,
        selection: RunSelection,
        surface: RecordMap,
        pressure: RecordMap,
        source_hashes: Mapping[str, str],
        terrain_provider: TerrainProvider | None = None,
    ):
        self.selection = selection
        self.surface = surface
        self.pressure = pressure
        self.source_hashes = dict(source_hashes)
        self.terrain_provider = terrain_provider

    def _provenance(self, method: str, trace=None):
        return Provenance(
            initial_time_utc=self.selection.run_utc,
            source_urls=tuple(item.url for item in self.selection.files),
            source_hashes=self.source_hashes,
            interpolation_method=method,
            trace=trace or {},
        )

    def query(self, query):
        if query.valid_time.tzinfo is None:
            raise InvalidQueryError("valid_time must be timezone-aware")
        if isinstance(query, AloftQuery):
            return self.query_aloft(query)
        if isinstance(query, SurfaceWindQuery):
            return self.query_surface_wind(query)
        if isinstance(query, EstimatedQnhQuery):
            return self.query_estimated_qnh(query)
        raise InvalidQueryError(f"unsupported query type: {type(query).__name__}")

    def query_many(self, queries):
        return tuple(self.query(query) for query in queries)

    def _surface_scalar(self, variable: str, latitude: float, longitude: float, target: datetime):
        times = [key[0] for key in self.surface if key[2] == variable]
        bracket = _time_bracket(times, target)
        if bracket is None:
            return None
        values_by_time = []
        trace = []
        interpolation_times = bracket[:1] if bracket[0] == bracket[1] else bracket
        for valid in interpolation_times:
            candidates = [key for key in self.surface if key[0] == valid and key[2] == variable]
            if not candidates:
                return None
            values, lat, lon = self.surface[candidates[0]]
            grid = _grid_bracket(lat, lon, latitude, longitude)
            if grid is None:
                return None
            yi, xi, lat_pair, lon_pair = grid
            corners = np.array(
                [[values[yi[0], xi[0]], values[yi[0], xi[1]]],
                 [values[yi[1], xi[0]], values[yi[1], xi[1]]]]
            )
            value = bilinear(longitude, latitude, *lon_pair, *lat_pair, corners)
            values_by_time.append(value)
            trace.append({"valid_time": valid.isoformat(), "latitudes": lat_pair, "longitudes": lon_pair})
        value = (
            values_by_time[0]
            if bracket[0] == bracket[1]
            else temporal(bracket[0], bracket[1], values_by_time[0], values_by_time[1], target)
        )
        return value, trace

    def _aloft_scalar(self, variable: str, query: AloftQuery):
        times = [key[0] for key in self.pressure if key[2] == variable]
        bracket = _time_bracket(times, query.valid_time)
        if bracket is None:
            return None
        time_values = []
        full_trace = []
        interpolation_times = bracket[:1] if bracket[0] == bracket[1] else bracket
        for valid in interpolation_times:
            sample_key = next(
                (key for key in self.pressure if key[0] == valid and key[2] == variable), None
            )
            if sample_key is None:
                return None
            _, lat, lon = self.pressure[sample_key]
            grid = _grid_bracket(lat, lon, query.latitude, query.longitude)
            if grid is None:
                return None
            yi, xi, lat_pair, lon_pair = grid
            corner_values = np.empty((2, 2), dtype=float)
            corner_trace = []
            for row, y in enumerate(yi):
                for column, x in enumerate(xi):
                    heights, values = [], []
                    for level in LEVELS_HPA:
                        hgt_key = (valid, level, "hgt")
                        value_key = (valid, level, variable)
                        if hgt_key in self.pressure and value_key in self.pressure:
                            heights.append(self.pressure[hgt_key][0][y, x])
                            values.append(self.pressure[value_key][0][y, x])
                    vertical = vertical_at_height(heights, values, query.altitude_msl_m)
                    if vertical is None:
                        return None
                    corner_values[row, column] = vertical.value
                    corner_trace.append(
                        {
                            "grid": [float(lat[y, x]), float(lon[y, x])],
                            "lower_height_m": vertical.lower_height_m,
                            "upper_height_m": vertical.upper_height_m,
                        }
                    )
            time_values.append(
                bilinear(
                    query.longitude,
                    query.latitude,
                    *lon_pair,
                    *lat_pair,
                    corner_values,
                )
            )
            full_trace.append({"valid_time": valid.isoformat(), "corners": corner_trace})
        value = (
            time_values[0]
            if bracket[0] == bracket[1]
            else temporal(bracket[0], bracket[1], time_values[0], time_values[1], query.valid_time)
        )
        return value, full_trace

    def query_aloft(self, query: AloftQuery):
        u = self._aloft_scalar("u", query)
        v = self._aloft_scalar("v", query)
        temperature = self._aloft_scalar("tmp", query)
        if u is None or v is None:
            return WeatherResult(
                Availability.UNAVAILABLE,
                "aloft",
                reason_code="VERTICAL_BRACKET_UNAVAILABLE",
                provenance=self._provenance("vertical-linear,bilinear,time-linear"),
            )
        speed_ms, speed_kt, direction = wind_metrics(u[0], v[0])
        values = {
            "u_ms": u[0],
            "v_ms": v[0],
            "wind_speed_ms": speed_ms,
            "wind_speed_kt": speed_kt,
            "wind_direction_deg_from": direction,
            "temperature_k": None if temperature is None else temperature[0],
            "temperature_c": None if temperature is None else temperature[0] - 273.15,
        }
        warnings = ["NOT_FOR_OPERATIONAL_USE"]
        if direction is None:
            warnings.append("CALM_WIND_DIRECTION_UNDEFINED")
        return WeatherResult(
            Availability.AVAILABLE,
            "aloft",
            values,
            warnings=tuple(warnings),
            provenance=self._provenance(
                "vertical-linear,bilinear,time-linear",
                {"u": u[1], "v": v[1], "temperature": None if temperature is None else temperature[1]},
            ),
        )

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
                self._terrain_provenance(),
            ),
        )

    def _terrain_provenance(self):
        provenance = getattr(self.terrain_provider, "provenance", None)
        if isinstance(provenance, Mapping):
            return dict(provenance)
        return {
            "terrain_source": getattr(self.terrain_provider, "source", None),
            "terrain_source_sha256": getattr(
                self.terrain_provider, "source_sha256", None
            ),
        }
