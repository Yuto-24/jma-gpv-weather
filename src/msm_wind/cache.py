from __future__ import annotations

import hashlib
import json
import os
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from .core import RemoteFile, download


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def file_lock(path: Path):
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+b")
    try:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        handle.close()


def validate_grib(path: Path) -> None:
    with path.open("rb") as handle:
        if handle.read(4) != b"GRIB":
            raise ValueError(f"not a GRIB file: {path}")
        handle.seek(-4, os.SEEK_END)
        if handle.read(4) != b"7777":
            raise ValueError(f"incomplete GRIB file: {path}")


def acquire_files(
    files: Iterable[RemoteFile], cache_dir: Path
) -> tuple[tuple[Path, ...], dict[str, str]]:
    paths: list[Path] = []
    hashes: dict[str, str] = {}
    for remote in files:
        run_dir = cache_dir / "raw" / f"{remote.run_utc:%Y%m%d%H%M%S}"
        destination = run_dir / remote.name
        lock = cache_dir / "locks" / f"{remote.name}.lock"
        with file_lock(lock):
            if destination.exists():
                try:
                    validate_grib(destination)
                except (OSError, ValueError):
                    corrupt = destination.with_name(
                        destination.name
                        + f".corrupt.{datetime.now(timezone.utc):%Y%m%d%H%M%S}"
                    )
                    destination.replace(corrupt)
            if not destination.exists():
                download(remote, destination)
            validate_grib(destination)
            hashes[remote.url] = sha256_file(destination)
        paths.append(destination)
    manifest = {
        "schema_version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                **asdict(remote),
                "run_utc": remote.run_utc.isoformat(),
                "path": str(path),
                "sha256": hashes[remote.url],
            }
            for remote, path in zip(files, paths)
        ],
    }
    manifest_path = paths[0].parent / "manifest.json" if paths else cache_dir / "manifest.json"
    temporary = manifest_path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    temporary.replace(manifest_path)
    return tuple(paths), hashes

