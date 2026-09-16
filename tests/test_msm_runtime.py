from dataclasses import replace
from datetime import timedelta
import hashlib
from io import BytesIO
import json
import sys
import struct
from zipfile import ZipFile, ZIP_DEFLATED
import zlib

import numpy as np
import pytest

from jma_gpv_weather import (
    Availability, MsmClient, MsmPreparedData, RunId, WeatherVariable,
)
from jma_gpv_weather.errors import (
    CacheIntegrityError, MissingVariableError, MsmError, NoCompatibleRunError,
    SelectedRunCoverageError,
)
from runtime_case import BOUNDS, INITIAL, desktop_case, queries, results


@pytest.fixture
def case(tmp_path):
    return desktop_case(tmp_path / "desktop")


def test_desktop_cache_and_portable_runtime_agree(case, tmp_path, monkeypatch):
    client, req, runs, selected, forecast = case
    expected = results(*case)
    # Analytical checks ensure this is not only two copies of the same mistake.
    assert forecast.query(queries()[0]).values["u_ms"] == pytest.approx(15.9)
    assert forecast.query(queries()[1]).values["temperature_k"] == pytest.approx(286.9)
    assert forecast.query(queries()[2]).values["temperature_k"] == pytest.approx(294.4)
    assert expected["status"]["update_available"]

    def forbidden(*args, **kwargs):
        pytest.fail("runtime path must not use network, disk cache or decoder")

    monkeypatch.setattr("jma_gpv_weather.grib.read_grib_records", forbidden)
    warm = client.prepare_run(selected, req, available_runs=runs)
    assert results(client, req, runs, selected, warm) == expected
    data = MsmPreparedData.from_forecast(warm)
    payload = data.to_bytes()
    restored = MsmPreparedData.from_bytes(payload, expected_sha256=hashlib.sha256(payload).hexdigest())
    # Runtime accepts fetched listing text, still parsed/selected by MSM code.
    listings = {url: client.source.read_listing(url) for url in client.listing_urls(req)}
    monkeypatch.setattr("jma_gpv_weather.msm.client.cached_listing", forbidden)
    monkeypatch.setattr("jma_gpv_weather.cache.acquire_files", forbidden)
    monkeypatch.setattr("jma_gpv_weather.normalized.prepare_records", forbidden)
    for module in ("pygrib", "fcntl", "xarray", "h5netcdf", "h5py"):
        monkeypatch.setitem(sys.modules, module, None)
    runtime = MsmClient(tmp_path / "must-not-exist", BOUNDS, source=client.source)
    acquired_runs = runtime.discover_runs(req, listings=listings)
    assert acquired_runs == runs
    prepared = runtime.prepare_run(selected, req, available_runs=acquired_runs, prepared_data=restored)
    assert results(runtime, req, acquired_runs, selected, prepared) == expected
    assert not runtime.cache_dir.exists()
    # Offline preparation also works without listing or discovery.
    assert runtime.prepare_run(selected, req, prepared_data=restored).query(queries()[0]) == forecast.query(queries()[0])


def test_supplied_listing_failures_are_not_coverage(case):
    client, req, *_ = case
    listings = {url: "" for url in client.listing_urls(req)}
    with pytest.raises(NoCompatibleRunError):
        client.discover_runs(req, listings=listings)
    del listings[next(iter(listings))]
    with pytest.raises(MsmError, match="acquired listing") as error:
        client.discover_runs(req, listings=listings)
    assert not isinstance(error.value, NoCompatibleRunError)


def test_malformed_acquired_filename_is_processing_failure(case):
    client, req, *_ = case
    listings = {url: "" for url in client.listing_urls(req)}
    listings[next(iter(listings))] = "Z__C_RJTD_20269999000000_MSM_GPV_Rjp_Lsurf_FH00-15_grib2.bin"
    with pytest.raises(MsmError, match="Invalid acquired listing") as error:
        client.discover_runs(req, listings=listings)
    assert not isinstance(error.value, NoCompatibleRunError)
    assert isinstance(error.value.__cause__, ValueError)


def test_fixed_run_is_never_replaced_or_downloaded(case, monkeypatch):
    client, req, runs, selected, forecast = case
    data = MsmPreparedData.from_forecast(forecast)
    monkeypatch.setattr(client, "discover_runs", lambda *a: pytest.fail("unexpected discovery"))
    with pytest.raises(SelectedRunCoverageError):
        client.prepare_run(RunId(INITIAL), req, prepared_data=data)
    with pytest.raises(SelectedRunCoverageError):
        client.prepare_run(selected, req, available_runs=(), prepared_data=data)
    with pytest.raises(CacheIntegrityError):
        client.prepare_run(RunId(INITIAL), req, available_runs=runs, prepared_data=data)
    later = replace(req, valid_times=(INITIAL + timedelta(days=2),))
    with pytest.raises(SelectedRunCoverageError):
        client.prepare_run(selected, later, prepared_data=data)


def test_missing_fields_remain_missing_variable_error(case):
    client, req, runs, selected, forecast = case
    data = MsmPreparedData.from_forecast(forecast)
    data.pressure.pop(next(k for k in data.pressure if k[2] == "tmp"))
    with pytest.raises(MissingVariableError):
        client.prepare_run(selected, req, prepared_data=data)
    data = MsmPreparedData.from_forecast(forecast)
    data.surface.clear()
    with pytest.raises(MissingVariableError):
        client.prepare_run(selected, req, prepared_data=data)


def test_temperature_only_and_missing_values_keep_semantics(case):
    client, req, runs, selected, forecast = case
    data = MsmPreparedData.from_forecast(forecast)
    data.pressure = {k: v for k, v in data.pressure.items() if k[2] in ("hgt", "tmp")}
    temperature_req = replace(req, variables=frozenset({WeatherVariable.ALOFT_TEMPERATURE}))
    prepared = client.prepare_run(selected, temperature_req, prepared_data=data)
    assert prepared.query(queries()[1]) == forecast.query(queries()[1])
    assert prepared.check_altitude_coverage(queries()[1]).availability == Availability.AVAILABLE
    data = MsmPreparedData.from_forecast(forecast)
    key = next(k for k in data.pressure if k[2] == "tmp")
    data.pressure[key][0][:] = np.nan
    restored = MsmPreparedData.from_bytes(data.to_bytes())
    prepared = client.prepare_run(selected, req, prepared_data=restored)
    assert prepared.check_altitude_coverage(queries()[-1]).reason_code == "SOURCE_VALUE_UNAVAILABLE"
    assert forecast.check_altitude_coverage(queries()[-1]).reason_code == "ALTITUDE_OUTSIDE_HGT_RANGE"


@pytest.mark.parametrize("damage", ["hash", "run", "model", "shape", "grid", "time", "level", "dtype", "masked"])
def test_invalid_injected_data_is_processing_failure(case, damage):
    client, req, runs, selected, forecast = case
    data = MsmPreparedData.from_forecast(forecast)
    key = next(iter(data.pressure))
    value, lat, lon = data.pressure[key]
    if damage == "hash":
        data.source_hashes.clear()
    elif damage == "run":
        data.selection = replace(data.selection, run_utc=INITIAL)
    elif damage == "model":
        first, *rest = data.selection.files
        data.selection = replace(data.selection, files=(replace(first, name=first.name.replace("MSM", "GSM")), *rest))
    elif damage == "shape":
        data.pressure[key] = (value[:1], lat, lon)
    elif damage == "grid":
        data.pressure[key] = (value, lat + 0.01, lon)
    elif damage == "time":
        data.pressure[(key[0].replace(tzinfo=None), *key[1:])] = data.pressure.pop(key)
    elif damage == "level":
        data.pressure[(key[0], 400, key[2])] = data.pressure.pop(key)
    elif damage == "dtype":
        data.pressure[key] = (value.astype(object), lat, lon)
    elif damage == "masked":
        data.pressure[key] = (np.ma.array(value, mask=True), lat, lon)
    with pytest.raises(CacheIntegrityError):
        client.prepare_run(selected, req, prepared_data=data)


def test_payload_hash_truncation_and_schema_validation(case):
    data = MsmPreparedData.from_forecast(case[-1])
    payload = data.to_bytes()
    with pytest.raises(CacheIntegrityError, match="SHA-256"):
        MsmPreparedData.from_bytes(payload, expected_sha256="0" * 64)
    with pytest.raises(CacheIntegrityError):
        MsmPreparedData.from_bytes(payload[:-20])
    with np.load(BytesIO(payload), allow_pickle=False) as archive:
        arrays = dict(archive)
    metadata = json.loads(arrays["metadata"].tobytes())
    metadata["model"] = "GSM_JAPAN"
    arrays["metadata"] = np.frombuffer(json.dumps(metadata).encode(), dtype=np.uint8)
    output = BytesIO()
    np.savez_compressed(output, **arrays)
    with pytest.raises(CacheIntegrityError):
        MsmPreparedData.from_bytes(output.getvalue())


def test_export_is_detached_and_rejects_other_models(case):
    forecast = case[-1]
    data = MsmPreparedData.from_forecast(forecast)
    key = next(iter(data.pressure))
    before = forecast.pressure[key][0].copy()
    data.pressure[key][0][:] = -123
    np.testing.assert_array_equal(forecast.pressure[key][0], before)
    with pytest.raises(TypeError):
        MsmPreparedData.from_forecast(object())


def test_corrupt_deflate_member_is_cache_integrity_error(case):
    payload = bytearray(MsmPreparedData.from_forecast(case[-1]).to_bytes())
    with ZipFile(BytesIO(payload)) as archive:
        member = archive.getinfo("metadata.npy")
        assert member.compress_type == ZIP_DEFLATED
    name_length, extra_length = struct.unpack_from("<HH", payload, member.header_offset + 26)
    start = member.header_offset + 30 + name_length + extra_length
    # DEFLATE BTYPE=3 is invalid; fail in the decompressor before the ZIP CRC.
    payload[start] = (payload[start] & 0xf8) | 0x07
    with pytest.raises(CacheIntegrityError) as error:
        MsmPreparedData.from_bytes(bytes(payload))
    assert isinstance(error.value.__cause__, zlib.error)


def test_unsigned_grid_cannot_hide_non_monotonic_coordinates(case):
    data = MsmPreparedData.from_forecast(case[-1])
    lon = np.tile(np.array([130, 131, 130], dtype=np.uint8), (2, 1))
    data.pressure = {key: (values, lat, lon)
                     for key, (values, lat, _) in data.pressure.items()}
    with pytest.raises(CacheIntegrityError, match="non-monotonic grid"):
        data.validate()


@pytest.mark.parametrize("damage", ["unsupported_compression", "encrypted"])
def test_unsupported_zip_features_are_cache_integrity_errors(case, damage):
    payload = bytearray(MsmPreparedData.from_forecast(case[-1]).to_bytes())
    with ZipFile(BytesIO(payload)) as archive:
        local = archive.getinfo("metadata.npy").header_offset
        central = archive.start_dir
    while payload[central:central + 4] == b"PK\x01\x02":
        name_length, extra_length, comment_length = struct.unpack_from("<HHH", payload, central + 28)
        if payload[central + 46:central + 46 + name_length] == b"metadata.npy":
            break
        central += 46 + name_length + extra_length + comment_length
    else:
        pytest.fail("metadata central directory entry missing")
    if damage == "unsupported_compression":
        struct.pack_into("<H", payload, local + 8, 99)
        struct.pack_into("<H", payload, central + 10, 99)
    else:
        for offset in (local + 6, central + 8):
            flags, = struct.unpack_from("<H", payload, offset)
            struct.pack_into("<H", payload, offset, flags | 1)
    with pytest.raises(CacheIntegrityError) as error:
        MsmPreparedData.from_bytes(bytes(payload))
    assert isinstance(error.value.__cause__, RuntimeError)
