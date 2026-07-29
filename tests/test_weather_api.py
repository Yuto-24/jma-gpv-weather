from datetime import datetime, timezone

import pytest
import numpy as np

from msm_wind.cache import cached_listing
from msm_wind.client import interpolation_bounds, required_valid_times, select_compatible_runs
from msm_wind.core import RISH_BASE, RemoteFile, RunSelection
from msm_wind.interpolation import bilinear, temporal, vertical_at_height
from msm_wind.models import ForecastRequirements, RunId, WeatherVariable
from msm_wind.qnh import estimate_qnh
from msm_wind import MsmClient
from msm_wind.dataset import PreparedForecast
from msm_wind.models import AloftQuery, Availability, EstimatedQnhQuery, SurfaceWindQuery
from msm_wind.normalized import load_records, save_records
from msm_wind.terrain import GridTerrainProvider

UTC = timezone.utc


def remote(run_hour: int, kind: str, first: int, last: int):
    run = datetime(2026, 7, 27, run_hour, tzinfo=UTC)
    name = f"run-{run_hour}-{kind}-{first}-{last}"
    return RemoteFile(name, f"{RISH_BASE}/{name}", run, kind, first, last)


def requirements(*times):
    return ForecastRequirements(
        tuple(times),
        frozenset(
            {
                WeatherVariable.ALOFT_WIND,
                WeatherVariable.ALOFT_TEMPERATURE,
                WeatherVariable.SURFACE_WIND,
                WeatherVariable.ESTIMATED_QNH,
            }
        ),
    )


def test_required_times_include_interpolation_brackets():
    req = requirements(datetime(2026, 7, 28, 1, 30, tzinfo=UTC))
    needed = required_valid_times(req)
    assert needed["Lsurf"] == (
        datetime(2026, 7, 28, 1, tzinfo=UTC),
        datetime(2026, 7, 28, 2, tzinfo=UTC),
    )
    assert needed["L-pall"] == (
        datetime(2026, 7, 28, 0, tzinfo=UTC),
        datetime(2026, 7, 28, 3, tzinfo=UTC),
    )


def test_selected_run_does_not_change_when_update_exists():
    req = requirements(datetime(2026, 7, 28, 0, tzinfo=UTC))
    older = RunSelection(
        datetime(2026, 7, 27, 9, tzinfo=UTC),
        (remote(9, "Lsurf", 0, 39), remote(9, "L-pall", 0, 39)),
    )
    newer = RunSelection(
        datetime(2026, 7, 27, 12, tzinfo=UTC),
        (remote(12, "Lsurf", 0, 39), remote(12, "L-pall", 0, 39)),
    )
    status = MsmClient().resolve_run(
        req, RunId(older.run_utc), available_runs=(newer, older)
    )
    assert status.selected_run == RunId(older.run_utc)
    assert status.latest_compatible_run == RunId(newer.run_utc)
    assert status.update_available


def test_4d_interpolation_primitives_are_linear():
    vertical = vertical_at_height([1000, 2000], [10, 30], 1500)
    assert vertical is not None and vertical.value == 20
    assert bilinear(0.5, 0.5, 0, 1, 0, 1, [[0, 10], [20, 30]]) == 15
    assert temporal(
        datetime(2026, 1, 1, tzinfo=UTC),
        datetime(2026, 1, 1, 2, tzinfo=UTC),
        10,
        30,
        datetime(2026, 1, 1, 1, tzinfo=UTC),
    ) == 20


def test_qnh_round_trip_for_isa_pressure():
    elevation = 149.0
    p0 = 101325.0
    factor = (1 - 0.0065 * elevation / 288.15) ** (9.80665 / (287.05287 * 0.0065))
    estimate = estimate_qnh(p0 * factor, 288.15 - 0.0065 * elevation, 0, elevation, elevation)
    assert estimate.qnh_pa == pytest.approx(p0, rel=2e-6)


def test_no_vertical_extrapolation():
    assert vertical_at_height([1000, 2000], [1, 2], 500) is None


def synthetic_prepared():
    t0 = datetime(2026, 7, 28, 0, tzinfo=UTC)
    t1 = datetime(2026, 7, 28, 3, tzinfo=UTC)
    lat = np.array([[30.0, 30.0], [31.0, 31.0]])
    lon = np.array([[130.0, 131.0], [130.0, 131.0]])
    selection = RunSelection(
        datetime(2026, 7, 27, 12, tzinfo=UTC),
        (remote(12, "Lsurf", 0, 39), remote(12, "L-pall", 0, 39)),
    )
    pressure = {}
    for valid, offset in ((t0, 0.0), (t1, 3.0)):
        for level, height in ((1000, 1000.0), (975, 2000.0)):
            pressure[valid, level, "hgt"] = (np.full((2, 2), height), lat, lon)
            pressure[valid, level, "u"] = (
                np.full((2, 2), 10 + offset + (height - 1000) / 100),
                lat,
                lon,
            )
            pressure[valid, level, "v"] = (np.zeros((2, 2)), lat, lon)
            pressure[valid, level, "tmp"] = (
                np.full((2, 2), 280 + offset + (height - 1000) / 1000),
                lat,
                lon,
            )
    surface = {}
    for valid, offset in ((t0, 0.0), (t1, 3.0)):
        for level, name, value in (
            (10, "u", 5 + offset),
            (10, "v", 0),
            (0, "sp", 100000),
            (2, "tmp_surface", 288.15),
            (2, "rh", 50),
            (0, "mslp", 101300),
        ):
            surface[valid, level, name] = (np.full((2, 2), value), lat, lon)
    return PreparedForecast(selection, surface, pressure, {}, lambda lat, lon: 100.0)


def test_prepared_forecast_queries_aloft_surface_and_qnh():
    prepared = synthetic_prepared()
    at = datetime(2026, 7, 28, 1, 30, tzinfo=UTC)
    aloft = prepared.query(AloftQuery(30.5, 130.5, at, 1500))
    assert aloft.availability == Availability.AVAILABLE
    assert aloft.values["u_ms"] == pytest.approx(16.5)
    assert aloft.values["temperature_k"] == pytest.approx(282.0)
    surface = prepared.query(SurfaceWindQuery(30.5, 130.5, at))
    assert surface.values["requested_height_agl_m"] == 0
    assert surface.values["representative_height_agl_m"] == 10
    qnh = prepared.query(EstimatedQnhQuery(30.5, 130.5, at, 149))
    assert qnh.availability == Availability.AVAILABLE
    assert qnh.values["label"] == "MSM-derived estimated QNH"


def test_prepared_forecast_never_connects_surface_to_aloft():
    result = synthetic_prepared().query(
        AloftQuery(30.5, 130.5, datetime(2026, 7, 28, tzinfo=UTC), 500)
    )
    assert result.availability == Availability.UNAVAILABLE
    assert result.reason_code == "VERTICAL_BRACKET_UNAVAILABLE"


def test_normalized_cache_round_trip(tmp_path):
    prepared = synthetic_prepared()
    path = tmp_path / "weather.nc"
    save_records(path, prepared.surface, prepared.pressure, {"initial_time_utc": "test"})
    surface, pressure = load_records(path)
    restored = PreparedForecast(
        prepared.selection, surface, pressure, {}, lambda lat, lon: 100
    )
    result = restored.query(
        AloftQuery(
            30.5,
            130.5,
            datetime(2026, 7, 28, 1, 30, tzinfo=UTC),
            1500,
        )
    )
    assert result.values["u_ms"] == pytest.approx(16.5)


def test_terrain_static_cache_round_trip(tmp_path):
    lat = np.array([[30.0, 30.0], [31.0, 31.0]])
    lon = np.array([[130.0, 131.0], [130.0, 131.0]])
    provider = GridTerrainProvider(np.array([[0.0, 100.0], [200.0, 300.0]]), lat, lon)
    restored = GridTerrainProvider.load(provider.save(tmp_path / "terrain.npz"))
    assert restored(30.5, 130.5) == pytest.approx(150)


def test_interpolation_bounds_add_grid_halo():
    expanded = interpolation_bounds(MsmClient().bounds)
    assert expanded.lat_min < 29.7
    assert expanded.lat_max > 35.2
    assert expanded.lon_min < 128.5
    assert expanded.lon_max > 134.8


def test_directory_listing_is_cached(tmp_path):
    calls = []

    def reader(url):
        calls.append(url)
        return "listing"

    assert cached_listing("http://example.test/day", tmp_path, reader) == "listing"
    assert cached_listing("http://example.test/day", tmp_path, reader) == "listing"
    assert calls == ["http://example.test/day"]
