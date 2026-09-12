from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable

from .models import RemoteFile


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
    files: Iterable[RemoteFile], cache_dir: Path,
    downloader: Callable[[RemoteFile, Path], Path],
) -> tuple[tuple[Path, ...], dict[str, str]]:
    files = tuple(files)
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
                downloader(remote, destination)
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


def cached_listing(url: str, cache_dir: Path, reader, ttl_seconds: int = 900) -> str:
    key = hashlib.sha256(url.encode("utf-8")).hexdigest()
    path = cache_dir / "listings" / f"{key}.html"
    lock = cache_dir / "locks" / f"listing-{key}.lock"
    with file_lock(lock):
        if path.exists() and time.time() - path.stat().st_mtime <= ttl_seconds:
            return path.read_text(encoding="ascii", errors="ignore")
        content = reader(url)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".html.tmp")
        temporary.write_text(content, encoding="ascii", errors="ignore")
        temporary.replace(path)
        return content


def verify_cache(cache_dir: Path) -> dict:
    files = sorted((cache_dir / "raw").glob("*/*.bin"))
    results = []
    valid = True
    for path in files:
        try:
            validate_grib(path)
            digest = sha256_file(path)
            results.append({"path": str(path), "sha256": digest, "valid": True})
        except (OSError, ValueError) as exc:
            valid = False
            results.append({"path": str(path), "valid": False, "error": str(exc)})
    return {"valid": valid, "files": results}
