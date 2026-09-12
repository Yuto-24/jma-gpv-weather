"""Regression checks at the source/cache/model seams moved by Issue #12."""
from dataclasses import asdict
from datetime import date, datetime, timezone
import io
import json
from types import SimpleNamespace

import numpy as np
import pytest

from jma_gpv_weather import AloftQuery, Bounds, MsmClient, RunId
from jma_gpv_weather import cache, grib
from jma_gpv_weather.errors import MsmError, MissingVariableError, SelectedRunCoverageError
from jma_gpv_weather.models import RemoteFile
from jma_gpv_weather.msm.spec import LEVELS_HPA, parse_listing
from jma_gpv_weather.normalized import load_records, normalized_key, save_records
from jma_gpv_weather.sources import rish
from test_weather_api import requirements, synthetic_prepared


def test_source_boundary_drives_discovery_download_and_warm_cache(tmp_path, monkeypatch):
    prepared = synthetic_prepared()
    run = prepared.selection.run_utc
    files = [
        f"Z__C_RJTD_{run:%Y%m%d%H%M%S}_MSM_GPV_Rjp_{kind}_FH00-39_grib2.bin"
        for kind in ("Lsurf", "L-pall")
    ]
    calls = []

    class Source:
        def directory_url(self, day):
            return f"http://example.test/archive/{day.isoformat()}"

        def read_listing(self, url):
            calls.append(("listing", url))
            return '\n'.join(files) if url.endswith("2026-07-27") else ""

        def download(self, remote, destination):
            calls.append(("download", remote.url))
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(b"GRIBtest7777")
            return destination

    def decode(paths, target_date, bounds, valid_times, *, pressure_levels):
        assert len(paths) == 2 and pressure_levels == LEVELS_HPA
        # Complete the pressure cube so real preparation validation is exercised.
        surface, pressure = prepared.surface, dict(prepared.pressure)
        for valid in {key[0] for key in pressure}:
            for level in LEVELS_HPA[2:]:
                for name in ("hgt", "u", "v", "tmp"):
                    pressure[valid, level, name] = pressure[valid, 975, name]
        return surface, pressure

    monkeypatch.setattr(grib, "read_grib_records", decode)
    # Exact time uses the existing complete synthetic surface and pressure fields.
    valid = datetime(2026, 7, 28, tzinfo=timezone.utc)
    req = requirements(valid)
    client = MsmClient(tmp_path, source=Source())
    runs = client.discover_runs(req)
    assert runs[0].run_utc == run
    status = client.resolve_run(req, RunId(run), available_runs=runs)
    assert status.selected_run_covers_request and not status.update_available
    first = client.prepare_run(status.selected_run, req, available_runs=runs)
    calls_before = list(calls)

    def no_decode(*args, **kwargs):
        pytest.fail("warm normalized cache must not decode GRIB again")

    monkeypatch.setattr(grib, "read_grib_records", no_decode)
    assert client.discover_runs(req) == runs
    second = client.prepare_run(status.selected_run, req, available_runs=runs)
    assert calls == calls_before
    query = AloftQuery(30.5, 130.5, valid, 1500)
    assert asdict(first.query(query)) == asdict(second.query(query))
    assert first.query(query).values['temperature_k'] == pytest.approx(280.5)
    assert second._surface_scalar('tmp_surface', 30.5, 130.5, valid)[0] == 288.15
    assert set(second.source_hashes) == {item.url for item in runs[0].files}
    assert all(len(value) == 64 for value in second.source_hashes.values())
    assert (tmp_path / 'raw' / str(RunId(run)) / 'manifest.json').exists()
    assert len(list((tmp_path / 'normalized' / 'v1' / str(RunId(run))).glob('*/weather.nc'))) == 1
    with pytest.raises(SelectedRunCoverageError):
        client.prepare_run(RunId(valid), req, available_runs=runs)


def test_raw_cache_quarantines_damage_and_preserves_manifest_hashes(tmp_path):
    remote = RemoteFile('field.bin', 'http://example.test/field.bin',
                        datetime(2026, 7, 27, 12, tzinfo=timezone.utc), 'Lsurf', 0, 15)
    destination = tmp_path / 'raw' / '20260727120000' / remote.name
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b'broken')
    calls = []

    def download(item, path):
        calls.append(item)
        path.write_bytes(b'GRIBpayload7777')
        return path

    paths, hashes = cache.acquire_files((remote,), tmp_path, download)
    assert paths == (destination,)
    assert next(destination.parent.glob('*.corrupt.*')).read_bytes() == b'broken'
    assert hashes == {remote.url: cache.sha256_file(destination)}
    manifest = json.loads((destination.parent / 'manifest.json').read_text())
    assert manifest['files'][0]['sha256'] == hashes[remote.url]
    assert cache.acquire_files((remote,), tmp_path, download) == (paths, hashes)
    assert len(calls) == 1
    assert cache.verify_cache(tmp_path)['valid']


@pytest.mark.parametrize('status', [200, 206])
def test_rish_download_resumes_or_restarts_when_range_is_ignored(tmp_path, monkeypatch, status):
    payload = b'GRIBpayload7777'
    destination = tmp_path / 'field.bin'
    partial = tmp_path / 'field.bin.part'
    partial.write_bytes(payload[:6])
    remote = RemoteFile(destination.name, 'http://example.test/field.bin',
                        datetime(2026, 7, 27, tzinfo=timezone.utc), 'Lsurf', 0, 15)

    def open_url(url, timeout, headers):
        assert url == remote.url and timeout == 60
        assert headers == {'Range': 'bytes=6-'}
        response = io.BytesIO(payload[6:] if status == 206 else payload)
        response.status = status
        return response

    monkeypatch.setattr(rish, '_urlopen', open_url)
    assert rish.RishSource().download(remote, destination) == destination
    assert destination.read_bytes() == payload
    assert not partial.exists()


def test_listing_url_parsing_and_transport_errors_stay_separate(monkeypatch):
    source = rish.RishSource('http://example.test/archive/')
    url = source.directory_url(date(2026, 7, 27))
    assert url == 'http://example.test/archive/2026/07/27'
    name = 'Z__C_RJTD_20260727120000_MSM_GPV_Rjp_Lsurf_FH00-15_grib2.bin'
    assert len(parse_listing(f'<a href="{name}">{name}</a>', url)) == 1
    assert parse_listing(name.replace('_MSM_', '_OTHER_'), url) == []

    def fail(*args, **kwargs):
        raise OSError('offline')

    monkeypatch.setattr(rish, '_urlopen', fail)
    with pytest.raises(MsmError, match='RISH directory listing failed'):
        rish.read_listing(url, attempts=1)


def test_model_supplies_grib_and_normalized_pressure_levels(tmp_path):
    message = SimpleNamespace(typeOfLevel='isobaricInhPa', level=400,
                              parameterCategory=0, parameterNumber=0, shortName='t')
    assert grib._identity(message, LEVELS_HPA) is None
    assert grib._identity(message, (400,)) == ('tmp', 400)
    prepared = synthetic_prepared()
    valid = prepared.selection.run_utc
    record = next(iter(prepared.pressure.values()))
    path = tmp_path / 'different-levels.nc'
    save_records(path, {}, {(valid, 400, 'tmp'): record}, {}, pressure_levels=(400,))
    _, pressure = load_records(path)
    assert set(pressure) == {(valid, 400, 'tmp')}
    np.testing.assert_array_equal(pressure[valid, 400, 'tmp'][0], record[0])


def test_missing_surface_temperature_keeps_preparation_error():
    prepared = synthetic_prepared()
    valid = datetime(2026, 7, 28, tzinfo=timezone.utc)
    surface = {key: value for key, value in prepared.surface.items() if key[2] != 'tmp_surface'}
    with pytest.raises(MissingVariableError, match='surface:tmp_surface'):
        MsmClient._validate_prepared(requirements(valid), {'Lsurf': (valid,)}, surface, {})


def test_normalized_key_stays_compatible_with_main():
    assert normalized_key(Bounds(), (datetime(2026, 7, 28, tzinfo=timezone.utc),)) == 'b1335793e4bc492e'
