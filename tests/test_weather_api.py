from datetime import datetime, timezone

import pytest

from msm_wind.client import required_valid_times, select_compatible_runs
from msm_wind.core import RISH_BASE, RemoteFile, RunSelection
from msm_wind.interpolation import bilinear, temporal, vertical_at_height
from msm_wind.models import ForecastRequirements, RunId, WeatherVariable
from msm_wind.qnh import estimate_qnh
from msm_wind import MsmClient

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
