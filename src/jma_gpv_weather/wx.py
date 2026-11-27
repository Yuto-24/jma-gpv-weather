"""MSM/GSM native humidity and aggregate cloud cover, without cloud-height inference."""
from dataclasses import replace
import math

import numpy as np

from .interpolation import _grid_bracket, bilinear, temporal
from .models import (
    Availability, CloudLayer, ForecastRequirements,
    HumidityProfileQuery, HumidityProfileResult, PRESSURE_RH_LEVELS_HPA,
    RunId, WeatherResult, WeatherVariable,
)

WX_VARIABLES = frozenset({WeatherVariable.PRESSURE_RELATIVE_HUMIDITY, WeatherVariable.CLOUD_COVER})
CLOUD_FIELDS = {CloudLayer.TOTAL: "cloud_total", CloudLayer.LOW: "cloud_low",
                CloudLayer.MIDDLE: "cloud_middle", CloudLayer.HIGH: "cloud_high"}


def requested_fields(variables, pressure_levels):
    """Explicit record requirements for opt-in WX preparation (including mixed requests)."""
    surface, pressure = set(), set()
    if WeatherVariable.SURFACE_WIND in variables:
        surface.update(("u", "v"))
    if WeatherVariable.SURFACE_TEMPERATURE in variables:
        surface.add("tmp_surface")
    if WeatherVariable.ESTIMATED_QNH in variables:
        surface.update(("sp", "mslp", "tmp_surface", "rh"))
    # AloftQuery includes temperature and its altitude coverage checks TMP even
    # when the caller only requested wind. Preserve that legacy dependency.
    for variable, names in ((WeatherVariable.ALOFT_WIND, ("hgt", "u", "v", "tmp")),
                            (WeatherVariable.ALOFT_TEMPERATURE, ("hgt", "tmp"))):
        if variable in variables:
            pressure.update((level, name) for level in pressure_levels for name in names)
    if WeatherVariable.PRESSURE_RELATIVE_HUMIDITY in variables:
        surface.add("sp")
        pressure.update((level, name) for level in PRESSURE_RH_LEVELS_HPA for name in ("hgt", "rh"))
    if WeatherVariable.CLOUD_COVER in variables:
        surface.update(CLOUD_FIELDS.values())
    return surface, pressure


def missing_wx_fields(requirements, needed, surface, pressure):
    """Absent records are preparation failures; present NaNs are source value gaps."""
    surface_names, pressure_fields = requested_fields(requirements.variables & WX_VARIABLES, ())
    return [f"{valid}:{level}:{name}"
            for valid in needed.get("L-pall", ()) for level, name in sorted(pressure_fields)
            if (valid, level, name) not in pressure] + [
                f"{valid}:surface:{name}"
                for valid in needed.get("Lsurf", ()) for name in sorted(surface_names)
                if (valid, 0, name) not in surface]


def coverage_levels(variables, legacy_levels):
    # A mixed request must be supported by every pressure variable it asks for.
    if WeatherVariable.PRESSURE_RELATIVE_HUMIDITY not in variables:
        return legacy_levels
    if variables & {WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE}:
        return tuple(level for level in PRESSURE_RH_LEVELS_HPA if level in legacy_levels)
    return PRESSURE_RH_LEVELS_HPA


def uses_wx_records(surface, pressure, model):
    return (any(key[2] in CLOUD_FIELDS.values() for key in surface)
            or any(key[2] == "rh" or (model == "MSM" and key[1] in (400, 300))
                   for key in pressure))


def _sample(records, name, level, times, query, *, percent=False, positive=False):
    """Sample only the official time endpoints, never bridge absent records/NaNs."""
    values, trace = [], []
    for valid in times:
        record = records.get((valid, level, name))
        if record is None:
            return None, "DATA_NOT_PREPARED", trace
        data, lat, lon = record
        grid = _grid_bracket(lat, lon, query.latitude, query.longitude)
        if grid is None:
            return None, "LOCATION_NOT_PREPARED", trace
        yi, xi, lat_pair, lon_pair = grid
        corners = np.asarray(data)[np.ix_(yi, xi)]
        trace.append({"valid_time": valid.isoformat(), "latitudes": lat_pair, "longitudes": lon_pair})
        if not np.isfinite(corners).all():
            return None, "MISSING_SOURCE_VALUE", trace
        if (percent and ((corners < 0) | (corners > 100)).any()
                or positive and (corners <= 0).any()):
            return None, "INVALID_SOURCE_VALUE", trace
        values.append(bilinear(query.longitude, query.latitude, *lon_pair, *lat_pair, corners))
    value = (values[0] if len(times) == 1 else
             temporal(times[0], times[-1], values[0], values[-1], query.valid_time))
    return value, None, trace


def query_wx(forecast, query, *, model):
    """Shared query implementation; callers retain their model's error boundary."""
    from .coverage import CoveragePoint
    from .msm import spec as msm
    from .gsm import spec as gsm
    spec = msm if model == "MSM" else gsm
    if query.valid_time.tzinfo is None:
        raise ValueError("valid_time must be timezone-aware")
    if not all(math.isfinite(v) for v in (query.latitude, query.longitude)):
        raise ValueError("query coordinates must be finite")
    profile = isinstance(query, HumidityProfileQuery)
    if profile:
        if (not query.pressure_levels_hpa or len(query.pressure_levels_hpa) > 16
                or any(not isinstance(level, int) or isinstance(level, bool) or level <= 0
                       for level in query.pressure_levels_hpa)
                or len(set(query.pressure_levels_hpa)) != len(query.pressure_levels_hpa)):
            raise ValueError("pressure levels must be distinct positive integers (at most 16)")
    else:
        layer = CloudLayer(query.layer)
    variable = WeatherVariable.PRESSURE_RELATIVE_HUMIDITY if profile else WeatherVariable.CLOUD_COVER
    requirements = ForecastRequirements((query.valid_time,), frozenset({variable}))
    coverage = spec.check_coverage(requirements, run=RunId(forecast.selection.run_utc),
                                  points=(CoveragePoint(query.latitude, query.longitude),))
    reason = ("OUTSIDE_DOMAIN" if "OUTSIDE_DOMAIN" in coverage.reason_codes else
              "TIME_OUTSIDE_COVERAGE" if coverage.outside_spec else None)
    needed = (msm.required_valid_times(requirements) if model == "MSM" else
              gsm.required_valid_times(requirements, forecast.selection.run_utc))
    trace = {"model": model, "wx_contract_version": 1,
             "units": {"rh": "%", "cloud_cover": "%", "hgt": "gpm", "sp": "Pa"},
             "vertical_interpolation": "none", "pressure_levels_hpa": PRESSURE_RH_LEVELS_HPA}
    method = "bilinear,time-linear,native-pressure-levels" if profile else "bilinear,time-linear,layer-aggregate"
    def provenance(extra):
        # Override GSM's legacy wind-level annotation only for WX results.
        result = forecast._provenance(method)
        files = tuple(f for f in forecast.selection.files if needed and any(
            f.first_hour <= (valid - f.run_utc).total_seconds() / 3600 <= f.last_hour
            for valid in needed.get(f.kind, ())))
        return replace(result, source_urls=tuple(f.url for f in files),
                       source_hashes={f.url: forecast.source_hashes[f.url] for f in files},
                       trace={**trace, **extra})

    if not profile:
        value, sample_reason, samples = ((None, reason, []) if reason else
            _sample(forecast.surface, CLOUD_FIELDS[layer], 0, needed["Lsurf"], query, percent=True))
        return WeatherResult(
            Availability.AVAILABLE if sample_reason is None else Availability.UNAVAILABLE,
            "cloud_cover", {"cloud_cover_percent": value, "layer": layer.value}, sample_reason,
            ("NOT_FOR_OPERATIONAL_USE", "LAYER_AGGREGATE_HAS_NO_CLOUD_BASE_OR_TOP"),
            provenance({"layer": layer.value, "samples": samples}),
        )

    sp, sp_reason, sp_trace = ((None, reason, []) if reason else
        _sample(forecast.surface, "sp", 0, needed["Lsurf"], query, positive=True))
    levels = []
    for level in query.pressure_levels_hpa:
        level_reason = "PRESSURE_LEVEL_UNSUPPORTED" if level not in PRESSURE_RH_LEVELS_HPA else reason
        hgt, rh, hgt_trace, rh_trace = None, None, [], []
        if level_reason is None:
            hgt, hgt_reason, hgt_trace = _sample(forecast.pressure, "hgt", level, needed["L-pall"], query)
            rh, rh_reason, rh_trace = _sample(forecast.pressure, "rh", level, needed["L-pall"], query, percent=True)
            level_reason = sp_reason or hgt_reason or rh_reason
            if sp is not None and level * 100 > sp:
                level_reason = "BELOW_MODEL_SURFACE"
                rh = None
        levels.append(WeatherResult(
            Availability.AVAILABLE if level_reason is None else Availability.UNAVAILABLE,
            "pressure_relative_humidity",
            {"pressure_hpa": level, "relative_humidity_percent": rh, "geopotential_height_gpm": hgt},
            level_reason, ("NOT_FOR_OPERATIONAL_USE", "HUMIDITY_IS_NOT_CLOUD_PROBABILITY"),
            provenance({"pressure_hpa": level, "humidity": rh_trace, "height": hgt_trace,
                        "surface_pressure": sp_trace}),
        ))
    available = sum(item.availability == Availability.AVAILABLE for item in levels)
    return HumidityProfileResult(
        Availability.AVAILABLE if available else Availability.UNAVAILABLE, tuple(levels), sp,
        None if available == len(levels) else reason or ("PROFILE_PARTIALLY_UNAVAILABLE" if available else "PROFILE_UNAVAILABLE"),
        provenance=provenance({"surface_pressure": sp_trace}),
    )
