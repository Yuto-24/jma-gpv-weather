"""Opt-in real RISH acceptance. No synthetic records and no network-failure skips."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import numpy as np
import pytest

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Availability, Bounds, ForecastRequirements,
    GsmClient, RunId, SurfaceTemperatureQuery,
)
from jma_gpv_weather import grib
from jma_gpv_weather.cache import sha256_file, verify_cache
from jma_gpv_weather.gsm.spec import SUPPORTED_VARIABLES

pytestmark = [pytest.mark.real_data, pytest.mark.skipif(
    os.environ.get('JMA_GPV_REAL_GSM') != '1',
    reason='set JMA_GPV_REAL_GSM=1 to download fixed RISH GSM Japan data',
)]


def test_fixed_gsm_run_queries_hashes_and_warm_cache(tmp_path, monkeypatch):
    import pygrib
    initial = datetime(2026, 9, 12, tzinfo=timezone.utc)
    run = RunId(initial)
    times = tuple(initial + timedelta(hours=h) for h in (0, 1.5, 131.5, 133.5, 264))
    requirements = ForecastRequirements(times, SUPPORTED_VARIABLES)
    root = Path(os.environ.get('JMA_GPV_REAL_CACHE', str(tmp_path)))
    client = GsmClient(root, Bounds(31.8, 31.95, 131.35, 131.55))
    runs = client.discover_runs(requirements, as_of=initial + timedelta(hours=6))
    assert len(runs) == 1 and runs[0].run_utc == initial
    status = client.resolve_run(requirements, run, runs)
    assert status.selected_run == run and status.selected_run_covers_request
    forecast = client.prepare_run(run, requirements, runs)
    results = []
    for valid in times:
        q = AloftQuery(31.877, 131.449, valid, 4572)
        aloft = forecast.query(q)
        temperature = forecast.query(AloftTemperatureQuery(q.latitude,q.longitude,valid,4572))
        surface = forecast.query(SurfaceTemperatureQuery(q.latitude,q.longitude,valid))
        assert aloft.availability == temperature.availability == surface.availability == Availability.AVAILABLE
        assert 0 <= aloft.values['wind_speed_ms'] < 150
        assert 180 < temperature.values['temperature_k'] < 330
        assert 230 < surface.values['temperature_k'] < 330
        assert temperature.values['temperature_k'] == aloft.values['temperature_k']
        assert aloft.provenance.initial_time_utc == initial
        assert aloft.provenance.trace['model'] == 'GSM_JAPAN'
        assert aloft.provenance.interpolation_method == 'vertical-linear,bilinear,time-linear'
        assert all(c['pressure_bracket_hpa'] for t in aloft.provenance.trace['u'] for c in t['corners'])
        results.append({'time': valid.isoformat(), 'aloft': dict(aloft.values), 'surface': dict(surface.values)})
    # Verify the 132-hour change against the actual decoded timestamps, not a mock.
    query = forecast.query(AloftQuery(31.877,131.449,times[3],4572))
    assert [t['valid_time'] for t in query.provenance.trace['u']] == [
        (initial + timedelta(hours=h)).isoformat() for h in (132,138)]
    query = forecast.query(SurfaceTemperatureQuery(31.877,131.449,times[3]))
    assert [t['valid_time'] for t in query.provenance.trace['temperature']] == [
        (initial + timedelta(hours=h)).isoformat() for h in (132,135)]

    verification = verify_cache(client.cache_dir)
    assert verification['valid'] and len(verification['files']) == 8
    for remote in runs[0].files:
        path = client.cache_dir/'raw'/str(run)/remote.name
        assert forecast.source_hashes[remote.url] == sha256_file(path)

    # Independent direct pygrib decode at an exact gridpoint/pressure height.
    pressure_file = next(f for f in runs[0].files if f.kind == 'L-pall' and f.first_hour == 0)
    path = client.cache_dir/'raw'/str(run)/pressure_file.name
    raw = {}
    with pygrib.open(str(path)) as messages:
        for m in messages:
            assert (m.Ni,m.Nj) == (241,301)
            assert m.iDirectionIncrementInDegrees == .125
            assert m.jDirectionIncrementInDegrees == .1
            if m.forecastTime == 0 and m.typeOfLevel == 'isobaricInhPa' and m.level == 850:
                if m.shortName in ('gh','u','v','t'):
                    values,lat,lon = m.data(lat1=31.899999,lat2=31.900001,lon1=131.499999,lon2=131.500001)
                    assert values.size == 1
                    raw[m.shortName] = float(values[0,0])
    assert set(raw) == {'gh','u','v','t'}
    exact = forecast.query(AloftQuery(31.9,131.5,initial,raw['gh']))
    for key, name in [('u_ms','u'),('v_ms','v'),('temperature_k','t')]:
        assert exact.values[key] == pytest.approx(raw[name],abs=1e-10)
    surface_file = next(f for f in runs[0].files if f.kind == 'Lsurf' and f.first_hour == 0)
    direct_surface = None
    with pygrib.open(str(client.cache_dir/'raw'/str(run)/surface_file.name)) as messages:
        # Drain the multi-field stream, as the production decoder does. Early
        # close leaves ecCodes multi-field state behind in pygrib 2.1.8.
        for m in messages:
            if m.forecastTime == 0 and m.shortName == '2t':
                assert m.level == 2 and m.typeOfLevel == 'heightAboveGround'
                values,_,_ = m.data(lat1=31.899999,lat2=31.900001,lon1=131.499999,lon2=131.500001)
                direct_surface = float(values[0,0])
    assert direct_surface is not None
    assert forecast.query(SurfaceTemperatureQuery(31.9,131.5,initial)).values['temperature_k'] == direct_surface

    def unexpected(*args, **kwargs):
        pytest.fail('warm cache must not download or decode')
    monkeypatch.setattr(client.source, 'download', unexpected)
    monkeypatch.setattr(grib, 'read_grib_records', unexpected)
    warm = client.prepare_run(run, requirements, runs)
    for valid in times:
        for q in (AloftQuery(31.877,131.449,valid,4572),SurfaceTemperatureQuery(31.877,131.449,valid)):
            assert asdict(warm.query(q)) == asdict(forecast.query(q))
    print(json.dumps({'run':str(run),'results':results,'source_hashes':forecast.source_hashes,
                      'direct_850hpa':raw,'direct_surface_k':direct_surface,
                      'raw_files':len(verification['files']),'warm_cache':'identical'},sort_keys=True))
