"""Concrete shared weather dataset: record interpolation and result assembly."""
from __future__ import annotations
from datetime import datetime
from typing import Mapping
import numpy as np
from .interpolation import (
    _grid_bracket, _time_bracket, bilinear, temporal, vertical_at_height, wind_metrics,
)
from .models import AloftQuery, Availability, Provenance, WeatherResult

RecordMap = Mapping[tuple[datetime, int, str], tuple[np.ndarray, np.ndarray, np.ndarray]]

class WeatherDataset:
    trace_pressure_levels = False
    def __init__(self, selection, surface, pressure, source_hashes, *, pressure_levels):
        self.selection = selection
        self.surface = surface
        self.pressure = pressure
        self.source_hashes = dict(source_hashes)
        self.pressure_levels = pressure_levels

    def _provenance(self, method: str, trace=None):
        return Provenance(
            initial_time_utc=self.selection.run_utc,
            source_urls=tuple(item.url for item in self.selection.files),
            source_hashes=self.source_hashes,
            interpolation_method=method,
            trace=trace or {},
        )

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
                    for level in self.pressure_levels:
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
                    if self.trace_pressure_levels:
                        corner_trace[-1]["pressure_bracket_hpa"] = tuple(
                            level for level in self.pressure_levels
                            if (valid, level, "hgt") in self.pressure
                            and (valid, level, variable) in self.pressure
                            and self.pressure[valid, level, "hgt"][0][y, x]
                            in (vertical.lower_height_m, vertical.upper_height_m)
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


    def query_surface_temperature(self, query):
        temperature = self._surface_scalar(
            "tmp_surface", query.latitude, query.longitude, query.valid_time
        )
        if temperature is None:
            return WeatherResult(
                Availability.UNAVAILABLE, "surface_temperature",
                reason_code="MISSING_SOURCE_VALUE",
                provenance=self._provenance("bilinear,time-linear"),
            )
        return WeatherResult(
            Availability.AVAILABLE, "surface_temperature",
            {"temperature_k": temperature[0], "temperature_c": temperature[0] - 273.15,
             "representative_height_agl_m": 2.0},
            provenance=self._provenance("bilinear,time-linear", {"temperature": temperature[1]}),
        )

    def query_aloft_temperature(self, query):
        temperature = self._aloft_scalar("tmp", query)
        if temperature is None:
            return WeatherResult(
                Availability.UNAVAILABLE, "aloft_temperature",
                reason_code="VERTICAL_BRACKET_UNAVAILABLE",
                provenance=self._provenance("vertical-linear,bilinear,time-linear"),
            )
        return WeatherResult(
            Availability.AVAILABLE, "aloft_temperature",
            {"temperature_k": temperature[0], "temperature_c": temperature[0] - 273.15},
            provenance=self._provenance(
                "vertical-linear,bilinear,time-linear", {"temperature": temperature[1]}
            ),
        )
