"""Explicit opt-in acceptance: network failures fail, never silently skip."""
from dataclasses import asdict
from datetime import datetime, timezone
import json
import os
from pathlib import Path

import pytest

from jma_gpv_weather import (
    AloftQuery, Availability, Bounds, EstimatedQnhQuery, ForecastRequirements,
    MsmClient, RunId, SurfaceWindQuery, WeatherVariable,
)
from jma_gpv_weather import grib
from jma_gpv_weather.cache import verify_cache

pytestmark = [
    pytest.mark.real_data,
    pytest.mark.skipif(os.environ.get('JMA_GPV_REAL_MSM') != '1',
                       reason='set JMA_GPV_REAL_MSM=1 to download fixed RISH MSM data'),
]


def test_fixed_rish_run_queries_and_cache(tmp_path, monkeypatch):
    cache_dir = Path(os.environ.get('JMA_GPV_REAL_CACHE', str(tmp_path)))
    run = RunId(datetime(2026, 7, 27, 12, tzinfo=timezone.utc))
    times = (datetime(2026, 7, 27, 12, tzinfo=timezone.utc),
             datetime(2026, 7, 27, 13, 30, tzinfo=timezone.utc))
    req = ForecastRequirements(times, frozenset(WeatherVariable))
    client = MsmClient(cache_dir, Bounds(31.8, 31.95, 131.35, 131.55))
    runs = client.discover_runs(req)
    status = client.resolve_run(req, run, available_runs=runs)
    assert status.selected_run == run and status.selected_run_covers_request
    prepared = client.prepare_run(run, req, available_runs=runs)
    results = []
    for valid in times:
        aloft = prepared.query(AloftQuery(31.877, 131.449, valid, 4572))
        surface = prepared.query(SurfaceWindQuery(31.877, 131.449, valid))
        assert aloft.availability == surface.availability == Availability.AVAILABLE
        assert 0 <= aloft.values['wind_speed_ms'] < 150
        assert 180 < aloft.values['temperature_k'] < 330
        assert 0 <= surface.values['wind_speed_ms'] < 100
        # main exposes surface temperature through the normalized fields/QNH input.
        temperature = prepared._surface_scalar('tmp_surface', 31.877, 131.449, valid)
        assert temperature is not None and 230 < temperature[0] < 330
        assert aloft.provenance.initial_time_utc == run.initial_time_utc
        assert aloft.provenance.interpolation_method == 'vertical-linear,bilinear,time-linear'
        assert aloft.provenance.trace['u']
        assert len(aloft.provenance.source_urls) == 2
        assert set(aloft.provenance.source_hashes) == set(aloft.provenance.source_urls)
        assert all(len(value) == 64 for value in aloft.provenance.source_hashes.values())
        qnh = prepared.query(EstimatedQnhQuery(31.877, 131.449, valid, 6))
        assert qnh.reason_code == 'MODEL_TERRAIN_UNAVAILABLE'
        results.append({'aloft': asdict(aloft), 'surface': asdict(surface),
                        'surface_temperature': temperature, 'qnh': asdict(qnh)})
    verification = verify_cache(cache_dir)
    assert verification['valid'] and len(verification['files']) == 2

    def no_decode(*args, **kwargs):
        pytest.fail('warm normalized cache must be reused')

    monkeypatch.setattr(grib, 'read_grib_records', no_decode)
    warm = client.prepare_run(run, req, available_runs=runs)
    for valid in times:
        query = AloftQuery(31.877, 131.449, valid, 4572)
        assert asdict(warm.query(query)) == asdict(prepared.query(query))
    print(json.dumps({'run': str(run), 'results': results, 'cache': verification},
                     default=str, sort_keys=True))
