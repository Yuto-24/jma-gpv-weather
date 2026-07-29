from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path

from . import (
    AloftQuery,
    Bounds,
    EstimatedQnhQuery,
    ForecastRequirements,
    GridTerrainProvider,
    MsmClient,
    RunId,
    SurfaceWindQuery,
    WeatherVariable,
)
from .errors import MsmError


def _datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise argparse.ArgumentTypeError("datetime must include a timezone")
    return parsed.astimezone(timezone.utc)


def _json_default(value):
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Path):
        return str(value)
    raise TypeError(type(value).__name__)


def _print(value):
    print(json.dumps(value, default=_json_default, ensure_ascii=False, indent=2))


def build_parser():
    parser = argparse.ArgumentParser(
        description="Query JMA MSM weather data from RISH", allow_abbrev=False
    )
    parser.add_argument("--cache-dir", type=Path, default=Path("data"))
    parser.add_argument("--lat-min", type=float, default=29.7)
    parser.add_argument("--lat-max", type=float, default=35.2)
    parser.add_argument("--lon-min", type=float, default=128.5)
    parser.add_argument("--lon-max", type=float, default=134.8)
    commands = parser.add_subparsers(dest="command", required=True)

    resolve = commands.add_parser("resolve", allow_abbrev=False)
    resolve.add_argument("--time", type=_datetime, action="append", required=True)
    resolve.add_argument("--selected-run", type=_datetime)

    prepare = commands.add_parser("prepare", allow_abbrev=False)
    prepare.add_argument("--time", type=_datetime, action="append", required=True)
    prepare.add_argument(
        "--variable",
        choices=[value.value for value in WeatherVariable],
        action="append",
        required=True,
    )
    prepare.add_argument("--run", type=_datetime)

    for name in ("query-aloft", "query-surface", "query-qnh"):
        query = commands.add_parser(name, allow_abbrev=False)
        query.add_argument("--time", type=_datetime, required=True)
        query.add_argument("--run", type=_datetime)
        query.add_argument("--lat", type=float, required=True)
        query.add_argument("--lon", type=float, required=True)
        query.add_argument("--terrain-cache", type=Path)
    commands.choices["query-aloft"].add_argument("--altitude-m-msl", type=float, required=True)
    commands.choices["query-qnh"].add_argument("--elevation-m-msl", type=float, required=True)

    terrain = commands.add_parser("prepare-terrain", allow_abbrev=False)
    terrain.add_argument("--input-grib", type=Path, required=True)
    terrain.add_argument("--source-manifest", type=Path, required=True)
    terrain.add_argument("--output", type=Path, required=True)
    commands.add_parser("cache-verify", allow_abbrev=False)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    bounds = Bounds(args.lat_min, args.lat_max, args.lon_min, args.lon_max)
    if bounds.lat_min > bounds.lat_max or bounds.lon_min > bounds.lon_max:
        print("error: invalid bounds", file=sys.stderr)
        return 2
    try:
        if args.command == "prepare-terrain":
            provider = GridTerrainProvider.from_grib(
                args.input_grib,
                bounds,
                source_manifest=args.source_manifest,
            )
            _print(
                {
                    "terrain_cache": provider.save(args.output),
                    "source": provider.source,
                    "source_sha256": provider.source_sha256,
                    "model_terrain_version": provider.model_terrain_version,
                }
            )
            return 0
        if args.command == "cache-verify":
            from .cache import verify_cache

            result = verify_cache(args.cache_dir)
            _print(result)
            return 0 if result["valid"] else 4
        client = MsmClient(args.cache_dir, bounds)
        if args.command == "resolve":
            requirements = ForecastRequirements(
                tuple(args.time), frozenset(WeatherVariable)
            )
            status = client.resolve_run(
                requirements,
                None if args.selected_run is None else RunId(args.selected_run),
            )
            _print(status)
            return 0
        if args.command == "prepare":
            requirements = ForecastRequirements(
                tuple(args.time),
                frozenset(WeatherVariable(value) for value in args.variable),
            )
            status = client.resolve_run(
                requirements, None if args.run is None else RunId(args.run)
            )
            if not status.selected_run_covers_request:
                _print(status)
                return 3
            prepared = client.prepare_run(status.selected_run, requirements)
            _print(
                {
                    "run_status": status,
                    "surface_records": len(prepared.surface),
                    "pressure_records": len(prepared.pressure),
                }
            )
            return 0

        variable = {
            "query-aloft": {
                WeatherVariable.ALOFT_WIND,
                WeatherVariable.ALOFT_TEMPERATURE,
            },
            "query-surface": {WeatherVariable.SURFACE_WIND},
            "query-qnh": {WeatherVariable.ESTIMATED_QNH},
        }[args.command]
        requirements = ForecastRequirements((args.time,), frozenset(variable))
        status = client.resolve_run(
            requirements, None if args.run is None else RunId(args.run)
        )
        if not status.selected_run_covers_request:
            _print(status)
            return 3
        terrain_provider = (
            GridTerrainProvider.load(args.terrain_cache)
            if args.terrain_cache is not None
            else None
        )
        prepared = client.prepare_run(
            status.selected_run, requirements, terrain_provider=terrain_provider
        )
        if args.command == "query-aloft":
            query = AloftQuery(args.lat, args.lon, args.time, args.altitude_m_msl)
        elif args.command == "query-surface":
            query = SurfaceWindQuery(args.lat, args.lon, args.time)
        else:
            query = EstimatedQnhQuery(args.lat, args.lon, args.time, args.elevation_m_msl)
        _print({"run_status": status, "result": prepared.query(query)})
        return 0
    except (MsmError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
