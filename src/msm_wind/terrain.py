from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from .core import Bounds, _subset_message
from .errors import TerrainValidationError
from .interpolation import bilinear


@dataclass
class GridTerrainProvider:
    values_m: np.ndarray
    latitudes: np.ndarray
    longitudes: np.ndarray
    source: str = "JMA MSM model terrain (Pzs)"
    source_sha256: str | None = None

    @classmethod
    def from_grib(cls, path: str | Path, bounds: Bounds):
        import pygrib

        grib = pygrib.open(str(path))
        try:
            message = next(iter(grib))
            values, lat, lon = _subset_message(message, bounds)
        finally:
            grib.close()
        digest = hashlib.sha256(Path(path).read_bytes()).hexdigest()
        return cls(values, lat, lon, source=str(path), source_sha256=digest)

    def save(self, path: str | Path) -> Path:
        destination = Path(path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".tmp")
        with temporary.open("wb") as handle:
            np.savez_compressed(
                handle,
                values_m=self.values_m,
                latitudes=self.latitudes,
                longitudes=self.longitudes,
                metadata=json.dumps(
                    {
                        "schema_version": 1,
                        "source": self.source,
                        "source_sha256": self.source_sha256,
                    }
                ),
            )
        temporary.replace(destination)
        manifest = destination.with_suffix(destination.suffix + ".json")
        manifest_temp = manifest.with_suffix(manifest.suffix + ".tmp")
        manifest_temp.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source": self.source,
                    "source_sha256": self.source_sha256,
                    "terrain_cache": str(destination),
                },
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest_temp.replace(manifest)
        return destination

    @classmethod
    def load(cls, path: str | Path):
        try:
            with np.load(path) as data:
                metadata = json.loads(str(data["metadata"]))
                return cls(
                    np.asarray(data["values_m"]),
                    np.asarray(data["latitudes"]),
                    np.asarray(data["longitudes"]),
                    metadata["source"],
                    metadata.get("source_sha256"),
                )
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise TerrainValidationError(
                f"cannot load Pzs terrain cache {path}: {exc}"
            ) from exc

    def __call__(self, latitude: float, longitude: float) -> float | None:
        lat_axis = self.latitudes[:, 0]
        lon_axis = self.longitudes[0, :]
        lat_order = np.argsort(lat_axis)
        lon_order = np.argsort(lon_axis)
        ordered_lat = lat_axis[lat_order]
        ordered_lon = lon_axis[lon_order]
        y = np.searchsorted(ordered_lat, latitude)
        x = np.searchsorted(ordered_lon, longitude)
        if y == 0 or x == 0 or y == len(ordered_lat) or x == len(ordered_lon):
            return None
        yi = (lat_order[y - 1], lat_order[y])
        xi = (lon_order[x - 1], lon_order[x])
        corners = np.array(
            [
                [self.values_m[yi[0], xi[0]], self.values_m[yi[0], xi[1]]],
                [self.values_m[yi[1], xi[0]], self.values_m[yi[1], xi[1]]],
            ]
        )
        if not np.isfinite(corners).all():
            return None
        return bilinear(
            longitude,
            latitude,
            ordered_lon[x - 1],
            ordered_lon[x],
            ordered_lat[y - 1],
            ordered_lat[y],
            corners,
        )
