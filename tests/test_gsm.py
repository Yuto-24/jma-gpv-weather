from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import json

import numpy as np
import pytest

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Availability, Bounds, CoveragePoint,
    CoverageState, ForecastRequirements, GsmClient, MsmClient, RunId,
    SurfaceTemperatureQuery, WeatherVariable,
)
from jma_gpv_weather.errors import (GpvError, MsmError, GsmCoverageError,
    GsmDiscoveryError, GsmRunUnavailableError, GsmProcessingError, NoCompatibleRunError)
from jma_gpv_weather.gsm import spec
from jma_gpv_weather.models import RunSelection
from jma_gpv_weather import grib

UTC = timezone.utc
RUN = datetime(2026, 9, 12, tzinfo=UTC)
VARS = spec.SUPPORTED_VARIABLES


def req(*hours, variables=VARS, run=RUN):
    return ForecastRequirements(tuple(run + timedelta(hours=h) for h in hours), frozenset(variables))


def name(run, kind, first, last):
    def fd(h):
        return f'{h//24:02d}{h%24:02d}'
    return f'Z__C_RJTD_{run:%Y%m%d%H%M%S}_GSM_GPV_Rjp_Gll0p1deg_{kind}_FD{fd(first)}-{fd(last)}_grib2.bin'


def files(run=RUN):
    names = [name(run, kind, first, last) for kind, intervals in spec.FILE_INTERVALS.items()
             for first, last in intervals if last <= spec.horizon(run)]
    return spec.parse_listing(' '.join(names), f'http://example.test/{run:%Y/%m/%d}')


class Source:
    def __init__(self, remote_files=(), error=None):
        self.files = remote_files
        self.error = error
        self.calls = []
        self.downloads = []

    def directory_url(self, day):
        return f'http://example.test/{day:%Y/%m/%d}'

    def read_listing(self, url):
        self.calls.append(url)
        if self.error:
            raise self.error
        return ' '.join(f.name for f in self.files if f.url.rsplit('/', 1)[0] == url)

    def download(self, remote, destination):
        self.downloads.append(remote)
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b'GRIBsynthetic7777')
        return destination


def test_filename_and_product_contract():
    assert len(files()) == 18
    assert len(files(RUN + timedelta(hours=6))) == 12
    assert spec.parse_listing(name(RUN, 'Lsurf', 0, 24).replace('Gll0p1deg_', ''), 'http://x') == []
    assert spec.parse_listing(name(RUN, 'Lsurf', 0, 24).replace('Rjp', 'Rgl'), 'http://x') == []
    assert spec.parse_listing(name(RUN, 'Lsurf', 0, 24).replace('20260912', '20269999'), 'http://x') == []
    assert spec.parse_listing(name(RUN, 'Lsurf', 0, 24).replace('0100', '0024'), 'http://x') == []
    assert spec.parse_listing(name(RUN + timedelta(hours=3), 'Lsurf', 0, 24), 'http://x') == []


@pytest.mark.parametrize('lat,lon,inside', [(20,120,True),(50,150,True),(20,150,True),(50,120,True),
    (19.9999,130,False),(50.0001,130,False),(30,119.9999,False),(30,150.0001,False)])
def test_domain_without_listing(lat, lon, inside):
    source = Source(error=AssertionError('must not list'))
    result = GsmClient(source=source).check_coverage(req(1.5), points=(CoveragePoint(lat,lon),), as_of=RUN)
    assert result.outside_spec is not inside
    assert source.calls == []


@pytest.mark.parametrize('run_hour,limit', [(0,264),(6,132),(12,264),(18,132)])
def test_horizon_and_initial_time_boundaries(run_hour, limit):
    run = RUN + timedelta(hours=run_hour)
    for hour in (0, limit-0.001, limit):
        assert not GsmClient.check_coverage(req(hour,run=run), run=RunId(run), as_of=run).outside_spec
    for hour in (-0.001,limit+0.001):
        assert GsmClient.check_coverage(req(hour,run=run),run=RunId(run),as_of=run).outside_spec
    assert GsmClient.check_coverage(req(1,run=run),run=RunId(run+timedelta(minutes=1)),as_of=run).outside_spec


def test_as_of_horizon_whole_request_and_timezone():
    assert GsmClient.check_coverage(req(0,264),as_of=RUN).candidate_runs == (RunId(RUN),)
    assert GsmClient.check_coverage(req(0,264.001),as_of=RUN).outside_spec
    assert GsmClient.check_coverage(req(264),as_of=RUN-timedelta(seconds=1)).outside_spec
    jst = timezone(timedelta(hours=9))
    assert GsmClient.check_coverage(req(0,264), run=RunId(RUN.astimezone(jst)),as_of=RUN).candidate_runs == (RunId(RUN),)
    with pytest.raises(ValueError):
        GsmClient.check_coverage(req(0),as_of=RUN.replace(tzinfo=None))


@pytest.mark.parametrize('height', [-1000, 0, 4572, 16000, 100000])
def test_msl_height_is_explicitly_data_dependent(height):
    result = GsmClient.check_coverage(req(0),points=(CoveragePoint(30,130,height),),as_of=RUN)
    assert result.state == CoverageState.REQUIRES_HGT
    assert not result.outside_spec
    assert 'MSL_ALTITUDE_REQUIRES_HGT' in result.reason_codes


@pytest.mark.parametrize('bracket,inside', [((1000,1000),True),((100,100),True),((150,100),True),
                                          ((1001,1000),False),((100,99),False),((875,850),False)])
def test_pressure_level_bracket_boundaries(bracket,inside):
    result = GsmClient.check_coverage(req(0),points=(CoveragePoint(30,130,pressure_bracket_hpa=bracket),),as_of=RUN)
    assert result.outside_spec is not inside
    assert result.pressure_levels_hpa == spec.LEVELS_HPA


def test_invalid_coverage_input_and_unsupported_variables():
    for values in [(float('nan'),130,None),(30,float('inf'),None),(30,130,float('nan'))]:
        with pytest.raises(ValueError):
            CoveragePoint(*values)
    for bracket in [(100,1000),(100,0),(100,), (100,float('nan'))]:
        with pytest.raises(ValueError):
            CoveragePoint(30,130,pressure_bracket_hpa=bracket)
    for variable in (WeatherVariable.ESTIMATED_QNH,WeatherVariable.SURFACE_WIND,'humidity'):
        result = GsmClient.check_coverage(req(0,variables={variable}),as_of=RUN)
        assert result.outside_spec and 'UNSUPPORTED_VARIABLE' in result.reason_codes
    for variable in VARS:
        assert not GsmClient.check_coverage(req(0,variables={variable}),as_of=RUN).outside_spec


def test_temporal_resolution_transition_and_file_split():
    needed = spec.required_valid_times(req(131.5,132,133.5,264),RUN)
    assert tuple((t-RUN).total_seconds()/3600 for t in needed['Lsurf']) == (131,132,135,264)
    assert tuple((t-RUN).total_seconds()/3600 for t in needed['L-pall']) == (129,132,138,264)
    assert spec.required_valid_times(req(264.001),RUN) is None
    selections = spec.select_compatible_runs(files(),req(24.5))
    assert len(selections) == 1 and len(selections[0].files) == 4
    assert {f.first_hour for f in selections[0].files} == {0,25,27}


def test_discovery_failure_is_not_coverage(tmp_path):
    source = Source(error=GpvError('network failed'))
    client = GsmClient(tmp_path,source=source)
    before = client.check_coverage(req(1.5),as_of=RUN)
    with pytest.raises(GsmDiscoveryError) as exc:
        client.discover_runs(req(1.5),as_of=RUN)
    assert isinstance(exc.value.__cause__,GpvError)
    assert not isinstance(exc.value,(GsmCoverageError,MsmError))
    assert client.check_coverage(req(1.5),as_of=RUN) == before
    missing = GsmClient(tmp_path/'missing',source=Source())
    with pytest.raises(GsmRunUnavailableError):
        missing.discover_runs(req(1.5),as_of=RUN)
    assert not missing.check_coverage(req(1.5),as_of=RUN).outside_spec
    source.calls.clear()
    with pytest.raises(GsmCoverageError):
        client.discover_runs(req(265),as_of=RUN)
    assert not source.calls


def test_discovery_incomplete_archive_and_no_cross_run_mix(tmp_path):
    fs = files()
    client=GsmClient(tmp_path,source=Source(fs))
    runs=client.discover_runs(req(1.5),as_of=RUN)
    assert len(runs)==1 and len(runs[0].files)==2
    # Different runs each supply only half the variables; never combine them.
    half=[f for f in fs if f.kind=='Lsurf']+[f for f in files(RUN-timedelta(hours=6)) if f.kind=='L-pall']
    assert spec.select_compatible_runs(half,req(1.5)) == ()
    incomplete=GsmClient(tmp_path/'other',source=Source(half))
    with pytest.raises(GsmRunUnavailableError):
        incomplete.discover_runs(req(1.5),as_of=RUN)


def test_run_pin_update_and_selected_missing_are_distinct(tmp_path):
    client=GsmClient(tmp_path,source=Source(error=AssertionError('no network')))
    older=RUN-timedelta(hours=6)
    runs=spec.select_compatible_runs(files()+files(older),req(1.5))
    status=client.resolve_run(req(1.5),available_runs=runs)
    assert status.selected_run==RunId(RUN)
    pinned=client.resolve_run(req(1.5),RunId(older),runs)
    assert pinned.selected_run==RunId(older) and pinned.update_available and pinned.selected_run_covers_request
    missing=client.resolve_run(req(1.5),RunId(older),runs[:1])
    assert missing.selected_run==RunId(older) and missing.warnings==('SELECTED_RUN_NOT_DISCOVERED',)
    incompatible=client.resolve_run(req(200),RunId(older),spec.select_compatible_runs(files(),req(200)))
    assert incompatible.warnings==('SELECTED_RUN_OUTSIDE_SPEC',)
    with pytest.raises(GsmRunUnavailableError):
        client.prepare_run(RunId(older),req(1.5),runs[:1])
    with pytest.raises(GsmCoverageError):
        client.prepare_run(RunId(older),req(200),runs)
    with pytest.raises(GsmRunUnavailableError):
        client.resolve_run(req(1.5),available_runs=())
    assert not client.source.calls


def synthetic_records(valid_times, pressure_levels):
    lat=np.array([[31.9,31.9],[31.8,31.8]])
    lon=np.array([[131.375,131.5],[131.375,131.5]])
    surface,pressure={},{}
    for valid in valid_times:
        offset=(valid-RUN).total_seconds()/3600
        horizontal=(lat-31.8)*10+(lon-131.375)*8
        surface[valid,2,'tmp_surface']=(280+offset+horizontal,lat,lon)
        for level in pressure_levels:
            height=(1000-level)*20+100
            for name,value in [('hgt',np.full((2,2),height)),('u',height/1000+offset+horizontal),
                               ('v',np.zeros((2,2))),('tmp',290-height/1000+offset+horizontal)]:
                pressure[valid,level,name]=(value,lat,lon)
    return surface,pressure


def prepare_synthetic(tmp_path,monkeypatch,variables=VARS):
    source=Source(files())
    client=GsmClient(tmp_path,Bounds(31.8,31.9,131.375,131.5),source=source)
    requirements=req(0,1.5,3,variables=variables)
    runs=spec.select_compatible_runs(files(),requirements)
    def decode(paths,target_date,bounds,valid_times,*,pressure_levels):
        return synthetic_records(valid_times,pressure_levels)
    monkeypatch.setattr(grib,'read_grib_records',decode)
    return client,requirements,runs,client.prepare_run(RunId(RUN),requirements,runs)


def test_weather_interpolation_and_provenance(tmp_path,monkeypatch):
    client,requirements,runs,prepared=prepare_synthetic(tmp_path,monkeypatch)
    valid=RUN+timedelta(hours=1.5)
    q=AloftQuery(31.85,131.4375,valid,1350)
    result=prepared.query(q)
    assert result.availability==Availability.AVAILABLE
    assert result.values['u_ms']==pytest.approx(3.85)
    assert result.values['temperature_k']==pytest.approx(291.15)
    surface=prepared.query(SurfaceTemperatureQuery(q.latitude,q.longitude,valid))
    assert surface.values['temperature_k']==pytest.approx(282.5)
    assert surface.values['representative_height_agl_m']==2
    assert prepared.query(AloftTemperatureQuery(q.latitude,q.longitude,valid,1350)).values['temperature_k']==result.values['temperature_k']
    assert prepared.query_many([q])[0]==result
    provenance=result.provenance
    assert provenance.initial_time_utc==RUN
    assert len(provenance.source_urls)==2
    assert all(len(v)==64 for v in provenance.source_hashes.values())
    assert provenance.trace['model']=='GSM_JAPAN'
    assert len(provenance.trace['u'])==2
    assert provenance.trace['u'][0]['corners'][0]['pressure_bracket_hpa']==(950,925)
    assert prepared.query(AloftQuery(q.latitude,q.longitude,valid,99)).reason_code=='VERTICAL_BRACKET_UNAVAILABLE'
    for height in [100,18100]:
        assert prepared.query(AloftQuery(q.latitude,q.longitude,valid,height)).availability==Availability.AVAILABLE
    assert prepared.query(AloftQuery(q.latitude,q.longitude,valid,18100.001)).availability==Availability.UNAVAILABLE
    assert prepared.query(AloftQuery(q.latitude,q.longitude,RUN+timedelta(hours=4),1350)).reason_code=='TIME_NOT_PREPARED'
    assert prepared.query_surface_temperature(SurfaceTemperatureQuery(q.latitude,q.longitude,RUN+timedelta(hours=4))).reason_code=='TIME_NOT_PREPARED'


def test_temperature_only_and_surface_only(tmp_path,monkeypatch):
    _,_,_,prepared=prepare_synthetic(tmp_path,monkeypatch,{WeatherVariable.ALOFT_TEMPERATURE})
    prepared.pressure={k:v for k,v in prepared.pressure.items() if k[2] in ('hgt','tmp')}
    result=prepared.query(AloftTemperatureQuery(31.85,131.4375,RUN,1350))
    assert result.availability==Availability.AVAILABLE
    _,_,_,surface=prepare_synthetic(tmp_path/'surface',monkeypatch,{WeatherVariable.SURFACE_TEMPERATURE})
    assert surface.query(SurfaceTemperatureQuery(31.85,131.4375,RUN)).availability==Availability.AVAILABLE


def test_cache_namespaces_and_warm_reuse(tmp_path,monkeypatch):
    client,requirements,runs,prepared=prepare_synthetic(tmp_path,monkeypatch)
    # Use actual MSM client orchestration at the same initial time/cache root.
    from jma_gpv_weather.msm.spec import parse_listing as msm_parse
    msm_source=Source()
    msm=MsmClient(tmp_path,client.bounds,source=msm_source)
    mf=msm_parse(' '.join(f'Z__C_RJTD_{RUN:%Y%m%d%H%M%S}_MSM_GPV_Rjp_{kind}_FH00-15_grib2.bin'
                         for kind in ['Lsurf','L-pall']),msm_source.directory_url(RUN.date()))
    msm_prepared=msm.prepare_run(RunId(RUN),requirements,(RunSelection(RUN,tuple(mf)),))
    for root in (tmp_path, tmp_path/'gsm-japan'):
        assert len(list((root/'raw'/str(RunId(RUN))).glob('*.bin')))==2
        assert (root/'raw'/str(RunId(RUN))/'manifest.json').exists()
        assert len(list((root/'normalized').rglob('weather.nc')))==1
        assert len(list((root/'normalized').rglob('manifest.json')))==1
        assert len(list((root/'locks').glob('*.lock')))>=3
    before={p: p.read_bytes() for p in (tmp_path/'normalized').rglob('*') if p.is_file()}
    def unexpected(*a,**kw):
        pytest.fail('warm cache must avoid download/decode')
    monkeypatch.setattr(grib,'read_grib_records',unexpected)
    client.source.download=unexpected
    warm=client.prepare_run(RunId(RUN),requirements,runs)
    query=AloftQuery(31.85,131.4375,RUN,1350)
    assert asdict(warm.query(query))==asdict(prepared.query(query))
    assert all(p.read_bytes()==data for p,data in before.items())
    temp=SurfaceTemperatureQuery(31.85,131.4375,RUN+timedelta(hours=1.5))
    old=msm_prepared._surface_scalar('tmp_surface',temp.latitude,temp.longitude,temp.valid_time)
    assert msm_prepared.query(temp).values['temperature_k']==old[0]


@pytest.mark.parametrize('stage',['download','decode','cache','missing'])
def test_processing_errors_never_become_coverage(tmp_path,monkeypatch,stage):
    client=GsmClient(tmp_path,source=Source(files()))
    requirements=req(1.5)
    runs=spec.select_compatible_runs(files(),requirements)
    def failed(*a,**kw):
        raise ValueError('corrupt data')
    if stage=='download':
        client.source.download=failed
    elif stage=='decode':
        monkeypatch.setattr(grib,'read_grib_records',failed)
    elif stage=='cache':
        (client.cache_dir/'raw').mkdir(parents=True)
        (client.cache_dir/'raw'/str(RunId(RUN))).write_text('not a directory')
    else:
        monkeypatch.setattr(grib,'read_grib_records',lambda *a,**kw: ({},{}))
    with pytest.raises(GsmProcessingError) as exc:
        client.prepare_run(RunId(RUN),requirements,runs)
    assert not isinstance(exc.value,(GsmCoverageError,GsmDiscoveryError,MsmError))
    assert not client.check_coverage(requirements).outside_spec


def test_msm_spec_separate_from_legacy_discovery(tmp_path):
    source=Source(error=GpvError('offline'))
    msm=MsmClient(tmp_path,source=source)
    before=msm.check_coverage(req(1.5),as_of=RUN)
    assert not before.outside_spec and not source.calls
    with pytest.raises(NoCompatibleRunError):
        msm.discover_runs(req(1.5))
    assert msm.check_coverage(req(1.5),as_of=RUN)==before
    assert MsmClient.check_coverage(req(79),run=RunId(RUN),as_of=RUN).outside_spec
    assert not MsmClient.check_coverage(req(78),run=RunId(RUN),as_of=RUN).outside_spec
    assert MsmClient.check_coverage(req(40,run=RUN+timedelta(hours=3)),run=RunId(RUN+timedelta(hours=3)),as_of=RUN+timedelta(hours=3)).outside_spec
    assert MsmClient.check_coverage(req(0),points=(CoveragePoint(30,130,pressure_bracket_hpa=(500,400)),),as_of=RUN).outside_spec


def test_partial_listing_failure_is_visible_even_with_an_older_run(tmp_path):
    class PartialSource(Source):
        def read_listing(self,url):
            if url.endswith('/2026/09/11'):
                raise OSError('partial listing failure')
            return super().read_listing(url)
    source=PartialSource(files())
    with pytest.raises(GsmDiscoveryError):
        GsmClient(tmp_path,source=source).discover_runs(req(1.5),as_of=RUN)


def test_normalized_corruption_and_changed_raw_source_are_rebuilt(tmp_path,monkeypatch):
    client,requirements,runs,prepared=prepare_synthetic(tmp_path,monkeypatch)
    nc=next((client.cache_dir/'normalized').rglob('weather.nc'))
    nc.write_bytes(b'broken cache')
    fixed=client.prepare_run(RunId(RUN),requirements,runs)
    q=SurfaceTemperatureQuery(31.85,131.4375,RUN)
    assert fixed.query(q)==prepared.query(q)
    assert nc.with_suffix('.nc.corrupt').exists()
    raw=next((client.cache_dir/'raw').rglob('*.bin'))
    raw.write_bytes(b'GRIBchanged raw data7777')
    calls=[]
    def decode(paths,target_date,bounds,valid_times,*,pressure_levels):
        calls.append(True)
        return synthetic_records(valid_times,pressure_levels)
    monkeypatch.setattr(grib,'read_grib_records',decode)
    fresh=client.prepare_run(RunId(RUN),requirements,runs)
    assert calls==[True] and fresh.source_hashes!=prepared.source_hashes
    manifest=json.loads(nc.with_name('manifest.json').read_text())
    assert manifest['source_hashes']==fresh.source_hashes


def test_model_listing_namespace_and_public_query_errors(tmp_path,monkeypatch):
    from jma_gpv_weather.cache import cached_listing
    url='http://example.test/day'
    msm=MsmClient(tmp_path)
    gsm=GsmClient(tmp_path)
    assert cached_listing(url,msm.cache_dir,lambda url:'msm')=='msm'
    assert cached_listing(url,gsm.cache_dir,lambda url:'gsm')=='gsm'
    assert len(list((tmp_path/'listings').glob('*.html')))==1
    assert len(list((tmp_path/'gsm-japan'/'listings').glob('*.html')))==1
    _,_,_,prepared=prepare_synthetic(tmp_path,monkeypatch)
    with pytest.raises(ValueError):
        prepared.query(AloftQuery(float('nan'),131,RUN,100))
    with pytest.raises(ValueError):
        prepared.query(SurfaceTemperatureQuery(31,131,RUN.replace(tzinfo=None)))
    from jma_gpv_weather import SurfaceWindQuery
    with pytest.raises(ValueError):
        prepared.query(SurfaceWindQuery(31,131,RUN))


def test_non_utc_run_uses_same_gsm_cache(tmp_path,monkeypatch):
    client,requirements,runs,prepared=prepare_synthetic(tmp_path,monkeypatch)
    offset_run=RunId(RUN.astimezone(timezone(timedelta(hours=5,minutes=30))))
    assert spec.required_valid_times(requirements,offset_run.initial_time_utc)==spec.required_valid_times(requirements,RUN)
    def unexpected(*a,**kw):
        pytest.fail('timezone-equivalent run must reuse cache')
    monkeypatch.setattr(grib,'read_grib_records',unexpected)
    warm=client.prepare_run(offset_run,requirements,runs)
    assert warm.selection==prepared.selection
    assert [p.name for p in (client.cache_dir/'raw').iterdir()]==[str(RunId(RUN))]


def test_partial_decode_does_not_publish_poisoned_cache(tmp_path,monkeypatch):
    source=Source(files())
    client=GsmClient(tmp_path,source=source)
    requirements=req(1.5)
    runs=spec.select_compatible_runs(files(),requirements)
    calls=[]
    def partial(paths,target_date,bounds,valid_times,*,pressure_levels):
        calls.append('partial')
        surface,_=synthetic_records(valid_times,pressure_levels)
        return surface,{}
    monkeypatch.setattr(grib,'read_grib_records',partial)
    with pytest.raises(GsmProcessingError,match='fields missing'):
        client.prepare_run(RunId(RUN),requirements,runs)
    assert not list((client.cache_dir/'normalized').rglob('weather.nc'))
    assert not list((client.cache_dir/'normalized').rglob('manifest.json'))
    def complete(paths,target_date,bounds,valid_times,*,pressure_levels):
        calls.append('complete')
        return synthetic_records(valid_times,pressure_levels)
    monkeypatch.setattr(grib,'read_grib_records',complete)
    forecast=client.prepare_run(RunId(RUN),requirements,runs)
    assert calls==['partial','complete']
    assert forecast.query(AloftQuery(31.85,131.4375,RUN+timedelta(hours=1.5),1350)).availability==Availability.AVAILABLE


def test_incomplete_existing_normalized_group_is_rebuilt(tmp_path,monkeypatch):
    from jma_gpv_weather.cache import sha256_file
    from jma_gpv_weather.normalized import save_records
    client,requirements,runs,prepared=prepare_synthetic(tmp_path,monkeypatch)
    nc=next((client.cache_dir/'normalized').rglob('weather.nc'))
    # Simulate a partial cache with a matching manifest (e.g. pre-fix writer).
    save_records(nc,prepared.surface,{}, {}, pressure_levels=spec.LEVELS_HPA)
    mp=nc.with_name('manifest.json')
    manifest=json.loads(mp.read_text())
    manifest['normalized_sha256']=sha256_file(nc)
    mp.write_text(json.dumps(manifest))
    calls=[]
    def decode(paths,target_date,bounds,valid_times,*,pressure_levels):
        calls.append(True)
        return synthetic_records(valid_times,pressure_levels)
    monkeypatch.setattr(grib,'read_grib_records',decode)
    fixed=client.prepare_run(RunId(RUN),requirements,runs)
    assert calls==[True] and nc.with_suffix('.nc.corrupt').exists()
    q=AloftQuery(31.85,131.4375,RUN,1350)
    assert fixed.query(q)==prepared.query(q)


def test_selected_run_empty_live_discovery_returns_status_but_errors_propagate(tmp_path):
    client=GsmClient(tmp_path,source=Source())
    status=client.resolve_run(req(1.5),selected_run=RunId(RUN))
    assert status.selected_run==RunId(RUN)
    assert status.latest_compatible_run is None and not status.update_available
    assert not status.selected_run_covers_request
    assert status.warnings==('SELECTED_RUN_NOT_DISCOVERED',)
    assert client.resolve_run(req(1.5),RunId(RUN),available_runs=())==status
    with pytest.raises(GsmRunUnavailableError):
        client.discover_runs(req(1.5))
    with pytest.raises(GsmRunUnavailableError):
        client.resolve_run(req(1.5))
    offline=GsmClient(tmp_path/'offline',source=Source(error=OSError('offline')))
    with pytest.raises(GsmDiscoveryError):
        offline.resolve_run(req(1.5),selected_run=RunId(RUN))
