from datetime import datetime, timezone
import hashlib
import os

import pytest

from jma_gpv_weather.cache import acquire_files
from jma_gpv_weather.errors import CacheIntegrityError, GpvError, NoCompatibleRunError, DownloadError
from jma_gpv_weather.models import ForecastRequirements, RunId, WeatherVariable
from jma_gpv_weather.msm.cached_client import CachedMsmClient
from jma_gpv_weather.msm.client import MsmClient
from jma_gpv_weather.msm.spec import parse_listing

UTC = timezone.utc
URL = 'https://example.invalid/2026/11/07'

class Source:
    calls = 0
    fail = True
    text = ''
    def directory_url(self, day):
        return URL
    def read_listing(self, url):
        self.calls += 1
        if self.fail:
            raise GpvError('listing unavailable')
        return self.text
    def download(self, *args):
        raise AssertionError('download must not happen')


def setup(tmp_path, *, partial=False):
    source = Source()
    client = CachedMsmClient(tmp_path, source=source)
    names = [f'Z__C_RJTD_20261107{hour:02d}0000_MSM_GPV_Rjp_{kind}_FH00-15_grib2.bin'
             for hour in [0, 3] for kind in ['Lsurf', 'L-pall']]
    source.text = '\n'.join(f'<a href="{name}">{name}</a>' for name in names)
    listing = tmp_path / 'listings' / (hashlib.sha256(URL.encode()).hexdigest() + '.html')
    listing.parent.mkdir(); listing.write_text(source.text); os.utime(listing, (1, 1))
    for remote in parse_listing(source.text, URL):
        if partial and remote.run_utc.hour == 3 and remote.kind == 'L-pall':
            continue
        path = tmp_path / 'raw' / f'{remote.run_utc:%Y%m%d%H%M%S}' / remote.name
        path.parent.mkdir(exist_ok=True, parents=True); path.write_bytes(b'GRIBtest7777')
    req = ForecastRequirements((datetime(2026,11,7,6,tzinfo=UTC),),
                               frozenset([WeatherVariable.ALOFT_WIND, WeatherVariable.SURFACE_TEMPERATURE]))
    return client, source, req, listing


def test_expired_listing_and_partial_newest_uses_complete_older(tmp_path):
    c,s,r,p = setup(tmp_path, partial=True)
    status = c.resolve_run(r)
    assert str(status.selected_run) == '20261107000000'
    assert c.acquisition_metadata == {'acquisition':'cached','reason':'LISTING_REFRESH_FAILED'}
    assert s.calls == 1
    assert c.resolve_run(r).selected_run == status.selected_run
    fixed_status = c.resolve_run(r, selected_run=RunId(datetime(2026,11,7,3,tzinfo=UTC)))
    assert str(fixed_status.selected_run) == '20261107030000'
    assert not fixed_status.selected_run_covers_request
    assert s.calls == 1
    assert p.stat().st_mtime == 1


def test_fixed_run_kept_and_next_action_recovers(tmp_path):
    c,s,r,p=setup(tmp_path)
    fixed=RunId(datetime(2026,11,7,tzinfo=UTC))
    assert c.resolve_run(r, fixed).selected_run == fixed
    s.fail=False
    c.begin_calculation()
    assert str(c.resolve_run(r).latest_compatible_run) == '20261107030000'
    assert not c.using_cached_forecast
    assert s.calls == 2


def test_default_client_has_no_stale_recovery(tmp_path):
    c,s,r,p=setup(tmp_path)
    with pytest.raises(NoCompatibleRunError):
        MsmClient(tmp_path, source=s).discover_runs(r)


@pytest.mark.parametrize('scenario',['no_listing','no_files','outside'])
def test_unavailable_cache_fails_closed(tmp_path, scenario):
    c,s,r,p=setup(tmp_path)
    if scenario=='no_listing': p.unlink()
    if scenario=='no_files':
        for f in (tmp_path/'raw').rglob('*.bin'): f.unlink()
    if scenario=='outside':
        r=ForecastRequirements((datetime(2026,11,10,tzinfo=UTC),),r.variables)
    with pytest.raises(NoCompatibleRunError): c.discover_runs(r)


def test_parse_error_is_not_transport_recovery(tmp_path, monkeypatch):
    c,s,r,p=setup(tmp_path); s.fail=False
    monkeypatch.setattr('jma_gpv_weather.msm.client.parse_listing',lambda *a: (_ for _ in ()).throw(ValueError('invalid')))
    with pytest.raises(GpvError): c.discover_runs(r)
    assert not c.using_cached_forecast


def test_corrupt_cache_never_downloads_or_skips_to_another_run(tmp_path):
    c,s,r,p=setup(tmp_path)
    run=c.discover_runs(r)[0]
    f=tmp_path/'raw'/f'{run.run_utc:%Y%m%d%H%M%S}'/run.files[0].name
    f.write_bytes(b'broken')
    with pytest.raises(CacheIntegrityError):
        c.prepare_run(RunId(run.run_utc),r)
    assert f.read_bytes()==b'broken'
    assert s.calls==1


def test_file_disappearing_after_selection_does_not_download(tmp_path):
    c,s,r,p=setup(tmp_path)
    run=c.discover_runs(r)[0]
    f=tmp_path/'raw'/f'{run.run_utc:%Y%m%d%H%M%S}'/run.files[0].name
    f.unlink()
    with pytest.raises(DownloadError):
        c.prepare_run(RunId(run.run_utc),r,available_runs=(run,))
