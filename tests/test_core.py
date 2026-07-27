from datetime import date, datetime, timezone

import numpy as np
import pytest

from msm_wind.core import (Bounds, RISH_BASE, RemoteFile, _subset_message,
    expected_valid_times, interpolate_at_height, parse_listing,
    select_latest_complete_run, target_window, wind_metrics)

UTC = timezone.utc


def remote(run, kind, first, last):
    run_utc = datetime.strptime(run, "%Y%m%d%H").replace(tzinfo=UTC)
    name = f"Z__C_RJTD_{run_utc:%Y%m%d%H%M%S}_MSM_GPV_Rjp_{kind}_FH{first:02d}-{last:02d}_grib2.bin"
    return RemoteFile(name, f"{RISH_BASE}/{name}", run_utc, kind, first, last)


def test_jst_window():
    start, end = target_window(date(2026, 7, 28))
    assert start == datetime(2026, 7, 27, 15, tzinfo=UTC)
    assert end == datetime(2026, 7, 28, 15, tzinfo=UTC)
    assert len(expected_valid_times(date(2026, 7, 28), 1)) == 24
    assert len(expected_valid_times(date(2026, 7, 28), 3)) == 8


def test_latest_complete_run_skips_incomplete_newer_run():
    files = [remote("2026072712", "Lsurf", 0, 15), remote("2026072712", "L-pall", 0, 15),
             remote("2026072709", "Lsurf", 0, 15), remote("2026072709", "Lsurf", 16, 33),
             remote("2026072709", "L-pall", 0, 15), remote("2026072709", "L-pall", 18, 33)]
    html = "\n".join(f'<a href="{f.name}">{f.name}</a>' for f in files)
    selection = select_latest_complete_run(parse_listing(html, RISH_BASE+"/2026/07/27"), date(2026, 7, 28))
    assert selection.run_utc == datetime(2026, 7, 27, 9, tzinfo=UTC)
    assert len(selection.files) == 4


def test_interpolate_uv_and_require_bracket():
    assert interpolate_at_height([4000, 5000], [10, 20], [-5, 5], 4500) == (15, 0, 4000, 5000)
    assert interpolate_at_height([1000, 2000], [1, 2], [3, 4], 4572) is None


def test_wind_direction_is_meteorological_from():
    speed, knots, direction = wind_metrics(np.array([10.0, 0.0]), np.array([0.0, -10.0]))
    assert speed.tolist() == [10, 10]
    assert knots[0] == pytest.approx(19.438444924406)
    assert direction.tolist() == pytest.approx([270, 0])


def test_nominal_edges_included_but_halo_excluded():
    class Message:
        def data(self, **kwargs):
            lat = np.array([[29.69999999999]*3, [35.2]*3, [35.200001]*3])
            lon = np.array([[128.49999999999, 134.75, 134.8125]]*3)
            return np.arange(9).reshape(3, 3), lat, lon
    values, lat, lon = _subset_message(Message(), Bounds())
    assert values.shape == (2, 2)
    assert (lat.min(), lat.max(), lon.min(), lon.max()) == (29.7, 35.2, 128.5, 134.75)
