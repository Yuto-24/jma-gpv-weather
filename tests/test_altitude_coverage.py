"""Post-prepare range exclusion must never hide a source/processing failure."""
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Availability, CoveragePoint, CoverageState,
    ForecastRequirements, GsmClient, MsmClient, SurfaceTemperatureQuery, WeatherVariable,
)
from jma_gpv_weather.errors import GsmProcessingError, InvalidQueryError
from jma_gpv_weather.gsm.dataset import PreparedGsmForecast
from jma_gpv_weather.gsm.spec import LEVELS_HPA as GSM_LEVELS
from jma_gpv_weather.models import RunSelection
from jma_gpv_weather.msm.dataset import PreparedForecast
from jma_gpv_weather.msm.spec import LEVELS_HPA as MSM_LEVELS

RUN = datetime(2026, 9, 12, tzinfo=timezone.utc)
AFTER = RUN + timedelta(hours=3)
QUERY = AloftQuery(30.5, 130.5, RUN + timedelta(hours=1.5), 550)
VARIABLES = frozenset({WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE})


@pytest.fixture(params=['MSM', 'GSM'])
def prepared(request):
    levels = MSM_LEVELS if request.param == 'MSM' else GSM_LEVELS
    lat = np.array([[31., 31.], [30., 30.]])
    lon = np.array([[130., 131.], [130., 131.]])
    pressure = {}
    for valid in (RUN, AFTER):
        for i, level in enumerate(levels):
            height = 100. + 100*i
            for name, value in [('hgt', height), ('u', height/1000 + 2), ('v', 1.), ('tmp', 290-height/1000)]:
                pressure[valid, level, name] = (np.full((2, 2), value), lat, lon)
    selection = RunSelection(RUN, ())
    if request.param == 'MSM':
        return PreparedForecast(selection, {}, pressure, {})
    requirements = ForecastRequirements((RUN, QUERY.valid_time, AFTER), VARIABLES)
    return PreparedGsmForecast(selection, {}, pressure, {}, requirements)


def assert_covered(prepared, query=QUERY):
    result = prepared.check_altitude_coverage(query)
    assert result.kind == 'altitude_coverage'
    assert result.availability == Availability.AVAILABLE and result.reason_code is None
    return result


def assert_outside(prepared, query):
    result = prepared.check_altitude_coverage(query)
    assert result.availability == Availability.UNAVAILABLE
    assert result.reason_code == 'ALTITUDE_OUTSIDE_HGT_RANGE'
    return result


def assert_unknown(prepared, query=QUERY):
    result = prepared.check_altitude_coverage(query)
    assert result.availability == Availability.UNAVAILABLE
    assert result.reason_code == 'SOURCE_VALUE_UNAVAILABLE'
    return result


def test_inside_all_endpoints_corners_and_existing_results_unchanged(prepared):
    before = asdict(prepared.query(QUERY))
    pressure_before = {k: tuple(a.copy() for a in arrays) for k, arrays in prepared.pressure.items()}
    result = assert_covered(prepared)
    assert len(result.provenance.trace['columns']) == 8
    assert result.provenance.initial_time_utc == RUN
    assert asdict(prepared.query(QUERY)) == before
    assert before['values']['u_ms'] == pytest.approx(2.55)
    assert before['values']['temperature_k'] == pytest.approx(289.45)
    for k, arrays in prepared.pressure.items():
        for actual, expected in zip(arrays, pressure_before[k]):
            np.testing.assert_array_equal(actual, expected)


@pytest.mark.parametrize('index', [0, 4, -1])
def test_exact_hgt_is_inside(prepared, index):
    height = prepared.pressure[RUN, prepared.pressure_levels[index], 'hgt'][0][0, 0]
    assert_covered(prepared, replace(QUERY, altitude_msl_m=float(height)))


@pytest.mark.parametrize('edge,offset', [(0, -.001), (-1, .001)])
def test_outside_lowest_or_highest(prepared, edge, offset):
    height = prepared.pressure[RUN, prepared.pressure_levels[edge], 'hgt'][0][0, 0]
    q = replace(QUERY, altitude_msl_m=float(height)+offset)
    assert_outside(prepared, q)
    # Keep legacy query result/error contract; only the new API is the signal.
    assert prepared.query(q).reason_code == 'VERTICAL_BRACKET_UNAVAILABLE'


@pytest.mark.parametrize('valid', [RUN, AFTER])
def test_one_time_endpoint_outside(prepared, valid):
    for level in prepared.pressure_levels:
        prepared.pressure[valid, level, 'hgt'][0][:] += 600
    assert_outside(prepared, QUERY)


@pytest.mark.parametrize('corner', [(0, 0), (0, 1), (1, 0), (1, 1)])
def test_one_of_four_corners_outside(prepared, corner):
    for level in prepared.pressure_levels:
        prepared.pressure[RUN, level, 'hgt'][0][corner] += 600
    assert_outside(prepared, QUERY)


@pytest.mark.parametrize('field', ['hgt', 'u', 'v', 'tmp'])
@pytest.mark.parametrize('failure', ['missing', 'nan', 'inf'])
def test_missing_or_nonfinite_field_is_unknown_even_for_outside_altitude(prepared, field, failure):
    key = (AFTER, prepared.pressure_levels[-1], field)
    if failure == 'missing':
        del prepared.pressure[key]
    else:
        prepared.pressure[key][0][1, 1] = np.nan if failure == 'nan' else np.inf
    assert_unknown(prepared)
    assert_unknown(prepared, replace(QUERY, altitude_msl_m=100000))


@pytest.mark.parametrize('bad_time', [RUN, AFTER])
def test_source_failure_dominates_range_failure_in_either_order(prepared, bad_time):
    # Every good column proves outside, but any bad column prevents a fallback signal.
    del prepared.pressure[bad_time, prepared.pressure_levels[4], 'hgt']
    assert_unknown(prepared, replace(QUERY, altitude_msl_m=-1000))


def test_whole_missing_time_endpoint_is_not_a_wider_bracket(prepared):
    for key in list(prepared.pressure):
        if key[0] == AFTER:
            record = prepared.pressure.pop(key)
            prepared.pressure[AFTER + timedelta(hours=3), key[1], key[2]] = record
    assert_unknown(prepared, replace(QUERY, altitude_msl_m=100000))


def test_only_actual_grid_points_and_times_are_required(prepared):
    # Exact time/gridpoint uses one column, not the other seven surrounding columns.
    for (valid, level, name), record in prepared.pressure.items():
        if valid == AFTER:
            record[0][:] = np.nan
        else:
            record[0][1, :] = np.nan
            record[0][0, 1] = np.nan
    result = assert_covered(prepared, replace(QUERY, latitude=31, longitude=130, valid_time=RUN))
    assert len(result.provenance.trace['columns']) == 1


def test_temperature_query_does_not_require_wind(prepared):
    prepared.pressure = {k: r for k, r in prepared.pressure.items() if k[2] in ('hgt', 'tmp')}
    q = AloftTemperatureQuery(QUERY.latitude, QUERY.longitude, QUERY.valid_time, QUERY.altitude_msl_m)
    assert_covered(prepared, q)
    assert_unknown(prepared, QUERY)
    del prepared.pressure[AFTER, prepared.pressure_levels[3], 'tmp']
    assert_unknown(prepared, q)


@pytest.mark.parametrize('corruption', ['level', 'shape', 'grid', 'nonmonotonic_hgt', 'duplicate_hgt', 'mask'])
def test_abnormal_records_do_not_become_altitude_exclusion(prepared, corruption):
    level = prepared.pressure_levels[4]
    key = (AFTER, level, 'hgt')
    values, lat, lon = prepared.pressure[key]
    if corruption == 'level':
        for name in ('hgt', 'u', 'v', 'tmp'):
            del prepared.pressure[AFTER, level, name]
    elif corruption == 'shape':
        prepared.pressure[key] = (values[:1], lat, lon)
    elif corruption == 'grid':
        prepared.pressure[key] = (values, lat+.1, lon)
    elif corruption == 'mask':
        prepared.pressure[key] = (np.ma.array(values, mask=[[False,False],[False,True]]), lat, lon)
    else:
        values[0, 0] = -100 if corruption == 'nonmonotonic_hgt' else 400
    assert_unknown(prepared, replace(QUERY, altitude_msl_m=100000))


def test_unprepared_location_and_time_are_not_altitude_exclusion(prepared):
    assert_unknown(prepared, replace(QUERY, latitude=80, altitude_msl_m=100000))
    assert_unknown(prepared, replace(QUERY, valid_time=AFTER+timedelta(hours=1), altitude_msl_m=100000))


def test_processing_exception_never_becomes_altitude_exclusion(prepared, monkeypatch):
    from jma_gpv_weather import weather
    def failed(*args):
        raise ValueError('injected processing failure')
    monkeypatch.setattr(weather, '_grid_bracket', failed)
    error = GsmProcessingError if isinstance(prepared, PreparedGsmForecast) else ValueError
    with pytest.raises(error, match='injected processing failure'):
        prepared.check_altitude_coverage(replace(QUERY, altitude_msl_m=100000))


@pytest.mark.parametrize('q', [replace(QUERY, altitude_msl_m=float('nan')),
    replace(QUERY, valid_time=QUERY.valid_time.replace(tzinfo=None)),
    SurfaceTemperatureQuery(30,130,RUN)])
def test_invalid_queries_are_errors(prepared, q):
    error = ValueError if isinstance(prepared, PreparedGsmForecast) else InvalidQueryError
    with pytest.raises(error):
        prepared.check_altitude_coverage(q)


@pytest.mark.parametrize('client', [MsmClient, GsmClient])
def test_offline_requires_hgt_is_unchanged(client):
    request = ForecastRequirements((QUERY.valid_time,), VARIABLES)
    result = client.check_coverage(request, points=(CoveragePoint(30.5,130.5,550),), as_of=RUN)
    assert result.state == CoverageState.REQUIRES_HGT and not result.outside_spec


def test_gsm_uses_132_to_138_pressure_bracket():
    from test_gsm import synthetic_records, files
    requirements = ForecastRequirements((RUN+timedelta(hours=133.5),), VARIABLES)
    times = (RUN+timedelta(hours=132), RUN+timedelta(hours=138))
    _, pressure = synthetic_records(times, GSM_LEVELS)
    prepared = PreparedGsmForecast(RunSelection(RUN, tuple(files())), {}, pressure, {}, requirements)
    q = AloftQuery(31.85,131.4375,requirements.valid_times[0],1350)
    result = assert_covered(prepared, q)
    assert {c['valid_time'] for c in result.provenance.trace['columns']} == {t.isoformat() for t in times}
    for level in GSM_LEVELS:
        prepared.pressure[times[1], level, 'hgt'][0][:] += 2000
    assert_outside(prepared, q)
