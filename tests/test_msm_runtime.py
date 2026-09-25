from dataclasses import replace
from datetime import timedelta
import hashlib
from io import BytesIO
import json
import sys
import struct
from zipfile import ZipFile, ZIP_DEFLATED
import zlib
from unittest.mock import patch

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
    assert prepared.query(queries()[1]).values == forecast.query(queries()[1]).values
    assert prepared.query(queries()[1]).provenance.source_urls == tuple(
        remote.url for remote in data.selection.files if remote.kind == "L-pall"
    )
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
    if damage == "encrypted":
        assert isinstance(error.value.__cause__, RuntimeError)
    else:
        assert "compression method" in str(error.value)


@pytest.mark.parametrize("method", [12, 14])  # ZIP_BZIP2 / ZIP_LZMA
def test_unbounded_zip_codecs_rejected_before_member_open(case, monkeypatch, method):
    payload = MsmPreparedData.from_forecast(case[-1]).to_bytes()
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", method) as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
    monkeypatch.setattr(ZipFile, "open", lambda *a, **kw: pytest.fail("Unbounded codec opened"))
    with pytest.raises(CacheIntegrityError, match="compression method"):
        MsmPreparedData.from_bytes(output.getvalue())


@pytest.mark.parametrize("variable,query_index,kind", [
    (WeatherVariable.ALOFT_TEMPERATURE, 1, "L-pall"),
    (WeatherVariable.SURFACE_TEMPERATURE, 2, "Lsurf"),
])
def test_narrow_requirements_match_desktop_provenance(case, variable, query_index, kind):
    client, req, _, selected, forecast = case
    data = MsmPreparedData.from_forecast(forecast)
    narrow = replace(req, variables=frozenset({variable}))
    runs = client.discover_runs(narrow)
    decoded = (data.surface, {}) if kind == "Lsurf" else ({}, data.pressure)
    with patch("jma_gpv_weather.grib.read_grib_records", return_value=decoded):
        desktop = client.prepare_run(selected, narrow, available_runs=runs)
    for available in (None, runs):
        restored = client.prepare_run(selected, narrow, available_runs=available, prepared_data=data)
        assert restored.query(queries()[query_index]) == desktop.query(queries()[query_index])
        assert all(remote.kind == kind for remote in restored.selection.files)
        expected_urls = tuple(remote.url for remote in restored.selection.files)
        assert restored.query(queries()[query_index]).provenance.source_urls == expected_urls
        assert restored.source_hashes == {url: data.source_hashes[url] for url in expected_urls}
        other_query = queries()[1 if kind == "Lsurf" else 2]
        assert restored.query(other_query) == desktop.query(other_query)
        assert restored.query(other_query).availability == Availability.UNAVAILABLE
        roundtrip = MsmPreparedData.from_bytes(MsmPreparedData.from_forecast(restored).to_bytes())
        assert client.prepare_run(selected, narrow, prepared_data=roundtrip).query(
            queries()[query_index]
        ) == restored.query(queries()[query_index])
    assert len(data.selection.files) == len(data.source_hashes) == 2


@pytest.mark.parametrize("limit", [
    "_MAX_PAYLOAD_BYTES", "_MAX_MEMBERS", "_MAX_MEMBER_BYTES", "_MAX_TOTAL_BYTES", "_MAX_METADATA_BYTES",
])
def test_archive_budgets_reject_before_numpy_load(case, monkeypatch, limit):
    payload = MsmPreparedData.from_forecast(case[-1]).to_bytes()
    with ZipFile(BytesIO(payload)) as archive:
        sizes = [info.file_size for info in archive.infolist()]
        limits = {"_MAX_PAYLOAD_BYTES": len(payload), "_MAX_MEMBERS": len(sizes),
                  "_MAX_MEMBER_BYTES": max(sizes), "_MAX_TOTAL_BYTES": sum(sizes),
                  "_MAX_METADATA_BYTES": archive.getinfo("metadata.npy").file_size}
    monkeypatch.setattr(f"jma_gpv_weather.prepared.{limit}", limits[limit] - 1)
    monkeypatch.setattr(np, "load", lambda *a, **kw: pytest.fail("np.load called before budget rejection"))
    with pytest.raises(CacheIntegrityError, match="limit"):
        MsmPreparedData.from_bytes(payload)


def test_archive_budgets_accept_exact_limits(case, monkeypatch):
    payload = MsmPreparedData.from_forecast(case[-1]).to_bytes()
    with ZipFile(BytesIO(payload)) as archive:
        sizes = [info.file_size for info in archive.infolist()]
        limits = {"_MAX_PAYLOAD_BYTES": len(payload), "_MAX_MEMBERS": len(sizes),
                  "_MAX_MEMBER_BYTES": max(sizes), "_MAX_TOTAL_BYTES": sum(sizes),
                  "_MAX_METADATA_BYTES": archive.getinfo("metadata.npy").file_size}
    for limit, value in limits.items():
        monkeypatch.setattr(f"jma_gpv_weather.prepared.{limit}", value)
    restored = MsmPreparedData.from_bytes(payload)
    assert restored.source_hashes == case[-1].source_hashes


def test_metadata_alias_cannot_bypass_metadata_budget(case, monkeypatch):
    payload = MsmPreparedData.from_forecast(case[-1]).to_bytes()
    output, shadow = BytesIO(), BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, source.read(name))
        metadata = np.load(BytesIO(source.read("metadata.npy")), allow_pickle=False).tobytes()
        np.save(shadow, np.frombuffer(metadata + b" " * (2 * 1024**2), dtype=np.uint8), allow_pickle=False)
        target.writestr("metadata", shadow.getvalue())
    monkeypatch.setattr(np, "load", lambda *a, **kw: pytest.fail("metadata alias reached np.load"))
    with pytest.raises(CacheIntegrityError, match="noncanonical"):
        MsmPreparedData.from_bytes(output.getvalue())


@pytest.mark.parametrize("forge_count", [False, True])
def test_member_count_rejected_before_zipinfo_allocation(monkeypatch, forge_count):
    output = BytesIO()
    with ZipFile(output, "w", ZIP_DEFLATED) as archive:
        for index in range(2049):
            archive.writestr(f"r{index}.npy", b"")
    payload = bytearray(output.getvalue())
    if forge_count:
        end = payload.rfind(b"PK\x05\x06")
        struct.pack_into("<HH", payload, end + 8, 1, 1)
    monkeypatch.setattr("jma_gpv_weather.prepared.ZipFile",
                        lambda *a, **kw: pytest.fail("ZipInfo allocation before member count rejection"))
    with pytest.raises(CacheIntegrityError, match="member count limit"):
        MsmPreparedData.from_bytes(bytes(payload))


def test_narrow_preparation_excludes_unselected_file_times(case):
    client, req, _, selected, forecast = case
    data = MsmPreparedData.from_forecast(forecast)
    original = next(remote for remote in data.selection.files if remote.kind == "Lsurf")
    extra = replace(original, name=original.name.replace("00-15", "16-33"),
                    url=original.url.replace("00-15", "16-33"), first_hour=16, last_hour=33)
    data.selection = replace(data.selection, files=(*data.selection.files, extra))
    data.source_hashes[extra.url] = "a" * 64
    key = (selected.initial_time_utc + timedelta(hours=18), 2, "tmp_surface")
    data.surface[key] = next(iter(data.surface.values()))
    restored = client.prepare_run(selected, req, prepared_data=data)
    assert key not in restored.surface and key in data.surface
    assert extra not in restored.selection.files and extra.url not in restored.source_hashes
    MsmPreparedData.from_forecast(restored).validate()


@pytest.mark.parametrize("forgery", ["shape", "header_length"])
def test_forged_npy_header_rejected_before_numpy_load(case, monkeypatch, forgery):
    payload = MsmPreparedData.from_forecast(case[-1]).to_bytes()
    header = BytesIO()
    if forgery == "shape":
        np.lib.format.write_array_header_1_0(header, {
            "descr": "<f8", "fortran_order": False, "shape": (2**40,),
        })
    else:
        header.write(np.lib.format.magic(2, 0) + struct.pack("<I", 2**32 - 1))
    output = BytesIO()
    with ZipFile(BytesIO(payload)) as source, ZipFile(output, "w", ZIP_DEFLATED) as target:
        for name in source.namelist():
            target.writestr(name, header.getvalue() if name == "r0.npy" else source.read(name))
    monkeypatch.setattr(np, "load", lambda *a, **kw: pytest.fail("unbounded NPY allocation attempted"))
    with pytest.raises(CacheIntegrityError):
        MsmPreparedData.from_bytes(output.getvalue())
