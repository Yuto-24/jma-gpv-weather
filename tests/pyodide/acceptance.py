"""Executed unchanged in Node and Chromium Pyodide against the desktop fixture."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import importlib.util
import json
import math
from pathlib import Path
import sys
import struct
from io import BytesIO
from zipfile import ZipFile

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Bounds, CoveragePoint, ForecastRequirements,
    MsmClient, MsmPreparedData, RunId, SurfaceTemperatureQuery, SurfaceWindQuery,
    WeatherVariable,
)
from jma_gpv_weather.errors import CacheIntegrityError, MissingVariableError, SelectedRunCoverageError


def compare(actual, expected):
    if isinstance(expected, dict):
        assert actual.keys() == expected.keys(), (actual, expected)
        for key in expected:
            compare(actual[key], expected[key])
    elif isinstance(expected, list):
        assert len(actual) == len(expected)
        for a, e in zip(actual, expected):
            compare(a, e)
    elif isinstance(expected, float):
        assert math.isclose(actual, expected, rel_tol=1e-12, abs_tol=1e-10), (actual, expected)
    else:
        assert actual == expected, (actual, expected)


def acceptance():
    assert sys.platform == "emscripten"
    unavailable = {name: importlib.util.find_spec(name) is None
                   for name in ("pygrib", "fcntl", "xarray", "h5netcdf", "h5py")}
    assert all(unavailable.values()), unavailable
    # Deny filesystem/weather networking even if a future import makes it available.
    def forbid_runtime_io(event, args):
        if event in ("socket.connect", "urllib.Request"):
            raise AssertionError(f"Unexpected weather network: {event}")
        if event in ("open", "os.mkdir") and str(args[0]).startswith("/no-cache"):
            raise AssertionError(f"Unexpected cache access: {args[0]}")
    sys.addaudithook(forbid_runtime_io)

    initial = datetime(2026, 7, 28, tzinfo=timezone.utc)
    valid = initial + timedelta(hours=1, minutes=30)
    selected = RunId(initial - timedelta(hours=3))
    req = ForecastRequirements((valid,), frozenset({
        WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
        WeatherVariable.SURFACE_TEMPERATURE, WeatherVariable.SURFACE_WIND,
    }))
    class AcquiredSource:
        def directory_url(self, day):
            return f"https://fixture.invalid/{day:%Y/%m/%d}"

        def read_listing(self, url):
            raise AssertionError("Listing already acquired")

        def download(self, remote, destination):
            raise AssertionError("Data already prepared")

    case = json.loads(Path("/case/case.json").read_text())
    payload = Path("/case/prepared.npz").read_bytes()
    data = MsmPreparedData.from_bytes(payload, expected_sha256=case["sha256"])
    client = MsmClient("/no-cache", Bounds(30, 31, 130, 131), source=AcquiredSource())
    runs = client.discover_runs(req, listings=case["listings"])
    forecast = client.prepare_run(selected, req, available_runs=runs, prepared_data=data)
    queries = (
        AloftQuery(30.4, 130.7, valid, 1500),
        AloftTemperatureQuery(30.4, 130.7, valid, 1500),
        SurfaceTemperatureQuery(30.4, 130.7, valid),
        SurfaceWindQuery(30.4, 130.7, valid),
        AloftQuery(30.4, 130.7, valid, 9000),
    )
    result = json.loads(json.dumps({
        "status": asdict(client.resolve_run(req, selected, available_runs=runs)),
        "coverage": asdict(client.check_coverage(req, points=(CoveragePoint(30.4, 130.7),),
                                                 run=selected, as_of=initial)),
        "queries": [asdict(forecast.query(query)) for query in queries],
        "altitude": [asdict(forecast.check_altitude_coverage(query)) for query in (queries[0], queries[-1])],
    }, default=str))
    compare(result, case["expected"])
    assert not Path("/no-cache").exists()
    warm = MsmPreparedData.from_bytes(data.to_bytes())
    assert client.prepare_run(selected, req, prepared_data=warm).query(queries[0]) == forecast.query(queries[0])
    narrow = ForecastRequirements((valid,), frozenset({WeatherVariable.SURFACE_TEMPERATURE}))
    narrowed = client.prepare_run(selected, narrow, prepared_data=data)
    surface_result = narrowed.query(queries[2])
    assert surface_result.values == forecast.query(queries[2]).values
    assert len(narrowed.selection.files) == 1 and narrowed.selection.files[0].kind == "Lsurf"
    urls = tuple(remote.url for remote in narrowed.selection.files)
    assert surface_result.provenance.source_urls == urls
    assert surface_result.provenance.source_hashes == {url: data.source_hashes[url] for url in urls}
    assert narrowed.query(queries[0]).availability.value == "unavailable"
    portable_narrowed = MsmPreparedData.from_bytes(MsmPreparedData.from_forecast(narrowed).to_bytes())
    assert client.prepare_run(selected, narrow, prepared_data=portable_narrowed).query(queries[2]) == surface_result
    damaged = bytearray(payload)
    with ZipFile(BytesIO(payload)) as archive:
        member = archive.getinfo("metadata.npy")
        central = archive.start_dir
    name_length, extra_length = struct.unpack_from("<HH", payload, member.header_offset + 26)
    start = member.header_offset + 30 + name_length + extra_length
    damaged[start] = (damaged[start] & 0xf8) | 0x07
    oversized = bytearray(payload)
    struct.pack_into("<I", oversized, central + 24, 32 * 1024**2 + 1)
    for action, error in (
        (lambda: MsmPreparedData.from_bytes(payload[:-20]), CacheIntegrityError),
        (lambda: MsmPreparedData.from_bytes(bytes(damaged)), CacheIntegrityError),
        (lambda: MsmPreparedData.from_bytes(bytes(oversized)), CacheIntegrityError),
        (lambda: MsmPreparedData.from_bytes(payload, expected_sha256="0" * 64), CacheIntegrityError),
        (lambda: client.prepare_run(RunId(initial), req, prepared_data=data), SelectedRunCoverageError),
    ):
        try:
            action()
        except error:
            pass
        else:
            raise AssertionError(f"Expected {error.__name__}")
    data.surface.clear()
    try:
        client.prepare_run(selected, req, prepared_data=data)
    except MissingVariableError:
        pass
    else:
        raise AssertionError("MissingVariableError expected")
    return json.dumps({"status": "passed", "python": sys.version.split()[0],
                       "platform": sys.platform, "unavailable": unavailable,
                       "queries": len(queries), "payload_bytes": len(payload)})


acceptance_result = acceptance()
