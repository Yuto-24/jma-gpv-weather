"""Deterministic desktop producer and shared public-API runtime acceptance case.

The GRIB decode is synthetic; acquisition, NetCDF preparation/cache and every
downstream calculation use production code. No external weather network is used.
"""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Bounds, CoveragePoint, ForecastRequirements,
    MsmClient, MsmPreparedData, RunId, SurfaceTemperatureQuery, SurfaceWindQuery,
    WeatherVariable,
)
from jma_gpv_weather.msm.spec import LEVELS_HPA

UTC = timezone.utc
INITIAL = datetime(2026, 7, 28, tzinfo=UTC)
VALID = INITIAL + timedelta(hours=1, minutes=30)
BOUNDS = Bounds(30, 31, 130, 131)


def requirements():
    return ForecastRequirements((VALID,), frozenset({
        WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
        WeatherVariable.SURFACE_TEMPERATURE, WeatherVariable.SURFACE_WIND,
    }))


class FixtureSource:
    def directory_url(self, day):
        return f"https://fixture.invalid/{day:%Y/%m/%d}"

    def read_listing(self, url):
        names = []
        for run in (INITIAL, INITIAL - timedelta(hours=3)):
            if url == self.directory_url(run.date()):
                for kind in ("Lsurf", "L-pall"):
                    names.append(f"Z__C_RJTD_{run:%Y%m%d%H%M%S}_MSM_GPV_Rjp_{kind}_FH00-15_grib2.bin")
        return '\n'.join(f'<a href="{name}">{name}</a>' for name in names)

    def download(self, remote, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"GRIB" + remote.name.encode() + b"7777")
        return destination


def records():
    lat, lon = np.meshgrid([31., 30.], [130., 130.5, 131.], indexing="ij")
    offset = (lat - 30) * 2 + (lon - 130) * 3
    surface, pressure = {}, {}
    for hour in (1, 2):
        valid = INITIAL + timedelta(hours=hour)
        for level, name, base in ((2, "tmp_surface", 290.), (10, "u", 5.), (10, "v", -2.)):
            surface[valid, level, name] = (base + offset + hour, lat, lon)
    for hour in (0, 3):
        valid = INITIAL + timedelta(hours=hour)
        for index, level in enumerate(LEVELS_HPA):
            hgt = 100 + index * 600 + offset
            fields = {"hgt": hgt, "u": 10 + hgt / 1000 + offset + hour,
                      "v": -5 + hgt / 2000 - offset + hour,
                      "tmp": 290 - hgt / 200 + offset + hour}
            for name, values in fields.items():
                pressure[valid, level, name] = (values, lat, lon)
    return surface, pressure


def desktop_case(cache_dir):
    source = FixtureSource()
    client = MsmClient(cache_dir, BOUNDS, source=source)
    req = requirements()
    runs = client.discover_runs(req)
    selected = RunId(INITIAL - timedelta(hours=3))
    with patch("jma_gpv_weather.grib.read_grib_records", return_value=records()):
        forecast = client.prepare_run(selected, req, available_runs=runs)
    return client, req, runs, selected, forecast


def queries():
    return (
        AloftQuery(30.4, 130.7, VALID, 1500),
        AloftTemperatureQuery(30.4, 130.7, VALID, 1500),
        SurfaceTemperatureQuery(30.4, 130.7, VALID),
        SurfaceWindQuery(30.4, 130.7, VALID),
        AloftQuery(30.4, 130.7, VALID, 9000),
    )


def results(client, req, runs, selected, forecast):
    return json.loads(json.dumps({
        "status": asdict(client.resolve_run(req, selected, available_runs=runs)),
        "coverage": asdict(client.check_coverage(req, points=(CoveragePoint(30.4, 130.7),),
                                                 run=selected, as_of=INITIAL)),
        "queries": [asdict(forecast.query(query)) for query in queries()],
        "altitude": [asdict(forecast.check_altitude_coverage(query)) for query in (queries()[0], queries()[-1])],
    }, default=str))


def write_case(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    client, req, runs, selected, forecast = desktop_case(directory / "desktop-cache")
    payload = MsmPreparedData.from_forecast(forecast).to_bytes()
    (directory / "prepared.npz").write_bytes(payload)
    (directory / "case.json").write_text(json.dumps({
        "sha256": hashlib.sha256(payload).hexdigest(),
        "listings": {url: client.source.read_listing(url) for url in client.listing_urls(req)},
        "expected": results(client, req, runs, selected, forecast),
    }, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    import sys
    write_case(sys.argv[1])
    from gsm_runtime_case import write_case as write_gsm_case
    write_gsm_case(sys.argv[1])
