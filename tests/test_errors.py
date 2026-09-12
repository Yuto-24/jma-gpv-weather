"""Model-neutral failures and the existing MSM public catch contract."""
from datetime import date, datetime, timezone
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from jma_gpv_weather import Bounds, ForecastRequirements, MsmClient, RunId, WeatherVariable
from jma_gpv_weather import cache, grib
from jma_gpv_weather.errors import (
    GpvError, MsmError, CacheIntegrityError, DownloadError, InvalidQueryError,
    MissingVariableError, NoCompatibleRunError, SelectedRunCoverageError,
    StaticTerrainUnavailableError,
)
from jma_gpv_weather.models import RemoteFile, RunSelection
from jma_gpv_weather.msm import csv, csv_cli
from jma_gpv_weather.msm.spec import LEVELS_HPA
from jma_gpv_weather.msm.terrain import GridTerrainProvider
from jma_gpv_weather.sources import rish

VALID = datetime(2026, 7, 27, 12, tzinfo=timezone.utc)
REMOTE = RemoteFile('field.bin', 'http://example.test/field.bin', VALID, 'L-pall', 0, 15)
SELECTION = RunSelection(VALID, (REMOTE,))
REQUIREMENTS = ForecastRequirements((VALID,), frozenset({WeatherVariable.ALOFT_WIND}))


def fail_offline(*args, **kwargs):
    raise OSError('offline')


@pytest.mark.parametrize('operation', ['listing', 'download'])
def test_rish_errors_are_neutral(tmp_path, monkeypatch, operation):
    monkeypatch.setattr(rish, '_urlopen', fail_offline)
    with pytest.raises(GpvError) as caught:
        if operation == 'listing':
            rish.read_listing(REMOTE.url, attempts=1)
        else:
            rish.download(REMOTE, tmp_path / REMOTE.name, attempts=1)
    assert type(caught.value) is GpvError
    assert not isinstance(caught.value, MsmError)
    assert 'offline' in str(caught.value)


def test_msm_download_failure_is_still_caught_by_msm_error(tmp_path, monkeypatch):
    monkeypatch.setattr(rish, '_urlopen', fail_offline)
    monkeypatch.setattr(rish.time, 'sleep', lambda seconds: None)
    with pytest.raises(MsmError, match='Download failed: .*offline') as caught:
        MsmClient(tmp_path).prepare_run(RunId(VALID), REQUIREMENTS, available_runs=(SELECTION,))
    assert type(caught.value) is MsmError
    assert type(caught.value.__cause__) is GpvError
    assert str(caught.value) == str(caught.value.__cause__)


@pytest.mark.parametrize('boundary', ['common', 'msm', 'csv'])
def test_grib_open_failure_at_common_and_msm_boundaries(tmp_path, monkeypatch, boundary):
    monkeypatch.setitem(sys.modules, 'pygrib', SimpleNamespace(open=fail_offline))
    paths = (tmp_path / REMOTE.name,)
    monkeypatch.setattr(cache, 'acquire_files', lambda *args: (paths, {}))
    error_type = GpvError if boundary == 'common' else MsmError
    with pytest.raises(error_type, match='Cannot open .*field.bin: offline') as caught:
        if boundary == 'common':
            grib.read_grib_records(paths, None, Bounds(), (VALID,), pressure_levels=LEVELS_HPA)
        elif boundary == 'msm':
            MsmClient(tmp_path).prepare_run(RunId(VALID), REQUIREMENTS, available_runs=(SELECTION,))
        else:
            csv.write_outputs(tmp_path, VALID.date(), Bounds(), SELECTION, paths)
    assert type(caught.value) is error_type
    common = caught.value if boundary == 'common' else caught.value.__cause__
    assert type(common) is GpvError
    assert isinstance(common.__cause__, OSError)


@pytest.mark.parametrize('boundary', ['common', 'msm', 'terrain'])
def test_empty_grib_subset_preserves_msm_contract_and_closes_file(tmp_path, monkeypatch, boundary):
    message = SimpleNamespace(
        typeOfLevel='isobaricInhPa', level=1000, parameterCategory=2,
        parameterNumber=2, shortName='u', validDate=VALID,
        data=lambda **kwargs: (np.ones((2, 2)), np.zeros((2, 2)), np.zeros((2, 2))),
    )
    closed = []

    class GribFile:
        def __iter__(self):
            return iter((message,))

        def close(self):
            closed.append(True)

    monkeypatch.setitem(sys.modules, 'pygrib', SimpleNamespace(open=lambda path: GribFile()))
    paths = (tmp_path / REMOTE.name,)
    monkeypatch.setattr(cache, 'acquire_files', lambda *args: (paths, {}))
    error_type = GpvError if boundary == 'common' else MsmError
    with pytest.raises(error_type, match='No grid points inside requested rectangle') as caught:
        if boundary == 'common':
            grib.read_grib_records(paths, None, Bounds(), (VALID,), pressure_levels=LEVELS_HPA)
        elif boundary == 'msm':
            MsmClient(tmp_path).prepare_run(RunId(VALID), REQUIREMENTS, available_runs=(SELECTION,))
        else:
            GridTerrainProvider.from_grib(paths[0], Bounds())
    assert type(caught.value) is error_type
    assert closed == [True]
    if boundary != 'common':
        assert type(caught.value.__cause__) is GpvError


@pytest.mark.parametrize('error_type', [
    MsmError, InvalidQueryError, NoCompatibleRunError, SelectedRunCoverageError,
    DownloadError, CacheIntegrityError, MissingVariableError, StaticTerrainUnavailableError,
    ValueError, OSError,
])
def test_msm_boundary_preserves_existing_error_types_and_identity(tmp_path, error_type):
    failure = error_type('unchanged')
    if issubclass(error_type, MsmError):
        assert issubclass(error_type, GpvError)

    class Source:
        def download(self, remote, destination):
            raise failure

    with pytest.raises(error_type) as caught:
        MsmClient(tmp_path, source=Source()).prepare_run(
            RunId(VALID), REQUIREMENTS, available_runs=(SELECTION,),
        )
    assert caught.value is failure
    if error_type in (ValueError, OSError):
        assert not isinstance(caught.value, GpvError)


def test_listing_failure_keeps_existing_msm_discovery_semantics(tmp_path, monkeypatch):
    monkeypatch.setattr(rish, '_urlopen', fail_offline)
    monkeypatch.setattr(rish.time, 'sleep', lambda seconds: None)
    with pytest.raises(NoCompatibleRunError, match='No forecast run covers'):
        MsmClient(tmp_path).discover_runs(REQUIREMENTS)
    with pytest.raises(MsmError, match='Could not discover MSM files: .*RISH directory listing failed'):
        csv.discover_run(date(2026, 7, 28))


def test_csv_cli_still_reports_download_errors_without_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(csv_cli, 'discover_run', lambda *args, **kwargs: SELECTION)
    monkeypatch.setattr(rish, '_urlopen', fail_offline)
    monkeypatch.setattr(rish.time, 'sleep', lambda seconds: None)
    assert csv_cli.main(['--date', '2026-07-28', '--work-dir', str(tmp_path)]) == 1
    assert 'error: Download failed:' in capsys.readouterr().err


def test_cache_validation_retains_value_error(tmp_path):
    path = tmp_path / 'broken.bin'
    path.write_bytes(b'not-grib')
    with pytest.raises(ValueError, match='not a GRIB file') as caught:
        cache.validate_grib(path)
    assert not isinstance(caught.value, GpvError)
