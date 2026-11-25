"""Fixed real-GPV WX acceptance and Linux portable size/load measurements.

Run from an installed checkout: python scripts/measure_wx.py --output /tmp/jma-wx
Downloads only four fixed RISH GRIBs (176 MiB); retains raw and prepared caches.
No publication, provider mutation, or live-run discovery occurs.
"""
import argparse
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from io import BytesIO
import json
from pathlib import Path
import platform
import subprocess
import sys
import time
from unittest.mock import patch
from zipfile import ZipFile

import numpy as np

from jma_gpv_weather import (
    AloftQuery, Bounds, CloudCoverQuery, CloudLayer, ForecastRequirements,
    GsmClient, GsmPreparedData, HumidityProfileQuery, MsmClient, MsmPreparedData,
    RunId, SurfaceTemperatureQuery, WeatherVariable,
)
from jma_gpv_weather.cache import acquire_files
from jma_gpv_weather.gsm import spec as gsm
from jma_gpv_weather.msm import spec as msm
from jma_gpv_weather.sources.rish import RishSource

RUN = datetime(2026, 9, 12, tzinfo=timezone.utc)
DIRECTORY = "https://database.rish.kyoto-u.ac.jp/arch/jmadata/data/gpv/original/2026/09/12"
INPUTS = {
    "MSM": {
        "L-pall": (50522465, "9f03772de3f164088339d52702b907f2f88e206d615112861abc74603f5f0c3f"),
        "Lsurf": (69241393, "dd83a4768653a0bd947b8b42c3dddd55b7f33f0cef779bdf3692ccfc99f8c479"),
    },
    "GSM_JAPAN": {
        "L-pall": (45315774, "d908038ce8aa059f5168ce07dd5b999ed65c81f3c91afa5737fa7350e738c19e"),
        "Lsurf": (19511629, "3f67f78e3276c09e0859d83e05e7f7b2fbe3d22b30e99fc4f7d55427fbe87fc9"),
    },
}


def rss():
    rows = Path("/proc/self/status").read_text().splitlines()
    return {key: int(next(row.split()[1] for row in rows if row.startswith(key + ":"))) * 1024
            for key in ("VmRSS", "VmHWM")}


def load_measurement(path, model):
    codec = MsmPreparedData if model == "MSM" else GsmPreparedData
    before = rss()
    start = time.perf_counter()
    payload = Path(path).read_bytes()
    data = codec.from_bytes(payload)
    seconds = time.perf_counter() - start
    after = rss()
    unique = {id(a): a for records in (data.surface, data.pressure) for record in records.values() for a in record}
    with ZipFile(BytesIO(payload)) as archive:
        members = archive.infolist()
        expanded = sum(m.file_size for m in members)
    return {"compressed_bytes": len(payload), "array_bytes": sum(a.nbytes for a in unique.values()),
            "expanded_zip_bytes": expanded, "members": len(members), "load_seconds": seconds,
            "rss_before_bytes": before["VmRSS"], "rss_after_bytes": after["VmRSS"],
            "peak_rss_bytes": after["VmHWM"]}


def run(output):
    import pygrib
    output.mkdir(parents=True, exist_ok=True)
    report = {"run_utc": RUN.isoformat(), "python": platform.python_version(), "numpy": np.__version__,
              "pygrib": pygrib.__version__, "platform": platform.platform(), "bounds": asdict(Bounds()),
              "window_hours": [0, 12], "models": {}}
    for model, spec, cls, codec in (("MSM", msm, MsmClient, MsmPreparedData),
                                   ("GSM_JAPAN", gsm, GsmClient, GsmPreparedData)):
        client = cls(output / "cache", Bounds(), source=RishSource(DIRECTORY.rsplit("/2026/", 1)[0]))
        names = [f"Z__C_RJTD_20260912000000_MSM_GPV_Rjp_{kind}_FH00-15_grib2.bin" if model == "MSM" else
                 f"Z__C_RJTD_20260912000000_GSM_GPV_Rjp_Gll0p1deg_{kind}_FD0000-0100_grib2.bin"
                 for kind in ("L-pall", "Lsurf")]
        files = spec.parse_listing(" ".join(names), DIRECTORY)
        print(f"{model}: acquire and verify fixed GRIB inputs", file=sys.stderr, flush=True)
        paths, hashes = acquire_files(files, client.cache_dir, client.source.download)
        for remote, path in zip(files, paths):
            size, digest = INPUTS[model][remote.kind]
            assert path.stat().st_size == size and hashes[remote.url] == digest, path

        times = tuple(RUN + timedelta(hours=h) for h in range(13))
        legacy = frozenset({WeatherVariable.ALOFT_WIND, WeatherVariable.ALOFT_TEMPERATURE,
                            WeatherVariable.SURFACE_TEMPERATURE})
        wx = frozenset({WeatherVariable.PRESSURE_RELATIVE_HUMIDITY, WeatherVariable.CLOUD_COVER})
        forecasts, metrics = {}, {}
        for name, variables in (("baseline", legacy), ("wx", wx), ("mixed", legacy | wx)):
            req = ForecastRequirements(times, variables)
            runs = spec.select_compatible_runs(files, req)
            print(f"{model}: {name} decode, cache and portable round-trip", file=sys.stderr, flush=True)
            forecast = client.prepare_run(RunId(RUN), req, runs)
            payload = codec.from_forecast(forecast).to_bytes()
            path = output / f"{model.lower()}-{name}.npz"
            path.write_bytes(payload)
            restored = codec.from_bytes(payload)
            portable = client.prepare_run(RunId(RUN), req, prepared_data=restored)
            queries = ([HumidityProfileQuery(31.877, 131.449, RUN + timedelta(hours=1.5)),
                        *(CloudCoverQuery(31.877, 131.449, RUN, layer) for layer in CloudLayer)] if name == "wx" else
                       [AloftQuery(31.877, 131.449, RUN + timedelta(hours=1.5), 4572),
                        SurfaceTemperatureQuery(31.877, 131.449, RUN)])
            assert [forecast.query(q) for q in queries] == [portable.query(q) for q in queries]
            with patch("jma_gpv_weather.grib.read_grib_records", side_effect=AssertionError("warm decode")), \
                 patch.object(client.source, "download", side_effect=AssertionError("warm download")):
                warm = client.prepare_run(RunId(RUN), req, runs)
            assert [forecast.query(q) for q in queries] == [warm.query(q) for q in queries]
            metrics[name] = json.loads(subprocess.check_output(
                [sys.executable, str(Path(__file__).resolve()), "--load", str(path), "--model", model], text=True))
            forecasts[name] = forecast

        # Verify each original value in mixed preparation, including HGT/ancillary fields.
        for group in ("surface", "pressure"):
            baseline, mixed = getattr(forecasts["baseline"], group), getattr(forecasts["mixed"], group)
            for key in set(baseline) & set(mixed):
                for expected, actual in zip(baseline[key], mixed[key]):
                    np.testing.assert_array_equal(actual, expected)
        for valid in (RUN, RUN + timedelta(hours=1.5), RUN + timedelta(hours=12)):
            for query in (AloftQuery(31.877, 131.449, valid, 4572), SurfaceTemperatureQuery(31.877, 131.449, valid)):
                assert forecasts["baseline"].query(query) == forecasts["mixed"].query(query)

        # Independent pygrib sampling (no library subset/interpolation helpers).
        direct = {}
        for path in paths:
            with pygrib.open(str(path)) as messages:
                for message in messages:
                    if message.validDate.replace(tzinfo=timezone.utc) != RUN:
                        continue
                    key = (message.parameterCategory, message.parameterNumber)
                    name = None
                    if message.typeOfLevel == "isobaricInhPa" and message.level == 850:
                        name = {(1, 1): "rh", (3, 5): "hgt"}.get(key)
                    if message.typeOfLevel == "surface":
                        name = {(6, 1): "total", (6, 3): "low", (6, 4): "middle", (6, 5): "high"}.get(key)
                    if name:
                        data, _, _ = message.data(lat1=31.899999, lat2=31.900001, lon1=131.499999, lon2=131.500001)
                        assert data.size == 1
                        direct[name] = float(data[0, 0])
        assert set(direct) == {"rh", "hgt", "total", "low", "middle", "high"}
        forecast = forecasts["wx"]
        level = forecast.query(HumidityProfileQuery(31.9, 131.5, RUN, (850,))).levels[0]
        assert level.values["relative_humidity_percent"] == direct["rh"]
        assert level.values["geopotential_height_gpm"] == direct["hgt"]
        for layer in CloudLayer:
            assert forecast.query(CloudCoverQuery(31.9, 131.5, RUN, layer)).values["cloud_cover_percent"] == direct[layer.value]
        report["models"][model] = {"sources": [{"url": f.url, "sha256": hashes[f.url], "bytes": p.stat().st_size}
                                                for f, p in zip(files, paths)],
                                    "measurements": metrics, "direct_gridpoint": direct,
                                    "checks": ["cold/warm/portable values and provenance equal",
                                               "legacy values and provenance equal in mixed request",
                                               "direct pygrib RH/HGT/cloud values equal"]}
        (output / "measurements.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("/tmp/jma-wx"))
    parser.add_argument("--load", type=Path)
    parser.add_argument("--model", choices=("MSM", "GSM_JAPAN"))
    args = parser.parse_args()
    result = load_measurement(args.load, args.model) if args.load else run(args.output)
    print(json.dumps(result, indent=2))
