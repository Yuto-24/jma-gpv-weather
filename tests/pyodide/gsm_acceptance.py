"""Appended to acceptance.py; run identically in Chromium and Node Pyodide."""
from jma_gpv_weather import GsmClient, GsmPreparedData
from jma_gpv_weather.errors import GsmCacheIntegrityError, GsmProcessingError, GsmRunUnavailableError


def gsm_acceptance():
    initial = datetime(2026, 9, 12, tzinfo=timezone.utc)
    req = ForecastRequirements(tuple(initial + timedelta(hours=h) for h in (13.5, 133.5)),
        frozenset({WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
                   WeatherVariable.SURFACE_TEMPERATURE}))
    selected = RunId(initial)

    class AcquiredSource:
        def directory_url(self, day):
            return f"https://gsm-fixture.invalid/{day:%Y/%m/%d}"

        def read_listing(self, url):
            raise AssertionError("Listing already acquired")

        def download(self, remote, destination):
            raise AssertionError("Data already prepared")

    case = json.loads(Path("/case/gsm-case.json").read_text())
    payload = Path("/case/gsm-prepared.npz").read_bytes()
    data = GsmPreparedData.from_bytes(payload, expected_sha256=case["sha256"])
    client = GsmClient("/no-cache/gsm", Bounds(31.8, 31.9, 131.375, 131.5), source=AcquiredSource())
    runs = client.discover_runs(req, listings=case["listings"])
    forecast = client.prepare_run(selected, req, available_runs=runs, prepared_data=data)
    queries = tuple(query for time in req.valid_times for query in (
        AloftQuery(31.85, 131.4375, time, 1350),
        AloftTemperatureQuery(31.85, 131.4375, time, 1350),
        SurfaceTemperatureQuery(31.85, 131.4375, time),
    ))
    high = AloftQuery(31.85, 131.4375, req.valid_times[-1], 19000)
    result = json.loads(json.dumps({
        "status": asdict(client.resolve_run(req, selected, available_runs=runs)),
        "coverage": asdict(client.check_coverage(req, run=selected,
                            points=(CoveragePoint(31.85, 131.4375, 1350),))),
        "queries": [asdict(forecast.query(query)) for query in queries],
        "altitude": [asdict(forecast.check_altitude_coverage(query)) for query in (queries[0], high)],
    }, default=str))
    compare(result, case["expected"])
    assert not Path("/no-cache").exists()
    warm = GsmPreparedData.from_bytes(data.to_bytes())
    assert client.prepare_run(selected, req, prepared_data=warm).query(queries[0]) == forecast.query(queries[0])
    narrow = ForecastRequirements((req.valid_times[0],), frozenset({WeatherVariable.SURFACE_TEMPERATURE}))
    narrowed = client.prepare_run(selected, narrow, prepared_data=data)
    assert narrowed.query(queries[2]).values == forecast.query(queries[2]).values
    assert len(narrowed.selection.files) == 1
    assert narrowed.query(queries[2]).provenance.source_hashes == {
        remote.url: data.source_hashes[remote.url] for remote in narrowed.selection.files
    }
    for action, error in (
        (lambda: GsmPreparedData.from_bytes(payload[:-20]), GsmCacheIntegrityError),
        (lambda: GsmPreparedData.from_bytes(payload, expected_sha256="0" * 64), GsmCacheIntegrityError),
        (lambda: GsmPreparedData.from_bytes(Path("/case/prepared.npz").read_bytes()), GsmCacheIntegrityError),
        (lambda: client.prepare_run(RunId(initial + timedelta(hours=6)), req, prepared_data=data), GsmRunUnavailableError),
    ):
        try:
            action()
        except error:
            pass
        else:
            raise AssertionError(f"Expected {error.__name__}")
    assert forecast.check_altitude_coverage(high).reason_code == "ALTITUDE_OUTSIDE_HGT_RANGE"
    for key, value in data.pressure.items():
        if key[2] == "hgt" and key[0] == initial + timedelta(hours=138):
            value[0][0, 0] = float("nan")
            break
    assert client.prepare_run(selected, req, prepared_data=data).check_altitude_coverage(high).reason_code == "SOURCE_VALUE_UNAVAILABLE"
    data.surface.clear()
    try:
        client.prepare_run(selected, req, prepared_data=data)
    except GsmProcessingError:
        pass
    else:
        raise AssertionError("GsmProcessingError expected")
    return {"status": "passed", "queries": len(queries), "payload_bytes": len(payload)}


acceptance_result = json.dumps({"msm": json.loads(acceptance_result), "gsm": gsm_acceptance()})
