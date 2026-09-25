"""Concrete shared weather dataset: record interpolation and result assembly."""
from __future__ import annotations
from datetime import datetime
from typing import Mapping
import numpy as np
from .interpolation import (
    _grid_bracket, _time_bracket, bilinear, temporal, vertical_at_height, wind_metrics,
)
from .models import AloftQuery, AloftTemperatureQuery, Availability, Provenance, WeatherResult

RecordMap = Mapping[tuple[datetime, int, str], tuple[np.ndarray, np.ndarray, np.ndarray]]

class WeatherDataset:
    trace_pressure_levels = False
    surface_temperature_level = None
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

    @staticmethod
    def _validate_altitude_query(query):
        if not isinstance(query, AloftQuery):
            raise ValueError("altitude coverage requires an aloft query")
        if query.valid_time.tzinfo is None:
            raise ValueError("valid_time must be timezone-aware")
        if not np.isfinite([query.latitude, query.longitude, query.altitude_msl_m]).all():
            raise ValueError("query coordinates and altitude must be finite")

    def _check_altitude_coverage(self, query, valid_times):
        """Classify real HGT only after validating every required source column.

        Model callers supply official pressure time endpoints. Query arithmetic,
        filtering, legacy unavailable reasons and provenance remain unchanged.
        """
        names = ("tmp",) if isinstance(query, AloftTemperatureQuery) else ("u", "v", "tmp")
        trace = {"pressure_levels_hpa": self.pressure_levels, "variables": names, "columns": []}

        def result(reason=None, detail=None):
            return WeatherResult(
                Availability.AVAILABLE if reason is None else Availability.UNAVAILABLE,
                "altitude_coverage", {"altitude_msl_m": query.altitude_msl_m},
                reason_code=reason,
                provenance=self._provenance(
                    "pressure-level-hgt-range", {**trace, **({"detail": detail} if detail else {})}
                ),
            )

        def unavailable(detail):
            return result("SOURCE_VALUE_UNAVAILABLE", detail)

        if not valid_times or not self.pressure_levels:
            return unavailable("TIME_OR_LEVELS_NOT_PREPARED")
        expected_bracket = (valid_times[0], valid_times[-1])
        for name in names:
            actual = _time_bracket([k[0] for k in self.pressure if k[2] == name], query.valid_time)
            if actual != expected_bracket:
                return unavailable("PRESSURE_TIME_ENDPOINT_UNAVAILABLE")

        outside = False
        for valid in valid_times:
            sample = next((r for k, r in self.pressure.items() if k[0] == valid and k[2] == names[0]), None)
            if sample is None:
                return unavailable("PRESSURE_TIME_ENDPOINT_UNAVAILABLE")
            _, lat, lon = sample
            lat, lon = np.asarray(lat), np.asarray(lon)
            if (lat.ndim != 2 or lon.shape != lat.shape or not lat.size
                    or not np.isfinite(lat).all() or not np.isfinite(lon).all()
                    or not np.array_equal(lat, np.broadcast_to(lat[:, :1], lat.shape))
                    or not np.array_equal(lon, np.broadcast_to(lon[:1, :], lon.shape))):
                return unavailable("INVALID_PRESSURE_GRID")
            for axis in (lat[:, 0], lon[0, :]):
                delta = np.diff(axis)
                if not ((delta > 0).all() or (delta < 0).all()):
                    return unavailable("INVALID_PRESSURE_GRID")
            grid = _grid_bracket(lat, lon, query.latitude, query.longitude)
            if grid is None:
                return unavailable("LOCATION_NOT_PREPARED")
            yi, xi = grid[:2]
            indices = np.ix_(sorted(set(yi)), sorted(set(xi)))
            heights = []
            for level in self.pressure_levels:
                for name in ("hgt", *names):
                    record = self.pressure.get((valid, level, name))
                    if record is None:
                        return unavailable(f"MISSING_PRESSURE_FIELD:{valid.isoformat()}:{level}:{name}")
                    values, field_lat, field_lon = record
                    # Do not reinterpret mismatched grids as a smaller HGT range.
                    if (np.shape(values) != lat.shape or not np.array_equal(field_lat, lat)
                            or not np.array_equal(field_lon, lon)):
                        return unavailable("INCONSISTENT_PRESSURE_GRID")
                    corners = np.asarray(np.ma.filled(values, np.nan))[indices]
                    if not np.isfinite(corners).all():
                        return unavailable(f"NONFINITE_PRESSURE_FIELD:{valid.isoformat()}:{level}:{name}")
                    if name == "hgt":
                        heights.append(corners)
            heights = np.stack(heights)
            # Pressure levels are descending; valid HGT columns strictly increase.
            if not (np.diff(heights, axis=0) > 0).all():
                return unavailable("INVALID_HGT_PROFILE")
            outside |= bool(((query.altitude_msl_m < heights[0])
                             | (query.altitude_msl_m > heights[-1])).any())
            for row, y in enumerate(sorted(set(yi))):
                for column, x in enumerate(sorted(set(xi))):
                    trace["columns"].append({
                        "valid_time": valid.isoformat(), "grid": [float(lat[y, x]), float(lon[y, x])],
                        "lowest_height_m": float(heights[0, row, column]),
                        "highest_height_m": float(heights[-1, row, column]),
                    })
        # An early out-of-range column must not hide a later data failure.
        return result("ALTITUDE_OUTSIDE_HGT_RANGE" if outside else None)

    def _surface_scalar(self, variable: str, latitude: float, longitude: float, target: datetime):
        keys = [key for key in self.surface if key[2] == variable
                and (variable != "tmp_surface" or self.surface_temperature_level is None
                     or key[1] == self.surface_temperature_level)]
        times = [key[0] for key in keys]
        bracket = _time_bracket(times, target)
        if bracket is None:
            return None
        values_by_time = []
        trace = []
        interpolation_times = bracket[:1] if bracket[0] == bracket[1] else bracket
        for valid in interpolation_times:
            candidates = [key for key in keys if key[0] == valid]
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
