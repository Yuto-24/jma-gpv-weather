from dataclasses import asdict, replace
from datetime import timedelta
from io import BytesIO
import json
from types import SimpleNamespace

import numpy as np
import pytest

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Availability, CloudCoverQuery, CloudLayer,
    CoveragePoint, HumidityProfileQuery, PRESSURE_RH_LEVELS_HPA, RunId,
    SurfaceTemperatureQuery, SurfaceWindQuery, EstimatedQnhQuery, WeatherVariable,
)
from jma_gpv_weather import grib, prepared
from jma_gpv_weather.errors import CacheIntegrityError, GsmCacheIntegrityError, GsmProcessingError, MissingVariableError
from wx_runtime_case import RUN, WX, Source, components, desktop_case, queries, records, requirements


@pytest.fixture(params=("MSM", "GSM_JAPAN"))
def case(request, tmp_path):
    return request.param, desktop_case(tmp_path, request.param)


def test_native_levels_interpolation_and_provenance(case):
    model, (_, _, _, _, forecast) = case
    profile = forecast.query(queries()[0])
    assert profile.availability == Availability.AVAILABLE
    assert profile.reason_code == "PROFILE_PARTIALLY_UNAVAILABLE"
    assert profile.surface_pressure_pa == 98000
    assert len(profile.levels) == 12
    assert profile.levels[0].reason_code == "BELOW_MODEL_SURFACE"
    assert profile.levels[0].values["relative_humidity_percent"] is None
    for index, sample in enumerate(profile.levels[1:], 1):
        assert sample.availability == Availability.AVAILABLE
        assert sample.values["pressure_hpa"] == PRESSURE_RH_LEVELS_HPA[index]
        assert sample.values["relative_humidity_percent"] == pytest.approx(45.1 + index * 2)
        assert sample.values["geopotential_height_gpm"] == pytest.approx(105.1 + index * 600)
        assert sample.provenance.trace["model"] == model
        assert sample.provenance.trace["units"]["hgt"] == "gpm"
        assert [t["valid_time"] for t in sample.provenance.trace["humidity"]] == [RUN.isoformat(), (RUN + timedelta(hours=3)).isoformat()]
        assert [t["valid_time"] for t in sample.provenance.trace["surface_pressure"]] == [(RUN + timedelta(hours=h)).isoformat() for h in (1, 2)]
        assert sample.provenance.initial_time_utc == RUN
        assert set(sample.provenance.source_urls) == set(forecast.source_hashes)
    for index, layer in enumerate(CloudLayer):
        cloud = forecast.query(replace(queries()[1], layer=layer))
        assert cloud.values == {"cloud_cover_percent": pytest.approx(15.1 + index * 10), "layer": layer.value}
        assert cloud.availability == Availability.AVAILABLE
        assert "LAYER_AGGREGATE_HAS_NO_CLOUD_BASE_OR_TOP" in cloud.warnings


@pytest.mark.parametrize("kind,field,damage,expected", [
    ("pressure", "rh", "missing", "DATA_NOT_PREPARED"),
    ("pressure", "rh", "nan", "MISSING_SOURCE_VALUE"),
    ("pressure", "rh", "invalid", "INVALID_SOURCE_VALUE"),
    ("pressure", "hgt", "nan", "MISSING_SOURCE_VALUE"),
    ("surface", "sp", "nan", "MISSING_SOURCE_VALUE"),
    ("surface", "sp", "invalid", "INVALID_SOURCE_VALUE"),
])
def test_missing_endpoint_never_bridges_to_more_distant_data(case, kind, field, damage, expected):
    _, (_, _, _, _, forecast) = case
    group = getattr(forecast, kind)
    key = (RUN + timedelta(hours=3 if kind == "pressure" else 2), 850 if kind == "pressure" else 0, field)
    if damage == "missing":
        del group[key]
    else:
        values, lat, lon = group[key]
        values = values.copy()
        values[1, 1] = np.nan if damage == "nan" else (101 if field == "rh" else 0)
        group[key] = values, lat, lon
    sample = forecast.query(HumidityProfileQuery(30.4, 130.7, queries()[0].valid_time, (850,))).levels[0]
    assert sample.availability == Availability.UNAVAILABLE
    assert sample.reason_code == expected


def test_classification_coverage_prepared_area_and_level(case):
    _, (_, _, _, _, forecast) = case
    assert forecast.query(queries()[5]).levels[1].reason_code == "PRESSURE_LEVEL_UNSUPPORTED"
    assert forecast.query(queries()[6]).reason_code == "LOCATION_NOT_PREPARED"
    assert forecast.query(queries()[7]).reason_code == "DATA_NOT_PREPARED"
    assert forecast.query(replace(queries()[1], latitude=10)).reason_code == "OUTSIDE_DOMAIN"
    assert forecast.query(replace(queries()[1], valid_time=RUN - timedelta(seconds=1))).reason_code == "TIME_OUTSIDE_COVERAGE"


def test_nan_cloud_is_not_clear_sky_and_exact_endpoint_needs_no_neighbor(case):
    _, (_, _, _, _, forecast) = case
    query = CloudCoverQuery(30, 130, RUN, CloudLayer.TOTAL)
    key = RUN, 0, "cloud_total"
    values, lat, lon = forecast.surface[key]
    forecast.surface[key] = (np.zeros_like(values), lat, lon)
    assert forecast.query(query).values["cloud_cover_percent"] == 0
    forecast.surface[key] = (np.full_like(values, np.nan), lat, lon)
    assert forecast.query(query).reason_code == "MISSING_SOURCE_VALUE"
    assert forecast.query(query).values["cloud_cover_percent"] is None


def rewrite_metadata(payload, modify):
    with np.load(BytesIO(payload), allow_pickle=False) as archive:
        arrays = {name: archive[name] for name in archive.files}
    metadata = json.loads(arrays["metadata"].tobytes())
    modify(metadata)
    arrays["metadata"] = np.frombuffer(json.dumps(metadata).encode(), dtype=np.uint8)
    output = BytesIO()
    np.savez_compressed(output, **arrays)
    return output.getvalue()


def test_portable_schema2_and_legacy_schema_guard(case):
    _, (client, codec, req, _, forecast) = case
    payload = codec.from_forecast(forecast).to_bytes()
    with np.load(BytesIO(payload)) as archive:
        assert json.loads(archive["metadata"].tobytes())["schema"] == 2
    restored = codec.from_bytes(payload)
    actual = client.prepare_run(RunId(RUN), req, prepared_data=restored)
    assert [asdict(actual.query(q)) for q in queries()] == [asdict(forecast.query(q)) for q in queries()]
    for schema in (0, 1, 3, True):
        corrupt = rewrite_metadata(payload, lambda m: m.update(schema=schema))
        with pytest.raises((CacheIntegrityError, GsmCacheIntegrityError)):
            codec.from_bytes(corrupt)
    wrong_model = rewrite_metadata(payload, lambda m: m.update(model="FOREIGN"))
    with pytest.raises((CacheIntegrityError, GsmCacheIntegrityError)):
        codec.from_bytes(wrong_model)


def test_producer_checks_portable_budgets(case, monkeypatch):
    _, (_, codec, _, _, forecast) = case
    monkeypatch.setattr(prepared, "_MAX_MEMBERS", 8)
    with pytest.raises((CacheIntegrityError, GsmCacheIntegrityError), match="member count"):
        codec.from_forecast(forecast).to_bytes()


def test_warm_cache_preserves_all_nan_records_and_schema(case, monkeypatch):
    model, (client, codec, req, runs, forecast) = case
    # Rewrite an independent cache with all-NaN RH; this is data, not an absent field.
    surface, pressure = records(model)
    for key, (value, lat, lon) in list(pressure.items()):
        if key[2] == "rh":
            pressure[key] = np.full_like(value, np.nan), lat, lon
    client.cache_dir = client.cache_dir / "nan"
    monkeypatch.setattr(grib, "read_grib_records", lambda *a, **k: (surface, pressure))
    cold = client.prepare_run(RunId(RUN), req, runs)
    def unexpected(*args, **kwargs):
        pytest.fail("warm cache must not decode")
    monkeypatch.setattr(grib, "read_grib_records", unexpected)
    warm = client.prepare_run(RunId(RUN), req, runs)
    assert set(warm.pressure) == set(cold.pressure)
    assert warm.query(queries()[0]) == cold.query(queries()[0])
    assert codec.from_forecast(warm).to_bytes()


def test_wx_opt_in_and_legacy_numerical_regression(tmp_path):
    for model in ("MSM", "GSM_JAPAN"):
        legacy = {WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE, WeatherVariable.SURFACE_TEMPERATURE}
        if model == "MSM":
            legacy.update((WeatherVariable.SURFACE_WIND, WeatherVariable.ESTIMATED_QNH))
        client, codec, req, runs, baseline = desktop_case(tmp_path / model, model, requirements(variables=legacy))
        payload = codec.from_forecast(baseline).to_bytes()
        with np.load(BytesIO(payload)) as archive:
            assert json.loads(archive["metadata"].tobytes())["schema"] == 1
        old = codec.from_bytes(payload)
        with pytest.raises((MissingVariableError, GsmProcessingError)):
            client.prepare_run(RunId(RUN), requirements(), prepared_data=old)
        _, _, _, _, mixed = desktop_case(tmp_path / model, model, requirements(variables=legacy | WX))
        legacy_queries = [AloftQuery(30.4, 130.7, queries()[0].valid_time, 1500),
                          AloftTemperatureQuery(30.4, 130.7, queries()[0].valid_time, 1500),
                          SurfaceTemperatureQuery(30.4, 130.7, queries()[0].valid_time)]
        if model == "MSM":
            baseline.terrain_provider = mixed.terrain_provider = lambda *_: 100
            legacy_queries += [SurfaceWindQuery(30.4, 130.7, queries()[0].valid_time),
                               EstimatedQnhQuery(30.4, 130.7, queries()[0].valid_time, 100)]
        for query in legacy_queries:
            assert mixed.query(query) == baseline.query(query)
        # WX HGT extension does not extend legacy MSM aloft wind coverage.
        assert baseline.pressure_levels == mixed.pressure_levels


@pytest.mark.parametrize("model", ("MSM", "GSM_JAPAN"))
def test_independent_requirements_and_offline_coverage(model, tmp_path):
    cls, _, spec = components(model)
    humidity = requirements(variables={WeatherVariable.PRESSURE_RELATIVE_HUMIDITY})
    cloud = requirements(variables={WeatherVariable.CLOUD_COVER})
    point = CoveragePoint(30, 130, pressure_bracket_hpa=(400, 300))
    assert cls.check_coverage(humidity, points=(point,), as_of=RUN).pressure_levels_hpa == PRESSURE_RH_LEVELS_HPA
    assert not cls.check_coverage(humidity, points=(point,), as_of=RUN).outside_spec
    point = replace(point, pressure_bracket_hpa=(300, 250))
    assert cls.check_coverage(humidity, points=(point,), as_of=RUN).outside_spec
    for req in (humidity, cloud):
        _, _, _, runs, forecast = desktop_case(tmp_path / req.variables.__repr__(), model, req)
        if req == cloud:
            assert not forecast.pressure
            assert {f.kind for f in runs[0].files} == {"Lsurf"}
        else:
            assert {k[2] for k in forecast.pressure} == {"hgt", "rh"}
            assert {k[2] for k in forecast.surface} == {"sp"}


def test_gsm_long_range_time_brackets(tmp_path):
    req = requirements((133.5,))
    _, _, _, _, forecast = desktop_case(tmp_path, "GSM_JAPAN", req,
                                        records("GSM_JAPAN", (132, 135, 138)))
    # Keep synthetic percentages physical at long forecast hours.
    for group in (forecast.surface, forecast.pressure):
        for key, (values, lat, lon) in list(group.items()):
            if key[2] == "rh" or key[2].startswith("cloud_"):
                group[key] = np.full_like(values, 70), lat, lon
    profile = forecast.query(HumidityProfileQuery(30.4, 130.7, req.valid_times[0]))
    sample = profile.levels[5]
    assert sample.availability == Availability.AVAILABLE
    assert [t["valid_time"] for t in sample.provenance.trace["humidity"]] == [(RUN + timedelta(hours=h)).isoformat() for h in (132, 138)]
    cloud = forecast.query(CloudCoverQuery(30.4, 130.7, req.valid_times[0]))
    assert [t["valid_time"] for t in cloud.provenance.trace["samples"]] == [(RUN + timedelta(hours=h)).isoformat() for h in (132, 135)]


def test_grib_numeric_identity_is_opt_in_and_level_bounded():
    def message(category, number, kind="surface", level=0):
        return SimpleNamespace(parameterCategory=category, parameterNumber=number,
                               typeOfLevel=kind, level=level, shortName="unknown")
    for number, name in ((1, "cloud_total"), (3, "cloud_low"), (4, "cloud_middle"), (5, "cloud_high")):
        assert grib._identity(message(6, number), (1000,), WX) == (name, 0)
        assert grib._identity(message(6, number), (1000,)) is None
        assert grib._identity(message(6, number, "isobaricInhPa", 850), (1000,), WX) is None
    for level in PRESSURE_RH_LEVELS_HPA:
        assert grib._identity(message(1, 1, "isobaricInhPa", level), (1000,), WX) == ("rh", level)
        assert grib._identity(message(3, 5, "isobaricInhPa", level), (1000,), WX) == ("hgt", level)
    assert grib._identity(message(1, 1, "isobaricInhPa", 250), (1000,), WX) is None


@pytest.mark.parametrize("model", ("MSM", "GSM_JAPAN"))
def test_wind_plus_wx_retains_implicit_temperature_and_altitude_coverage(model, tmp_path):
    wind = {WeatherVariable.ALOFT_WIND}
    # Legacy GRIB decoding includes TMP even when only wind was requested.
    baseline_data = records(model, variables=wind | {WeatherVariable.ALOFT_TEMPERATURE})
    _, _, _, _, baseline = desktop_case(tmp_path / "legacy", model,
                                        requirements(variables=wind), baseline_data)
    _, _, _, _, mixed = desktop_case(tmp_path / "mixed", model,
                                     requirements(variables=wind | {WeatherVariable.CLOUD_COVER}))
    query = AloftQuery(30.4, 130.7, queries()[0].valid_time, 1500)
    assert baseline.query(query).values["temperature_k"] is not None
    assert mixed.query(query).values == baseline.query(query).values
    assert baseline.check_altitude_coverage(query).availability == Availability.AVAILABLE
    assert mixed.check_altitude_coverage(query).availability == Availability.AVAILABLE
