from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

from .core import Bounds, MsmError, discover_run, download, write_outputs


def build_parser():
    parser = argparse.ArgumentParser(description="Download JMA MSM wind from RISH and create bounded CSV files")
    parser.add_argument("--date", required=True, type=date.fromisoformat, help="target JST date (YYYY-MM-DD)")
    parser.add_argument("--work-dir", type=Path, default=Path("data"))
    parser.add_argument("--output-dir", type=Path, default=Path("outputs"))
    parser.add_argument("--lat-min", type=float, default=29.7)
    parser.add_argument("--lat-max", type=float, default=35.2)
    parser.add_argument("--lon-min", type=float, default=128.5)
    parser.add_argument("--lon-max", type=float, default=134.8)
    parser.add_argument("--discover-only", action="store_true")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    bounds = Bounds(args.lat_min, args.lat_max, args.lon_min, args.lon_max)
    if bounds.lat_min > bounds.lat_max or bounds.lon_min > bounds.lon_max:
        print("error: invalid bounds", file=sys.stderr); return 2
    try:
        selection = discover_run(args.date)
        print(f"selected run: {selection.run_utc.isoformat()}")
        for remote in selection.files: print(f"  {remote.url}")
        if args.discover_only: return 0
        run_dir = args.work_dir/f"{selection.run_utc:%Y%m%d%H%M%S}"
        local = []
        for remote in selection.files:
            destination = run_dir/remote.name
            if destination.exists(): print(f"cached: {destination}")
            else:
                print(f"downloading: {remote.url}")
                download(remote, destination)
            local.append(destination)
        metadata = write_outputs(args.output_dir, args.date, bounds, selection, local)
        print(f"complete: {args.output_dir}")
        for name, summary in metadata["outputs"].items(): print(f"  {name}: {summary['rows']:,} rows")
        return 0
    except (MsmError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr); return 1


if __name__ == "__main__":
    raise SystemExit(main())
