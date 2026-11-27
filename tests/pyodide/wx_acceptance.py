"""Appended to existing acceptance cases in both Chromium and Node Pyodide."""
from jma_gpv_weather import CloudCoverQuery, CloudLayer, HumidityProfileQuery


def wx_acceptance():
    initial = datetime(2026, 9, 12, tzinfo=timezone.utc)
    valid = initial + timedelta(hours=1.5)
    req = ForecastRequirements((valid,), frozenset({
        WeatherVariable.PRESSURE_RELATIVE_HUMIDITY, WeatherVariable.CLOUD_COVER,
    }))
    queries = (HumidityProfileQuery(30.4, 130.7, valid),
               *(CloudCoverQuery(30.4, 130.7, valid, layer) for layer in CloudLayer),
               HumidityProfileQuery(30.4, 130.7, valid, (300, 250)),
               CloudCoverQuery(32, 130.7, valid),
               CloudCoverQuery(30.4, 130.7, initial + timedelta(hours=9)))
    result = {}
    for model, cls, codec in (("msm", MsmClient, MsmPreparedData), ("gsm_japan", GsmClient, GsmPreparedData)):
        case = json.loads(Path(f"/case/wx-{model}.json").read_text())
        payload = Path(f"/case/wx-{model}.npz").read_bytes()
        data = codec.from_bytes(payload, expected_sha256=case["sha256"])
        client = cls("/no-cache/wx")
        forecast = client.prepare_run(RunId(initial), req, prepared_data=data)
        actual = json.loads(json.dumps([asdict(forecast.query(q)) for q in queries], default=str))
        compare(actual, case["expected"])
        # Native modules, cache and networking remain forbidden by acceptance.py.
        assert not Path("/no-cache").exists()
        warm = client.prepare_run(RunId(initial), req, prepared_data=codec.from_bytes(data.to_bytes()))
        assert warm.query(queries[0]) == forecast.query(queries[0])
        assert actual[0]["levels"][0]["reason_code"] == "BELOW_MODEL_SURFACE"
        # NaN is preserved across the actual WASM codec and never becomes 0%.
        for key, record in data.pressure.items():
            if key[2] == "rh":
                record[0][:] = float("nan")
        missing = client.prepare_run(RunId(initial), req, prepared_data=codec.from_bytes(data.to_bytes()))
        assert missing.query(queries[0]).levels[5].reason_code == "MISSING_SOURCE_VALUE"
        result[model] = {"queries": len(queries), "payload_bytes": len(payload)}
    return result


acceptance_result += "; WX: " + json.dumps(wx_acceptance(), sort_keys=True)
