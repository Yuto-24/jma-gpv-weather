"""Desktop GSM producer spanning its FH132 time-resolution transition."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import numpy as np

from jma_gpv_weather import (
    AloftQuery, AloftTemperatureQuery, Bounds, CoveragePoint, ForecastRequirements,
    GsmClient, GsmPreparedData, RunId, SurfaceTemperatureQuery, WeatherVariable,
)
from jma_gpv_weather.gsm import spec

INITIAL = datetime(2026, 9, 12, tzinfo=timezone.utc)
BOUNDS = Bounds(31.8, 31.9, 131.375, 131.5)


def requirements():
    return ForecastRequirements(tuple(INITIAL + timedelta(hours=h) for h in (13.5, 133.5)),
        frozenset({WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
                   WeatherVariable.SURFACE_TEMPERATURE}))


class FixtureSource:
    def directory_url(self, day):
        return f"https://gsm-fixture.invalid/{day:%Y/%m/%d}"

    def read_listing(self, url):
        def fd(hour):
            return f"{hour//24:02d}{hour%24:02d}"
        names = []
        for run in (INITIAL, INITIAL + timedelta(hours=6)):
            if url != self.directory_url(run.date()):
                continue
            for kind, intervals in spec.FILE_INTERVALS.items():
                for first, last in intervals:
                    if last <= spec.horizon(run):
                        names.append(f"Z__C_RJTD_{run:%Y%m%d%H%M%S}_GSM_GPV_Rjp_Gll0p1deg_"
                                     f"{kind}_FD{fd(first)}-{fd(last)}_grib2.bin")
        return "\n".join(names)

    def download(self, remote, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"GRIB" + remote.name.encode() + b"7777")
        return destination


def records(paths, target_date, bounds, valid_times, *, pressure_levels):
    lat = np.array([[31.9, 31.9], [31.8, 31.8]])
    lon = np.array([[131.375, 131.5], [131.375, 131.5]])
    offset = (lat - 31.8) * 10 + (lon - 131.375) * 8
    surface, pressure = {}, {}
    for valid in valid_times:
        hour = (valid - INITIAL).total_seconds() / 3600
        # These are the actual GSM product schedules, not the union of both.
        if hour in spec.forecast_hours(INITIAL, "Lsurf"):
            surface[valid, 0, "tmp_surface"] = (250 + offset, lat, lon)
            surface[valid, 2, "tmp_surface"] = (280 + hour / 10 + offset, lat, lon)
            surface[valid, 10, "u"] = (np.ones_like(lat), lat, lon)
        if hour in spec.forecast_hours(INITIAL, "L-pall"):
            for level in pressure_levels:
                height = (1000 - level) * 20 + 100
                for name, value in (
                    ("hgt", np.full_like(lat, height)),
                    ("u", height / 1000 + hour / 10 + offset),
                    ("v", np.zeros_like(lat)),
                    ("tmp", 290 - height / 1000 + hour / 10 + offset),
                ):
                    pressure[valid, level, name] = (value, lat, lon)
    return surface, pressure


def desktop_case(cache_dir):
    client = GsmClient(cache_dir, BOUNDS, source=FixtureSource())
    req = requirements()
    runs = client.discover_runs(req)
    selected = RunId(INITIAL)
    with patch("jma_gpv_weather.grib.read_grib_records", side_effect=records):
        forecast = client.prepare_run(selected, req, available_runs=runs)
    return client, req, runs, selected, forecast


def queries():
    return tuple(query for time in requirements().valid_times for query in (
        AloftQuery(31.85, 131.4375, time, 1350),
        AloftTemperatureQuery(31.85, 131.4375, time, 1350),
        SurfaceTemperatureQuery(31.85, 131.4375, time),
    ))


def results(client, req, runs, selected, forecast):
    high = AloftQuery(31.85, 131.4375, req.valid_times[-1], 19000)
    return json.loads(json.dumps({
        "status": asdict(client.resolve_run(req, selected, available_runs=runs)),
        "coverage": asdict(client.check_coverage(req, run=selected,
                            points=(CoveragePoint(31.85, 131.4375, 1350),))),
        "queries": [asdict(forecast.query(query)) for query in queries()],
        "altitude": [asdict(forecast.check_altitude_coverage(query))
                     for query in (queries()[0], high)],
    }, default=str))


def write_case(directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    # Always exercise the decoded coexistence case, even when regenerated.
    with TemporaryDirectory(prefix="gsm-desktop-", dir=directory) as cache:
        case = desktop_case(Path(cache))
    client, req, runs, selected, forecast = case
    payload = GsmPreparedData.from_forecast(forecast).to_bytes()
    (directory / "gsm-prepared.npz").write_bytes(payload)
    (directory / "gsm-case.json").write_text(json.dumps({
        "sha256": hashlib.sha256(payload).hexdigest(),
        "listings": {url: client.source.read_listing(url) for url in client.listing_urls(req)},
        "expected": results(*case),
    }, sort_keys=True, indent=2) + "\n")


if __name__ == "__main__":
    import sys
    write_case(sys.argv[1])
