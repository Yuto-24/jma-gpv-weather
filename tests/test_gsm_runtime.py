from copy import deepcopy
from dataclasses import replace
from datetime import timedelta
import hashlib
from io import BytesIO
import struct
import sys
from zipfile import ZipFile

import numpy as np
import pytest

from jma_gpv_weather import (
    AloftQuery, Availability, ForecastRequirements, GsmClient, GsmPreparedData,
    MsmPreparedData, RunId, WeatherVariable,
)
from jma_gpv_weather.errors import (
    CacheIntegrityError, GsmCacheIntegrityError, GsmCoverageError, GsmDiscoveryError,
    GsmProcessingError, GsmRunUnavailableError, MsmError,
)
from gsm_runtime_case import BOUNDS, INITIAL, desktop_case, queries, results
from runtime_case import desktop_case as msm_case


@pytest.fixture
def case(tmp_path):
    return desktop_case(tmp_path / "desktop")


def test_desktop_and_portable_share_gsm_time_brackets_and_provenance(case, tmp_path, monkeypatch):
    client, req, runs, selected, forecast = case
    expected = results(*case)
    assert expected["status"]["update_available"]
    assert forecast.query(queries()[0]).values["u_ms"] == pytest.approx(3.7)
    assert forecast.query(queries()[3]).values["u_ms"] == pytest.approx(15.7)
    assert forecast.query(queries()[5]).values["temperature_k"] == pytest.approx(294.35)
    data = GsmPreparedData.from_forecast(forecast)
    payload = data.to_bytes()
    restored = GsmPreparedData.from_bytes(payload, expected_sha256=hashlib.sha256(payload).hexdigest())
    listings = {url: client.source.read_listing(url) for url in client.listing_urls(req)}

    def forbidden(*args, **kwargs):
        pytest.fail("portable GSM must not acquire, cache or decode")

    for name in ("cached_listing", "acquire_files", "prepare_records", "file_lock"):
        monkeypatch.setattr(f"jma_gpv_weather.gsm.client.{name}", forbidden)
    for module in ("pygrib", "fcntl", "xarray", "h5netcdf", "h5py"):
        monkeypatch.setitem(sys.modules, module, None)
    runtime = GsmClient(tmp_path / "must-not-exist", BOUNDS, source=client.source)
    acquired = runtime.discover_runs(req, listings=listings)
    assert acquired == runs
    prepared = runtime.prepare_run(selected, req, available_runs=acquired, prepared_data=restored)
    assert results(runtime, req, acquired, selected, prepared) == expected
    assert runtime.prepare_run(selected, req, prepared_data=restored).query(queries()[0]) == forecast.query(queries()[0])
    assert not runtime.cache_dir.exists()


def test_listing_urls_and_supplied_mapping_have_no_io_and_fail_closed(case, monkeypatch):
    client, req, *_ = case
    monkeypatch.setattr(client.source, "read_listing", lambda *a: pytest.fail("must not read"))
    urls = client.listing_urls(req)
    assert urls and len(urls) == len(set(urls))
    with pytest.raises(GsmRunUnavailableError):
        client.discover_runs(req, listings=dict.fromkeys(urls, ""))
    for bad in ({}, dict.fromkeys(urls, None)):
        with pytest.raises(GsmDiscoveryError) as error:
            client.discover_runs(req, listings=bad)
        assert not isinstance(error.value, GsmCoverageError)


def test_portable_fixed_run_and_foreign_model_are_not_replaced(case, tmp_path):
    client, req, runs, selected, forecast = case
    data = GsmPreparedData.from_forecast(forecast)
    newer = RunId(INITIAL + timedelta(hours=6))
    with pytest.raises(GsmRunUnavailableError):
        client.prepare_run(newer, req, prepared_data=data)
    with pytest.raises(GsmCacheIntegrityError):
        client.prepare_run(newer, req, available_runs=runs, prepared_data=data)
    msm = MsmPreparedData.from_forecast(msm_case(tmp_path / "msm")[-1])
    with pytest.raises(TypeError):
        client.prepare_run(selected, req, prepared_data=msm)
    with pytest.raises(GsmCacheIntegrityError):
        GsmPreparedData.from_bytes(msm.to_bytes())
    with pytest.raises(CacheIntegrityError):
        MsmPreparedData.from_bytes(data.to_bytes())


def test_narrowing_filters_sources_and_records_without_mutating_snapshot(case):
    client, req, _, selected, forecast = case
    data = GsmPreparedData.from_forecast(forecast)
    before = data.to_bytes()
    narrow = ForecastRequirements((req.valid_times[0],),
                                  frozenset({WeatherVariable.SURFACE_TEMPERATURE}))
    prepared = client.prepare_run(selected, narrow, prepared_data=data)
    result = prepared.query(queries()[2])
    assert result.values == forecast.query(queries()[2]).values
    assert len(prepared.selection.files) == 1
    assert prepared.pressure == {}
    assert all(key[0] < INITIAL + timedelta(hours=25) for key in prepared.surface)
    assert result.provenance.source_hashes == {
        remote.url: data.source_hashes[remote.url] for remote in prepared.selection.files
    }
    assert data.to_bytes() == before
    restored = GsmPreparedData.from_bytes(GsmPreparedData.from_forecast(prepared).to_bytes())
    assert client.prepare_run(selected, narrow, prepared_data=restored).query(queries()[2]) == result


@pytest.mark.parametrize("kind", ["surface", "pressure", "time", "hgt"])
def test_missing_fields_or_brackets_are_processing_not_coverage(case, kind):
    client, req, _, selected, forecast = case
    data = GsmPreparedData.from_forecast(forecast)
    if kind in ("surface", "pressure"):
        getattr(data, kind).clear()
    elif kind == "time":
        data.pressure = {k: v for k, v in data.pressure.items()
                         if k[0] != INITIAL + timedelta(hours=138)}
    else:
        data.pressure = {k: v for k, v in data.pressure.items() if k[2] != "hgt"}
    with pytest.raises(GsmProcessingError) as error:
        client.prepare_run(selected, req, prepared_data=data)
    assert not isinstance(error.value, (GsmCoverageError, MsmError))


def test_hgt_exclusion_and_missing_value_remain_distinct(case):
    client, req, _, selected, forecast = case
    data = GsmPreparedData.from_forecast(forecast)
    q = AloftQuery(31.85, 131.4375, req.valid_times[-1], 19000)
    prepared = client.prepare_run(selected, req, prepared_data=data)
    assert prepared.check_altitude_coverage(q).reason_code == "ALTITUDE_OUTSIDE_HGT_RANGE"
    next(v[0] for k, v in data.pressure.items() if k[2] == "hgt" and
         k[0] == INITIAL + timedelta(hours=138))[0, 0] = np.nan
    restored = GsmPreparedData.from_bytes(data.to_bytes())
    assert client.prepare_run(selected, req, prepared_data=restored).check_altitude_coverage(q).reason_code == "SOURCE_VALUE_UNAVAILABLE"


@pytest.mark.parametrize("damage", ["hash", "foreign_run", "level", "time", "grid", "dtype"])
def test_invalid_data_is_gsm_integrity_failure(case, damage):
    data = GsmPreparedData.from_forecast(case[-1])
    key = next(iter(data.pressure))
    if damage == "hash":
        data.source_hashes[next(iter(data.source_hashes))] = "bad"
    elif damage == "foreign_run":
        data.selection = replace(data.selection, run_utc=INITIAL + timedelta(hours=6))
    elif damage == "level":
        data.pressure[key[0], 123, key[2]] = data.pressure.pop(key)
    elif damage == "time":
        # FH135 is a surface time after FH132, but not a pressure time.
        data.pressure[INITIAL + timedelta(hours=135), key[1], key[2]] = data.pressure.pop(key)
    elif damage == "grid":
        data.pressure[key][1][0, 0] = 70
    else:
        data.pressure[key] = (data.pressure[key][0].astype(object), *data.pressure[key][1:])
    with pytest.raises(GsmCacheIntegrityError) as error:
        data.to_bytes()
    assert not isinstance(error.value, (GsmCoverageError, MsmError))


@pytest.mark.parametrize("damage", ["truncated", "sha", "expanded", "npy_shape"])
def test_corrupt_archive_rejected_as_gsm_integrity(case, damage, monkeypatch):
    payload = GsmPreparedData.from_forecast(case[-1]).to_bytes()
    if damage == "sha":
        with pytest.raises(GsmCacheIntegrityError):
            GsmPreparedData.from_bytes(payload, expected_sha256="0" * 64)
        return
    if damage == "truncated":
        payload = payload[:-20]
    elif damage == "expanded":
        payload = bytearray(payload)
        with ZipFile(BytesIO(payload)) as archive:
            central = archive.start_dir
        struct.pack_into("<I", payload, central + 24, 32 * 1024**2 + 1)
    else:
        # Keep ZIP sizes consistent but forge an allocation in a tiny NPY header.
        out = BytesIO()
        with ZipFile(BytesIO(payload)) as archive, ZipFile(out, "w") as forged:
            for name in archive.namelist():
                value = archive.read(name)
                if name == "r0.npy":
                    header = BytesIO()
                    np.lib.format.write_array_header_1_0(header, {
                        "descr": "<f8", "fortran_order": False, "shape": (2**40,),
                    })
                    value = header.getvalue()
                forged.writestr(name, value)
        payload = out.getvalue()
    monkeypatch.setattr(np, "load", lambda *a, **kw: pytest.fail("unsafe allocation"))
    with pytest.raises(GsmCacheIntegrityError):
        GsmPreparedData.from_bytes(bytes(payload))
