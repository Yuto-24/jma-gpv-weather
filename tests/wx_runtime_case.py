"""Deterministic WX fixture: real preparation/cache/codec/query, synthetic decoded grids."""
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
from unittest.mock import patch

import numpy as np

from jma_gpv_weather import (
    Bounds, CloudCoverQuery, CloudLayer, ForecastRequirements, GsmClient, GsmPreparedData,
    HumidityProfileQuery, MsmClient, MsmPreparedData, PRESSURE_RH_LEVELS_HPA, RunId, WeatherVariable,
)
from jma_gpv_weather.gsm import spec as gsm
from jma_gpv_weather.msm import spec as msm
from jma_gpv_weather.wx import requested_fields

RUN = datetime(2026, 9, 12, tzinfo=timezone.utc)
BOUNDS = Bounds(30, 31, 130, 131)
WX = frozenset({WeatherVariable.PRESSURE_RELATIVE_HUMIDITY, WeatherVariable.CLOUD_COVER})


def requirements(hours=(1.5,), variables=WX):
    return ForecastRequirements(tuple(RUN + timedelta(hours=h) for h in hours), frozenset(variables))


class Source:
    def __init__(self, model):
        self.model = model

    def directory_url(self, day):
        return f"https://fixture.invalid/{day:%Y/%m/%d}"

    def read_listing(self, url):
        if not url.endswith("2026/09/12"):
            return ""
        if self.model == "MSM":
            return " ".join(f"Z__C_RJTD_20260912000000_MSM_GPV_Rjp_{k}_FH00-15_grib2.bin"
                            for k in ("Lsurf", "L-pall"))
        return " ".join(f"Z__C_RJTD_20260912000000_GSM_GPV_Rjp_Gll0p1deg_{k}_FD{a//24:02d}{a%24:02d}-{b//24:02d}{b%24:02d}_grib2.bin"
                        for k, intervals in gsm.FILE_INTERVALS.items() for a, b in intervals)

    def download(self, remote, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"GRIB" + remote.name.encode() + b"7777")
        return destination


def components(model):
    return (MsmClient, MsmPreparedData, msm) if model == "MSM" else (GsmClient, GsmPreparedData, gsm)


def records(model, hours=(0, 1, 2, 3, 6), *, variables=WX):
    _, _, spec = components(model)
    pressure_lat, pressure_lon = np.meshgrid([31., 30.], [130., 131.], indexing="ij")
    surface_axis = [31., 30.5, 30.] if model == "MSM" else [31., 30.]
    surface_lat, surface_lon = np.meshgrid(surface_axis, sorted(surface_axis), indexing="ij")
    surface_lon += 100
    surface, pressure = {}, {}
    surface_fields, pressure_fields = requested_fields(variables, spec.LEVELS_HPA)
    for hour in hours:
        valid = RUN + timedelta(hours=hour)
        surface_hours = range(133) if hour <= 132 else range(135, 265, 3)
        pressure_step = 3 if hour <= 132 else 6
        if hour in surface_hours:
            offset = 2 * (surface_lat - 30) + 4 * (surface_lon - 130) + hour
            fields = {"sp": np.full(offset.shape, 98000.), "cloud_total": 10 + offset,
                      "cloud_low": 20 + offset, "cloud_middle": 30 + offset, "cloud_high": 40 + offset,
                      "tmp_surface": 290 + offset, "rh": 55 + offset, "mslp": 101000 + offset,
                      "u": 5 + offset, "v": -2 + offset}
            for name in surface_fields:
                level = 10 if name in ("u", "v") else 2 if name in ("tmp_surface", "rh") else 0
                surface[valid, level, name] = (fields[name], surface_lat, surface_lon)
        if hour % pressure_step == 0:
            offset = 2 * (pressure_lat - 30) + 4 * (pressure_lon - 130) + hour
            for level, name in pressure_fields:
                index = gsm.LEVELS_HPA.index(level)
                fields = {"hgt": 100 + index * 600 + offset, "rh": 40 + index * 2 + offset,
                          "u": 10 + index + offset, "v": -5 + index + offset,
                          "tmp": 290 - index * 4 + offset}
                pressure[valid, level, name] = (fields[name], pressure_lat, pressure_lon)
    return surface, pressure


def desktop_case(root, model, req=None, data=None):
    client_cls, codec, spec = components(model)
    req = requirements() if req is None else req
    source = Source(model)
    client = client_cls(root, BOUNDS, source=source)
    remote = spec.parse_listing(source.read_listing(source.directory_url(RUN.date())), source.directory_url(RUN.date()))
    runs = spec.select_compatible_runs(remote, req)
    if data is None:
        data = records(model, variables=req.variables)
    with patch("jma_gpv_weather.grib.read_grib_records", return_value=data):
        forecast = client.prepare_run(RunId(RUN), req, available_runs=runs)
    return client, codec, req, runs, forecast


def queries(valid=RUN + timedelta(hours=1.5)):
    return (HumidityProfileQuery(30.4, 130.7, valid),
            *(CloudCoverQuery(30.4, 130.7, valid, layer) for layer in CloudLayer),
            HumidityProfileQuery(30.4, 130.7, valid, (300, 250)),
            CloudCoverQuery(32, 130.7, valid),
            CloudCoverQuery(30.4, 130.7, RUN + timedelta(hours=9)))


def write_case(directory):
    directory = Path(directory)
    for model in ("MSM", "GSM_JAPAN"):
        _, codec, _, _, forecast = desktop_case(directory / "wx-desktop" / model, model)
        payload = codec.from_forecast(forecast).to_bytes()
        stem = "wx-" + model.lower()
        (directory / f"{stem}.npz").write_bytes(payload)
        (directory / f"{stem}.json").write_text(json.dumps({
            "sha256": hashlib.sha256(payload).hexdigest(),
            "expected": [asdict(forecast.query(q)) for q in queries()],
        }, default=str, indent=2) + "\n")
